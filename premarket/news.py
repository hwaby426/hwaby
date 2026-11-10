"""盘前消息采集 —— 公告 / 个股新闻 / 7x24 快讯 / 本地喂料。

设计原则
---------
1. **多 provider，任一失败不影响其他**。盘前时间紧，某个源挂了不能让整个简报跑不出来。
2. **只做只读抓取**，不改动 `data_fetcher/` 数据源层。
3. **公告优先于新闻**。公告是公司自己说的、有法律效力、且是唯一有确定时间戳的硬信息；
   新闻是二手解读，权重应低于公告。这一点直接体现在 `source` 字段和后续打分上。
4. 本地文件 provider 是为了让「我自己看到一条消息」也能进分析流程。

已验证可用的源（2026-10 实测）：
  * 东财个股公告   np-anotice-stock.eastmoney.com/api/security/ann
  * 新浪个股新闻   vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/
  * 新浪 7x24 快讯 zhibo.sina.com.cn/api/zhibo/feed  （滚动实时，覆盖昨夜今晨）
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from loguru import logger

_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'),
    'Referer': 'https://finance.sina.com.cn',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

# 东财接口对 Referer 敏感：用新浪的 Referer 会返回 HTTP 567（反爬）。
_EM_REFERER = 'https://data.eastmoney.com/notices/'

# 公告 vs 新闻 的权重档位（在 sentiment 里会用到）
SOURCE_WEIGHT = {
    'announcement': 1.0,   # 公司公告，硬信息
    'news': 0.6,           # 个股新闻，二手解读
    'flash': 0.4,          # 7x24 快讯，需要按代码过滤后才有意义
    'local': 0.8,          # 用户手工喂料
}


@dataclass
class NewsItem:
    title: str
    source: str            # announcement / news / flash / local
    published: str = ''    # 发布时间串
    url: str = ''
    summary: str = ''
    code: str = ''         # 关联代码（快讯里可能没有）
    # 相关性分层：
    #   direct  = 标题直接点到这只票（进个股打分）
    #   market  = 大盘/板块背景噪音（只做背景，不进个股打分）
    relevance: str = 'market'

    @property
    def weight(self) -> float:
        return SOURCE_WEIGHT.get(self.source, 0.5)

    def score_weight(self) -> float:
        """参与个股打分的实际权重。市场噪音直接归零。"""
        return self.weight if self.relevance == 'direct' else 0.0

    def age_hours(self, now: Optional[datetime] = None) -> float:
        """距现在的小时数。解析不出时间就返回 999（当作很旧）。"""
        now = now or datetime.now()
        s = (self.published or '').strip()
        fmts = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
                '%m-%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d')
        for f in fmts:
            try:
                dt = datetime.strptime(s, f)
                if dt.year == 1900:      # 只有 "10-29 18:27" 这种缺年份的
                    dt = dt.replace(year=now.year)
                return abs((now - dt).total_seconds()) / 3600.0
            except Exception:            # noqa: BLE001
                continue
        return 999.0


def _http_get(url: str, timeout: int = 12, retries: int = 1,
              encoding: str = 'utf-8', referer: str = '') -> str:
    headers = dict(_HEADERS)
    if referer:
        headers['Referer'] = referer
    last = ''
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            return raw.decode(encoding, 'ignore')
        except Exception as e:           # noqa: BLE001
            last = f'{type(e).__name__}: {e}'
            time.sleep(0.4)
    raise RuntimeError(last)


# ----------------------------------------------------------------------
# 东财个股公告
# ----------------------------------------------------------------------
def fetch_announcements(code: str, days: int = 14, limit: int = 30) -> List[NewsItem]:
    """抓东财个股公告。

    days 给 14 天而不是 7 天：A 股公司发公告不规律，业绩预告/增减持/解禁这类
    关键公告往往间隔很久。但**盘后新发的公告权重最高**（次日直接反映在开盘），
    这个时效性差异在 `sentiment` 里按 age_hours 衰减处理。
    """
    num = code.split('.')[-1]
    url = (f'https://np-anotice-stock.eastmoney.com/api/security/ann'
           f'?page_size={limit}&page_index=1&ann_type=A&client_source=web'
           f'&stock_list={num}&f_node=0&s_node=0')
    try:
        text = _http_get(url, referer=_EM_REFERER)
        j = json.loads(text)
        items = (j.get('data') or {}).get('list') or []
    except Exception as e:               # noqa: BLE001
        logger.warning(f'[盘前] {code} 公告抓取失败: {e}')
        return []

    cutoff = datetime.now() - timedelta(days=days)
    out: List[NewsItem] = []
    for it in items:
        dt_s = (it.get('notice_date') or '')[:19]
        try:
            dt = datetime.strptime(dt_s, '%Y-%m-%d %H:%M:%S')
        except Exception:                # noqa: BLE001
            try:
                dt = datetime.strptime(dt_s[:10], '%Y-%m-%d')
            except Exception:            # noqa: BLE001
                dt = None
        if dt and dt < cutoff:
            continue
        title = (it.get('title') or '').strip()
        if not title:
            continue
        # 标题自带 "贵州茅台:xxx" 前缀，去掉更干净
        if ':' in title or '：' in title:
            for sep in (':', '：'):
                if sep in title:
                    title = title.split(sep, 1)[1].strip()
                    break
        out.append(NewsItem(
            title=title, source='announcement', published=dt_s,
            url=f"https://data.eastmoney.com/notices/detail/{num}/{it.get('notice_id','')}.html",
            code=code,
        ))
    return out


# ----------------------------------------------------------------------
# 新浪个股新闻
# ----------------------------------------------------------------------
_NEWS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})&nbsp;(\d{2}:\d{2})&nbsp;&nbsp;"
    r"<a[^>]*href='([^']*)'>([^<]*)</a>", re.S)


def fetch_stock_news(code: str, name: str = '', limit: int = 20,
                     days: int = 3) -> List[NewsItem]:
    """抓新浪个股新闻（HTML 解析）。

    注意：新浪这个"个股相关资讯"页会混入同行业和大盘新闻（实测茅台页里有
    "中金公司跌停""收评：双创指数均跌超4%"这类）。所以每条都要打 relevance 标，
    只有标题真点到这只票的才进个股打分，其余当背景噪音。
    """
    url = f'https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{code}.phtml'
    try:
        html = _http_get(url, encoding='gbk')
    except Exception as e:               # noqa: BLE001
        logger.warning(f'[盘前] {code} 新闻抓取失败: {e}')
        return []

    cutoff = datetime.now() - timedelta(days=days)
    out: List[NewsItem] = []
    for m in _NEWS_RE.finditer(html):
        d, t, link, title = m.groups()
        title = re.sub(r'\s+', ' ', title).strip()
        if not title:
            continue
        try:
            if datetime.strptime(d, '%Y-%m-%d') < cutoff:
                continue
        except Exception:                # noqa: BLE001
            pass
        out.append(NewsItem(title=title, source='news',
                            published=f'{d} {t}', url=link, code=code,
                            relevance=_judge_relevance(title, code, name)))
        if len(out) >= limit:
            break
    return out


def _judge_relevance(title: str, code: str, name: str = '') -> str:
    """标题是否直接指向这只票。"""
    if name and name in title:
        return 'direct'
    num = code.split('.')[-1]
    if num and num in title:
        return 'direct'
    return 'market'


# ----------------------------------------------------------------------
# 新浪 7x24 快讯（滚动，覆盖昨夜今晨）
# ----------------------------------------------------------------------
def fetch_flash_news(hours: float = 16.0, max_pages: int = 15) -> List[NewsItem]:
    """抓新浪 7x24 财经快讯，翻页累积直到覆盖 `hours` 小时。

    踩过的坑：`page_size` 上限是 **100**，传 200 会被服务端静默截断成 10 条
    （不报错，只是数据不够，很容易误以为"今晚没消息"）。
    另外快讯很密集，一页只覆盖十几分钟，必须翻页才能覆盖整个夜间时段。
    """
    out: List[NewsItem] = []
    for page in range(1, max_pages + 1):
        url = (f'https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size=100'
               f'&zhibo_id=152&tag_id=0&dire=f&dpc=1')
        try:
            j = json.loads(_http_get(url, retries=0))
            lst = j['result']['data']['feed']['list']
        except Exception as e:           # noqa: BLE001
            if not out:
                logger.warning(f'[盘前] 7x24快讯抓取失败: {e}')
            break
        if not lst:
            break

        for it in lst:
            txt = re.sub(r'\s+', ' ', (it.get('rich_text') or '')).strip()
            if not txt:
                continue
            title = txt if len(txt) <= 120 else txt[:117] + '...'
            out.append(NewsItem(
                title=title, source='flash',
                published=(it.get('create_time') or '')[:16],
                url=it.get('docurl', ''),
            ))

        # 已经覆盖够长时间就停
        if out and out[-1].age_hours() > hours:
            break

    return [x for x in out if x.age_hours() <= hours]


# 快讯标准格式：【海螺新材：前三季营收37.54亿元 净亏损6129.14万元】……
# 公司名一定在【】里、后面紧跟冒号，这是最可靠的锚点。
_FLASH_COMPANY_RE = re.compile(r'【([^】]{2,16}?)\s*[：:]')


def filter_flash_by_company(items: List[NewsItem],
                            names: List[str]) -> List[NewsItem]:
    """按快讯【】里的公司名**精确**匹配，只保留真正属于这只票的快讯。

    踩过的坑：曾经用"股票简称的子串"去全文模糊匹配，结果"海螺新材"的核心词
    "新材"把"上纬新材""康欣新材""宏柏新材""凯立新材"全吸了进来，
    一只票的综合分被无关快讯从 -16.9 拉到 -61.4。
    **子行业同名公司极多，模糊匹配在 A 股是灾难**，必须精确。
    """
    wanted = {n.strip() for n in (names or []) if n and n.strip()}
    if not wanted:
        return []
    out = []
    for it in items:
        m = _FLASH_COMPANY_RE.search(it.title)
        if not m:
            continue
        if m.group(1).strip() in wanted:
            out.append(it)
    return out


# ----------------------------------------------------------------------
# 本地喂料：把"我看到的一条消息"也放进分析流程
# ----------------------------------------------------------------------
def load_local_news(path: str) -> List[NewsItem]:
    """从本地文件读消息，一行一条（空行和 # 开头忽略）。

    支持在行首写 `[code]` 指定归属，例如：
        [sh.600519] 茅台公告：拟回购不低于30亿元
    """
    if not path or not os.path.exists(path):
        return []
    out: List[NewsItem] = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                code = ''
                m = re.match(r'^\[([^\]]+)\]\s*(.*)$', s)
                if m:
                    code, s = m.group(1).strip(), m.group(2).strip()
                if s:
                    out.append(NewsItem(title=s, source='local', code=code,
                                        published=datetime.now().strftime('%Y-%m-%d %H:%M')))
    except Exception as e:               # noqa: BLE001
        logger.warning(f'[盘前] 本地消息读取失败: {e}')
    return out


# ----------------------------------------------------------------------
# 汇总入口
# ----------------------------------------------------------------------
# 公司名"核心词"黑名单：这些是行业通用后缀，用来匹配会把半个行业吸进来
# （"海螺新材"的核心词"新材"曾把上纬/康欣/宏柏/凯立新材全吸进打分）。
# 核心词命中黑名单时，只允许全称匹配。
GENERIC_CORE_STOP = {
    '新材', '科技', '股份', '集团', '控股', '生物', '电子', '能源', '环保',
    '医药', '食品', '汽车', '通信', '智能', '建设', '化工', '实业', '发展',
    '国际', '投资', '置业', '材料', '光电', '电气', '机械', '软件', '网络',
    '信息', '数据', '健康', '医疗', '金属', '矿业', '地产', '农业', '物流',
    '航空', '旅游', '酒店', '传媒', '教育', '电力', '燃气', '水务', '重工',
}


def filter_flash_loose(flash: List[NewsItem], name: str,
                       extra_keywords: Optional[List[str]] = None) -> List[NewsItem]:
    """第二通道：匹配**不带【】**的快讯。

    规则：全称命中 → 收；核心词命中但核心词不在黑名单 → 收。
    只处理无【】的条目（有【】的已由 filter_flash_by_company 精确处理），
    避免把"【上纬新材】"这种别家公司的条目再吸一遍。
    """
    if not name:
        return []
    core_ok = []
    for k in (extra_keywords or []):
        k = (k or '').strip()
        if k and len(k) >= 2 and k not in GENERIC_CORE_STOP:
            core_ok.append(k)
    out = []
    for it in flash:
        if _FLASH_COMPANY_RE.search(it.title):
            continue                      # 有【】的走精确通道
        if name in it.title or any(k in it.title for k in core_ok):
            out.append(it)
    return out


def gather_news(
    code: str,
    name: str = '',
    hours: float = 16.0,
    local_path: Optional[str] = None,
    use_network: bool = True,
    extra_keywords: Optional[List[str]] = None,
    flash: Optional[List[NewsItem]] = None,
) -> List[NewsItem]:
    """收集一只票的全部盘前消息，按「公告优先、时间由新到旧」排序。

    `flash` 由调用方（service）一次性抓好后传进来复用——
    20 只票各抓一遍翻页快讯既慢又容易被限流。
    """
    items: List[NewsItem] = []

    items.extend(load_local_news(local_path) if local_path else [])

    if use_network:
        anns = fetch_announcements(code)
        for a in anns:
            a.relevance = 'direct'     # 公司自己发的公告，天然直接相关
        items.extend(anns)

        items.extend(fetch_stock_news(code, name=name))

        # 快讯双通道：【公司名】精确 + 无【】条目的全称/安全核心词
        if flash is None:
            flash = fetch_flash_news(hours=hours)
        hit = filter_flash_by_company(flash, [name])
        loose = filter_flash_loose(flash, name, extra_keywords)
        seen_t = {x.title for x in hit}
        for x in hit + [y for y in loose if y.title not in seen_t]:
            x.relevance = 'direct'
            x.code = code
        items.extend((hit + [y for y in loose if y.title not in seen_t])[:20])

    # 去重（同标题只留一条）
    seen = set()
    uniq: List[NewsItem] = []
    for it in items:
        k = it.title.strip()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)

    # 公告优先，其次按时间新→旧
    uniq.sort(key=lambda x: (-(x.weight), x.age_hours()))
    return uniq
