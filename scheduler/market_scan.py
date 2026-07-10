from datetime import datetime
from typing import List, Optional
from loguru import logger
import time

from data_fetcher.baostock_fetcher import normalize_code
from data_fetcher.sina_fetcher import get_realtime_quotes
from data_fetcher.stock_pool import get_stock_pool_from_db, get_stock_name_map
from signals.signal_service import generate_latest_daily_signal, save_signals
from signals.base import SignalRecord
from scheduler.intraday_service import (
    build_intraday_daily_df, build_historical_daily_df,
    get_next_trading_day_open,
)


def _print_signal(sig, name_map, verbose=False):
    """打印单条信号"""
    name = name_map.get(sig.code, '')
    type_str = '买入' if sig.signal_type == 1 else '卖出'
    logger.info(
        f"[信号] {sig.signal_time} {sig.code} {name} {type_str} "
        f"策略={sig.strategy} 价格={sig.price:.2f} "
        f"强度={sig.signal_strength} 原因={sig.reason}"
    )
    if verbose and getattr(sig, 'indicators', None):
        ind = sig.indicators
        # 按字段优先级排序输出
        preferred = ['close', 'ma5', 'ma10', 'ma20', 'ma60',
                     'dif', 'dea', 'macd',
                     'k', 'd', 'j',
                     'rsi6', 'rsi12', 'rsi24',
                     'bias6', 'bias12', 'bias24',
                     'wr10', 'wr6',
                     'upper', 'mid', 'lower',
                     'cci', 'atr',
                     'volume', 'vol_ma5', 'vol_ma10', 'vol_ma20', 'volume_ratio']
        keys = preferred + [k for k in ind.keys() if k not in preferred]
        lines = [f"   └─ [{sig.code}] 指标:"]
        for k in keys:
            if k not in ind:
                continue
            v = ind[k]
            if isinstance(v, float):
                if abs(v) >= 1000:
                    lines.append(f"      · {k:14s} = {v:.2f}")
                else:
                    lines.append(f"      · {k:14s} = {v:.4f}")
            else:
                lines.append(f"      · {k:14s} = {v}")
        logger.info("\n".join(lines))


def _print_progress(total, pool_size, buy_count, sell_count, elapsed_sec, extra=''):
    """打印扫描进度"""
    logger.info(
        f"扫描进度: {total}/{pool_size} "
        f"({total*100//pool_size}%) "
        f"买入信号: {buy_count} 卖出信号: {sell_count} "
        f"耗时: {elapsed_sec:.0f}s "
        f"{extra}"
    )


def _is_restricted(code: str) -> bool:
    """判断股票是否属于受限板块（科创板/北交所），无权限交易。

    规则（覆盖 normalize_code 前后的格式）：
      - 科创板：sh.688, sh.689, 或 纯数字 688xxx / 689xxx
      - 北交所  ：bj.xxx, sh.8xxx, sh.430xxx, sh.92xxxx, 或 纯数字 8xxxx / 430xxx / 92xxxx
    """
    if not code:
        return False
    c = str(code).lower().strip()

    # —— 带前缀形式 ——
    if c.startswith('bj.'):
        return True
    if c.startswith('sh.688') or c.startswith('sh.689'):
        return True
    if c.startswith('sh.8') or c.startswith('sh.430') or c.startswith('sh.92'):
        return True

    # —— 纯数字形式 ——
    digits = ''.join(ch for ch in c if ch.isdigit())
    if len(digits) >= 6:
        if digits.startswith('688') or digits.startswith('689'):
            return True
        if (digits.startswith('8') or digits.startswith('430')
                or digits.startswith('92')):
            return True
    return False


def scan_market_intraday(
    strategy_names: Optional[List[str]] = None,
    signal_type: str = 'buy',
    min_price: float = 2.0,
    max_price: float = 200.0,
    batch_size: int = 100,
    save: bool = True,
    scan_date: str = None,
    codes: Optional[List[str]] = None,
    check_volume: bool = True,
) -> List[SignalRecord]:
    """
    全市场日线信号扫描

    Args:
        strategy_names: 策略列表，None表示全部
        signal_type: buy=只输出买入，sell=只输出卖出，all=全部
        min_price: 最低价格过滤
        max_price: 最高价格过滤
        batch_size: 每批处理股票数
        save: 是否保存到数据库
        scan_date: 扫描指定日期 (YYYY-MM-DD)。None表示盘中扫描当日；传具体日期则用历史数据扫描，适用于周末/盘后
        codes: 指定股票代码列表，None表示扫描全市场
        check_volume: 买入信号是否要求放量（MACD金叉策略专用，默认 True）
    """
    if codes:
        stock_pool = codes
        verbose = True
    else:
        all_pool = get_stock_pool_from_db()
        stock_pool = [c for c in all_pool if not _is_restricted(c)]
        if len(stock_pool) != len(all_pool):
            logger.info(f"全市场过滤：原 {len(all_pool)} 只，剔除受限板块 {len(all_pool) - len(stock_pool)} 只，剩余 {len(stock_pool)} 只")
        verbose = False
    if not stock_pool:
        logger.error("股票池为空，请先运行 update-stock-list 更新股票列表")
        return []

    if scan_date:
        logger.info(f"开始全市场历史扫描，日期: {scan_date}，共 {len(stock_pool)} 只股票")
        return _scan_market_historical(
            stock_pool, scan_date, strategy_names,
            signal_type, min_price, max_price, save,
            check_volume=check_volume,
            verbose=verbose,
        )

    logger.info(f"开始全市场盘中扫描，共 {len(stock_pool)} 只股票")
    logger.info(f"策略: {strategy_names or '全部'}")
    logger.info(f"信号类型: {signal_type}  价格范围: {min_price}-{max_price}")
    logger.info(f"成交量过滤: {'开启' if check_volume else '关闭'}")
    if verbose:
        logger.info(f"【调试模式】打印每只股票的指标数据")

    name_map = get_stock_name_map()

    all_buy_signals = []
    all_sell_signals = []
    total_scanned = 0
    total_with_data = 0
    start_time = datetime.now()

    for batch_start in range(0, len(stock_pool), batch_size):
        batch = stock_pool[batch_start:batch_start + batch_size]

        try:
            quotes_df = get_realtime_quotes(batch)
        except Exception as e:
            logger.error(f"获取第 {batch_start//batch_size+1} 批行情失败: {e}")
            time.sleep(1)
            continue

        if quotes_df.empty:
            continue

        for code in batch:
            total_scanned += 1
            normalized_code = normalize_code(code)
            stock_name = name_map.get(normalized_code, '')

            quote_rows = quotes_df[quotes_df['code'] == normalized_code]
            if quote_rows.empty:
                if verbose:
                    logger.info(f"[数据] {normalized_code} {stock_name} 无实时行情")
                continue

            quote = quote_rows.iloc[0].to_dict()
            price = float(quote.get('price', 0))

            if price <= 0 or price < min_price or price > max_price:
                if verbose:
                    logger.info(f"[数据] {normalized_code} {stock_name} 价格={price:.2f} 超出 {min_price}-{max_price} 范围")
                continue

            df = build_intraday_daily_df(normalized_code, quote)
            if df.empty or len(df) < 35:
                if verbose:
                    logger.info(f"[数据] {normalized_code} {stock_name} K线={len(df)} 根 < 35，跳过")
                continue

            total_with_data += 1

            if verbose:
                logger.info(f"[数据] {normalized_code} {stock_name} K线={len(df)} 根 价格={price:.2f}")

            try:
                today_signals = generate_latest_daily_signal(
                    df, normalized_code, strategy_names, check_volume=check_volume,
                    check_last_row=True,
                    verbose=verbose,
                )
            except Exception as e:
                if verbose:
                    logger.error(f"[数据] {normalized_code} {stock_name} 信号计算失败: {e}")
                else:
                    logger.debug(f"{code} 信号计算失败: {e}")
                continue

            if verbose and not today_signals:
                logger.info(f"  └─ 无信号输出（各策略详细诊断信息已打印在上方）")

            for sig in today_signals:
                if signal_type in ('buy', 'all') and sig.signal_type == 1:
                    all_buy_signals.append(sig)
                    _print_signal(sig, name_map, verbose=verbose)
                if signal_type in ('sell', 'all') and sig.signal_type == -1:
                    all_sell_signals.append(sig)
                    _print_signal(sig, name_map, verbose=verbose)

        elapsed = (datetime.now() - start_time).total_seconds()

        _print_progress(
            batch_start + len(batch), len(stock_pool),
            len(all_buy_signals), len(all_sell_signals), elapsed
        )

    all_signals = all_buy_signals + all_sell_signals

    total_elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info(f"全市场扫描完成，总耗时: {total_elapsed:.0f}s")
    logger.info(f"扫描股票: {total_scanned} 只  有效数据: {total_with_data} 只")
    logger.info(f"买入信号: {len(all_buy_signals)} 个  卖出信号: {len(all_sell_signals)} 个")

    if all_buy_signals:
        logger.info("-" * 40)
        logger.info(f"【买入信号列表】共 {len(all_buy_signals)} 个")
        all_buy_signals.sort(key=lambda x: x.signal_strength or 0, reverse=True)
        for i, sig in enumerate(all_buy_signals[:50], 1):
            logger.info(
                f"  {i:2d}. {sig.code} {name_map.get(sig.code, '')} {sig.strategy} "
                f"价格={sig.price:.2f} 强度={sig.signal_strength} 原因={sig.reason}"
            )
            if verbose:
                _print_signal(sig, name_map, verbose=True)
        if len(all_buy_signals) > 50:
            logger.info(f"  ... 还有 {len(all_buy_signals) - 50} 个")

    if all_sell_signals:
        logger.info("-" * 40)
        logger.info(f"【卖出信号列表】共 {len(all_sell_signals)} 个")
        all_sell_signals.sort(key=lambda x: x.signal_strength or 0, reverse=True)
        for i, sig in enumerate(all_sell_signals[:50], 1):
            logger.info(
                f"  {i:2d}. {sig.code} {name_map.get(sig.code, '')} {sig.strategy} "
                f"价格={sig.price:.2f} 强度={sig.signal_strength} 原因={sig.reason}"
            )
            if verbose:
                _print_signal(sig, name_map, verbose=True)
        if len(all_sell_signals) > 50:
            logger.info(f"  ... 还有 {len(all_sell_signals) - 50} 个")

    logger.info("=" * 60)

    if save and all_signals:
        save_signals(all_signals)
        logger.info(f"已保存 {len(all_signals)} 个信号到数据库")

    return all_signals


def _scan_market_historical(
    stock_pool: List[str],
    scan_date: str,
    strategy_names: Optional[List[str]],
    signal_type: str,
    min_price: float,
    max_price: float,
    save: bool,
    check_volume: bool = True,
    verbose: bool = False,
) -> List[SignalRecord]:
    """历史扫描模式 —— 不依赖实时行情，直接用数据库中的历史K线判断指定日期的信号"""
    logger.info(f"策略: {strategy_names or '全部'}")
    logger.info(f"信号类型: {signal_type}  价格范围: {min_price}-{max_price}")
    logger.info(f"成交量过滤: {'开启' if check_volume else '关闭'}")
    logger.info(f"数据范围: 从 {scan_date} 往前180天")
    if verbose:
        logger.info(f"【调试模式】打印每只股票的指标数据")

    # 全市场扫描时（非指定代码）剔除科创板/北交所
    restricted_count = sum(1 for c in stock_pool if _is_restricted(c))
    if restricted_count > 0:
        filtered_pool = [c for c in stock_pool if not _is_restricted(c)]
        logger.info(f"全市场过滤：原 {len(stock_pool)} 只，剔除受限板块 {restricted_count} 只，剩余 {len(filtered_pool)} 只")
        stock_pool = filtered_pool

    name_map = get_stock_name_map()

    all_buy_signals = []
    all_sell_signals = []
    total_scanned = 0
    total_with_data = 0
    # —— 新增：MACD 预测金叉各条件的累计统计（用于调试"信号太少"）——
    from collections import defaultdict
    _diag_strategy = None
    _diag_stats = None
    _diag_price_filtered = 0
    _diag_data_short = 0
    if strategy_names and any('MACD预测金叉' in s for s in strategy_names):
        from signals.macd_strategy import MACDPredictiveCrossStrategy
        _diag_strategy = MACDPredictiveCrossStrategy()
        _diag_stats = defaultdict(lambda: {'total': 0, 'ok': 0})
    start_time = datetime.now()

    for code in stock_pool:
        total_scanned += 1
        normalized_code = normalize_code(code)
        stock_name = name_map.get(normalized_code, '')

        if total_scanned % 200 == 0 and not verbose:
            elapsed = (datetime.now() - start_time).total_seconds()
            _print_progress(
                total_scanned, len(stock_pool),
                len(all_buy_signals), len(all_sell_signals), elapsed
            )

        df = build_historical_daily_df(normalized_code, scan_date)
        if df.empty or len(df) < 35:
            if verbose:
                logger.info(f"[数据] {normalized_code} {stock_name} K线={len(df)} 根 < 35，跳过")
            continue

        price = float(df['close'].iloc[-1])
        if price <= 0 or price < min_price or price > max_price:
            if verbose:
                logger.info(f"[数据] {normalized_code} {stock_name} 价格={price:.2f} 超出 {min_price}-{max_price} 范围")
            continue

        total_with_data += 1

        # ---------- 预计算指标（用于 diagnose_last_row 和信号生成，只算一次） ----------
        df_precalc = None
        if _diag_stats is not None:
            try:
                from indicators.mytt_indicators import calc_all_indicators
                df_precalc = calc_all_indicators(df.copy())
                # 诊断：MACD 预测金叉各条件累计统计
                diag = _diag_strategy.diagnose_last_row(df_precalc)
                for c in diag.get('conditions', []):
                    name = c['name']
                    _diag_stats[name]['total'] += 1
                    if c.get('ok'):
                        _diag_stats[name]['ok'] += 1
            except Exception as e:
                logger.debug(f"diagnose {normalized_code} 失败: {e}")
                df_precalc = None
        # ---------------------------------------------------------

        if verbose:
            logger.info(f"[数据] {normalized_code} {stock_name} K线={len(df)} 根 价格={price:.2f}")

        try:
            signals = generate_latest_daily_signal(
                df, normalized_code, strategy_names, check_volume=check_volume,
                check_last_row=True, historical_mode=True,
                verbose=verbose,
                _df_precalc=df_precalc,
            )
        except Exception as e:
            if verbose:
                logger.error(f"[数据] {normalized_code} {stock_name} 信号计算失败: {e}")
            else:
                logger.debug(f"{code} 信号计算失败: {e}")
            continue

        # —— 历史模式：信号日收盘确认，次日开盘买入 ——
        if signals:
            next_open = get_next_trading_day_open(normalized_code, scan_date)
            if next_open is not None and next_open > 0:
                for sig in signals:
                    sig.price = next_open
            else:
                # 下一交易日无数据（如 scan_date 是数据库最新一天），保留原收盘价并提示
                if verbose:
                    logger.debug(f"  {normalized_code} 无法获取 {scan_date} 下一交易日开盘价，保留收盘价作为信号价")

        if verbose and not signals:
            logger.info(f"  └─ 无信号输出（各策略详细诊断信息已打印在上方）")

        for sig in signals:
            if signal_type in ('buy', 'all') and sig.signal_type == 1:
                all_buy_signals.append(sig)
                _print_signal(sig, name_map, verbose=verbose)
            if signal_type in ('sell', 'all') and sig.signal_type == -1:
                all_sell_signals.append(sig)
                _print_signal(sig, name_map, verbose=verbose)

    all_signals = all_buy_signals + all_sell_signals

    total_elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info(f"[历史扫描 {scan_date}] 完成，总耗时: {total_elapsed:.0f}s")
    logger.info(f"扫描股票: {total_scanned} 只  有效数据: {total_with_data} 只")
    logger.info(f"买入信号: {len(all_buy_signals)} 个  卖出信号: {len(all_sell_signals)} 个")

    # ---------- MACD 预测金叉：打印各条件累计统计 ----------
    if _diag_stats is not None and total_with_data > 0:
        logger.info("-" * 60)
        logger.info(f"【MACD 预测金叉 · 各条件通过率】样本数: {total_with_data}")
        logger.info(f"  说明：✅ 满足；❌ 不满足。total/ok 分别表示“被统计的股票数/满足该条件的股票数”。")
        if not _diag_stats:
            logger.info("  （未统计到任何条件，可能 df 中没有足够的 MACD/MA 指标）")
        else:
            for name, v in _diag_stats.items():
                pct = v['ok'] / v['total'] * 100 if v['total'] else 0
                bar = '█' * int(pct / 5) + '·' * (20 - int(pct / 5))
                logger.info(f"  [{'✅' if pct > 50 else '❌'}] {name:<35s}  ok={v['ok']:>5}/total={v['total']:>5} ({pct:>5.1f}%)  {bar}")
    # -----------------------------------------------------------

    if all_buy_signals:
        logger.info("-" * 40)
        logger.info(f"【买入信号列表 {scan_date}】共 {len(all_buy_signals)} 个")
        all_buy_signals.sort(key=lambda x: x.signal_strength or 0, reverse=True)
        for i, sig in enumerate(all_buy_signals[:50], 1):
            logger.info(
                f"  {i:2d}. {sig.code} {name_map.get(sig.code, '')} {sig.strategy} "
                f"价格={sig.price:.2f} 强度={sig.signal_strength} 原因={sig.reason}"
            )
            if verbose:
                _print_signal(sig, name_map, verbose=True)
        if len(all_buy_signals) > 50:
            logger.info(f"  ... 还有 {len(all_buy_signals) - 50} 个")

    if all_sell_signals:
        logger.info("-" * 40)
        logger.info(f"【卖出信号列表 {scan_date}】共 {len(all_sell_signals)} 个")
        all_sell_signals.sort(key=lambda x: x.signal_strength or 0, reverse=True)
        for i, sig in enumerate(all_sell_signals[:50], 1):
            logger.info(
                f"  {i:2d}. {sig.code} {name_map.get(sig.code, '')} {sig.strategy} "
                f"价格={sig.price:.2f} 强度={sig.signal_strength} 原因={sig.reason}"
            )
            if verbose:
                _print_signal(sig, name_map, verbose=True)
        if len(all_sell_signals) > 50:
            logger.info(f"  ... 还有 {len(all_sell_signals) - 50} 个")

    logger.info("=" * 60)

    if save and all_signals:
        save_signals(all_signals)
        logger.info(f"已保存 {len(all_signals)} 个信号到数据库")

    return all_signals
