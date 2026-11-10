"""板块（行业）维度 —— 个股消息的下一层是行业消息。

为什么要这一层
--------------
盘前三类信息里原来缺了一块：
* 个股公告/新闻 → 纯 alpha（已覆盖）
* 外围市场      → 全市场 beta（已覆盖）
* **行业/板块** → 介于两者之间：行业政策、景气度、板块轮动，
  对个股影响常常比美股隔夜还直接（"白酒消费税传闻"之于茅台，
  比道指 +0.5% 重要得多），但原来完全没有通道进来。

取数方案（2026-10-29 重写）
--------------------------
本地 `stock_info.industry` **5264 只全为 NULL**，行业归属只能在线取。

最初的做法是「全市场代码→行业映射」分页拉一遍（约 55 次连续请求）落盘缓存，
实测**稳定触发东财限流**（RemoteDisconnected / 567），首跑必挂。

改用 `slist spt=3`（单票所属板块列表）：
* **一只票 1 次请求**，直接返回该股所属全部板块**及当日涨跌幅**，
  不需要再拉板块清单做名字匹配；
* 响应里板块代码（BKxxxx）可与其他接口交叉校验；
* 实测 `clist`（全市场/板块列表）被拒时，`slist` 依然正常。

行业识别：返回列表里**地区板块（"广东板块""北京板块"）之前的那一段**就是
行业链条（从细分到宽泛），例如深信服 → 软件开发 / 计算机 / 横向通用软件。
四个样本（300454 / 002612 / 300740 / 600519）全部命中，比名字模糊匹配可靠。

行业归属落盘缓存 30 天（极少变动）；板块涨跌幅每次实时取，不缓存。
任一环节失败都降级：行业取不到就 ok=False，analyzer 自动把权重让回其他维度。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from premarket import peers

from premarket.news import NewsItem

_EM_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Referer': 'https://quote.eastmoney.com/',
}

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.data_cache')
_INDUSTRY_CACHE_FILE = os.path.join(_CACHE_DIR, 'sector_industry.json')
_INDUSTRY_TTL_DAYS = 30

# 东财单次请求间隔下限（秒）。连续快速请求会被掐断，宁可慢一点。
_MIN_INTERVAL = 0.45
_last_call_ts = 0.0
_throttle_lock = threading.Lock()

# 板块名后缀为「板块」的是地区板块（"广东板块""北京板块"），不属于行业分类
_REGION_SUFFIX = '板块'

# 风格/概念标签兜底黑名单（当响应里找不到地区板块时用来排除）
_CONCEPT_BLACKLIST = {
    '融资融券', '深股通', '沪股通', '股权激励', 'QFII重仓', '基金重仓',
    '深成500', '创业板综', '上证380', '中证500', '沪深300', '上证50',
    '预盈预增', '预亏预减', '昨日涨停', '昨日连板', '举牌', '参股银行',
    '参股券商', '参股新三板', '转债标的', '机构重仓', '社保重仓', '保险重仓',
    'MSCI中国', '标准普尔', '富时罗素', '百元股', '低价股', '高送转',
}

# 行业名 -> 快讯匹配关键词（行业名本身会自动加入，这里放同义/上位词）
# 快讯里很少写东财的标准行业名（"互联网服务"），更多写通俗词（"电商""网游"）
INDUSTRY_KEYWORDS: Dict[str, List[str]] = {
    '酿酒行业': ['白酒', '酿酒', '酒业', '酒企'], '白酒': ['白酒', '酿酒', '酒业', '酒企'],
    '白酒Ⅱ': ['白酒', '酿酒', '酒业', '酒企'], '白酒Ⅲ': ['白酒', '酿酒', '酒业', '酒企'],
    '软件开发': ['软件', '信创', 'SaaS', '国产软件'],
    '横向通用软件': ['软件', '信创', 'SaaS'],
    '互联网服务': ['互联网', '电商', '平台经济', '网游', '网络游戏'],
    '计算机设备': ['计算机', '服务器', '算力', '数据中心'],
    '通信设备': ['通信', '5G', '6G', '光模块', '光通信'],
    '半导体': ['半导体', '芯片', '晶圆', '存储芯片', '光刻'],
    '电子元件': ['电子', 'PCB', '消费电子'],
    '消费电子': ['消费电子', '果链', '苹果产业链'],
    '电池': ['锂电', '电池', '储能', '固态电池'],
    '光伏设备': ['光伏', '硅片', '组件'],
    '风电设备': ['风电'],
    '汽车整车': ['汽车', '车企', '新能源车', '电动汽车'],
    '汽车零部件': ['汽车零部件', '智能驾驶', '自动驾驶'],
    '证券': ['券商', '证券', '投行'],
    '银行': ['银行', '降息', 'LPR', '存款利率'],
    '保险': ['保险', '险资'],
    '房地产开发': ['地产', '楼市', '房地产', '土拍'],
    '医药商业': ['医药流通', '药店'],
    '化学制药': ['医药', '药品', '集采', '创新药', '仿制药'],
    '中药': ['中药'],
    '医疗器械': ['医疗器械', '医疗设备'],
    '医疗服务': ['医疗服务', 'CXO', '医美'],
    '医疗美容': ['医美', '医疗美容', '玻尿酸'],
    '医美服务': ['医美', '医疗美容'],
    '美容护理': ['化妆品', '美妆', '医美', '护肤', '个护'],
    '化妆品': ['化妆品', '美妆', '护肤', '个护'],
    '品牌化妆品': ['化妆品', '美妆', '护肤'],
    '纺织服装': ['服装', '纺织', '服饰', '女装', '男装'],
    '服装家纺': ['服装', '纺织', '服饰', '女装', '家纺'],
    '食品': ['食品', '零食', '预制菜'],
    '食品饮料': ['食品', '饮料'],
    '饮料制造': ['饮料', '乳业', '乳制品'],
    '农林牧渔': ['生猪', '猪价', '养殖', '种业', '粮食'],
    '有色金属': ['有色', '铜', '铝', '稀土'],
    '贵金属': ['黄金', '金价', '贵金属'],
    '煤炭行业': ['煤炭', '煤价'],
    '石油行业': ['石油', '油价', '油气'],
    '钢铁': ['钢铁', '钢价'],
    '化工': ['化工', '化品'],
    '军工': ['军工', '国防', '航母', '导弹'],
    '航天航空': ['军工', '航天', '卫星', '火箭'],
    '游戏': ['游戏', '版号', '电竞'],
    '文化传媒': ['传媒', '影视', '短剧', '出版'],
    '旅游酒店': ['旅游', '酒店', '免税', '出行'],
    '航空机场': ['航空', '机场', '航运'],
    '物流': ['物流', '快递'],
    '电网设备': ['电网', '特高压', '电力设备'],
    '电力': ['电力', '电价', '绿电'],
    '环保': ['环保', '碳中和', '污水'],
    '建材': ['建材', '水泥', '玻璃'],
    '家电': ['家电', '空调', '冰洗'],
    '机械': ['机械', '工程机械', '机器人'],
    '机器人': ['机器人', '人形机器人'],
}

# 行业 -> **核心题材板块**（东财概念板块的标准名）
# ------------------------------------------------------------------
# 为什么需要这张表：行业分类是「基本面归属」，个股实际被资金交易时往往挂在
# 另一条题材线上。典型例子是深信服/绿盟——行业归「软件开发 / 横向通用软件」，
# 但盘面上它们跟的是「网络安全 / 数据安全 / 信创」。
# 10-29 实测：软件开发 −4.21%，而同题材 8 只票中位 −9.61%，差 5.4 个点，
# 只看行业板块会严重低估板块压力。
#
# 为什么不用「板块列表前 N 个」或「名字含关键词」这类启发式：
# 都试过，不可靠——深信服列表前三个是「云计算 / 物联网 / 深圳特区」，
# 而「深圳特区 / 西部大开发」是地区板块却不带「板块」后缀，启发式筛不掉；
# 按关键词筛又会把「婴童概念 / 网红经济」拉进化妆品。
# 所以改成**受控映射**：只有表里写明的名字才会被采纳，匹配不到就返回空（安全降级）。
THEME_BOARDS: Dict[str, List[str]] = {
    '软件开发': ['网络安全', '数据安全', '信创', '国产软件', 'VPN', '云计算'],
    '横向通用软件': ['网络安全', '数据安全', '信创', '国产软件', '云计算'],
    'IT服务': ['信创', '数据要素', '智慧城市', '云计算'],
    '计算机设备': ['算力概念', '数据中心', '信创', '人工智能'],
    '通信设备': ['光模块', '算力概念', '5G概念', 'CPO概念'],
    '半导体': ['芯片概念', '存储芯片', '国产芯片', '第三代半导体'],
    '电池': ['固态电池', '锂电', '储能', '钠离子电池'],
    '光伏设备': ['光伏概念', 'HJT电池', 'TOPCon电池'],
    '汽车整车': ['新能源车', '智能驾驶', '汽车整车'],
    '汽车零部件': ['智能驾驶', '汽车零部件', '一体化压铸'],
    '医疗器械': ['医疗器械概念', '医美概念', '创新医疗'],
    '化学制药': ['创新药', '减肥药', '仿制药', 'CXO概念'],
    '医疗美容': ['医美概念', '医疗美容'],
    '医美服务': ['医美概念', '医疗美容'],
    '化妆品': ['医美概念', '化妆品概念'],
    '品牌化妆品': ['化妆品概念', '医美概念'],
    '美容护理': ['医美概念', '化妆品概念'],
    '白酒Ⅱ': ['酿酒概念', '白酒概念'],
    '白酒Ⅲ': ['酿酒概念', '白酒概念'],
    '酿酒行业': ['酿酒概念', '白酒概念'],
    '证券': ['券商概念', '参股期货'],
    '电力': ['绿电', '特高压', '虚拟电厂'],
    '电网设备': ['特高压', '虚拟电厂', '电网设备'],
    '机械': ['人形机器人', '机器人概念', '工业母机'],
    '机器人': ['人形机器人', '机器人概念'],
    '航天航空': ['大飞机', '低空经济', '军工'],
    '农牧饲渔': ['种业', '生猪概念', '预制菜'],
}


@dataclass
class SectorContext:
    industry: str = ''                # 东财行业名（如"软件开发"）
    board_code: str = ''              # 对应行业板块代码（如 BK0737）
    board_name: str = ''
    board_pct: Optional[float] = None  # 昨日板块涨跌幅 %
    chain: List[str] = field(default_factory=list)   # 行业链条（细分→宽泛）
    themes: List[dict] = field(default_factory=list)  # 核心题材板块 [{'name','code','pct'}]
    peer: Optional[object] = None     # peers.PeerSnapshot：同业等权口径（本地库，不联网）
    news: List[NewsItem] = field(default_factory=list)  # 板块相关快讯
    news_hits: Dict[str, List[str]] = field(default_factory=dict)  # 标题 → 命中关键词
    news_kind: Dict[str, str] = field(default_factory=dict)        # 标题 → wide/company
    news_raw: float = 0.0             # 板块消息原始分
    # 板块分怎么算出来的（可追溯）：用了哪个涨跌幅、除以多少
    perf_pct: Optional[float] = None  # 实际进入评分的板块涨跌幅 %
    perf_src: str = ''                # 来源：板块指数 / 同业等权
    perf_denom: Optional[float] = None  # 波动率归一化后的分母 %
    score: float = 0.0                # -100 ~ +100
    ok: bool = False
    note: str = ''


# ----------------------------------------------------------------------
# HTTP：带限速 + 重试
# ----------------------------------------------------------------------
def _throttle() -> None:
    """保证两次东财请求之间至少间隔 _MIN_INTERVAL 秒。"""
    global _last_call_ts
    with _throttle_lock:
        gap = time.time() - _last_call_ts
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        _last_call_ts = time.time()


def _http_get_json(url: str, timeout: int = 12, retries: int = 2) -> dict:
    last = ''
    for i in range(retries + 1):
        try:
            _throttle()
            req = urllib.request.Request(url, headers=_EM_HEADERS)
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            return json.loads(raw.decode('utf-8', 'ignore'))
        except Exception as e:           # noqa: BLE001
            last = f'{type(e).__name__}: {e}'
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(last)


def _secid(code: str) -> str:
    """sh.600519 / sh600519 / 600519 -> 1.600519（东财 secid）"""
    num = code.split('.')[-1]
    num = ''.join(ch for ch in num if ch.isdigit())
    prefix = 1 if num.startswith(('6', '9')) or code.lower().startswith('sh') else 0
    if code.lower().startswith('bj') or num.startswith(('8', '43', '92')):
        prefix = 0
    return f'{prefix}.{num}'


# ----------------------------------------------------------------------
# 行业归属与板块行情（每票 1 次请求）
# ----------------------------------------------------------------------
def fetch_stock_boards(code: str) -> List[dict]:
    """一只票所属全部板块 + 当日涨跌幅。

    返回 [{'code':'BK0737','name':'软件开发','pct':-4.21}, ...]，按东财默认顺序。
    """
    secid = _secid(code)
    url = ('https://push2.eastmoney.com/api/qt/slist/get'
           f'?spt=3&fltt=2&invt=2&fields=f12,f14,f3&secid={secid}'
           '&pn=1&pz=50&po=1&np=1')
    try:
        j = _http_get_json(url)
    except Exception as e:               # noqa: BLE001
        logger.warning(f'[盘前] {code} 板块归属抓取失败: {e}')
        return []

    out: List[dict] = []
    for x in (j.get('data') or {}).get('diff') or []:
        nm = str(x.get('f14') or '').strip()
        if not nm or nm == '-':
            continue
        pct = x.get('f3')
        out.append({
            'code': str(x.get('f12') or ''),
            'name': nm,
            'pct': float(pct) if isinstance(pct, (int, float)) else None,
        })
    return out


def pick_industry(boards: List[dict]) -> List[str]:
    """从板块列表里切出行业链条。

    东财返回顺序是「行业（细分→宽泛）→ 地区板块 → 概念/风格标签」，
    所以**第一个地区板块之前的那一段**就是行业链。四个样本实测一致：
    深信服 → 软件开发 / 计算机 / 横向通用软件；茅台 → 白酒Ⅱ / 白酒Ⅲ / 食品饮料。
    找不到地区板块时退回黑名单过滤。
    """
    if not boards:
        return []

    chain: List[str] = []
    for b in boards:
        nm = b['name']
        if nm.endswith(_REGION_SUFFIX):     # 地区板块 = 行业段结束
            break
        chain.append(nm)

    if not chain:                           # 没有地区板块：按黑名单排
        chain = [b['name'] for b in boards
                 if b['name'] not in _CONCEPT_BLACKLIST
                 and not b['name'].endswith(_REGION_SUFFIX)]

    # 概念标签可能混进前段（如"股权激励"），再洗一遍
    chain = [n for n in chain if n not in _CONCEPT_BLACKLIST]
    return chain


def pick_themes(boards: List[dict], chain: List[str], limit: int = 3) -> List[dict]:
    """从板块列表里挑出**核心题材板块**（受 THEME_BOARDS 映射表约束）。

    只认映射表里写明的名字，顺序也按表的优先级，与东财返回顺序无关——
    东财的顺序是按板块 ID 排的，不是相关度（深信服前三个是"云计算/物联网/深圳特区"）。

    Returns:
        [{'name': '网络安全', 'code': 'BK0655', 'pct': -3.91}, ...]
        行业没有配置映射时返回 []，调用方按"无题材"处理。
    """
    if not boards or not chain:
        return []

    wanted: List[str] = []
    for key in chain:
        for n in THEME_BOARDS.get(key, []):
            if n not in wanted:
                wanted.append(n)
    if not wanted:
        return []

    by_name = {b['name']: b for b in boards}
    out: List[dict] = []
    for n in wanted:
        b = by_name.get(n)
        if b is not None:
            out.append({'name': b['name'], 'code': b['code'], 'pct': b['pct']})
            if len(out) >= limit:
                break
    return out


# ---- 行业归属落盘缓存（30 天，极少变动）----
def _load_industry_cache() -> Dict[str, dict]:
    try:
        with open(_INDUSTRY_CACHE_FILE, 'r', encoding='utf-8') as f:
            pack = json.load(f)
        if time.time() - float(pack.get('fetched_ts', 0)) < _INDUSTRY_TTL_DAYS * 86400:
            return pack.get('map') or {}
    except Exception:                    # noqa: BLE001
        pass
    return {}


def _save_industry_cache(mp: Dict[str, dict]) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_INDUSTRY_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fetched_ts': time.time(), 'map': mp}, f, ensure_ascii=False)
    except Exception:                    # noqa: BLE001
        pass


def _num(code: str) -> str:
    return ''.join(ch for ch in code.split('.')[-1] if ch.isdigit())


def resolve_sector(code: str, use_cache: bool = True,
                   use_network: bool = True) -> SectorContext:
    """一只票的行业归属 + 板块涨跌幅。每票 1 次网络请求。

    use_network=False 时完全不发请求（离线模式），返回 ok=False，
    让 analyzer 退回三档权重——不能因为"板块分=0"白占 0.20 的权重。
    """
    ctx = SectorContext()
    key = _num(code)

    if not use_network:
        hit = _load_industry_cache().get(key) if use_cache else None
        if hit and hit.get('chain'):
            ctx.industry = hit['chain'][0]
            ctx.chain = list(hit['chain'])
            ctx.board_code = hit.get('board_code', '')
            ctx.themes = [{'name': n, 'code': '', 'pct': None}
                          for n in (hit.get('themes') or [])]
            ctx.board_code = hit.get('board_code', '')
        ctx.note = '离线模式：未取板块涨跌与板块消息，不计入综合分'
        return ctx

    cached = _load_industry_cache() if use_cache else {}
    hit = cached.get(key)

    boards = fetch_stock_boards(code)
    if not boards:
        # 网络挂了但有缓存 → 用缓存里的行业名，涨跌幅留空
        if hit and hit.get('chain'):
            ctx.industry = hit['chain'][0]
            ctx.chain = list(hit['chain'])
            ctx.board_code = hit.get('board_code', '')
            ctx.board_name = ctx.industry
            ctx.themes = [{'name': n, 'code': '', 'pct': None}
                          for n in (hit.get('themes') or [])]
            ctx.ok = True
            ctx.note = '板块涨跌幅未取到（网络问题），仅用缓存行业归属'
        else:
            ctx.note = '行业归属未取到（网络问题），板块维度缺失'
        return ctx

    chain = pick_industry(boards)
    if not chain:
        ctx.note = '未能从板块列表识别出行业（可能为北交所或新股）'
        return ctx

    ctx.industry = chain[0]
    ctx.chain = chain
    ctx.themes = pick_themes(boards, chain)
    primary = next((b for b in boards if b['name'] == ctx.industry), None)
    if primary:
        ctx.board_code = primary['code']
        ctx.board_name = primary['name']
        ctx.board_pct = primary['pct']

    # 回写缓存（只为下次网络失败兜底）；题材名一起存，涨跌幅不存（每天变）
    cached[key] = {'chain': chain, 'board_code': ctx.board_code,
                   'themes': [t['name'] for t in ctx.themes]}
    _save_industry_cache(cached)

    ctx.ok = True
    return ctx


# ----------------------------------------------------------------------
# 板块消息
# ----------------------------------------------------------------------
# 泛词黑名单：这类词在任何行业的新闻里都会出现，拿来做板块关键词必然误吸。
# 教训来自实测——同义词表里给"品牌化妆品"加了"品牌"，
# 结果把「东风汽车与宁德时代签署战略合作」「张雪机车扩产」「惠发食品成立新公司」
# 全吸进了化妆品板块（它们标题里都有"品牌"）。
_GENERIC_KW = {
    '品牌', '消费', '产品', '服务', '科技', '业务', '公司', '行业', '市场',
    '数据', '平台', '项目', '订单', '客户', '渠道', '销售', '研发', '技术',
    '产业', '国内', '海外', '企业', '集团', '上市', '股票', '股价',
}

# 例行工商注册类快讯：只有"公司成立/经营范围含XX"，没有任何交易价值
_JUNK_HINTS = ('企查查', '天眼查', '经营范围包含', '法定代表人为')


def sector_keywords(industry: str, chain: Optional[List[str]] = None,
                    themes: Optional[List[str]] = None) -> List[str]:
    """行业关键词 = 行业名本身 + 同义词表 + 行业链上的宽泛行业名 + 核心题材名。

    题材名（网络安全/信创/数据安全…）也要进来：快讯写的是"某安全厂商中标"、
    "护网行动启动"，不会写"软件开发行业"。
    泛词（_GENERIC_KW）一律剔除——板块关键词误吸的根源几乎都是这类词。
    """
    kws: List[str] = []

    def _add(w: str) -> None:
        w = (w or '').strip()
        if len(w) < 2:                   # 单字关键词（"乳""酒"）误伤面太大
            return
        if w and w not in kws and w not in _GENERIC_KW:
            kws.append(w)

    for name in ([industry] if industry else []) + list(chain or [])[1:]:
        _add(name)
        for extra in INDUSTRY_KEYWORDS.get(name, []):
            _add(extra)

    for name in themes or []:
        _add(name)

    # 行业名里的后缀对匹配没帮助，去掉再试一次
    for suf in ('行业', 'Ⅱ', 'Ⅲ', 'II', 'III'):
        if industry and industry.endswith(suf):
            base = industry[:-len(suf)]
            _add(base)
            for extra in INDUSTRY_KEYWORDS.get(base, []):
                _add(extra)
    return kws


def filter_sector_news(flash: List[NewsItem], keywords: List[str],
                       exclude_titles: Optional[set] = None,
                       hits_out: Optional[Dict[str, List[str]]] = None
                       ) -> List[NewsItem]:
    """从快讯里筛行业相关条目。已在个股 direct 里的不重复算。

    hits_out: 传入一个 dict，会填上「标题 → 命中的关键词」，
    报告层据此标注"为什么这条算板块消息"——
    快讯正文常在两三行之后才出现行业词，不标命中词会被误认为误报。
    """
    if not keywords:
        return []
    excl = exclude_titles or set()
    out = []
    for it in flash:
        if it.title in excl:
            continue
        if any(h in it.title for h in _JUNK_HINTS):     # 例行工商注册类快讯
            continue
        hit = [k for k in keywords if k and k in it.title]
        if not hit:
            continue
        it.relevance = 'sector'
        out.append(it)
        if hits_out is not None:
            hits_out[it.title] = hit
    return out


# 板块级 vs 单家公司：单家公司的三季报说明不了整个行业，不能和行业政策等量齐观
_COMPANY_HEAD_RE = re.compile(r'^【?([^】：:]{2,10})[：:]')
_WIDE_HINTS = (
    '政策', '监管', '行业', '板块', '集体', '全线', '普涨', '普跌', '景气',
    '关税', '集采', '标准', '规划', '意见', '通知', '协会', '统计局',
    '工信部', '发改委', '财政部', '国务院', '央行', '证监会',
    '涨价', '降价', '提价', '减产', '扩产', '产能', '库存', '出口', '进口',
    '销售额', '零售额', '新原料', '产业链', '需求', '景气度',
)


def classify_sector_news(item: NewsItem) -> str:
    """'wide' = 板块级（政策/行业数据/板块异动）；'company' = 单家公司消息。"""
    t = item.title
    if not _COMPANY_HEAD_RE.match(t):
        return 'wide'
    if any(h in t for h in _WIDE_HINTS):
        return 'wide'
    return 'company'


def _score_sector_news(items: List[NewsItem],
                       kinds: Optional[Dict[str, str]] = None) -> float:
    """板块消息原始分：复用规则词典，但权重打折（板块消息对个股是间接影响）。

    板块级消息权重 1.0，单家公司消息 0.5 且最多计 4 条——
    否则某行业一天出十几份三季报，会把板块分推到极端，
    而"同行都在亏"和"某一家在亏"对个股的含义完全不同。
    """
    from premarket.sentiment import score_with_rules, _time_decay
    kinds = kinds or {}
    total = 0.0
    n_company = 0
    for it in items[:20]:
        kind = kinds.get(it.title, 'wide')
        if kind == 'company':
            if n_company >= 4:
                continue
            n_company += 1
        w = 1.0 if kind == 'wide' else 0.5
        v = score_with_rules(it)
        if v.polarity != 0:
            total += v.polarity * 0.5 * w * _time_decay(it.age_hours())
    return round(total, 2)


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------
def _perf_denom(peer: Optional[object]) -> float:
    """板块分的分母 = 2×该板块日收益波动率（%，夹在 2.0~6.0 之间）。

    构造同业快照不可用时退回 2×池化中位 σ（实测 11 个板块的等权日收益 std
    中位 2.04%，保守取整 2.0 → 分母 4.0%），见 premarket/peers.py 模块注释。
    """
    if peer is not None and getattr(peer, 'denom', None):
        return float(peer.denom)
    return round(peers.SIGMA_MULT * peers.sigma_of(None), 2)


def build_sector_context(
    code: str,
    flash: Optional[List[NewsItem]] = None,
    exclude_titles: Optional[set] = None,
    resolved: Optional[SectorContext] = None,
    use_cache: bool = True,
    use_network: bool = True,
) -> SectorContext:
    """一只票的板块画像：行业归属 + 板块昨日涨跌 + 板块消息。

    resolved: 可传入已取好的 SectorContext（避免重复请求同一只票）。
    use_network=False: 离线模式，不联网、不计入综合分。
    """
    ctx = (resolved if resolved is not None
           else resolve_sector(code, use_cache=use_cache, use_network=use_network))
    if not ctx.ok:
        return ctx

    if flash:
        kws = sector_keywords(ctx.industry, ctx.chain,
                              themes=[t['name'] for t in ctx.themes])
        ctx.news = filter_sector_news(flash, kws, exclude_titles=exclude_titles,
                                      hits_out=ctx.news_hits)
        for it in ctx.news:
            ctx.news_kind[it.title] = classify_sector_news(it)
        # 板块级消息排前面，单家公司消息靠后（行业内多家同向时仍会被计入）
        ctx.news.sort(key=lambda x: (ctx.news_kind.get(x.title) != 'wide',
                                     x.age_hours()))
        ctx.news_raw = _score_sector_news(ctx.news, ctx.news_kind)

    # ---- 同业口径（本地库，不联网）----
    # 同时提供两样东西：① 波动率（用来做板块分的分母）② 个股相对同业的强弱
    if ctx.peer is None:
        ctx.peer = peers.peer_snapshot(code, ctx.industry, ctx.chain, ctx.themes)

    # 合成：板块昨日表现 60% + 板块消息 40%
    # 分母不再固定 3%，改成 **2×该板块自身波动率**：
    #   银行 σ≈1.0% → 分母 2.0%（跌 2% 才算极端）
    #   半导体 σ≈3.0% → 分母 6.0%（跌 2% 只是平常波动）
    # 固定分母的问题是"同一个绝对涨跌在不同板块含义完全不同"——实测板块间
    # 日收益 std 差 2.4 倍（银行 1.25% ~ 半导体 2.99%），用 3% 一刀切，
    # 等于让银行天天触顶、半导体几乎不触发。
    perf_pct, perf_src = None, ''
    if ctx.board_pct is not None:
        perf_pct, perf_src = ctx.board_pct, '板块指数'
    elif ctx.peer is not None and ctx.peer.ok and ctx.peer.ew_pct is not None:
        # 板块指数没取到（限流/离线）时，用同业等权顶上——它反而更贴资金实际在炒的票
        perf_pct, perf_src = ctx.peer.ew_pct, '同业等权'
    ctx.perf_pct, ctx.perf_src = perf_pct, perf_src

    perf_part = 0.0
    if perf_pct is not None:
        denom = _perf_denom(ctx.peer)
        ctx.perf_denom = round(denom, 2)
        perf_part = max(-1.0, min(1.0, perf_pct / denom))
    news_part = math.tanh(ctx.news_raw / 4.0)
    ctx.score = round(100.0 * math.tanh(0.6 * perf_part + 0.4 * news_part), 1)
    return ctx
