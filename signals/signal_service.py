from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
from loguru import logger
import pandas as pd

from db.database import session_scope
from db.models import TradeSignal
from indicators.mytt_indicators import calc_all_indicators
from signals.manager import get_all_strategies, get_strategy
from signals.base import SignalRecord


def save_signals(records: List[SignalRecord]):
    if not records:
        return
    with session_scope() as session:
        for rec in records:
            sig_time = datetime.strptime(rec.signal_time[:10], '%Y-%m-%d')
            if len(rec.signal_time) > 10:
                try:
                    sig_time = datetime.strptime(rec.signal_time, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    sig_time = datetime.strptime(rec.signal_time[:10], '%Y-%m-%d')
            existing = (
                session.query(TradeSignal)
                .filter_by(
                    code=rec.code,
                    period=rec.period,
                    strategy=rec.strategy,
                    signal_type=rec.signal_type,
                )
                .filter(TradeSignal.signal_time == sig_time)
                .first()
            )
            if existing:
                existing.price = rec.price
                existing.signal_strength = rec.signal_strength
                existing.reason = rec.reason
                existing.indicators = rec.indicators
                existing.description = rec.description
            else:
                ts = TradeSignal(
                    code=rec.code,
                    period=rec.period,
                    strategy=rec.strategy,
                    signal_type=rec.signal_type,
                    signal_time=sig_time,
                    price=rec.price,
                    signal_strength=rec.signal_strength,
                    reason=rec.reason,
                    indicators=rec.indicators,
                    description=rec.description,
                )
                session.add(ts)


def generate_daily_signals_for_stock(
    df_daily: pd.DataFrame,
    code: str,
    strategy_names: Optional[List[str]] = None,
    check_volume: bool = True,
) -> List[SignalRecord]:
    if df_daily.empty:
        return []
    df = calc_all_indicators(df_daily)
    all_records = []
    if strategy_names:
        strategies = [get_strategy(name, check_volume=check_volume) for name in strategy_names]
    else:
        strategies = get_all_strategies(check_volume=check_volume)
    for strategy in strategies:
        records = strategy.generate_signal_records(df, code, period='daily', time_col='date')
        all_records.extend(records)
    return all_records


def generate_latest_daily_signal(
    df_daily: pd.DataFrame,
    code: str,
    strategy_names: Optional[List[str]] = None,
    check_volume: bool = True,
    check_last_row: bool = False,
    historical_mode: bool = False,
) -> List[SignalRecord]:
    """只计算最新一根K线的信号，性能优化版（用于全市场扫描）

    信号语义：
        - 总是检查最后一根K线（last_idx）是否形成信号
        - 不传 --date（实时/盘中扫描）：
            * 最后一行 = 今日合成K线（带实时行情）
            * signal_time = 最后一根K线日期（今日/扫描当日）
            * price = 最后一行 close（即实时价格）
        - 传 --date（历史扫描）：
            * 最后一行 = 指定日期的历史K线
            * signal_time = 下一交易日（= 指定日期 + 1工作日，跳过周末）
            * price = 下一交易日 open（由调用方在信号后重写，因为本函数只拿到指定日期之前的数据）
    """
    if df_daily.empty or len(df_daily) < 35:
        return []

    df = calc_all_indicators(df_daily)
    if df.empty or len(df) < 1:
        return []

    # 只检查最后一根K线的信号
    last_idx = len(df) - 1
    last_date = str(df['date'].iloc[last_idx]) if 'date' in df.columns else ''

    # 历史模式下：signal_time = 下一交易日
    signal_time_str = last_date
    if historical_mode and 'date' in df.columns:
        from datetime import datetime, timedelta
        try:
            base_date = datetime.strptime(last_date, '%Y-%m-%d')
            next_day = base_date + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            signal_time_str = next_day.strftime('%Y-%m-%d')
        except (ValueError, TypeError):
            pass

    if strategy_names:
        strategies = [get_strategy(name, check_volume=check_volume) for name in strategy_names]
    else:
        strategies = get_all_strategies(check_volume=check_volume)

    all_records = []
    for strategy in strategies:
        try:
            signals = strategy.generate_signals(df)
            strengths = strategy.calc_strength(df, signals)
            reasons = strategy.calc_reason(df, signals)

            sig = int(signals.iloc[last_idx])
            if sig == 0:
                continue

            strength = float(strengths.iloc[last_idx])
            reason = str(reasons.iloc[last_idx]) if reasons.iloc[last_idx] else ""
            price_val = float(df['close'].iloc[last_idx])

            all_records.append(SignalRecord(
                code=code,
                period='daily',
                strategy=strategy.name,
                signal_type=sig,
                signal_time=signal_time_str,
                price=price_val,
                signal_strength=strength,
                reason=reason,
                indicators=strategy.get_indicator_snapshot(df, last_idx),
                description=f"{strategy.name} {'买入' if sig == 1 else '卖出'}信号",
            ))
        except Exception as e:
            logger.debug(f"{code} {strategy.name} 计算最新信号失败: {e}")

    return all_records

def calc_hold_pnl_from_trade_signals(
    strategy: str = 'MACD金叉',
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    codes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """从 trade_signals 读取 MACD 买入信号，**信号日的下一交易日开盘买入**（信号在
    收盘后才能确认，次日才可执行），一直持有到 end_date 收盘卖出。

    返回: [{
        'code': str,
        'strategy': str,
        'buy_date': str,            # 买入日（= 信号日后第一个交易日，open 买入）
        'sell_date': str,           # 卖出日（<= end_date，收盘卖出；无K线时用实时价）
        'reason': str,
        'signal_price': float,      # 信号记录里的 price
        'buy_price': float,         # 买入价：信号日后第一个交易日的 open
        'sell_price': float,        # 卖出价：end_date 的 close（无K线则用实时价）
        'hold_days': int,           # 持仓交易天数
        'pnl_amount': float,
        'pnl_pct': float,
    }]
    """
    from db.database import session_scope
    from db.models import TradeSignal, DailyKline
    from data_fetcher.baostock_fetcher import normalize_code

    out: List[Dict[str, Any]] = []

    with session_scope() as session:
        q = session.query(TradeSignal).filter(
            TradeSignal.strategy == strategy,
            TradeSignal.reason.contains(strategy),
            TradeSignal.signal_type == 1,
        )
        if start_date:
            # 信号过滤：只看 start_date 当天出现的信号
            # 注意：end_date 仅用于计算持有期末的卖出价，不用于信号过滤
            q = q.filter(TradeSignal.signal_time >= start_date)
            q = q.filter(TradeSignal.signal_time <= start_date + ' 23:59:59')
        if codes:
            norm_codes = [normalize_code(c) for c in codes]
            q = q.filter(TradeSignal.code.in_(norm_codes))

        q = q.order_by(TradeSignal.signal_time.asc(), TradeSignal.code.asc())
        raw_signals = q.all()
        if not raw_signals:
            logger.info("trade_signals 中无符合条件的 MACD 买入信号")
            return out

        signal_dicts = []
        for s in raw_signals:
            sig_time = s.signal_time
            sig_time_str = (
                sig_time.strftime('%Y-%m-%d')
                if hasattr(sig_time, 'strftime')
                else str(sig_time)[:10]
            )
            signal_dicts.append({
                'code': s.code,
                'strategy': s.strategy,
                'signal_time': sig_time_str,
                'reason': (s.reason or '').strip()[:64],
                'price': float(s.price) if s.price is not None else None,
            })

        # 每只股票的最早信号日 → 用于限定 DailyKline 查询范围
        code_to_min_signal: Dict[str, str] = {}
        for sd in signal_dicts:
            cur = code_to_min_signal.get(sd['code'])
            if cur is None or sd['signal_time'] < cur:
                code_to_min_signal[sd['code']] = sd['signal_time']

        code_list = sorted(code_to_min_signal.keys())

        # 批量查 DailyKline（范围：每只股票最早信号日 ~ end_date）
        # 这样既能拿到每个信号日的 open，也能拿到 end_date 的 close
        kline_rows = session.query(DailyKline).filter(
            DailyKline.code.in_(code_list),
            DailyKline.trade_date >= min(code_to_min_signal.values()),
            (DailyKline.trade_date <= end_date) if end_date else True,
            DailyKline.adjustflag == 2,
        ).all()

        # 建两个索引：
        # 1. {(code, date): (open, close)} 用于精确查找
        # 2. {code: [(date, open, close), ...]} 按日期排序，便于找最后一个交易日
        kline_map: Dict[Tuple[str, str], Tuple[float, float]] = {}
        kline_by_code: Dict[str, List[Tuple[str, float, float]]] = {}
        for r in kline_rows:
            if r.open is None or r.close is None:
                continue
            date_str = str(r.trade_date)
            op = float(r.open)
            cl = float(r.close)
            kline_map[(r.code, date_str)] = (op, cl)
            kline_by_code.setdefault(r.code, []).append((date_str, op, cl))

        for code in kline_by_code:
            kline_by_code[code].sort(key=lambda x: x[0])

    # --- 如果 end_date 没有K线数据，尝试获取实时价格作为卖出价 ---
    use_realtime_for_sell = False
    realtime_map: Dict[str, float] = {}
    if end_date:
        need_realtime_codes = []
        for code in code_list:
            rows = kline_by_code.get(code, [])
            if not rows or rows[-1][0] < end_date:
                need_realtime_codes.append(code)
        if need_realtime_codes:
            logger.info(
                f"end_date {end_date} 有 {len(need_realtime_codes)} 只股票无K线，尝试获取实时行情"
            )
            try:
                from data_fetcher.sina_fetcher import get_realtime_quotes
                quotes_df = get_realtime_quotes(need_realtime_codes)
                if quotes_df is not None and not quotes_df.empty:
                    for _, row in quotes_df.iterrows():
                        rc = str(row.get('code', ''))
                        price = float(row.get('price', 0))
                        if rc and price > 0:
                            realtime_map[rc] = price
                    logger.info(f"成功获取 {len(realtime_map)} 只股票的实时行情")
                    use_realtime_for_sell = True
                else:
                    logger.warning("实时行情为空，将继续使用数据库历史数据")
            except Exception as e:
                logger.warning(f"获取实时行情失败({e})，将继续使用数据库历史数据")

    for sd in signal_dicts:
        code = sd['code']
        signal_time = sd['signal_time']

        # --- 买入价：信号日的下一交易日 open（信号在收盘后确认，次日才能买入）---
        rows_for_code = kline_by_code.get(code, [])
        # 找到信号日后的第一个交易日（排除信号日本身）
        buy_kl = None
        for r in rows_for_code:
            if r[0] > signal_time:
                buy_kl = r
                break

        if buy_kl is not None:
            buy_date = buy_kl[0]
            buy_price = buy_kl[1]
        else:
            # 信号日后没有历史K线 → 尝试拉实时行情作为买入价
            if not use_realtime_for_sell or code not in realtime_map:
                continue
            buy_date = end_date or signal_time
            buy_price = realtime_map[code]
            logger.debug(
                f"{code} 信号日 {signal_time} 之后无K线数据，"
                f"使用实时价 {buy_price} 作为买入价"
            )

        if buy_price <= 0:
            continue

        # --- 卖出价：优先 end_date 当日K线；若无则用实时行情；再无则回退最后历史K线 ---
        rows_for_code = kline_by_code.get(code, [])
        valid_rows = [row for row in rows_for_code if row[0] >= buy_date]

        # 判断最后一根K线是否达到 end_date
        last_hist_date = valid_rows[-1][0] if valid_rows else None
        has_end_date_kline = last_hist_date is not None and end_date and last_hist_date >= end_date

        if use_realtime_for_sell and not has_end_date_kline and code in realtime_map:
            # 用实时行情作为卖出价
            sell_price = realtime_map[code]
            sell_date = end_date
            # 持仓天数 = 历史K线中从buy_date开始的根数 + 1（end_date当天算1日）
            if valid_rows:
                buy_idx = None
                for i, row in enumerate(valid_rows):
                    if row[0] == buy_date:
                        buy_idx = i
                        break
                if buy_idx is not None:
                    hold_days = len(valid_rows) - buy_idx + 1  # +1 因为 end_date 也算1天
                else:
                    hold_days = len(valid_rows) + 1
            else:
                hold_days = 1
        elif valid_rows:
            # 用数据库中最后一根历史K线
            last_row = valid_rows[-1]
            sell_date, _, sell_price = last_row
            buy_idx = None
            for i, row in enumerate(valid_rows):
                if row[0] == buy_date:
                    buy_idx = i
                    break
            if buy_idx is None:
                hold_days = len(valid_rows)
            else:
                hold_days = len(valid_rows) - buy_idx
        else:
            # 完全没有数据，回退到 signal.price
            if not sd['price']:
                continue
            sell_price = float(sd['price'])
            sell_date = buy_date
            hold_days = 0

        pnl_amount = sell_price - buy_price
        pnl_pct = (sell_price / buy_price - 1) * 100

        out.append({
            'code': code,
            'strategy': sd['strategy'],
            'buy_date': buy_date,
            'sell_date': sell_date,
            'reason': sd['reason'],
            'signal_price': round(sd['price'], 4) if sd['price'] is not None else None,
            'buy_price': round(buy_price, 4),
            'sell_price': round(sell_price, 4),
            'hold_days': int(hold_days),
            'pnl_amount': round(pnl_amount, 4),
            'pnl_pct': round(pnl_pct, 4),
        })

    return out


def generate_and_save_daily_signals(
    df_daily: pd.DataFrame,
    code: str,
    strategy_names: Optional[List[str]] = None,
    check_volume: bool = True,
) -> int:
    records = generate_daily_signals_for_stock(df_daily, code, strategy_names, check_volume)
    save_signals(records)
    return len(records)


# =====================================================================
# MACD 预测信号验证 —— 回测 "MACD预测金叉" 是否真的在随后几天形成金叉
# =====================================================================

def verify_macd_predictions(
    signal_date: str,
    codes: Optional[List[str]] = None,
    check_volume: bool = True,
    forward_days: int = 5,
) -> Dict[str, Any]:
    """验证指定日期的 "MACD预测金叉" 信号是否真的实现。

    流程:
      1. 用 signal_date 及之前的历史K线，计算当日有哪些股票触发
         "MACD预测金叉" 信号
      2. 对每只信号股票，向后再取 N 天(默认 5 天) 的实际K线数据
      3. 重新计算 MACD，观察后续是否:
         a. DIF 上穿 DEA 形成金叉   ✓=命中, ✗=未命中
         b. MACD柱(负值)继续增大   即 绝对值继续减小
         c. 股价涨跌幅
      4. 返回每只股票的详细结果 + 汇总统计

    Args:
        signal_date: 信号产生日 YYYY-MM-DD
        codes: 限制股票代码列表（None=全市场扫描）
        check_volume: 是否启用 MACD 成交量过滤
        forward_days: 向后验证多少个交易日

    Returns:
        {
          'signal_date': '2026-07-02',
          'total_signals': 12,
          'cross_within_1d': 3,    # 次日金叉数
          'cross_within_3d': 5,    # 3日内金叉数
          'cross_within_5d': 8,    # 5日内金叉数
          'bar_keep_growing_1d': 7, # 次日MACD柱继续增大
          'hit_rate_3d': 41.7,     # 3日内金叉命中率 %
          'avg_pct_3d': 1.23,      # 3日平均涨跌幅 %
          'detail': [
             {
               'code': 'sh.600036',
               'name': '招商银行',
               'close_on_signal': 35.20,
               'dif_on_signal': -0.250,
               'dea_on_signal': -0.180,
               'macd_on_signal': -0.140,
               'reason': '3日缩短42% | DIF=-0.25(向上) | 需涨1.8% | 量比=1.3',
               'strength': 75.0,
               'days_to_cross': 2,      # 第几天金叉 (None=未金叉)
               'cross_date': '2026-07-04',
               'bar_continued_1d': True,
               'close_1d': 35.45, 'pct_1d': 0.71,
               'close_3d': 35.95, 'pct_3d': 2.13,
               'close_5d': 35.88, 'pct_5d': 1.93,
             },
             ...
          ],
        }
    """
    from data_fetcher.baostock_fetcher import (
        get_daily_kline_df, normalize_code,
    )
    from data_fetcher.stock_pool import get_stock_pool_from_db, get_stock_name_map
    from scheduler.intraday_service import build_historical_daily_df
    from signals.manager import get_strategy
    from datetime import datetime, timedelta

    name_map = get_stock_name_map()

    # ---- Step 1. 确定扫描范围 ----
    if codes:
        pool = [normalize_code(c) for c in codes]
    else:
        pool_raw = get_stock_pool_from_db()
        pool = [normalize_code(c) for c in pool_raw]
    if not pool:
        return {'signal_date': signal_date, 'total_signals': 0, 'detail': []}

    # ---- Step 2. 在 signal_date 当日检测 "MACD预测金叉" 信号 ----
    logger.info(f"[验证] Step1: 检测 {signal_date} 当日的 MACD 预测金叉信号，股票 {len(pool)} 只")
    strategy = get_strategy('MACD预测金叉', check_volume=check_volume)

    signal_items: List[Dict[str, Any]] = []
    for code in pool:
        df_sig = build_historical_daily_df(code, signal_date)
        if df_sig.empty or len(df_sig) < 35:
            continue
        from indicators.mytt_indicators import calc_all_indicators
        df_ind = calc_all_indicators(df_sig)
        if df_ind.empty:
            continue
        last_idx = len(df_ind) - 1

        # 只看最后一行(=signal_date)
        sig_series = strategy.generate_signals(df_ind)
        if int(sig_series.iloc[last_idx]) != 1:
            continue

        last = df_ind.iloc[last_idx]
        reasons = strategy.calc_reason(df_ind, sig_series)
        strengths = strategy.calc_strength(df_ind, sig_series)
        signal_items.append({
            'code': code,
            'close_on_signal': float(last['close']),
            'dif_on_signal': float(last['dif']),
            'dea_on_signal': float(last['dea']),
            'macd_on_signal': float(last['macd']),
            'reason': str(reasons.iloc[last_idx]) if reasons.iloc[last_idx] else '',
            'strength': float(strengths.iloc[last_idx]),
        })

    if not signal_items:
        logger.info(f"[验证] {signal_date} 无任何 MACD 预测金叉信号")
        return {'signal_date': signal_date, 'total_signals': 0, 'detail': []}

    total = len(signal_items)
    logger.info(f"[验证] Step1: 共 {total} 只股票触发预测金叉信号")

    # ---- Step 3. 对每只信号股票，取 signal_date 之后 N 天K线验证 ----
    try:
        base_dt = datetime.strptime(signal_date, '%Y-%m-%d')
    except ValueError:
        logger.error(f"日期格式错误: {signal_date}")
        return {'signal_date': signal_date, 'total_signals': total, 'detail': []}

    end_lookup = (base_dt + timedelta(days=forward_days + 10)).strftime('%Y-%m-%d')
    # 为节省性能，先一次性取每只股票 forward 窗口的K线
    detail_rows = []

    for it in signal_items:
        code = it['code']
        # 取 signal_date 之前 40 天(预热) + 之后 N 天数据，
        # 这样可以直接整段 calc_all_indicators 得到 signal_day 及之后每天的 MACD
        start_lookup = (base_dt - timedelta(days=60)).strftime('%Y-%m-%d')
        df_fwd = get_daily_kline_df(code, start_date=start_lookup, end_date=end_lookup)
        if df_fwd.empty or len(df_fwd) < 35:
            continue

        from indicators.mytt_indicators import calc_all_indicators
        df_fwd_ind = calc_all_indicators(df_fwd)
        if df_fwd.empty:
            continue

        # 找到 signal_date 在 df_fwd_ind 中的位置
        sig_idx = None
        for i in range(len(df_fwd_ind)):
            if str(df_fwd_ind['date'].iloc[i]) == signal_date:
                sig_idx = i
                break
        if sig_idx is None:
            continue

        # 取 signal_day 之后的 forward_days 根K线
        max_fwd = min(len(df_fwd_ind) - sig_idx - 1, forward_days)
        if max_fwd <= 0:
            # signal_day 就是最后一天（比如是今天且无后续K线）
            detail_rows.append({
                **it,
                'name': name_map.get(code, ''),
                'days_to_cross': None,
                'cross_date': None,
                'bar_continued_1d': None,
                'close_1d': None, 'pct_1d': None,
                'close_3d': None, 'pct_3d': None,
                'close_5d': None, 'pct_5d': None,
                'note': '信号日之后无足够K线数据，无法验证',
            })
            continue

        sig_price = it['close_on_signal']
        dif_on_sig = it['dif_on_signal']
        dea_on_sig = it['dea_on_signal']
        macd_on_sig = it['macd_on_signal']

        # ---- 判断金叉: DIF 从 < DEA 变为 >= DEA ----
        days_to_cross = None
        cross_date = None
        for j in range(1, max_fwd + 1):
            cur_idx = sig_idx + j
            dif_j = float(df_fwd_ind['dif'].iloc[cur_idx])
            dea_j = float(df_fwd_ind['dea'].iloc[cur_idx])
            # 判断金叉: 前一日 dif < dea，当日 dif >= dea
            prev_dif = dif_on_sig if j == 1 else float(df_fwd_ind['dif'].iloc[cur_idx - 1])
            prev_dea = dea_on_sig if j == 1 else float(df_fwd_ind['dea'].iloc[cur_idx - 1])
            if (prev_dif < prev_dea) and (dif_j >= dea_j):
                days_to_cross = j
                cross_date = str(df_fwd_ind['date'].iloc[cur_idx])
                break

        # ---- 判断MACD柱是否继续增大(负值继续变大/绝对值变小) ----
        bar_continued_1d = None
        if max_fwd >= 1:
            macd_next = float(df_fwd_ind['macd'].iloc[sig_idx + 1])
            bar_continued_1d = macd_next > macd_on_sig

        # ---- 计算 1日/3日/5日 的收盘价与涨跌幅 ----
        def get_close_after(offset: int):
            target_idx = sig_idx + offset
            if target_idx >= len(df_fwd_ind):
                return None
            return float(df_fwd_ind['close'].iloc[target_idx])

        close_1d = get_close_after(1)
        close_3d = get_close_after(3) if max_fwd >= 3 else None
        close_5d = get_close_after(5) if max_fwd >= 5 else None

        def pct(c):
            return round((c - sig_price) / sig_price * 100.0, 2) if c is not None else None

        detail_rows.append({
            **it,
            'name': name_map.get(code, ''),
            'days_to_cross': days_to_cross,
            'cross_date': cross_date,
            'bar_continued_1d': bar_continued_1d,
            'close_1d': close_1d, 'pct_1d': pct(close_1d),
            'close_3d': close_3d, 'pct_3d': pct(close_3d),
            'close_5d': close_5d, 'pct_5d': pct(close_5d),
        })

    # ---- Step 4. 汇总统计 ----
    verified = [d for d in detail_rows if d.get('note') != '信号日之后无足够K线数据，无法验证']
    n_verified = len(verified)

    cross_1d = sum(1 for d in verified if d['days_to_cross'] is not None and d['days_to_cross'] <= 1)
    cross_3d = sum(1 for d in verified if d['days_to_cross'] is not None and d['days_to_cross'] <= 3)
    cross_5d = sum(1 for d in verified if d['days_to_cross'] is not None and d['days_to_cross'] <= 5)
    bar_ok = sum(1 for d in verified if d.get('bar_continued_1d') is True)

    def avg_pct(field):
        vals = [d[field] for d in verified if d.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    result = {
        'signal_date': signal_date,
        'total_signals': total,
        'verified': n_verified,
        'cross_within_1d': cross_1d,
        'cross_within_3d': cross_3d,
        'cross_within_5d': cross_5d,
        'bar_keep_growing_1d': bar_ok,
        'hit_rate_1d_pct': round(cross_1d / n_verified * 100, 1) if n_verified else None,
        'hit_rate_3d_pct': round(cross_3d / n_verified * 100, 1) if n_verified else None,
        'hit_rate_5d_pct': round(cross_5d / n_verified * 100, 1) if n_verified else None,
        'bar_hit_rate_1d_pct': round(bar_ok / n_verified * 100, 1) if n_verified else None,
        'avg_pct_1d': avg_pct('pct_1d'),
        'avg_pct_3d': avg_pct('pct_3d'),
        'avg_pct_5d': avg_pct('pct_5d'),
        'detail': detail_rows,
    }
    return result


