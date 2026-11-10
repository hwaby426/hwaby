"""利好 / 利空判定：规则词典打底 + 大模型精判（可选）。

为什么要两层
-------------
规则词典的价值在于**确定性**：零依赖、秒级、可复现，且能把"业绩预增""减持"
"立案调查"这类强信号稳定抓出来。它的短板是读不懂复杂语义——
"业绩预增 30%，但低于市场预期" 会被判成纯利好。

大模型能补这个短板，但它有成本和延迟，而且没配 key 就跑不了。
所以设计成：**规则永远先跑，LLM 只作为可选的二次精判**。
没配 key 时自动退回规则结果，并在报告里显式标注"未经语义复核"。

评分口径
--------
raw_score = Σ(类别分 × 消息源权重 × 时效衰减)
news_score = 100 * tanh(raw_score / 4)   → 落在 -100 ~ +100

时效衰减：盘后/今晨刚出的消息权重最高，一周前的旧闻几乎不影响开盘。
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

from premarket.news import NewsItem

# ----------------------------------------------------------------------
# 词典：类别 -> (关键词列表, 分值)
# 分值量级参考：±5 = 足以改变次日开盘方向；±2~3 = 明确但可博弈；±1 = 轻微
# ----------------------------------------------------------------------
POSITIVE_RULES: Dict[str, Tuple[List[str], float]] = {
    '业绩向好': (['业绩预增', '预增', '预盈', '扭亏为盈', '扭亏', '净利润增长',
                  '净利增长', '业绩增长', '业绩超预期', '超出预期', '超市场预期',
                  '大超预期', '营收增长', '利润增长', '同比大增', '大幅增长'], 3.0),
    '重大订单': (['中标', '签订合同', '签署合同', '重大合同', '订单', '框架协议',
                  '战略合作', '供货协议', '采购合同'], 2.5),
    '回购增持': (['回购', '增持', '股份回购', '增持计划', '回购计划',
                  '倡议增持'], 2.5),
    '资本运作': (['并购', '重组', '资产注入', '收购', '要约收购', '借壳',
                  '重大资产重组'], 2.0),
    '产品提价': (['提价', '涨价', '上调价格', '产品价格上调', '提价函'], 2.0),
    '研发突破': (['获批', '临床', '新药', '专利', '技术突破', '量产', '投产',
                  '取得突破', '注册证书', '首台套'], 2.5),
    '政策扶持': (['补贴', '政府补助', '税收优惠', '专项支持', '政策利好',
                  '入选', '认定'], 1.5),
    '分红送转': (['分红', '派息', '送转', '高送转', '利润分配'], 1.0),
    '行业景气': (['行业景气', '需求增长', '景气度', '供不应求', '产能满载'], 1.0),
}

NEGATIVE_RULES: Dict[str, Tuple[List[str], float]] = {
    '退市风险': (['退市', 'st', '暂停上市', '终止上市', '破产', '清算'], -5.0),
    '监管处罚': (['立案调查', '立案', '处罚', '问询函', '监管函', '警示函',
                  '被查', '违规', '纪律处分', '行政处罚'], -4.0),
    # 注意"低于市场预期"这种中间夹字的写法——只写"低于预期"会漏掉，
    # 而它恰恰是"预增但不及预期"这种复合语义的关键负面对冲项
    '业绩变脸': (['业绩预减', '预减', '预亏', '亏损', '净利下滑', '业绩下滑',
                  '不及预期', '低于预期', '低于市场预期', '不及市场预期',
                  '低于机构预期', '业绩变脸', '同比下降', '营收下降',
                  '净利润下降', '亏损扩大'], -3.0),
    '减值计提': (['商誉减值', '减值', '计提', '资产减值', '坏账'], -2.5),
    '减持':     (['减持', '拟减持', '大宗交易', '股份减持', '被动减持'], -2.5),
    '生产事故': (['停产', '停工', '事故', '爆炸', '火灾', '召回', '质量问题'], -2.5),
    # "无限售条件流通股上市流通"这种标题：'限售股上市流通'匹配不上
    # （中间隔着"条件...流通"），必须用更长的连续子串
    '解禁':     (['解禁', '限售股上市流通', '限售股份上市', '解除限售',
                  '限售条件流通股上市', '限售流通股上市', '股改限售股'], -2.0),
    '诉讼冻结': (['诉讼', '仲裁', '冻结', '查封', '被执行', '失信'], -2.0),
    '股权质押': (['质押', '平仓风险', '被动平仓', '补充质押'], -2.0),
    '摊薄':     (['定增', '非公开发行', '可转债', '配股', '增发'], -1.0),
    '人事变动': (['辞职', '离职', '高管变动', '免去', '辞任'], -1.0),
    '对外担保': (['担保', '对外担保', '连带责任'], -1.0),
}

# 中性：出现这些词说明是例行公告，不参与打分
NEUTRAL_HINTS = ['股东大会', '董事会决议', '监事会', '会计政策变更', '关联交易',
                 '召开', '通知', '更正', '补充', '延期', '变更', '聘任',
                 '募集资金', '说明会', '业绩说明会', '权益分派实施', '停牌提示']

# 定期报告：标题本身**没有方向性**（三季报可能是大增也可能是巨亏）。
# 规则词典读不出好坏，必须标出来交给人工或大模型——这是规则层最大的盲区，
# 不能默默判成 0 分了事，否则用户会以为"这条已经分析过了"。
PERIODIC_REPORT_HINTS = ['季度报告', '三季度报告', '一季报', '二季报', '三季报',
                         '半年报', '半年度报告', '年度报告', '年报', '季报',
                         '业绩快报', '主要经营数据']

# 否定/豁免词：命中时把分数往 0 方向压。
# 注意：**不能放单字"无"**——"无锡子公司中标""无线电""无人机"全会被误伤
# （实测："无锡子公司中标3.2亿"被压两折）。否定语义至少用两字词。
NEGATION_HINTS = ['未收到', '不存在', '不涉及', '没有', '否认', '澄清',
                  '不属实', '未有', '未发生', '未构成', '不存在重大']


@dataclass
class ItemVerdict:
    item: NewsItem
    category: str = ''       # 命中的类别
    polarity: float = 0.0    # 该条目的原始分（未乘权重/衰减）
    hits: List[str] = field(default_factory=list)
    llm_label: str = ''      # 利好 / 利空 / 中性 / 无法判断
    llm_reason: str = ''
    llm_confidence: float = 0.0
    llm_score: float = 0.0   # LLM 给的 -5 ~ +5 分
    used_llm: bool = False


@dataclass
class SentimentResult:
    code: str
    name: str = ''
    raw_score: float = 0.0
    news_score: float = 0.0      # -100 ~ +100
    verdict: str = ''            # 消息面偏多 / 偏空 / 中性
    verdicts: List[ItemVerdict] = field(default_factory=list)
    llm_enabled: bool = False
    llm_note: str = ''
    no_news: bool = False

    @property
    def top_items(self) -> List[ItemVerdict]:
        return sorted(self.verdicts, key=lambda v: abs(v.polarity), reverse=True)


def _kw_in(title_l: str, kw: str) -> bool:
    """关键词匹配。中文直接子串；纯 ASCII 词（如 st）要求字母边界，
    防止 "1st"/"best" 这类英文误命中。匹配在 lower 后的标题上做，
    这样规则里写 'st' 也能命中标题里的 'ST'。"""
    if kw.isascii():
        k = kw.lower()
        i = title_l.find(k)
        while i >= 0:
            left_ok = (i == 0 or not title_l[i - 1].isascii()
                       or not title_l[i - 1].isalnum())
            j = i + len(k)
            right_ok = (j >= len(title_l) or not title_l[j].isascii()
                        or not title_l[j].isalnum())
            if left_ok and right_ok:
                return True
            i = title_l.find(k, i + 1)
        return False
    return kw in title_l


def _match_rules(title: str) -> Tuple[str, float, List[str]]:
    """在标题里找命中的规则，返回 (类别描述, 净分值, 命中词列表)。

    **按类别聚合后求净值**，而不是取"绝对值最大的那一类"。
    原因：真实公告经常是复合语义——"业绩预增但低于市场预期"同时命中
    '业绩向好'(+3) 和 '业绩变脸'(-3)，取最大绝对值会得到 +3（纯利好，错），
    求净值得到 0（中性，对）。"中标 5 亿并拟减持 2%"= +2.5-2.5 = 0 也更合理。

    同一类别内多个关键词只计一次，避免"业绩预增+净利润增长"被算两遍。
    """
    title_l = title.lower()
    cats: List[str] = []
    hits: List[str] = []
    net = 0.0
    for cat, (kws, sc) in list(POSITIVE_RULES.items()) + list(NEGATIVE_RULES.items()):
        matched = [k for k in kws if _kw_in(title_l, k)]
        if not matched:
            continue
        net += sc
        cats.append(cat)
        hits.extend(matched)
    return ' + '.join(cats), round(net, 2), hits


def _time_decay(hours: float) -> float:
    """时效衰减：盘后/今晨的消息最值钱，越旧越不值钱。

    0-14h  → 1.0   （昨夜今晨，直接决定次日开盘）
    14-24h → 0.7
    24-72h → 0.4
    >7天   → 0.1
    """
    if hours <= 14:
        return 1.0
    if hours <= 24:
        return 0.7
    if hours <= 72:
        return 0.4
    if hours <= 168:
        return 0.15
    return 0.1


def _apply_negation(title: str, score: float) -> Tuple[float, str]:
    """命中否定词时把分数压向 0。返回 (新分数, 说明)。"""
    for n in NEGATION_HINTS:
        if n in title:
            return score * 0.2, f'含豁免/否定表述「{n}」，分数打两折'
    return score, ''


def score_with_rules(item: NewsItem) -> ItemVerdict:
    v = ItemVerdict(item=item)
    title = item.title

    cat, sc, hits = _match_rules(title)
    periodic = any(h in title for h in PERIODIC_REPORT_HINTS)

    if not cat and periodic:
        # 定期报告：方向未知，标出来给人/大模型看，不许悄悄算 0 分
        v.category = '定期报告（方向待定，需看数据）'
        v.polarity = 0.0
        v.hits = [h for h in PERIODIC_REPORT_HINTS if h in title]
        return v

    if not cat:
        if any(h in title for h in NEUTRAL_HINTS):
            v.category = '例行公告'
        elif any(h in title for h in NEGATION_HINTS):
            v.category = '澄清/否认类'
        else:
            v.category = '无明显信号'
        v.polarity = 0.0
        return v

    sc, note = _apply_negation(title, sc)
    v.category = cat + ('｜定期报告' if periodic else '')
    v.polarity = sc
    v.hits = hits
    if note:
        v.category = f'{v.category}（{note}）'
    return v


# ----------------------------------------------------------------------
# 大模型精判（可选）
# ----------------------------------------------------------------------
def _llm_config() -> Optional[Dict[str, str]]:
    base = os.getenv('PREMARKET_LLM_BASE_URL', '').strip()
    key = os.getenv('PREMARKET_LLM_API_KEY', '').strip()
    if not base or not key:
        return None
    return {
        'base': base.rstrip('/'),
        'key': key,
        'model': os.getenv('PREMARKET_LLM_MODEL', 'deepseek-chat').strip(),
    }


def _call_llm(cfg: Dict[str, str], payload: list, timeout: int = 60) -> Optional[str]:
    url = f"{cfg['base']}/chat/completions"
    body = json.dumps({
        'model': cfg['model'],
        'messages': [
            {'role': 'system', 'content': (
                '你是A股盘前研究助手。对每条消息判断其对**次日开盘**的利好/利空方向。'
                '只输出JSON数组，每项含 title/score/reason/confidence/label。'
                'score 为 -5(重大利空) 到 +5(重大利好) 的整数或一位小数；'
                'label 取 利好/利空/中性 之一；confidence 为 0~1。'
                '注意区分"业绩预增但低于预期"这类复合语义，以及"例行公告"应判中性。'
                '不要输出JSON以外的内容。'
            )},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
        ],
        'temperature': 0.1,
    }, ensure_ascii=False).encode('utf-8')

    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {cfg['key']}",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')
        j = json.loads(raw)
        return j['choices'][0]['message']['content']
    except Exception as e:                # noqa: BLE001
        logger.warning(f'[盘前] 大模型调用失败: {type(e).__name__}: {e}')
        return None


def _parse_llm_json(text: str) -> List[dict]:
    """从可能带 ```json 围栏的输出里抠出 JSON 数组。"""
    s = (text or '').strip()
    if s.startswith('```'):
        s = s.strip('`')
        if s.lower().startswith('json'):
            s = s[4:]
    i, j = s.find('['), s.rfind(']')
    if i < 0 or j <= i:
        return []
    try:
        out = json.loads(s[i:j + 1])
        return out if isinstance(out, list) else []
    except Exception:                     # noqa: BLE001
        return []


def refine_with_llm(verdicts: List[ItemVerdict], max_items: int = 12) -> bool:
    """对规则判定过的条目做二次语义精判。成功返回 True。"""
    cfg = _llm_config()
    if cfg is None:
        return False

    # 优先送"规则判定有方向"的和"公告类"的，纯中性条目意义不大
    cands = [v for v in verdicts if abs(v.polarity) > 0 or v.item.source == 'announcement']
    if not cands:
        return False
    cands = cands[:max_items]

    payload = [
        {'title': v.item.title, 'source': v.item.source,
         'published': v.item.published, 'rule_score': v.polarity}
        for v in cands
    ]
    text = _call_llm(cfg, payload)
    if not text:
        return False

    arr = _parse_llm_json(text)
    if not arr:
        return False

    by_title = {r.get('title', '').strip(): r for r in arr if isinstance(r, dict)}
    hit = 0
    for v in cands:
        r = by_title.get(v.item.title.strip())
        if not r:
            continue
        try:
            v.llm_score = float(r.get('score', 0))
        except Exception:                 # noqa: BLE001
            v.llm_score = 0.0
        v.llm_label = str(r.get('label', '')).strip()
        v.llm_reason = str(r.get('reason', '')).strip()
        try:
            v.llm_confidence = float(r.get('confidence', 0))
        except Exception:                 # noqa: BLE001
            v.llm_confidence = 0.0
        v.used_llm = True
        hit += 1
    return hit > 0


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------
def analyze_news(
    code: str,
    items: List[NewsItem],
    name: str = '',
    use_llm: bool = True,
) -> SentimentResult:
    """对一只票的消息做利好/利空打分。"""
    res = SentimentResult(code=code, name=name)

    if not items:
        res.no_news = True
        res.verdict = '无可分析消息'
        return res

    scored: List[ItemVerdict] = []
    total = 0.0
    for it in items:
        v = score_with_rules(it)
        # 只有 direct（真的点到这只票）才进个股分；市场噪音只保留展示
        if it.score_weight() > 0 and v.polarity != 0:
            decay = _time_decay(it.age_hours())
            total += v.polarity * it.score_weight() * decay
        scored.append(v)
    res.verdicts = scored
    res.raw_score = round(total, 2)

    # 大模型精判
    if use_llm:
        cfg = _llm_config()
        if cfg is None:
            res.llm_note = ('未配置 PREMARKET_LLM_API_KEY / PREMARKET_LLM_BASE_URL，'
                            '本轮为纯规则判定，未经语义复核')
        elif refine_with_llm(scored):
            res.llm_enabled = True
            # LLM 命中时，用 LLM 分覆盖规则分重新合成（规则分仍保留用于对照）
            llm_total = 0.0
            for v in scored:
                if not v.used_llm:
                    continue
                decay = _time_decay(v.item.age_hours())
                llm_total += v.llm_score * v.item.score_weight() * decay
            res.llm_note = f'已用 {cfg["model"]} 复核 {sum(1 for v in scored if v.used_llm)} 条'
            # 规则与 LLM 各占一半，避免单一来源走偏
            total = (total + llm_total) / 2.0
            res.raw_score = round(total, 2)
        else:
            res.llm_note = '大模型调用失败，已退回纯规则结果'

    res.news_score = round(100.0 * math.tanh(total / 4.0), 1)
    s = res.news_score
    if s >= 30:
        res.verdict = '消息面明显偏多'
    elif s >= 10:
        res.verdict = '消息面偏多'
    elif s > -10:
        res.verdict = '消息面中性'
    elif s > -30:
        res.verdict = '消息面偏空'
    else:
        res.verdict = '消息面明显偏空'
    return res
