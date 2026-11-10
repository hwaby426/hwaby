"""ML 特征与标签构造（P0-3）。

对外只有两个入口：

    build_all(...)  -> 全市场特征 DataFrame（含标签）
    save(...)       -> 写 parquet / 落库 stock_features

特征全部由 t 日及之前的日线算出；标签由 `features.labels` 单独挂上，
两者在 `build_one` 返回的同一行里并存，直接取 X/y 即可。

设计取舍
--------
- **指标复用** `indicators.mytt_indicators.calc_all_indicators`，不重写 MA/MACD/KDJ。
- **逐票计算**：指标是有状态的（EMA/KDJ），跨票合并会串味，必须按 code groupby。
- **相对强度与市场超额在横截面阶段算**：需要同日全市场中位数，不能在单票里算。
- **ST 状态只能取「当前」**：`daily_kline` 没有 is_st 列，BaoStock 的 isST 字段
  当初没入库，历史 ST 状态无法还原。这是已知局限，`is_st` 只作近似标记，
  样本筛选请用 `days_since_ipo` + 价格/成交额条件，不要依赖历史 ST。
"""

import os
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import BASE_DIR
from db.database import engine
from features.labels import build_labels, attach_excess
from indicators.mytt_indicators import calc_all_indicators
from sqlalchemy import text

FEATURE_DIR = os.path.join(str(BASE_DIR), '.data_cache')
DEFAULT_PARQUET = os.path.join(FEATURE_DIR, 'features.parquet')

# 落库 / 建特征表用的列顺序（与 db.models.StockFeature 对齐）
FEATURE_COLUMNS = [
    'code', 'trade_date',
    # 原始量额（**不进 X**，只用于流动性过滤 / 停牌判定 / 成交额约束）
    'volume', 'amount',
    # 价格
    'ret_1', 'ret_3', 'ret_5', 'ret_10', 'ret_20',
    'ma5_bias', 'ma10_bias', 'ma20_bias', 'gap_open', 'close_pos', 'high_dist_20',
    # 量
    'vol_ratio_5', 'vol_ratio_5_20', 'turnover', 'vol_surge_up', 'vol_surge_dn',
    # 指标
    'rsi_12', 'kdj_k', 'kdj_d', 'kdj_j', 'macd_dif', 'macd_dea', 'macd_hist',
    'boll_pos', 'cci', 'atr_pct',
    # 波动 / 相对
    'volatility_20', 'max_dd_20', 'rel_strength_20', 'vol_obv_slope_5',
    # 上下文
    'days_since_ipo', 'is_st',
    # 标签
    'fwd_ret_1', 'fwd_ret_3', 'fwd_ret_5',
    'fwd_openret_1', 'fwd_openret_3', 'fwd_openret_5', 'fwd_open_gap',
    'fwd_up_1', 'fwd_up_3', 'fwd_up_5', 'fwd_excess_5',
    'fwd_tradable_1', 'fwd_tradable_3', 'fwd_tradable_5', 'label_valid',
]

# 纯特征列（不含标签、不含标识列），供模型直接当 X。
# 注意 fwd_open_gap 虽然叫 fwd_*，但它是「t+1 开盘相对 t 收盘的跳空」，
# 同样使用了未来数据，**不能进 X**，所以这里按前缀排除是对的。
# volume / amount 是绝对量，跨股票不可比（大盘股永远大于小盘股），
# 归一化后的 vol_ratio_5 / turnover 才是特征，原始量额只作过滤用。
X_COLUMNS = [
    c for c in FEATURE_COLUMNS
    if c not in ('code', 'trade_date', 'volume', 'amount')
    and not c.startswith(('fwd_', 'label_'))
]


# ======================================================================
# 1. 数据装载
# ======================================================================
def load_daily(start: Optional[str] = None, end: Optional[str] = None,
               codes: Optional[List[str]] = None) -> pd.DataFrame:
    """读日线，全部转成 float64。

    用 read_sql + 显式 cast，不用 ORM：190 万行走 ORM 会慢一个数量级。
    """
    # ⚠️ 必须剔除指数。`daily_kline` 里有 286 个指数（sz.399001 等），它们的
    # volume / amount / turnover 与个股语义不同（指数的「换手率」不是可交易
    # 份额的周转），却和个股一样能算出全部 32 个特征并拿到 label_valid=1。
    # 2026-11-01 之前这里没过滤，导致特征表混进 93,236 行指数（占 5.5%），
    # 全市场均值基准和分位排序都被污染：按 amount*100/turnover 反推的
    # 流通市值 99 分位变成 19 万亿（真实最大值是茅台 1.7 万亿）。
    from data.universe import stock_where

    where, params = [stock_where()], {}
    if start:
        where.append("trade_date >= :s")
        params['s'] = start
    if end:
        where.append("trade_date <= :e")
        params['e'] = end
    if codes:
        # text() 不会自动展开 IN 的 tuple，手工拼占位符
        ph = ', '.join(f':c{i}' for i in range(len(codes)))
        where.append(f"code IN ({ph})")
        for i, c in enumerate(codes):
            params[f'c{i}'] = c

    sql = "SELECT code, trade_date, open, high, low, close, volume, amount, " \
          "pct_chg, turnover FROM daily_kline"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY code, trade_date"

    df = pd.read_sql(text(sql), engine, params=params)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    for c in ('open', 'high', 'low', 'close', 'volume', 'amount',
              'pct_chg', 'turnover'):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    logger.info(f"装载日线 {len(df):,} 行 / {df['code'].nunique()} 只")
    return df


def load_meta() -> Dict[str, dict]:
    """code(带点) -> {list_date, is_st}。

    注意 `stock_info.code` 是 `sh600000`（无点），`daily_kline.code` 是
    `sh.600000`（有点），按 symbol 拼回带点格式才能对上。
    """
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT symbol, list_date, is_st FROM stock_info "
            "WHERE is_index = 0 AND list_date IS NOT NULL"
        )).fetchall()

    def dotted(sym: str) -> str:
        if sym.startswith(('6', '9')):
            return f"sh.{sym}"
        if sym.startswith(('0', '2', '3')):
            return f"sz.{sym}"
        if sym.startswith(('4', '8')):
            return f"bj.{sym}"
        return sym

    return {
        dotted(sym): {'list_date': ld, 'is_st': int(st or 0)}
        for sym, ld, st in rows
    }


# ======================================================================
# 2. 单票特征
# ======================================================================
def build_one(code: str, g: pd.DataFrame, trading_days: pd.DatetimeIndex,
              meta: Dict[str, dict],
              horizons: Iterable[int] = (1, 3, 5)) -> pd.DataFrame:
    """构造单只票的特征 + 标签。g 必须已按 trade_date 升序。"""
    if len(g) < 25:            # 指标至少要 20 日窗口，太短的票没有信息量
        return pd.DataFrame()

    d = g.sort_values('trade_date').reset_index(drop=True).copy()
    d = calc_all_indicators(d)

    close = d['close'].astype(float)
    high = d['high'].astype(float)
    low = d['low'].astype(float)
    open_ = d['open'].astype(float)
    vol = d['volume'].astype(float)
    pre_close = close.shift(1)

    # ── 价格 ──
    d['ret_1'] = close / pre_close - 1
    d['ret_3'] = close / close.shift(3) - 1
    d['ret_5'] = close / close.shift(5) - 1
    d['ret_10'] = close / close.shift(10) - 1
    d['ret_20'] = close / close.shift(20) - 1

    for n in (5, 10, 20):
        d[f'ma{n}_bias'] = close / d[f'ma{n}'].replace(0, np.nan) - 1

    d['gap_open'] = open_ / pre_close - 1
    rng = (high - low)
    d['close_pos'] = np.where(rng > 0, (close - low) / rng, 0.5)
    d['high_dist_20'] = close / close.rolling(20, min_periods=10).max() - 1

    # ── 成交量 ──
    d['vol_ratio_5'] = vol / d['vol_ma5'].replace(0, np.nan)
    d['vol_ratio_5_20'] = d['vol_ma5'] / d['vol_ma20'].replace(0, np.nan)
    d['vol_surge_up'] = ((d['vol_ratio_5'] > 1.5) & (d['ret_1'] > 0.02)).astype(int)
    d['vol_surge_dn'] = ((d['vol_ratio_5'] > 1.5) & (d['ret_1'] < -0.02)).astype(int)

    # ── 指标改名，统一前缀 ──
    d['rsi_12'] = d['rsi12']
    d['kdj_k'], d['kdj_d'], d['kdj_j'] = d['k'], d['d'], d['j']
    d['macd_dif'], d['macd_dea'], d['macd_hist'] = d['dif'], d['dea'], d['macd']
    band = (d['upper'] - d['lower'])
    d['boll_pos'] = np.where(band > 0, (close - d['lower']) / band, 0.5)
    d['atr_pct'] = d['atr'] / close.replace(0, np.nan)

    # ── 波动 / OBV ──
    d['volatility_20'] = d['ret_1'].rolling(20, min_periods=10).std()
    # 近似「过去 20 天内相对前高的最深回撤」；严格窗口内最大回撤要逐窗 apply，
    # 190 万行下慢到不可接受，这个近似保留了「近期是否在深水区」的信息
    roll_max = close.rolling(20, min_periods=10).max()
    d['max_dd_20'] = (close / roll_max - 1).rolling(20, min_periods=10).min()

    obv = (np.sign(d['ret_1'].fillna(0)) * vol).cumsum()
    denom = vol.rolling(5, min_periods=3).sum().replace(0, np.nan)
    d['vol_obv_slope_5'] = obv.diff(5) / denom

    # ── 上下文 ──
    m = meta.get(code, {})
    ipo = m.get('list_date')
    d['days_since_ipo'] = (
        (d['trade_date'] - pd.Timestamp(ipo)).dt.days if ipo is not None else np.nan
    )
    d['is_st'] = int(m.get('is_st', 0))

    # ── 标签 ──
    st_col = pd.Series(d['is_st'].astype(bool).values, index=d.index)
    d = build_labels(d, trading_days, horizons=horizons, is_st=st_col)

    keep = [c for c in FEATURE_COLUMNS if c in d.columns]
    return d[keep]


# ======================================================================
# 3. 全市场
# ======================================================================
def build_all(start: Optional[str] = None, end: Optional[str] = None,
              codes: Optional[List[str]] = None,
              min_bars: int = 25, warmup_days: int = 180) -> pd.DataFrame:
    """构造全市场特征表（含标签）。

    Args:
        start/end: **输出**区间。start 之前的 `warmup_days` 会被读进来算指标预热，
                   但不输出——否则 start 之后前 20~60 天的 MA60/volatility 全是 NaN。
                   算标签需要 t+n 的数据，end 附近尾部 n 天的 label_valid 会是 0，
                   这是正确行为。
        codes: 限定股票，None=全市场。
        min_bars: 少于这么多根 K 线的票跳过。
        warmup_days: 为指标预热往前多读的自然日数。
    """
    load_start = None
    if start:
        load_start = (pd.Timestamp(start) - pd.Timedelta(days=warmup_days)).strftime('%Y-%m-%d')

    daily = load_daily(load_start, end, codes)
    if daily.empty:
        logger.warning("没有日线数据")
        return pd.DataFrame()

    trading_days = pd.DatetimeIndex(sorted(daily['trade_date'].unique()))
    meta = load_meta()
    logger.info(f"交易日历 {len(trading_days)} 天，基础资料 {len(meta)} 只")

    parts: List[pd.DataFrame] = []
    skipped = 0
    for i, (code, g) in enumerate(daily.groupby('code', sort=False), 1):
        if len(g) < min_bars:
            skipped += 1
            continue
        try:
            f = build_one(code, g, trading_days, meta)
        except Exception as e:
            logger.warning(f"{code} 特征构造失败: {e}")
            skipped += 1
            continue
        if not f.empty:
            parts.append(f)
        if i % 1000 == 0:
            logger.info(f"  已处理 {i} 只")

    if not parts:
        return pd.DataFrame()

    feat = pd.concat(parts, ignore_index=True)
    logger.info(f"特征构造完成 {len(feat):,} 行 / {feat['code'].nunique()} 只 "
                f"(跳过 {skipped} 只)")

    # 先裁到目标区间，再做横截面（省掉预热期的分组开销）
    if start:
        feat = feat[feat['trade_date'] >= pd.Timestamp(start)].reset_index(drop=True)

    feat = add_cross_section(feat)
    return feat


def add_cross_section(feat: pd.DataFrame) -> pd.DataFrame:
    """补同日横截面特征：rel_strength_20 与 fwd_excess_5。

    必须按 trade_date 分组。用全样本均值会让「当天市场整体涨跌」混进特征，
    模型学到的是 beta，回测时看起来有效、实盘选股没有 alpha。
    """
    if feat.empty:
        return feat
    by_day = feat.groupby('trade_date')
    med20 = by_day['ret_20'].transform('median')
    feat['rel_strength_20'] = feat['ret_20'] - med20
    feat = attach_excess(feat, 'fwd_ret_5')
    return feat


# ======================================================================
# 4. 落盘 / 落库
# ======================================================================
def save_parquet(feat: pd.DataFrame, path: str = DEFAULT_PARQUET) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = [c for c in FEATURE_COLUMNS if c in feat.columns]
    feat[cols].to_parquet(path, index=False)
    logger.info(f"已写 {path} ({len(feat):,} 行)")
    return path


def create_table():
    """建 stock_features 表（若不存在）。"""
    from db.models import StockFeature
    StockFeature.__table__.create(bind=engine, checkfirst=True)
    logger.info("stock_features 表就绪")


def save_to_db(feat: pd.DataFrame, chunk: int = 5000) -> int:
    """写 stock_features，按 (code, trade_date) 幂等覆盖。"""
    if feat.empty:
        return 0
    create_table()

    cols = [c for c in FEATURE_COLUMNS if c in feat.columns]
    data = feat[cols].copy()
    data['trade_date'] = pd.to_datetime(data['trade_date']).dt.date

    # NaN / NaT -> None，否则 pymysql 会把 nan 当字符串写进去
    data = data.astype(object).where(pd.notna(data), None)
    records = data.to_dict('records')

    upd = ', '.join(f"{c}=VALUES({c})" for c in cols if c not in ('code', 'trade_date'))
    sql = text(
        f"INSERT INTO stock_features ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON DUPLICATE KEY UPDATE {upd}"
    )

    total = 0
    with engine.begin() as conn:
        for i in range(0, len(records), chunk):
            batch = records[i:i + chunk]
            conn.execute(sql, batch)
            total += len(batch)
            if (i // chunk) % 20 == 0:
                logger.info(f"  写库 {total:,}/{len(records):,}")
    logger.info(f"stock_features 写入 {total:,} 行")
    return total


def load_features(parquet: str = DEFAULT_PARQUET) -> pd.DataFrame:
    if not os.path.exists(parquet):
        raise FileNotFoundError(f"{parquet} 不存在，先跑 build_all + save_parquet")
    return pd.read_parquet(parquet)


def drop_index_rows(df: pd.DataFrame) -> pd.DataFrame:
    """剔除指数行。**所有读取特征表的下游都必须过这一道。**

    为什么单独抽出来：`load_daily` 已在源头过滤，但 2026-11-01 之前落盘的
    parquet / 数据库里混着 286 个指数（93,236 行，占 5.5%）。它们能算出全部
    32 个特征、也能拿到 `label_valid=1`，却不可交易；留在表里会把「全市场
    等权均值」这个超额基准算歪，还会污染分位排序。

    典型症状：用 `amount*100/turnover` 反推流通市值，99 分位变成 **19 万亿**
    （真实最大值是茅台 1.7 万亿）——因为深证成指的 amount 是全市场成交额之和。
    """
    from data.universe import is_index_code

    if 'code' not in df.columns:
        return df
    mask = ~df['code'].astype(str).map(is_index_code)
    if mask.all():
        return df
    n = int((~mask).sum())
    logger.warning(f'剔除特征表内的指数 {n:,} 行（{n / max(len(df), 1) * 100:.2f}%）')
    return df[mask].reset_index(drop=True)
