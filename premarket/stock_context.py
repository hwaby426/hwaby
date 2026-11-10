"""个股盘前状态 —— 昨天收盘后，这只票站在什么位置上。

盘前要回答的不只是"有没有消息"，还有：
* 昨天是涨停还是跌停？（涨停次日通常有溢价但也更容易炸板）
* 现在是站在均线上方还是下方？（决定支撑/压力在哪）
* 量能是放大还是萎缩？（放量突破 vs 缩量回调，含义完全相反）
* 主力资金昨天是进还是出？
* 距离前高/前低还有多少空间？（决定盈亏比）

技术位在盘前的权重低于消息面——它不产生新信息，只是告诉你"如果消息落地，
价格大概会走到哪"。所以本模块只输出**事实和关键位**，不下"该买该卖"的结论，
结论交给 `analyzer` 综合。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from premarket.global_market import _http_get

_EM_REFERER = 'https://data.eastmoney.com/'


@dataclass
class StockContext:
    code: str
    name: str = ''
    ok: bool = False
    error: str = ''

    last_date: str = ''
    data_lag_days: int = -1    # 数据滞后多少个自然日；>=2 就该提示先跑 update-daily
    close: float = 0.0
    pct_chg: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    amount: float = 0.0        # 元

    ma5: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    bias20: float = 0.0        # 相对 MA20 乖离 %

    vol_ma5: float = 0.0
    volume_ratio: float = 0.0  # 昨日量 / 前5日均量

    atr14: float = 0.0
    atr_pct: float = 0.0       # ATR / close，衡量日常波动幅度

    range_low_60: float = 0.0
    range_high_60: float = 0.0
    pos_in_range: float = 50.0  # 0=60日最低, 100=60日最高

    main_net_flow: Optional[float] = None   # 主力净流入（元，东财）
    flow_days: int = 0

    limit_event: str = ''      # 涨停 / 跌停 / 一字涨停 / 一字跌停 / ''
    notes: List[str] = field(default_factory=list)


def _infer_limit_pct(code: str, is_st: bool = False) -> float:
    """按板块推断涨跌停幅度（与 backtest/tradability.py 保持一致口径）。
    ST/*ST 在主板是 5%（创业板 ST 仍是 20%，此处按主板口径保守处理）。"""
    num = code.split('.')[-1]
    if num.startswith(('688', '689')):
        return 20.0
    if num.startswith(('300', '301')):
        return 20.0
    if num.startswith(('8', '43', '92')):
        return 30.0
    return 5.0 if is_st else 10.0


def trading_day_lag(last_date: str, today=None) -> int:
    """数据滞后多少**个交易日**。周一早上对上周五 = 1，不报警。"""
    today = today or pd.Timestamp.now().normalize()
    try:
        d0 = pd.Timestamp(str(last_date)[:10])
        return int(np.busday_count(d0.date(), today.date()))
    except Exception:                    # noqa: BLE001
        return -1


def allowed_lag(now=None) -> int:
    """当前时点允许的滞后交易日数（超过它就必须告警）。

    固定用 ``lag >= 2`` 只在盘前跑得通：盘前 8:30 时"昨天"的K线本就是最新，
    lag=1 正常。但**收盘后**（如 20:00）当天K线应已入库，此时 lag=1 意味着
    整整丢了一个交易日 —— 而丢掉的那天可能正是暴跌日（300369 在 2026-10-29
    丢了 −10.79% 这一根，结论从"偏空"错成"中性"）。所以按时间点动态收紧。
    """
    now = now or pd.Timestamp.now()
    if bool(np.is_busday(now.normalize().date())) and now.hour >= 15:
        return 0                          # 交易日收盘后：当天数据必须已在库
    return 1                              # 盘前/盘中/周末：昨天的数据即最新


def load_context(code: str, name: str = '', days: int = 120,
                 with_flow: bool = True,
                 is_st: Optional[bool] = None) -> StockContext:
    """从本地缓存/DB 取日线，算出盘前状态。取不到就 ok=False，不要抛异常。

    指标计算统一走 total_return 口径（用 pct_chg 链式重建）：
    库里前复权 close 有 297 只票存在复权断点（见 data/conventions.py），
    直接算 MA/ATR 会把"复权因子换挡"当成暴跌/暴涨。重建后序列以**最新
    交易日为锚**，末日 OHLC = 真实成交价，昨收/昨高/昨低与行情软件一致。
    """
    ctx = StockContext(code=code, name=name)

    try:
        from data.cache import get_cache
        cache = get_cache()
        try:
            cache.build(force=False)
        except Exception:                # noqa: BLE001
            pass
        df = cache.get(code)
    except Exception as e:               # noqa: BLE001
        ctx.error = f'K线读取失败: {e}'
        return ctx

    if df is None or df.empty:
        ctx.error = '无K线数据'
        return ctx

    df = df.reset_index(drop=True).tail(days).reset_index(drop=True)
    if len(df) < 20:
        ctx.error = f'K线不足20根（实际{len(df)}）'
        return ctx

    # 统一口径：前复权 -> 总收益序列（锚定末日，末日价不变）
    try:
        from data.conventions import rebuild_total_return
        df = rebuild_total_return(df)
    except Exception as e:               # noqa: BLE001
        logger.debug(f'[盘前] {code} 总收益重建失败，退回原始序列: {e}')

    try:
        c = pd.to_numeric(df['close'], errors='coerce').astype(float)
        o = pd.to_numeric(df['open'], errors='coerce').astype(float)
        h = pd.to_numeric(df['high'], errors='coerce').astype(float)
        l = pd.to_numeric(df['low'], errors='coerce').astype(float)
        v = pd.to_numeric(df['volume'], errors='coerce').astype(float)
        amt = pd.to_numeric(df['amount'], errors='coerce').astype(float)
        pct = pd.to_numeric(df['pct_chg'], errors='coerce').astype(float)
    except Exception as e:               # noqa: BLE001
        ctx.error = f'字段转换失败: {e}'
        return ctx

    ctx.ok = True
    ctx.last_date = str(df['date'].iloc[-1])[:10]
    ctx.data_lag_days = trading_day_lag(ctx.last_date)   # 交易日口径
    ctx.close = float(c.iloc[-1])
    ctx.open = float(o.iloc[-1])
    ctx.high = float(h.iloc[-1])
    ctx.low = float(l.iloc[-1])
    ctx.pct_chg = float(pct.iloc[-1]) if not np.isnan(pct.iloc[-1]) else 0.0
    ctx.volume = float(v.iloc[-1])
    ctx.amount = float(amt.iloc[-1])

    # ---- 均线 ----
    ctx.ma5 = float(c.tail(5).mean())
    ctx.ma20 = float(c.tail(20).mean())
    ctx.ma60 = float(c.tail(min(60, len(c))).mean())
    ctx.bias20 = round((ctx.close / ctx.ma20 - 1) * 100, 2) if ctx.ma20 else 0.0

    # ---- 量能 ----
    ctx.vol_ma5 = float(v.tail(6).head(5).mean()) if len(v) >= 6 else float(v.tail(5).mean())
    ctx.volume_ratio = round(ctx.volume / ctx.vol_ma5, 2) if ctx.vol_ma5 else 0.0

    # ---- ATR14（真实波幅，Wilder 口径）----
    if len(c) >= 15:
        prev_c = c.shift(1)
        tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        ctx.atr14 = float(tr.tail(14).mean())
        ctx.atr_pct = round(ctx.atr14 / ctx.close * 100, 2) if ctx.close else 0.0

    # ---- 60日区间位置 ----
    win = min(60, len(c))
    ctx.range_low_60 = float(l.tail(win).min())
    ctx.range_high_60 = float(h.tail(win).max())
    span = ctx.range_high_60 - ctx.range_low_60
    ctx.pos_in_range = round((ctx.close - ctx.range_low_60) / span * 100, 1) if span > 0 else 50.0

    # ---- 昨日涨跌停（ST 股 5%）----
    if is_st is None:
        is_st = 'ST' in (name or '').upper()
    lim = _infer_limit_pct(code, is_st=is_st)
    if ctx.pct_chg >= lim - 0.6:
        flat = abs(ctx.high - ctx.low) / ctx.close < 0.005 if ctx.close else False
        ctx.limit_event = '一字涨停' if flat else '涨停'
    elif ctx.pct_chg <= -(lim - 0.6):
        flat = abs(ctx.high - ctx.low) / ctx.close < 0.005 if ctx.close else False
        ctx.limit_event = '一字跌停' if flat else '跌停'

    # ---- 人话提示 ----
    _build_notes(ctx)

    if with_flow:
        ctx.main_net_flow, ctx.flow_days = fetch_main_flow(code)

    return ctx


def _build_notes(ctx: StockContext) -> None:
    n = ctx.notes
    lag_limit = allowed_lag()
    if ctx.data_lag_days > lag_limit:
        n.append(f'⚠ 本地K线只到 {ctx.last_date}（滞后 {ctx.data_lag_days} 个交易日），'
                 f'盘前请先跑 `python main.py update-daily`，否则技术位与昨日收盘价都是旧的')
    if ctx.limit_event:
        n.append(f'昨日{ctx.limit_event}（{ctx.pct_chg:+.2f}%），今日开盘需防情绪反转')

    if ctx.bias20 > 8:
        n.append(f'高于MA20 {ctx.bias20:.1f}%，短期偏离偏大，追高性价比下降')
    elif ctx.bias20 < -8:
        n.append(f'低于MA20 {ctx.bias20:.1f}%，处于超跌区，反弹弹性大但趋势未修复')
    else:
        n.append(f'MA20乖离 {ctx.bias20:+.1f}%，处于正常区间')

    if ctx.volume_ratio >= 1.8:
        n.append(f'昨日放量（量比 {ctx.volume_ratio:.2f}），资金关注度上升')
    elif ctx.volume_ratio <= 0.6:
        n.append(f'昨日缩量（量比 {ctx.volume_ratio:.2f}），观望情绪浓')

    if ctx.pos_in_range >= 85:
        n.append(f'处于60日区间高位（{ctx.pos_in_range:.0f}%），上方为前高压力区')
    elif ctx.pos_in_range <= 15:
        n.append(f'处于60日区间低位（{ctx.pos_in_range:.0f}%），下方为前低支撑区')

    if ctx.atr_pct:
        n.append(f'日均波幅 ATR {ctx.atr_pct:.1f}%（约 {ctx.atr14:.2f} 元），'
                 f'可作为日内止损参考宽度')

    n.append(f'关键位：昨高 {ctx.high:.2f} / 昨低 {ctx.low:.2f}')


def fetch_main_flow(code: str, days: int = 5) -> tuple:
    """抓东财主力净流入（元）。返回 (最近一日净额, 拿到的天数)。失败返回 (None, 0)。"""
    num = code.split('.')[-1]
    mkt = '1' if code.lower().startswith('sh') else '0'
    url = (f'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get'
           f'?secid={mkt}.{num}&fields1=f1,f2,f3,f7'
           f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'
           f'&klt=101&lmt={days}')
    try:
        j = json.loads(_http_get(url, referer=_EM_REFERER, timeout=10))
        kl = (j.get('data') or {}).get('klines') or []
        if not kl:
            return None, 0
        last = float(kl[-1].split(',')[1])
        return last, len(kl)
    except Exception as e:               # noqa: BLE001
        logger.debug(f'[盘前] {code} 资金流抓取失败: {e}')
        return None, 0


def format_flow(x: Optional[float]) -> str:
    if x is None:
        return '—'
    a = abs(x)
    if a >= 1e8:
        return f'{x / 1e8:+.2f}亿'
    if a >= 1e4:
        return f'{x / 1e4:+.0f}万'
    return f'{x:+.0f}'


def tech_score(ctx: StockContext) -> float:
    """把技术状态压成一个 -100~+100 的分。

    技术位在盘前的解释力弱于消息面，所以这个分只用来做**方向微调**，
    权重在 analyzer 里给得比较低。构成：
      均线排列 40% + 区间位置 30% + 昨日量价配合 30%
    """
    if not ctx.ok:
        return 0.0

    # 均线排列：站上 MA5/MA20/MA60 各加一点
    ma_part = 0.0
    ma_part += 1.0 if ctx.close > ctx.ma5 else -1.0
    ma_part += 1.0 if ctx.close > ctx.ma20 else -1.0
    ma_part += 1.0 if ctx.close > ctx.ma60 else -1.0
    ma_part /= 3.0                                  # -1 ~ +1

    # 区间位置：0~100 映射到 -1~+1
    pos_part = (ctx.pos_in_range - 50.0) / 50.0

    # 量价配合：涨且放量=强，涨且缩量=弱；跌且放量=弱，跌且缩量=中性
    vp = 0.0
    if ctx.pct_chg > 0:
        vp = 1.0 if ctx.volume_ratio >= 1.2 else (0.3 if ctx.volume_ratio >= 0.8 else -0.2)
    elif ctx.pct_chg < 0:
        vp = -1.0 if ctx.volume_ratio >= 1.2 else (-0.2 if ctx.volume_ratio >= 0.8 else 0.2)

    raw = 0.40 * ma_part + 0.30 * pos_part + 0.30 * vp

    # 单日大阴线单独补一刀：上面 vp 那一档只判**方向**和量能，不看**幅度**，
    # 于是跌 1% 与跌 10.79% 同分。300369 在 2026-10-29 跌 −10.79%、
    # 5 日换手 89.7%、从历史最高收盘砸下来，技术位却还是 +20.1（只因收盘仍在
    # MA20 上方），这就是幅度缺位的直接后果。跌超 5% 后按超出部分追加惩罚。
    if ctx.pct_chg <= -5.0:
        raw -= min((abs(ctx.pct_chg) - 5.0) / 10.0 + 0.15, 0.60)

    return round(100.0 * np.tanh(raw * 1.2), 1)
