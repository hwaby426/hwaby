"""同业等权口径 —— 「板块跌了多少」到底该怎么量。

为什么需要这个
--------------
盘前系统原本只看**板块指数**涨跌（东财 BK 代码）。但它有两个结构性偏差：

1. **成分股太杂，跌幅被平均掉**
   2026-10-29 网络安全板块指数 −3.91%，而 8 只纯网安票等权 **−8.39%**、中位 **−9.61%**。
   用指数解释个股，会把板块因素系统性错记成个股利空——深信服 −13.34% 相对指数
   "跑输 9.13pct"，相对同业只跑输 4.95pct，差近一倍。

2. **板块之间波动率差 2.4 倍**
   实测日收益 std：银行 1.25%、白酒 1.74%、化妆品 1.88%、光伏 2.40%、
   网络安全 2.51%、消费电子 2.69%、半导体 2.99%。
   原来板块分用**固定** 3% 分母（`clamp(pct/3)`），等于让银行跌 3% 就算极端（2.4σ）、
   半导体跌 3% 只算平常（1σ）——同一个绝对涨跌在不同板块被给了同一个分。
   这里算出的 `sigma` 就是用来做波动率归一化的分母。

设计约束
--------
* **只用本地库**（`daily_kline.pct_chg`），完全不联网 —— 东财板块指数接口一限流就废，
  而这套口径要长期每天跑，不能依赖它。
* **成分股是受控表**，宁可没有也不误吸。自动凑同业试过且失败：`stock_info.industry`
  是 NULL（100% 空），板块成分接口（clist）易限流，按相关性聚类又会引入未来函数。
* **恒定剔除自身**：算板块压力时把自己算进去，会得到"我跌所以板块跌"的循环论证。
* 数据不足时返回 None，调用方退回板块指数口径 —— 不猜。

怎么扩表
--------
往 `PEER_BASKETS` 里加一行即可，key 用东财**板块/题材名**（会先按题材名匹配，
再按行业链名匹配）。成分股代码必须能在本地库里查到，否则自动跳过；
有效成分少于 3 只时该篮子视为不可用。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

# 板块/题材名 → 核心成分股。
# 选股原则：主业纯度高的龙头（不收"沾边"的概念股），4~10 只，等权。
PEER_BASKETS: Dict[str, List[str]] = {
    # 网络安全：纯网安/数据安全，不含综合 IT
    '网络安全': ['sz.300454', 'sz.300369', 'sz.002439', 'sh.688023', 'sh.688561',
                 'sz.002212', 'sh.688030', 'sz.300768', 'sz.002268'],
    '数据安全': ['sz.300454', 'sz.300369', 'sz.002439', 'sh.688023', 'sh.688561',
                 'sh.688030', 'sz.002268'],
    # 白酒
    '白酒': ['sh.600519', 'sz.000858', 'sz.000568', 'sh.600809', 'sz.002304',
             'sz.000596', 'sh.603369'],
    # 化妆品 / 医美
    '化妆品': ['sz.300740', 'sz.002612', 'sh.603605', 'sz.300896', 'sz.300957',
               'sh.603983', 'sh.600315'],
    '医疗美容': ['sz.300740', 'sz.002612', 'sh.603605', 'sz.300896', 'sz.300957'],
    # 电子
    '半导体': ['sh.688981', 'sh.603986', 'sz.002371', 'sz.300661', 'sh.688012'],
    '消费电子': ['sz.002475', 'sz.002241'],
    # 新能源
    '锂电池': ['sz.300750', 'sz.002594', 'sz.300014', 'sz.002460'],
    '光伏设备': ['sh.601012', 'sz.002129', 'sz.300274', 'sz.002459'],
    # 金融
    '银行': ['sh.600036', 'sh.601398', 'sh.601166', 'sz.000001'],
    '证券': ['sh.600030', 'sh.601688', 'sz.300059'],
    # 医药 / 军工
    '化学制药': ['sh.600276', 'sz.300760', 'sh.603259'],
    '航空装备': ['sh.600893', 'sz.000768'],
}

# 行业链名 → 篮子名（只放**含义安全**的映射；不做"软件开发→网络安全"这种跨语义映射，
# 那会把用友网络、恒生电子也算进网安篮子）
BASKET_ALIASES: Dict[str, str] = {
    '白酒Ⅱ': '白酒', '白酒Ⅲ': '白酒', '白酒II': '白酒', '白酒III': '白酒',
    '食品饮料': '白酒',
    '化妆品': '化妆品', '美容护理': '化妆品', '医疗美容': '医疗美容',
    '半导体': '半导体', '半导体设备': '半导体', '集成电路': '半导体',
    '消费电子': '消费电子', '电子元件': '消费电子',
    '电池': '锂电池', '锂电池': '锂电池', '能源金属': '锂电池',
    '光伏设备': '光伏设备', '光伏': '光伏设备',
    '银行': '银行', '国有大型银行Ⅱ': '银行', '股份制银行Ⅱ': '银行',
    '证券Ⅱ': '证券', '证券': '证券', '证券III': '证券',
    '化学制药': '化学制药', '医疗器械': '化学制药', '医疗服务': '化学制药',
    '航空装备Ⅱ': '航空装备', '航空装备': '航空装备', '航天装备': '航空装备',
}

MIN_REFS = 3          # 有效同业少于 3 只 → 不可用
SIGMA_WINDOW = 60     # 波动率窗口（交易日）
SIGMA_MIN_OBS = 20
SIGMA_FLOOR = 1.0     # 波动率下限 %：太小的 sigma 会让分母趋近 0，分数爆炸
SIGMA_MULT = 2.0      # 分母 = 2σ ≈ p95，即"两倍标准差算极端"
DENOM_MIN = 2.0
DENOM_MAX = 6.0

_PANEL: Optional[pd.DataFrame] = None
_PANEL_KEYS = 0


@dataclass
class PeerSnapshot:
    basket: str = ''
    refs: List[str] = field(default_factory=list)      # 实际参与计算的同业（已剔自身）
    as_of: str = ''
    ew_pct: Optional[float] = None       # 同业等权涨跌幅 %
    median_pct: Optional[float] = None   # 同业中位涨跌幅 %
    sigma: Optional[float] = None        # 同业等权日收益波动率（%）
    stock_pct: Optional[float] = None    # 个股当日涨跌幅 %
    rel_pct: Optional[float] = None      # 个股 − 同业中位（pct）
    ok: bool = False
    note: str = ''

    @property
    def denom(self) -> Optional[float]:
        """波动率归一化后的板块分分母（%）。"""
        if self.sigma is None:
            return None
        return max(DENOM_MIN, min(DENOM_MAX, SIGMA_MULT * self.sigma))

    def rel_label(self) -> str:
        """个股相对同业的定性判断（阈值按 A 股板块内部分化常态取 ±3pct）。"""
        if self.rel_pct is None:
            return ''
        if self.rel_pct <= -5:
            return '个股独立走弱（远弱于同业）'
        if self.rel_pct <= -3:
            return '弱于同业'
        if self.rel_pct >= 5:
            return '个股独立走强（远强于同业）'
        if self.rel_pct >= 3:
            return '强于同业'
        return '与同业同步'


def _all_codes() -> List[str]:
    return sorted({c for v in PEER_BASKETS.values() for c in v})


def _load_panel() -> pd.DataFrame:
    """一次性把全部篮子的日涨跌幅读进内存（pivot: 日期 × 代码）。"""
    global _PANEL, _PANEL_KEYS
    keys = len(_all_codes())
    if _PANEL is not None and _PANEL_KEYS == keys:
        return _PANEL

    codes = _all_codes()
    q = "','".join(codes)
    try:
        from db.database import engine
        df = pd.read_sql(
            "select code, trade_date, pct_chg from daily_kline "
            f"where code in ('{q}') and pct_chg is not null", engine)
    except Exception:                    # noqa: BLE001  库不可用 → 该口径整体降级
        df = pd.DataFrame(columns=['code', 'trade_date', 'pct_chg'])

    if df.empty:
        _PANEL = pd.DataFrame()
    else:
        df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
        df['trade_date'] = df['trade_date'].astype(str).str.slice(0, 10)
        _PANEL = df.pivot_table(index='trade_date', columns='code', values='pct_chg')
        _PANEL = _PANEL.sort_index()
    _PANEL_KEYS = keys
    return _PANEL


def basket_for(industry: str, chain: Optional[List[str]] = None,
               themes: Optional[List[dict]] = None) -> Optional[str]:
    """给一只票挑同业篮子。**题材优先于行业**。

    题材名更具体：同样是东财行业「软件开发」，深信服题材里有「网络安全」就该用网安篮子，
    而用友网络题材里没有 → 返回 None，退回板块指数口径。反过来先按行业匹配，
    就会把整个软件板块硬塞进网安篮子。
    """
    for t in themes or []:
        nm = (t.get('name') if isinstance(t, dict) else str(t)) or ''
        if nm in PEER_BASKETS:
            return nm
        if nm in BASKET_ALIASES:
            return BASKET_ALIASES[nm]
    for nm in [industry] + list(chain or []):
        if not nm:
            continue
        if nm in PEER_BASKETS:
            return nm
        if nm in BASKET_ALIASES:
            return BASKET_ALIASES[nm]
    return None


def peer_snapshot(code: str, industry: str = '', chain: Optional[List[str]] = None,
                  themes: Optional[List[dict]] = None,
                  as_of: Optional[str] = None) -> Optional[PeerSnapshot]:
    """个股 vs 同业：等权涨跌、中位涨跌、波动率、相对强弱。

    as_of: 对齐到哪一天（默认取该股最后有数据的交易日）。
    """
    basket = basket_for(industry, chain, themes)
    if not basket:
        return None

    snap = PeerSnapshot(basket=basket)
    panel = _load_panel()
    if panel.empty:
        snap.note = '本地库不可用'
        return snap

    members = [c for c in PEER_BASKETS.get(basket, []) if c in panel.columns]
    refs = [c for c in members if c != code]
    if len(refs) < MIN_REFS:
        snap.note = f'{basket} 篮子有效成分不足 {MIN_REFS} 只'
        return snap

    sub = panel[refs]
    if as_of and as_of in sub.index:
        sub = sub.loc[:as_of]
    if sub.empty:
        snap.note = '无对齐交易日'
        return snap

    # 注意：面板索引是**全代码并集**，最后一行可能只有别的篮子的票有数据
    # （例如网安股更新到 10-29、白酒还停在 10-28，10-29 那行对白酒是全 NaN）。
    # 所以日期要按"本篮子自己的有效成分数"回退找，不能直接取 index[-1]。
    counts = sub.notna().sum(axis=1)
    ok_dates = counts[counts >= MIN_REFS].index
    if len(ok_dates) == 0:
        snap.note = f'近端同业有效成分始终不足 {MIN_REFS} 只'
        return snap
    last = ok_dates[-1]
    sub = sub.loc[:last]
    row = sub.loc[last].dropna()
    if len(row) < MIN_REFS:
        snap.note = f'{last} 同业仅 {len(row)} 只有数据'
        return snap

    # 等权序列：每个交易日要求 ≥60% 成分有数据，否则该日剔除
    need = max(MIN_REFS, int(math.ceil(0.6 * len(refs))))
    ew = sub.mean(axis=1, skipna=True)[sub.notna().sum(axis=1) >= need]
    ew = ew.dropna()
    if len(ew) < SIGMA_MIN_OBS:
        snap.note = f'同业等权序列仅 {len(ew)} 天，不足以算波动率'
        return snap

    snap.refs = refs
    snap.as_of = str(last)[:10]
    # 当日等权/中位都用**同一批有数据的成分**算，保证与 as_of 严格同日
    snap.ew_pct = round(float(row.mean()), 2)
    snap.median_pct = round(float(row.median()), 2)
    snap.sigma = round(float(ew.tail(SIGMA_WINDOW).std()), 2)
    if snap.sigma < SIGMA_FLOOR:
        snap.sigma = SIGMA_FLOOR

    if code in panel.columns and last in panel.index:
        v = panel.loc[last, code]
        if pd.notna(v):
            snap.stock_pct = round(float(v), 2)
            snap.rel_pct = round(snap.stock_pct - snap.median_pct, 2)

    snap.ok = True
    return snap


def sigma_of(snapshot: Optional[PeerSnapshot]) -> float:
    """取波动率；同业不可用时退回全市场板块的池化中位（实测 2.04%），取整 2.0。"""
    if snapshot is not None and snapshot.sigma:
        return float(snapshot.sigma)
    return 2.0


def clear_cache() -> None:
    """测试用：丢弃已加载的行情面板。"""
    global _PANEL, _PANEL_KEYS
    _PANEL = None
    _PANEL_KEYS = 0
