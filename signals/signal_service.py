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


