"""
数据口径审计与修正
==================

本模块只做「解释与修正现有数据」，**不改动数据源**（data_fetcher / DB 写入逻辑）。

背景（基于本机数据库实测，2026-10）：

1. **库里只有 adjustflag=2（前复权）**，没有后复权/不复权。
   实测发现 `pct_chg` 与 `close` 在除权日会**不一致**，例如：
   `sh.688110` 2026-08-14：前复权 close 由 112.500 → 78.131（隐含 -30.55%），
   而 `pct_chg = +4.08`。这说明——

   * `pct_chg` = 交易所公布的**真实涨跌幅**（已按除权参考价计算，当日不受除权跳空影响）
   * `open/high/low/close` = **前复权价**（除权日会出现跳空）

   两者混用会造成逻辑不自洽（例如用前复权 close 算收益、却用 pct_chg 判定涨跌停）。

2. **前复权会被未来重写**：每发生一次分红送转，历史上所有前复权价都会变。
   直接后果是**同一份回测今天跑和半年后跑结果不同**，不可复现。
   `rebuild_total_return()` 用 pct_chg 链式复利重建一条**与未来分红无关**的序列，
   只要历史 pct_chg 不变，结果就永远一致。

3. **幸存者偏差**：`stock_info` 里 5264 只票 **status 全为 1、无一只退市**，
   且 `list_date` 100% 为 NULL；82.4% 的票首批 K 线都始于 2025-06-27（建库快照日）。
   即：股票池是「今天的在市清单」回溯到历史，**退市股从未入库**。
   任何跨越 2023-08 ~ 2025-06 的回测，实际只跑在少数几只股票上，且全部是「活到今天的赢家」。
   `coverage_report()` 把这件事量化并显式告警。

4. **次新股过滤失效**：`stock_info.list_date` 全为空，无法判断上市天数，
   只能用「该股在库内的首根 K 线」做代理，见 `bars_before()`。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

__all__ = [
    'detect_adjust_breaks',
    'rebuild_total_return',
    'audit_code',
    'audit_universe',
    'bars_before',
    'coverage_report',
    'point_in_time_universe',
    'print_coverage_report',
    'PRICE_MODES',
]

PRICE_MODES = ('adjusted', 'total_return')

# 默认口径。为什么默认不是库里的前复权，见模块文档与 market_break_summary()：
# 实测 5264 只票里 297 只（5.6%）的前复权 close 链存在从未发生过的跳空，
# 最坏 -34.8%（sz.002961 2026-08-14：close 由 19.310 跳到 12.591，
# 而当日 amount/volume 反推的成交均价仍在 19 元附近、pct_chg 为 -2.12%）。
# 用这条 close 算指标和收益会把"数据刷新导致的因子换挡"当成真实暴跌。
DEFAULT_PRICE_MODE = 'total_return'

_BREAK_SUMMARY_CACHE: Dict[str, object] = {}


def market_break_summary(cache=None, tol_pct: float = 1.0) -> Dict[str, object]:
    """全市场扫描前复权断点，向量化实现（结果按缓存文件名 memoize）。

    判定方法：前复权 close 的链式涨跌幅 与 交易所公布的 pct_chg 相差超过 tol
    → 该日 close 序列发生了因子换挡，用它算指标/收益会出错。
    """
    if cache is None:
        from data.cache import get_cache
        cache = get_cache()
        try:
            cache.build(force=False)
        except Exception:
            pass

    key = f'{getattr(cache, "parquet_path", id(cache))}|{tol_pct}'
    if key in _BREAK_SUMMARY_CACHE:
        return dict(_BREAK_SUMMARY_CACHE[key])

    try:
        df = cache._load_local()
    except Exception:
        return {'codes': 0, 'codes_with_breaks': 0, 'break_days': 0}
    if df is None or df.empty or 'pct_chg' not in df.columns:
        return {'codes': 0, 'codes_with_breaks': 0, 'break_days': 0}

    d = df[['code', 'date', 'close', 'pct_chg']].copy()
    d['close'] = pd.to_numeric(d['close'], errors='coerce')
    d['pct_chg'] = pd.to_numeric(d['pct_chg'], errors='coerce')
    d = d.sort_values(['code', 'date'])
    g = d.groupby('code', sort=False)
    implied = g['close'].pct_change() * 100.0
    diff = (implied - d['pct_chg']).abs()
    # 每只票的第一根K线没有前收盘，implied 为 NaN，NaN > tol 为 False，天然被排除
    mask = diff > tol_pct
    hit = d[mask.fillna(False)]
    worst = float(implied[mask.fillna(False)].abs().max()) if len(hit) else 0.0

    res = {
        'codes': int(d['code'].nunique()),
        'codes_with_breaks': int(hit['code'].nunique()),
        'break_days': int(len(hit)),
        'worst_implied_pct': round(worst, 2),
        'top_dates': hit['date'].astype(str).value_counts().head(3).to_dict() if len(hit) else {},
    }
    _BREAK_SUMMARY_CACHE[key] = res
    return dict(res)


# ----------------------------------------------------------------------
# 1. 复权断点检测
# ----------------------------------------------------------------------
def detect_adjust_breaks(
    df: pd.DataFrame,
    tol_pct: float = 1.0,
    close_col: str = 'close',
    pct_col: str = 'pct_chg',
    date_col: str = 'date',
) -> pd.DataFrame:
    """找出「前复权 close 的链式涨跌幅」与「pct_chg」不一致的交易日。

    这些日子几乎都是**除权除息/送转/配股**日（前复权因子换挡），
    也可能混入数据源异常。返回空 DataFrame 表示序列自洽。

    注意：容差不能设太小，Decimal(6,2) 只存两位小数，
    低价股的正常舍入误差就能到 0.05pp 量级。
    """
    if df.empty or close_col not in df.columns or pct_col not in df.columns:
        return pd.DataFrame()

    close = pd.to_numeric(df[close_col], errors='coerce').astype(float)
    pct = pd.to_numeric(df[pct_col], errors='coerce').astype(float)
    implied = close.pct_change() * 100.0
    diff = (implied - pct).abs()

    mask = diff > tol_pct
    # 第一根 K 线没有前收盘，implied 必然为 NaN，跳过
    if len(mask):
        mask.iloc[0] = False

    out = pd.DataFrame({
        'date': df[date_col].astype(str) if date_col in df.columns else df.index.astype(str),
        'close': close,
        'pct_chg': pct,
        'implied_pct': implied.round(4),
        'diff': diff.round(4),
    })[mask]
    return out.reset_index(drop=True)


# ----------------------------------------------------------------------
# 2. 用 pct_chg 重建「总收益序列」（等价于后复权，且不会被未来分红重写）
# ----------------------------------------------------------------------
def rebuild_total_return(
    df: pd.DataFrame,
    anchor: str = 'last',
    cols: Sequence[str] = ('open', 'high', 'low', 'close'),
    close_col: str = 'close',
    pct_col: str = 'pct_chg',
) -> pd.DataFrame:
    """把前复权 OHLC 重建为「链式复利序列」。

    做法：以 pct_chg 逐日复利得到一条指数，再整体系数缩放，
    使锚点处的价格等于库里的前复权价（前复权在最新交易日 = 真实成交价）。

    * `anchor='last'`（默认）：末值 = 末日前复权收盘价 → 即**后复权**，
      历史价格高于当时真实成交价，但**日间收益 = 真实总收益**，
      且序列**不随未来分红送转而改变**，回测可复现。
    * `anchor='first'`：首值 = 首日前复权收盘价。

    `open/high/low` 按当日与 close 的比例同步缩放，保留日内结构。
    """
    out = df.copy()
    if out.empty or pct_col not in out.columns or close_col not in out.columns:
        return out

    pct = pd.to_numeric(out[pct_col], errors='coerce').fillna(0.0).astype(float) / 100.0
    pct = pct.clip(lower=-0.99)           # 防御脏数据导致的负价格
    idx = (1.0 + pct).cumprod()

    close = pd.to_numeric(out[close_col], errors='coerce').astype(float)
    if anchor == 'first':
        k = float(close.iloc[0]) / float(idx.iloc[0])
    else:
        k = float(close.iloc[-1]) / float(idx.iloc[-1])
    if not np.isfinite(k) or k <= 0:
        return out

    new_close = idx * k
    # 逐日缩放系数：保证 open/high/low 与 close 的相对关系不变
    scale = np.where(close.values > 0, new_close.values / np.where(close.values > 0, close.values, 1.0), 1.0)

    out[close_col] = new_close.values
    for c in cols:
        if c == close_col or c not in out.columns:
            continue
        v = pd.to_numeric(out[c], errors='coerce').astype(float).values
        out[c] = v * scale

    out.attrs['price_mode'] = 'total_return'
    out.attrs['price_anchor'] = anchor
    return out


def _series_return(close: pd.Series) -> float:
    c = pd.to_numeric(close, errors='coerce').astype(float).dropna()
    if len(c) < 2 or c.iloc[0] <= 0:
        return 0.0
    return float(c.iloc[-1] / c.iloc[0] - 1.0)


def audit_code(df: pd.DataFrame, date_col: str = 'date') -> Dict[str, object]:
    """对单只股票做口径体检，返回可打印的字典。"""
    if df.empty:
        return {'bars': 0}
    breaks = detect_adjust_breaks(df, date_col=date_col)
    close = pd.to_numeric(df.get('close'), errors='coerce').astype(float)
    rebuilt = rebuild_total_return(df)

    start = str(df[date_col].iloc[0]) if date_col in df.columns else ''
    end = str(df[date_col].iloc[-1]) if date_col in df.columns else ''
    return {
        'bars': int(len(df)),
        'start': start,
        'end': end,
        'adjust_breaks': int(len(breaks)),
        'break_dates': breaks['date'].head(10).tolist() if len(breaks) else [],
        'ret_adjusted_pct': round(_series_return(close) * 100, 2),
        'ret_total_return_pct': round(_series_return(rebuilt['close']) * 100, 2),
        'last_close_db': round(float(close.iloc[-1]), 3) if len(close) else None,
        'first_close_db': round(float(close.iloc[0]), 3) if len(close) else None,
        'first_close_rebuilt': round(float(rebuilt['close'].iloc[0]), 3) if len(rebuilt) else None,
    }


def audit_universe(
    data: Dict[str, pd.DataFrame],
    date_col: str = 'date',
    sample: int = 5,
) -> Dict[str, object]:
    """对一批股票做口径体检，只统计 + 列出样本，避免全量慢。"""
    total_breaks = 0
    codes_with_breaks: List[str] = []
    max_diff_ret = 0.0
    examples: List[Dict[str, object]] = []

    for code, df in data.items():
        if df is None or df.empty:
            continue
        b = detect_adjust_breaks(df, date_col=date_col)
        if len(b):
            total_breaks += len(b)
            codes_with_breaks.append(code)
        a = audit_code(df, date_col=date_col)
        d = abs(float(a.get('ret_adjusted_pct', 0)) - float(a.get('ret_total_return_pct', 0)))
        max_diff_ret = max(max_diff_ret, d)
        if len(examples) < sample and len(b):
            examples.append({'code': code, **{k: a[k] for k in ('bars', 'adjust_breaks', 'break_dates', 'ret_adjusted_pct', 'ret_total_return_pct')}})

    return {
        'codes': int(len(data)),
        'codes_with_breaks': int(len(codes_with_breaks)),
        'total_break_days': int(total_breaks),
        'max_return_gap_pp': round(max_diff_ret, 2),
        'examples': examples,
    }


# ----------------------------------------------------------------------
# 3. 次新股：上市天数代理（stock_info.list_date 全为空，只能用库内首根K线）
# ----------------------------------------------------------------------
def bars_before(cache, code: str, start_date: str) -> int:
    """该股在 `start_date` 之前共有多少根日K（作为「已上市交易日」的代理）。

    数据源不动的前提下，`stock_info.list_date` 100% 为 NULL，
    只能用「库内首根K线」近似上市日。若某股是建库快照之后才上市的，
    这个值偏小 —— 属于**保守**方向（会多过滤，不会放过次新股）。
    """
    try:
        full = cache.get(code)
    except Exception:
        return 0
    if full is None or full.empty:
        return 0
    dcol = 'date' if 'date' in full.columns else full.columns[0]
    d = pd.to_datetime(full[dcol], errors='coerce')
    return int((d < pd.Timestamp(start_date)).sum())


# ----------------------------------------------------------------------
# 4. 幸存者偏差 / 股票池覆盖度
# ----------------------------------------------------------------------
def coverage_report(cache=None) -> Dict[str, object]:
    """量化股票池的时点覆盖情况，暴露幸存者偏差。

    返回字段中 `survivorship_warnings` 是需要写进回测报告的免责说明。
    """
    if cache is None:
        from data.cache import get_cache
        cache = get_cache()
        try:
            cache.build(force=False)
        except Exception:
            pass

    idx = cache._index_by_code()
    if not idx:
        return {'codes': 0, 'survivorship_warnings': ['缓存为空，无法评估覆盖度']}

    firsts: List[pd.Timestamp] = []
    lasts: List[pd.Timestamp] = []
    for df in idx.values():
        dcol = 'date' if 'date' in df.columns else df.columns[0]
        d = pd.to_datetime(df[dcol], errors='coerce')
        if len(d):
            firsts.append(d.min())
            lasts.append(d.max())

    fs = pd.Series(firsts)
    ls = pd.Series(lasts)
    global_max = ls.max()

    # 有效样本量随时间的曲线：某日有多少只票在库内有数据
    grid = pd.date_range(fs.min(), global_max, freq='ME')
    curve = []
    for g in grid:
        n = int((fs <= g).sum())
        curve.append((str(g.date())[:7], n))

    # 末日之前就断更的票（疑似退市 / 长期停牌）
    stale = int((ls < global_max).sum())
    # 首批 K 线高度集中在少数几天 = 建库快照，而非真实上市日
    top_first = fs.dt.strftime('%Y-%m-%d').value_counts().head(5)
    concentration = float(top_first.iloc[0] / len(fs)) if len(fs) else 0.0

    warnings: List[str] = []
    if stale == 0:
        warnings.append(
            f'库中 {len(fs)} 只票最后交易日全部为 {global_max.date()}，'
            f'**没有一只退市/长期停牌股** —— 股票池是「今天仍在市」的清单回溯历史，存在幸存者偏差'
        )
    else:
        warnings.append(f'{stale} 只票在 {global_max.date()} 之前断更（疑似退市/停牌），占比 {stale / len(fs) * 100:.2f}%')

    if concentration > 0.5:
        top_day = str(top_first.index[0])
        warnings.append(
            f'{concentration * 100:.1f}% 的票首批K线同为 {top_day}（建库快照日），'
            f'说明该日之前的历史只有 {int(len(fs) - top_first.iloc[0])} 只票可用，'
            f'跨越该日之前的回测样本严重不足且非随机'
        )

    return {
        'codes': int(len(fs)),
        'global_first': str(fs.min().date()),
        'global_last': str(global_max.date()),
        'first_date_top': {str(k): int(v) for k, v in top_first.items()},
        'first_date_concentration': round(concentration, 4),
        'stale_codes': stale,
        'coverage_curve': curve,
        'survivorship_warnings': warnings,
    }


def point_in_time_universe(
    cache,
    date: str,
    min_history_days: int = 0,
) -> List[str]:
    """返回在 `date` 这一时点上真正可选的交易标的（在库内有数据且历史足够长）。"""
    ts = pd.Timestamp(date)
    idx = cache._index_by_code()
    codes: List[str] = []
    for code, df in idx.items():
        dcol = 'date' if 'date' in df.columns else df.columns[0]
        d = pd.to_datetime(df[dcol], errors='coerce')
        n_before = int((d < ts).sum())
        has_today = bool(((d <= ts) & (d >= ts)).any()) or n_before > 0
        if has_today and n_before >= min_history_days:
            codes.append(code)
    return sorted(codes)


def print_coverage_report(rep: Dict[str, object]) -> None:
    """把覆盖度报告打印成人话。"""
    if not rep or rep.get('codes') == 0:
        print('  （无覆盖度数据）')
        return
    print(f"  在库股票数        : {rep['codes']}")
    print(f"  数据范围          : {rep['global_first']} ~ {rep['global_last']}")
    print(f"  首日集中度        : {rep['first_date_concentration'] * 100:.1f}%  "
          f"(top: {list(rep['first_date_top'].items())[:3]})")
    print(f"  提前断更(疑似退市): {rep['stale_codes']}")
    curve = rep.get('coverage_curve') or []
    if curve:
        print('  有效样本量曲线（月末）:')
        for d, n in curve[:: max(1, len(curve) // 8)]:
            print(f'    {d}: {n}')
    print('  ⚠ 口径告警:')
    for w in rep.get('survivorship_warnings', []):
        print(f'    - {w}')
