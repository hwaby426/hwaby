"""外围市场隔夜快照 —— 盘前第一件事：昨天夜里世界发生了什么。

为什么盘前必须看外围
----------------------
A 股的开盘缺口，绝大部分由"我们休市时外面发生了什么"决定。
9:15 集合竞价开始前，能拿到的最有信息量的东西依次是：

1. **富时中国 A50 期货** —— 几乎 24 小时交易，是外资对 A 股的直接定价，
   也是唯一在 A 股盘前还在实时变动的 A 股相关标的（权重最高）。
2. **纳斯达克中国金龙指数** —— 中概股隔夜表现，映射外资对中国资产的态度。
3. **恒生 / 恒生科技** —— 港股与 A 股联动，且港股开盘早于 A 股。
4. **美股三大指数** —— 整体风险偏好，纳指映射成长风格、道指映射价值周期。
5. **人民币汇率** —— 贬值利空外资流入、利好出口链。
6. **黄金 / 原油** —— 不进大盘环境分，只作为板块提示。

注意口径差异：**美股是昨夜收盘价**（已定格），**A50 是实时价**（还在动）。
所以"环境分"里 A50 给的是当下读数，美股给的是隔夜读数，不要混为一谈。

数据源：新浪 `hq.sinajs.cn`（与项目里 `data_fetcher/sina_fetcher.py` 同源，
本模块只做只读快照，不改动数据源层）。
"""

from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

SINA_QUOTE_URL = 'https://hq.sinajs.cn/list={codes}'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Referer': 'https://finance.sina.com.cn',
}

# code -> (sina符号, 中文名, 类别, 对A股环境分的权重, 是否为A股相关资产的直接定价)
# 权重含义：该指标对"明日A股开盘方向"的解释力。负数权重表示该指标上涨反而利空
# （例如人民币升值=USDCNY下跌=利好）。
_INSTRUMENTS: List[Tuple[str, str, str, float, str]] = [
    # ---- 直接定价 A 股预期 ----
    ('a50',      'hf_CHA50CFD', '富时中国A50期货', 1.00, '实时'),
    ('golden_dragon', 'gb_$hxc', '纳斯达克中国金龙', 1.00, '隔夜收盘'),
    # ---- 港股 ----
    ('hsi',      'rt_hkHSI',    '恒生指数',   0.45, '隔夜收盘'),
    ('hstech',   'rt_hkHSTECH', '恒生科技',   0.35, '隔夜收盘'),
    # ---- 美股 ----
    ('ixic',     'gb_ixic',     '纳斯达克',   0.45, '隔夜收盘'),
    ('dji',      'gb_dji',      '道琼斯',     0.25, '隔夜收盘'),
    ('inx',      'gb_$inx',     '标普500',    0.30, '隔夜收盘'),
    # ---- 汇率（USDCNY 上涨=人民币贬值=利空）----
    ('usdcny',   'fx_susdcny',  '美元/离岸人民币', -0.30, '实时'),
    # ---- 商品（不进环境分，只做板块提示）----
    ('gold',     'hf_GC',       '纽约黄金',   0.0,  '实时'),
    ('oil',      'hf_CL',       '纽约原油',   0.0,  '实时'),
]

# 商品 -> 受影响的 A 股板块提示（涨/跌两种说法都要给，别把下跌说成"走强"）
_COMMODITY_HINTS = {
    'gold': ('贵金属/黄金股', '山东黄金、紫金矿业等贵金属链偏多',
             '贵金属链承压'),
    'oil':  ('石油石化/油服', '中国石油、中海油服等石化链偏多',
             '石化链承压，航空/化工等成本端受益'),
}

# 风格映射：哪些指数对应哪种 A 股风格
_STYLE_HINTS = {
    'ixic':  '科技成长（半导体/AI/新能源）',
    'hstech': '科技成长（互联网/半导体）',
    'dji':   '价值周期（银行/地产/基建）',
    'inx':   '整体风险偏好',
    'golden_dragon': '中概/互联网/外资偏好',
    'hsi':   '港股映射（金融/地产/消费）',
}


@dataclass
class Quote:
    key: str
    name: str
    price: Optional[float]
    pct: Optional[float]          # 涨跌幅 %
    timing: str                   # 隔夜收盘 / 实时
    weight: float                 # 对 A 股环境分的权重
    raw_time: str = ''
    error: str = ''


@dataclass
class GlobalSnapshot:
    quotes: Dict[str, Quote] = field(default_factory=dict)
    env_score: float = 0.0        # -100 ~ +100
    env_verdict: str = ''
    bullets: List[str] = field(default_factory=list)     # 人话解读
    sector_hints: List[str] = field(default_factory=list)  # 板块提示
    fetched_at: str = ''


def _http_get(url: str, timeout: int = 10, retries: int = 2,
              referer: str = '') -> str:
    headers = dict(_HEADERS)
    if referer:
        headers['Referer'] = referer
    last = ''
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=timeout).read().decode('gbk', 'ignore')
        except Exception as e:          # noqa: BLE001
            last = f'{type(e).__name__}: {e}'
            time.sleep(0.4)
    raise RuntimeError(last)


def _f(value, default=None) -> Optional[float]:
    try:
        v = float(value)
        return v if v == v else default   # NaN 过滤
    except Exception:                      # noqa: BLE001
        return default


def _parse_line(var_name: str, body: str) -> Tuple[Optional[float], Optional[float], str]:
    """解析一条 sina 行情，返回 (现价, 涨跌幅%, 时间串)。

    三类格式字段位不同，逐个处理；任何一个字段拿不到就返回 None，
    让上层标记 error 而不是抛异常——盘前工具少一个指标也得继续跑。
    """
    p = body.split(',')
    if not p or p[0] == '':
        return None, None, ''

    if var_name.startswith('gb_'):
        # 名称, 现价, 涨跌幅%, 时间, 涨跌额, 开盘, 最高, 最低, 52周高, 52周低
        return _f(p[1]), _f(p[2]), (p[3] if len(p) > 3 else '')

    if var_name.startswith('rt_hk'):
        # code, 名称, 今开, 昨收, 最高, 最低, 现价, 涨跌额, 涨跌幅%
        return _f(p[6]), _f(p[8]), (p[16] if len(p) > 16 else '')

    if var_name.startswith('hf_'):
        # 实测格式：现价, 买价, 卖价, 最高, 最低, 时间, 昨收, 开盘, 持仓, 买量, 卖量, 日期, 名称
        # 例 hf_CHA50CFD: 12909.640,,12908.000,12910.000,12917.000,12887.000,
        #                18:22:32,12899.000,12894.000,...
        # 注意 p[6] 是**时间字符串**，昨收在 p[7] —— 这里踩过坑
        price = _f(p[0])
        prev = _f(p[7]) if len(p) > 7 else None
        pct = round((price / prev - 1) * 100, 3) if (price and prev) else None
        return price, pct, (p[6] if len(p) > 6 else '')

    if var_name.startswith('fx_'):
        # 时间, 今开, 昨收, 最高, ..., 名称, 涨跌幅%, 涨跌额, ...
        price = _f(p[1])
        pct = _f(p[10]) if len(p) > 10 else None
        return price, pct, (p[0] if len(p) > 0 else '')

    return None, None, ''


def fetch_global_snapshot(use_network: bool = True) -> GlobalSnapshot:
    """抓一圈外围市场，算出 -100~+100 的「隔夜环境分」。"""
    snap = GlobalSnapshot()
    snap.fetched_at = time.strftime('%Y-%m-%d %H:%M:%S')

    if not use_network:
        snap.env_verdict = '（离线模式，未取外围行情）'
        return snap

    codes = ','.join(sym for _, sym, _, _, _ in _INSTRUMENTS)
    try:
        text = _http_get(SINA_QUOTE_URL.format(codes=codes))
    except Exception as e:                 # noqa: BLE001
        logger.warning(f'[盘前] 外围行情抓取失败: {e}')
        snap.env_verdict = f'外围行情抓取失败（{type(e).__name__}），环境分缺省为 0'
        return snap

    for line in text.split('\n'):
        line = line.strip()
        if not line.startswith('var hq_str_'):
            continue
        try:
            head, rest = line.split('=', 1)
            var_name = head.replace('var hq_str_', '').strip()
            body = rest.split('"')[1] if '"' in rest else ''
        except Exception:                  # noqa: BLE001
            continue

        meta = next((m for m in _INSTRUMENTS if m[1] == var_name), None)
        if meta is None:
            continue
        key, _sym, name, weight, timing = meta

        try:
            price, pct, rtime = _parse_line(var_name, body)
        except Exception as e:             # noqa: BLE001
            snap.quotes[key] = Quote(key, name, None, None, timing, weight, error=str(e))
            continue

        if pct is None:
            snap.quotes[key] = Quote(key, name, price, None, timing, weight,
                                     rtime, error='涨跌幅解析失败')
            continue
        snap.quotes[key] = Quote(key, name, price, pct, timing, weight, rtime)

    _build_env_score(snap)
    return snap


def _build_env_score(snap: GlobalSnapshot) -> None:
    """把各指标的涨跌幅按权重合成环境分，并写出人话解读。

    合成方式：score = 100 * Σ(w_i * tanh(pct_i / 3)) / Σ|w_i|
    - 用 tanh 把涨跌幅压到 [-1,1]，避免单日暴涨暴跌（比如 A50 一天 +8%）主导结论
    - 除以 Σ|w| 归一化，缺失指标不会导致分数系统性偏移
    """
    num = 0.0
    den = 0.0
    for key, q in snap.quotes.items():
        if q.pct is None or q.error:
            continue
        w = q.weight
        if w == 0:
            continue
        import math
        num += w * math.tanh(q.pct / 3.0)
        den += abs(w)

    if den > 0:
        snap.env_score = round(100.0 * num / den, 1)

    s = snap.env_score
    if s >= 40:
        snap.env_verdict = '外围显著偏暖'
    elif s >= 15:
        snap.env_verdict = '外围偏暖'
    elif s > -15:
        snap.env_verdict = '外围中性'
    elif s > -40:
        snap.env_verdict = '外围偏冷'
    else:
        snap.env_verdict = '外围显著偏冷'

    # ---- 人话解读：按影响力排序 ----
    ranked = sorted(
        [q for q in snap.quotes.values() if q.pct is not None and not q.error],
        key=lambda q: abs(q.weight), reverse=True,
    )
    for q in ranked:
        arrow = '↑' if q.pct > 0 else ('↓' if q.pct < 0 else '—')
        if q.weight == 0:
            continue
        # USDCNY 上涨是人民币贬值，方向要反过来讲
        if q.key == 'usdcny':
            tone = '人民币贬值（利空外资流入）' if q.pct > 0 else '人民币升值（利好外资流入）'
            snap.bullets.append(f'{q.name} {arrow}{abs(q.pct):.2f}%  [{q.timing}] → {tone}')
        else:
            style = _STYLE_HINTS.get(q.key, '')
            extra = f' → {style}' if style else ''
            snap.bullets.append(f'{q.name} {arrow}{abs(q.pct):.2f}%  [{q.timing}]{extra}')

    # ---- 板块提示（商品，不计入环境分）----
    for key, (sector, up_text, down_text) in _COMMODITY_HINTS.items():
        q = snap.quotes.get(key)
        if q and q.pct is not None and not q.error and abs(q.pct) >= 0.5:
            tail = up_text if q.pct > 0 else down_text
            snap.sector_hints.append(f'{q.name} {q.pct:+.2f}%｜{sector}：{tail}')

    # ---- 风格提示 ----
    tech = [q for q in (snap.quotes.get('ixic'), snap.quotes.get('hstech'))
            if q and q.pct is not None and not q.error]
    value = snap.quotes.get('dji')
    if tech and value and value.pct is not None and not value.error:
        tavg = sum(q.pct for q in tech) / len(tech)
        if tavg - value.pct > 1.0:
            snap.sector_hints.append(
                f'科技({tavg:+.2f}%)明显强于道指({value.pct:+.2f}%) → 成长风格占优')
        elif value.pct - tavg > 1.0:
            snap.sector_hints.append(
                f'道指({value.pct:+.2f}%)明显强于科技({tavg:+.2f}%) → 价值/防御风格占优')
