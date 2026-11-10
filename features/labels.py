"""1/3/5 日前瞻标签构造。

这一层是整个 ML 链路里最容易做错的地方，三个坑都在这里处理：

1. **停牌错位**。`daily_kline` 在停牌日没有行。若直接 `close.shift(-1)`，
   拿到的是「下一个有数据的交易日」而不是「下一个交易日」。中间停牌 3 天时
   `fwd_ret_1` 实际变成了 4 天后收益，标签与特征的时间跨度对不上。
   做法：先 reindex 到全市场交易日历（停牌处理成 NaN），再 shift，
   停牌日的未来价自然是 NaN → 标签失效，而不是错位。

2. **尾样本**。最后 n 天没有 t+n 的价格，标签必须是 NaN + `label_valid=0`，
   **不能填 0 当成「没涨」**。填 0 会让模型学到「临近今天都别买」。

3. **涨跌停不可成交**。A 股各板块幅度不同（主板 10 / 创业板·科创板 20 /
   北交所 30 / ST 5），用统一 ±9.8% 判涨停会把创业板正常波动误判成涨停。
   `fwd_tradable_n` 标记 t+n 日能否按收盘价成交，供回测层和样本筛选使用。
"""

from typing import Iterable, Optional

import numpy as np
import pandas as pd

# 涨跌停判定的容差（价格按 0.01 元取整，实际涨幅常是 9.97 / 19.98 这类值）
_LIMIT_TOL = 0.95


def limit_pct(code: str, is_st: bool = False) -> float:
    """按板块返回涨跌幅限制（百分数，如 10.0）。

    用统一阈值判涨停是常见错误：创业板/科创板 ±20%，北交所 ±30%，
    ST ±5%，主板 ±10%。同一根 +15% 的 K 线在主板是异常、在创业板是正常波动。
    """
    market, _, symbol = code.partition('.')
    if not symbol:            # 传入的是纯数字
        symbol, market = code, ''

    if symbol.startswith(('688', '689')):          # 科创板
        return 20.0
    if symbol.startswith(('300', '301')):          # 创业板
        return 20.0
    if market == 'bj' or symbol.startswith(('43', '83', '87', '88', '92')):
        return 30.0
    if is_st:
        return 5.0
    return 10.0


def _fwd_on_calendar(values: pd.Series, trading_days: pd.DatetimeIndex,
                     n: int) -> pd.Series:
    """把某列按全市场交易日历后移 n 个交易日，再对齐回原始索引。

    停牌日（日历上有、该票没数据）在 reindex 后是 NaN，shift 会把 NaN 带下来，
    因此「t+n 那天停牌」自然得到 NaN 而不是错位的价格。
    """
    cal = values.reindex(trading_days)
    return cal.shift(-n).reindex(values.index)


def build_labels(df: pd.DataFrame, trading_days: pd.DatetimeIndex,
                 horizons: Iterable[int] = (1, 3, 5),
                 is_st: Optional[pd.Series] = None) -> pd.DataFrame:
    """给单只票的日线加上 fwd_* 标签列。

    Args:
        df: 单只票的日线，必须含 trade_date / close / high / low / open /
            pct_chg，已按 trade_date 升序。
        trading_days: 全市场交易日历（pd.DatetimeIndex，升序）。
        horizons: 预测周期，默认 1/3/5。
        is_st: 与 df 等长的 ST 标记，用于定涨跌停幅度；None 视作非 ST。

    Returns:
        原 df 的副本，追加：
            fwd_ret_{n}        未来 n 个交易日收益率
            fwd_up_{n}         1 涨 / 0 不涨 / <NA> 无效
            fwd_tradable_{n}   t+n 是否可按收盘价成交（非一字板/非停牌）
            fwd_excess_{n}     相对全市场中位数的超额（在 assemble 阶段填）
            label_valid        所有 n 的标签都齐全且 t+1 可买
    """
    if df.empty:
        return df

    horizons = tuple(horizons)   # 后面要用 min()，生成器只能遍历一次

    out = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out['trade_date']))
    # 用索引赋值而不是 set_index：trade_date 要保留为普通列，
    # set_index 会把它从列里挪走、后面 reset_index 又会撞名
    out.index = idx
    out.index.name = 'trade_date'

    # ── 涨跌停幅度（逐行，因为 ST 状态会变） ──
    if is_st is None:
        st_flag = pd.Series(False, index=out.index)
    else:
        st_flag = pd.Series(np.asarray(is_st, dtype=bool), index=out.index)

    close = pd.to_numeric(out['close'], errors='coerce').astype(float)
    high = pd.to_numeric(out['high'], errors='coerce').astype(float)
    low = pd.to_numeric(out['low'], errors='coerce').astype(float)
    open_ = pd.to_numeric(out['open'], errors='coerce').astype(float)
    pct = pd.to_numeric(out['pct_chg'], errors='coerce').astype(float)

    # 每行的涨停阈值：板块 + ST
    code = str(out['code'].iloc[0]) if 'code' in out.columns else ''
    lim_series = np.array([limit_pct(code, bool(s)) for s in st_flag])

    # 一字板：最高=最低（全天一个价）且涨幅/跌幅触及限制
    one_price = (high - low).abs() < 1e-6
    is_up_limit = one_price & (pct >= lim_series * _LIMIT_TOL)
    is_dn_limit = one_price & (pct <= -lim_series * _LIMIT_TOL)

    # 实际建仓价：t+1 开盘。方案 10.4 要求「不能假设所有信号都能以收盘价成交」，
    # 而 t 日收盘时信号才成立，最早只能 t+1 开盘下单，所以这个口径才是可执行的。
    entry_open = _fwd_on_calendar(open_, trading_days, 1)
    # 次日跳空 = 按开盘买入相对 t 日收盘的缺口，正数意味着信号后已经涨掉一部分
    out['fwd_open_gap'] = entry_open / close - 1.0

    for n in horizons:
        fwd_close = _fwd_on_calendar(close, trading_days, n)
        ret = fwd_close / close - 1.0

        # 未来价缺失（停牌/尾样本/退市）→ NaN，不能当 0
        out[f'fwd_ret_{n}'] = ret

        # 同一 horizon 的「可执行口径」：t+1 开盘买入，t+n 收盘卖出。
        # 与 fwd_ret_{n} 的差就是「信号后追高的成本」，选股时不能用收盘价自欺。
        out[f'fwd_openret_{n}'] = fwd_close / entry_open - 1.0

        up = pd.Series(pd.NA, index=out.index, dtype='Int64')
        valid = ret.notna()
        up[valid] = (ret[valid] > 0).astype(int)
        out[f'fwd_up_{n}'] = up

        # 可成交性分买卖两个视角，不能混成一个 flag：
        #   t+1 是**买入日** → 一字涨停买不进（一字跌停反而能买到，只是接刀）
        #   t+n 是**卖出日** → 一字跌停卖不掉（一字涨停反而卖得掉）
        # 未来价缺失（尾样本/停牌/退市）时留空，不记 0——0 的含义是「确定买不到」，
        # 与「不知道」是两件事。
        fwd_up_lim = _fwd_on_calendar(is_up_limit.astype(float), trading_days, n)
        fwd_dn_lim = _fwd_on_calendar(is_dn_limit.astype(float), trading_days, n)
        has_price = fwd_close.notna()

        if n == min(horizons):
            blocked = fwd_up_lim.fillna(0) >= 0.5      # 买入视角
        else:
            blocked = fwd_dn_lim.fillna(0) >= 0.5      # 卖出视角

        tradable = pd.Series(pd.NA, index=out.index, dtype='Int64')
        tradable[has_price] = (~blocked[has_price]).astype(int)
        out[f'fwd_tradable_{n}'] = tradable

    # 标签有效性：1/3/5 日收益都算得出，t+1 确实买得进，且**t 日和 t+1 都有成交**
    #
    # 最后一条是必须的，漏了会造出假超额。停牌在 daily_kline 里是有行的，
    # 只是 volume=0、价格是停牌前的旧值（ffill）。于是：
    #   · t 日停牌 → 当天根本选不出这只票，而缩量因子（vol_ratio_5_20）恰好
    #     会把「成交量为 0」当成「最缩量」排在最前面；
    #   · t+1 停牌 → 开盘价是假的（不是真能成交的价），fwd_openret 却在拿它
    #     跟复牌后的价格算收益，得到的是只存在于回测里的「复牌补涨」。
    # 实测 vol_ratio_5_20 的最低 20 只里有 16% 是停牌股，它们 5 日平均 +4.56%，
    # 剔除后该因子的日均超额从 +1.23% 掉到 +0.54%——一半的「超额」是假的。
    if 'volume' in out.columns:
        vol_s = pd.to_numeric(out['volume'], errors='coerce').fillna(0.0)
    else:                       # 兼容不传 volume 的老调用方
        vol_s = pd.Series(1.0, index=out.index)
    vol_next = _fwd_on_calendar(vol_s, trading_days, 1)
    has_vol = (vol_s > 0) & (vol_next > 0).fillna(False)

    req_ret = [f'fwd_ret_{n}' for n in horizons]
    ok = (out[req_ret].notna().all(axis=1)
          & (out['fwd_tradable_1'] == 1)
          & has_vol)
    out['label_valid'] = ok.astype(int)

    return out.reset_index(drop=True)


def attach_excess(feat: pd.DataFrame, col: str = 'fwd_ret_5') -> pd.DataFrame:
    """给标签补同日全市场中位数超额。

    必须按 trade_date 分组取中位数，不能用全样本均值——否则每天的
    「市场整体涨跌」会混进标签，模型学到的是市场 beta 而不是选股 alpha。
    """
    if feat.empty or col not in feat.columns:
        return feat
    med = feat.groupby('trade_date')[col].transform('median')
    out_col = 'fwd_excess_5' if col == 'fwd_ret_5' else f'{col}_excess'
    feat[out_col] = feat[col] - med
    return feat
