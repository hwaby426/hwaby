import sys
import click
from loguru import logger

from config.settings import settings

logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>", level="INFO")
logger.add(settings.LOG_DIR / "stock_{time:YYYYMMDD}.log", rotation="00:00", retention="30 days", encoding="utf-8")


@click.group()
def cli():
    """A股买卖点检测系统"""
    pass


@cli.command()
@click.option('--stock-pool', default=None, help='股票池，逗号分隔')
@click.option('--skip-min-kline', is_flag=True, default=False, help='跳过分钟K线获取')
def init(stock_pool, skip_min_kline):
    """初始化数据库并全量拉取历史数据（日线+5min+15min），支持Ctrl+C中断"""
    from db.init_db import init_db
    init_db()
    from scheduler.daily_job import daily_update_job
    from data_fetcher.sina_fetcher import get_minute_kline
    from data_fetcher.min_kline_service import save_min_klines, get_last_min_kline_time
    from data_fetcher.min_kline_builder import KlineBar
    from data_fetcher.baostock_fetcher import normalize_code
    from datetime import datetime

    pool = stock_pool.split(',') if stock_pool else None
    
    try:
        daily_update_job(stock_pool=pool)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，日线更新中断...")
        return

    # 获取分钟K线数据
    if not skip_min_kline:
        if pool is None:
            from config.settings import settings
            pool = settings.get_stock_pool()
        if pool:
            logger.info(f"开始获取分钟K线数据，股票池: {len(pool)} 只")
            try:
                for code in pool:
                    code = normalize_code(code)
                    for period in ['5min', '15min']:
                        scale = int(period.replace('min', ''))
                        df = get_minute_kline(code, scale=scale, datalen=200)
                        if df.empty:
                            logger.warning(f"获取 {code} {period} K线数据失败")
                            continue
                        bars = []
                        for _, row in df.iterrows():
                            try:
                                kline_time = datetime.strptime(row['kline_time'], '%Y-%m-%d %H:%M:%S')
                            except:
                                continue
                            bars.append(KlineBar(
                                code=code,
                                period=period,
                                kline_time=kline_time,
                                open=row['open'],
                                high=row['high'],
                                low=row['low'],
                                close=row['close'],
                                volume=row['volume'],
                                amount=row.get('amount', 0),
                                closed=True,
                            ))
                        if bars:
                            save_min_klines(code, period, bars)
                            logger.info(f"获取 {code} {period} K线数据成功，保存 {len(bars)} 条")
                logger.info("分钟K线数据获取完成")
            except KeyboardInterrupt:
                logger.info("收到 Ctrl+C，分钟K线获取中断...")


@cli.command()
@click.option('--stock-pool', default=None, help='股票池，逗号分隔')
def update_daily(stock_pool):
    """更新日线数据并计算信号，支持Ctrl+C中断"""
    from scheduler.daily_job import daily_update_job
    from config.settings import settings
    
    if stock_pool:
        pool = stock_pool.split(',')
    else:
        pool = settings.get_stock_pool()
    
    try:
        daily_update_job(stock_pool=pool)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，更新中断...")


@cli.command()
@click.option('--max-stocks', default=None, type=int, help='最多处理多少只（测试用）')
def init_market(max_stocks):
    """全市场日线数据初始化（从stock_info表读取股票列表）"""
    from data_fetcher.baostock_fetcher import init_market_all
    try:
        init_market_all(max_stocks=max_stocks)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，初始化中断...")


@cli.command()
def update_market():
    """全市场每日更新（日线数据+信号计算）"""
    from scheduler.daily_job import daily_update_market_all
    try:
        daily_update_market_all()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，更新中断...")


@cli.command()
def realtime():
    """启动实时盯盘"""
    import signal
    import threading
    from scheduler.realtime_job import RealtimeMonitor

    monitor = RealtimeMonitor()
    stop_event = threading.Event()

    def signal_handler(signum, frame):
        logger.info("收到停止信号，正在退出...")
        stop_event.set()
        monitor.stop()

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except (ValueError, OSError):
        pass

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，正在退出...")
        monitor.stop()
    logger.info("实时监控已完全退出")


@cli.command()
@click.option('--strategies', default=None, help='策略列表，逗号分隔，默认全部策略')
@click.option('--signal-type', default='buy', help='信号类型: buy/sell/all，默认buy')
@click.option('--min-price', default=2.0, type=float, help='最低价格过滤')
@click.option('--max-price', default=200.0, type=float, help='最高价格过滤')
@click.option('--no-save', is_flag=True, default=False, help='不保存到数据库，只打印')
@click.option('--date', 'scan_date', default=None, help='（单日期）指定扫描日期 YYYY-MM-DD，不指定则为盘中实时扫描')
@click.option('--start', 'start_date', default=None, help='（批量）起始日期 YYYY-MM-DD，配合 --end 批量扫描区间内的每个交易日')
@click.option('--end', 'end_date', default=None, help='（批量）结束日期 YYYY-MM-DD')
@click.option('--codes', default=None, help='指定股票代码，逗号分隔，如 --codes sh.600519,sz.000001。不指定则扫描全市场')
@click.option('--no-volume', is_flag=True, default=False, help='关闭 MACD金叉策略的成交量过滤（默认开启）')
def scan_market(strategies, signal_type, min_price, max_price, no_save, scan_date, start_date, end_date, codes, no_volume):
    """全市场日线信号扫描 —— 盘中扫描 / 指定单日期扫描 / 批量扫描区间内每个交易日

    用法：
      1) 盘中实时扫描：
           python main.py scan-market --strategies MACD预测金叉
      2) 单日期历史扫描：
           python main.py scan-market --strategies MACD预测金叉 --date 2026-07-07
      3) 批量扫描两个日期中每一天的预测（自动跳过非交易日）：
           python main.py scan-market --strategies MACD预测金叉 --start 2026-07-01 --end 2026-07-07
    """
    from scheduler.market_scan import scan_market_intraday

    strategy_list = strategies.split(',') if strategies else None
    code_list = [c.strip() for c in codes.split(',') if c.strip()] if codes else None

    # 批量模式：start + end 都传，自动枚举交易日并每天扫一次
    if start_date and end_date:
        from datetime import datetime, timedelta
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
            end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
        except ValueError:
            logger.error(f"日期格式错误: {start_date}/{end_date}，请使用 YYYY-MM-DD")
            return

        if start_dt > end_dt:
            logger.error(f"起始日期 {start_date} 晚于结束日期 {end_date}")
            return

        dates = []
        cur = start_dt
        while cur <= end_dt:
            # 只保留周一到周五（非周末）。更严格可以用实际交易日历，但这里够用
            if cur.weekday() < 5:
                dates.append(cur.strftime('%Y-%m-%d'))
            cur += timedelta(days=1)

        if not dates:
            logger.warning(f"区间 {start_date} ~ {end_date} 没有有效的周一到周五")
            return

        logger.info(f"=== 批量扫描 {len(dates)} 个交易日：{dates[0]} ~ {dates[-1]} ===")

        daily_summary = []
        total_signals = 0
        for i, d in enumerate(dates, 1):
            logger.info(f"—— [{i}/{len(dates)}] 扫描 {d} ——")
            signals = scan_market_intraday(
                strategy_names=strategy_list,
                signal_type=signal_type,
                min_price=min_price,
                max_price=max_price,
                save=not no_save,
                scan_date=d,
                codes=code_list,
                check_volume=not no_volume,
            )
            n_signals = len(signals) if isinstance(signals, list) else 0
            # signal_type: 1=buy, -1=sell
            n_buy = sum(1 for s in signals if (isinstance(s, dict) and s.get('signal_type') == 1)
                        or (hasattr(s, 'signal_type') and getattr(s, 'signal_type') == 1)) if isinstance(signals, list) else 0
            n_sell = n_signals - n_buy
            daily_summary.append((d, n_buy, n_sell))
            total_signals += n_signals

        # 汇总表
        logger.info("=" * 70)
        logger.info(f"【批量扫描汇总】{dates[0]} ~ {dates[-1]}，共 {len(dates)} 个交易日，总信号 {total_signals} 个")
        # 用表头 + 每日一行，对齐输出
        header = f"  {'日期':>12}  | {'买入':>6}  | {'卖出':>6}"
        logger.info(header)
        logger.info(f"  {'-'*12}  | {'-'*6}  | {'-'*6}")
        for d, n_buy, n_sell in daily_summary:
            logger.info(f"  {d:>12}  | {n_buy:>6}  | {n_sell:>6}")
        return

    # 单日期模式 / 盘中模式（原逻辑）
    if scan_date:
        from datetime import datetime
        try:
            datetime.strptime(scan_date, '%Y-%m-%d')
        except ValueError:
            logger.error(f"日期格式错误: {scan_date}，请使用 YYYY-MM-DD")
            return

    try:
        scan_market_intraday(
            strategy_names=strategy_list,
            signal_type=signal_type,
            min_price=min_price,
            max_price=max_price,
            save=not no_save,
            scan_date=scan_date,
            codes=code_list,
            check_volume=not no_volume,
        )
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，扫描中断...")


@cli.command()
def scheduler():
    """启动完整调度器（日线+实时）"""
    from scheduler.scheduler_service import run_scheduler
    run_scheduler()


@cli.command()
@click.option('--code', required=True, help='股票代码')
@click.option('--strategy', required=True, help='策略名称')
@click.option('--start', required=True, help='起始日期 YYYY-MM-DD')
@click.option('--end', required=True, help='结束日期 YYYY-MM-DD')
@click.option('--capital', default=100000, type=float, help='初始资金')
@click.option('--period', default='daily', help='K线周期: daily/5min/15min')
@click.option('--t0', is_flag=True, default=False, help='T+0模式：当天买入当天可卖出')
def backtest(code, strategy, start, end, capital, period, t0):
    """单只股票回测，支持T+0/T+1模式"""
    from datetime import datetime, timedelta
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_df, get_daily_kline_batch
    from data_fetcher.min_kline_service import get_min_kline_df
    from backtest.portfolio import run_single_backtest

    code = normalize_code(code)

    # 提前 70 天作为指标预热（MACD EMA 依赖历史数据）
    warmup_start = (datetime.strptime(start, '%Y-%m-%d').date() - timedelta(days=70)).strftime('%Y-%m-%d')

    if period == 'daily':
        df = get_daily_kline_df(code, start_date=warmup_start, end_date=end)
    else:
        # 分钟K线回测
        df = get_min_kline_df(code, period, start_date=start, end_date=end)

    if df.empty:
        logger.error(f"未找到 {code} 的 {period} K线数据")
        return

    # 分钟K线需要调整时间列名
    if period != 'daily' and 'kline_time' in df.columns:
        df = df.rename(columns={'kline_time': 'date'})

    result = run_single_backtest(df, code, strategy, initial_capital=capital, period=period, t0=t0, trade_start_date=start)
    _print_backtest_result(result, t0=t0)


@cli.command()
@click.option('--codes', required=True, help='股票代码列表，逗号分隔')
@click.option('--strategy', required=True, help='策略名称')
@click.option('--start', required=True, help='起始日期')
@click.option('--end', required=True, help='结束日期')
@click.option('--capital', default=100000, type=float, help='初始资金')
@click.option('--t0', is_flag=True, default=False, help='T+0模式：当天买入当天可卖出')
def backtest_portfolio(codes, strategy, start, end, capital, t0):
    """多标的组合回测"""
    from datetime import datetime, timedelta
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_df, get_daily_kline_batch
    from backtest.portfolio import run_portfolio_backtest

    code_list = [normalize_code(c.strip()) for c in codes.split(',') if c.strip()]

    # 提前 70 天作为指标预热（MACD EMA 依赖历史数据）
    warmup_start = (datetime.strptime(start, '%Y-%m-%d').date() - timedelta(days=70)).strftime('%Y-%m-%d')
    data_dict = get_daily_kline_batch(code_list, start_date=warmup_start, end_date=end)
    if not data_dict:
        logger.error("无有效K线数据")
        return
    result = run_portfolio_backtest(data_dict, strategy, initial_capital=capital, t0=t0, trade_start_date=start)
    logger.info(f"组合回测结果 [{mode_str(t0)}]: 总收益={result.total_return}% 年化={result.annual_return}% "
                f"最大回撤={result.max_drawdown}% 夏普={result.sharpe_ratio} "
                f"胜率={result.win_rate}% 交易次数={result.total_trades}")


@cli.command()
@click.option('--date', default=None, help='日期 YYYY-MM-DD，默认今天')
@click.option('--type', 'signal_type', default='all', help='信号类型 buy/sell/all')
@click.option('--min-strength', default=0, type=float, help='最小信号强度')
@click.option('--limit', default=50, type=int, help='最大条数')
def scan(date, signal_type, min_strength, limit):
    """扫描日线买卖信号"""
    from analysis.scanner import scan_signals, print_signal_report

    st = None
    if signal_type == 'buy':
        st = 1
    elif signal_type == 'sell':
        st = -1

    df = scan_signals(
        trade_date=date,
        period='daily',
        signal_type=st,
        min_strength=min_strength,
        limit=limit,
    )
    title = f"日线信号扫描 ({date or '今天'})"
    print_signal_report(df, title=title)


@cli.command()
@click.option('--date', default=None, help='日期')
@click.option('--type', 'signal_type', default='buy', help='buy/sell')
@click.option('--min-strength', default=50, type=float, help='最小强度')
def resonance(date, signal_type, min_strength):
    """多周期共振扫描"""
    from analysis.scanner import multi_period_resonance, print_signal_report

    st = 1 if signal_type == 'buy' else -1
    df = multi_period_resonance(trade_date=date, signal_type=st, min_strength=min_strength)
    title = f"多周期共振 {'买入' if st == 1 else '卖出'}信号 ({date or '今天'})"
    print_signal_report(df, title=title)


@cli.command()
@click.option('--code', required=True, help='股票代码')
@click.option('--period', default='5min', help='K线周期: 5min/15min/30min/60min')
@click.option('--datalen', default=1023, type=int, help='数据条数，最大1023')
def fetch_min_kline(code, period, datalen):
    """手动获取分钟K线数据并保存到数据库"""
    from data_fetcher.sina_fetcher import get_minute_kline
    from data_fetcher.min_kline_service import save_min_klines, get_last_min_kline_time
    from data_fetcher.min_kline_builder import KlineBar
    from data_fetcher.baostock_fetcher import normalize_code
    from datetime import datetime

    code = normalize_code(code)
    scale = int(period.replace('min', ''))

    last_time = get_last_min_kline_time(code, period)
    if last_time:
        logger.info(f"数据库已有 {code} {period} 数据，最后时间: {last_time}")
    else:
        logger.info(f"数据库中无 {code} {period} 数据，首次拉取")

    logger.info(f"开始获取 {code} {period} K线数据，条数={datalen}")
    df = get_minute_kline(code, scale=scale, datalen=datalen)

    if df.empty:
        logger.error(f"获取 {code} {period} K线数据失败")
        return

    # 转换为 KlineBar 列表
    bars = []
    new_count = 0
    for _, row in df.iterrows():
        try:
            kline_time = datetime.strptime(row['kline_time'], '%Y-%m-%d %H:%M:%S')
        except:
            continue
        # 增量判断：只保留比最后一条时间新的数据
        if last_time:
            last_dt = datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')
            if kline_time <= last_dt:
                continue
            new_count += 1
        bar = KlineBar(
            code=code,
            period=period,
            kline_time=kline_time,
            open=row['open'],
            high=row['high'],
            low=row['low'],
            close=row['close'],
            volume=row['volume'],
            amount=row.get('amount', 0),
            closed=True,
        )
        bars.append(bar)

    if not bars:
        logger.info(f"没有新的 {period} K线数据需要更新")
        return

    # 保存到数据库
    save_min_klines(code, period, bars)
    if last_time:
        logger.info(f"成功新增 {len(bars)} 条 {period} K线数据（跳过 {len(df) - len(bars)} 条已有数据）")
    else:
        logger.info(f"成功保存 {len(bars)} 条 {period} K线数据")


@cli.command()
@click.option('--date', default=None, help='目标日期 YYYY-MM-DD，默认今天（往前找最近交易日）')
def update_stock_list(date):
    """更新全A股票列表（过滤ETF、指数、ST）"""
    from data_fetcher.stock_pool import update_stock_info
    count = update_stock_info(target_date=date)
    logger.info(f"更新完成，共 {count} 只股票")


@cli.command()
@click.option('--market', default=None, help='市场: sh/sz，默认全部')
@click.option('--limit', default=None, type=int, help='限制数量')
def stock_pool(market, limit):
    """查看股票池列表"""
    from data_fetcher.stock_pool import get_stock_pool_from_db
    codes = get_stock_pool_from_db(market=market, limit=limit)
    logger.info(f"股票池共 {len(codes)} 只")
    for code in codes[:20]:
        logger.info(f"  - {code}")
    if len(codes) > 20:
        logger.info(f"  ... 还有 {len(codes) - 20} 只")


@cli.command()
def init_db():
    """仅初始化数据库表结构"""
    from db.init_db import init_db
    init_db()


@cli.command()
def strategies():
    """列出所有可用策略"""
    from signals.manager import get_strategy_names
    names = get_strategy_names()
    logger.info(f"可用策略 ({len(names)} 个):")
    for name in names:
        logger.info(f"  - {name}")


@cli.command()
@click.option('--code', default=None, help='股票代码，不传则使用配置的股票池')
@click.option('--strategies', default=None, help='策略列表，逗号分隔，默认全部策略')
@click.option('--start', required=True, help='起始日期 YYYY-MM-DD')
@click.option('--end', default=None, help='结束日期 YYYY-MM-DD，默认今天')
@click.option('--capital', default=100000, type=float, help='初始资金')
@click.option('--period', default='daily', help='K线周期: daily/5min/15min')
@click.option('--sort-by', default='total_return', help='排序指标: total_return/annual_return/sharpe_ratio/win_rate/profit_factor')
@click.option('--t0', is_flag=True, default=False, help='T+0模式：当天买入当天可卖出')
@click.option('--no-volume', is_flag=True, default=False, help='关闭 MACD金叉 策略的成交量过滤（默认开启，要求成交量 >= 1.25 x 5日均量）')
def compare(code, strategies, start, end, capital, period, sort_by, t0, no_volume):
    """单只股票/多只股票 多策略对比回测"""
    from datetime import datetime, timedelta
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_df, get_daily_kline_batch
    from data_fetcher.min_kline_service import get_min_kline_df
    from analysis.strategy_compare import run_strategy_compare, print_compare_table
    from config.settings import settings

    if end is None:
        end = datetime.now().strftime('%Y-%m-%d')

    strategy_list = strategies.split(',') if strategies else None

    if code:
        code_list = [normalize_code(c.strip()) for c in code.split(',') if c.strip()]
    else:
        code_list = settings.get_stock_pool()
        logger.info(f"未指定股票，使用配置股票池 ({len(code_list)} 只)")

    # 提前 70 天作为指标预热（MACD EMA 依赖历史数据）
    warmup_start = (datetime.strptime(start, '%Y-%m-%d').date() - timedelta(days=70)).strftime('%Y-%m-%d')

    for c in code_list:
        c = normalize_code(c)

        if period == 'daily':
            df = get_daily_kline_df(c, start_date=warmup_start, end_date=end)
        else:
            df = get_min_kline_df(c, period, start_date=start, end_date=end)

        if df.empty:
            logger.warning(f"未找到 {c} 的 {period} K线数据，跳过")
            continue

        if period != 'daily' and 'kline_time' in df.columns:
            df = df.rename(columns={'kline_time': 'date'})

        result = run_strategy_compare(df, c, strategy_names=strategy_list,
                                      initial_capital=capital, period=period, t0=t0,
                                      trade_start_date=start,
                                      check_volume=not no_volume)
        print_compare_table(result, sort_by=sort_by)
        logger.info("")


@cli.command('compare-all')
@click.option('--codes', default=None, help='股票代码列表，逗号分隔。不指定则使用配置股票池')
@click.option('--all-code', is_flag=True, default=False, help='使用全市场所有股票（覆盖--codes和配置池）')
@click.option('--strategies', default=None, help='策略列表，逗号分隔，默认全部策略')
@click.option('--start', required=True, help='起始日期 YYYY-MM-DD')
@click.option('--end', required=True, help='结束日期 YYYY-MM-DD')
@click.option('--capital', default=100000, type=float, help='初始资金')
@click.option('--period', default='daily')
@click.option('--top-n', default=3, type=int, help='推荐策略数量')
@click.option('--max-stocks', default=None, type=int, help='最多处理多少只股票（测试用）')
@click.option('--t0', is_flag=True, default=False, help='T+0模式：当天买入当天可卖出')
@click.option('--no-volume', is_flag=True, default=False, help='关闭 MACD金叉策略的成交量过滤（默认开启）')
def compare_all(codes, all_code, strategies, start, end, capital, period, top_n, max_stocks, t0, no_volume):
    """多股票多策略综合对比 + 策略推荐"""
    from datetime import datetime, timedelta
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_batch
    from data_fetcher.stock_pool import get_stock_pool_from_db
    from config.settings import settings
    from analysis.strategy_compare import (
        run_multi_stock_compare, print_multi_compare_table,
        print_stock_returns, recommend_strategies
    )

    # 1. 确定股票代码列表
    if all_code:
        raw_codes = get_stock_pool_from_db()
        code_list = [normalize_code(c) for c in raw_codes]
        logger.info(f"全市场模式: {len(code_list)} 只股票")
    elif codes:
        code_list = [normalize_code(c.strip()) for c in codes.split(',') if c.strip()]
        logger.info(f"使用指定股票: {len(code_list)} 只")
    else:
        # 默认使用配置中的股票池
        pool_from_settings = settings.get_stock_pool()
        if pool_from_settings:
            code_list = [normalize_code(c.strip()) for c in pool_from_settings]
            logger.info(f"使用配置股票池: {len(code_list)} 只")
        else:
            raw_codes = get_stock_pool_from_db()
            code_list = [normalize_code(c) for c in raw_codes]
            logger.info(f"配置池为空，使用全市场股票: {len(code_list)} 只")

    # 2. 测试限制
    if max_stocks:
        code_list = code_list[:max_stocks]
        logger.info(f"测试模式：只处理前 {max_stocks} 只")

    strategy_list = strategies.split(',') if strategies else None

    # 3. 批量拉取数据 —— 提前 70 天作为指标预热（MACD EMA 依赖历史数据）
    #    不加预热会导致不同 start_date 下信号不一致
    warmup_start = (datetime.strptime(start, '%Y-%m-%d').date() - timedelta(days=70)).strftime('%Y-%m-%d')
    data_dict = get_daily_kline_batch(code_list, start_date=warmup_start, end_date=end)
    skipped = len(code_list) - len(data_dict)

    if skipped > 0:
        logger.warning(f"跳过 {skipped} 只股票：无K线数据")

    if not data_dict:
        logger.error("无有效K线数据")
        return

    logger.info(f"参与对比股票: {len(data_dict)}只, 策略: {len(strategy_list) if strategy_list else '全部'}")
    logger.info(f"MACD 成交量过滤: {'关闭' if no_volume else '开启'}")

    compare_df, detail_df = run_multi_stock_compare(
        data_dict, strategy_names=strategy_list,
        initial_capital=capital, period=period,
        t0=t0,
        trade_start_date=start,
        check_volume=not no_volume,
    )

    print_multi_compare_table(compare_df, title=f"多股票策略综合对比 ({len(data_dict)}只股票) [{mode_str(t0)}]")

    print_stock_returns(detail_df, title="每只股票收益明细")

    recommendations = recommend_strategies(compare_df, top_n=top_n)
    if recommendations:
        logger.info("")
        logger.info(f"=== 综合推荐 Top {top_n} [{mode_str(t0)}] ===")
        for rec in recommendations:
            logger.info(
                f"  {rec['rank']}. {rec['strategy']} (综合分: {rec['score']}) "
                f"| 平均收益: {rec['avg_return']}% | 夏普: {rec['avg_sharpe']} "
                f"| 盈利占比: {rec['win_ratio']}% | 平均胜率: {rec['avg_winrate']}%"
            )


def mode_str(t0: bool) -> str:
    """返回交易模式字符串"""
    return 'T+0' if t0 else 'T+1'


def _print_backtest_result(result, t0=False):
    logger.info("=" * 60)
    logger.info(f"回测结果 [{mode_str(t0)}] - {result.code} | {result.strategy} | {result.period}")
    logger.info(f"区间: {result.start_date} ~ {result.end_date}")
    logger.info("-" * 60)
    logger.info(f"初始资金: {result.initial_capital:,.2f}")
    logger.info(f"最终资金: {result.final_capital:,.2f}")
    logger.info(f"总收益率: {result.total_return}%")
    logger.info(f"年化收益: {result.annual_return}%")
    logger.info(f"最大回撤: {result.max_drawdown}%")
    logger.info(f"夏普比率: {result.sharpe_ratio}")
    logger.info(f"胜率:     {result.win_rate}%")
    logger.info(f"交易次数: {result.total_trades}")
    logger.info(f"盈亏比:   {result.profit_factor}")
    logger.info("=" * 60)



@cli.command('analyze-macd')
@click.option('--days', default=10, type=int, help='分析最近多少个交易日的买入信号，默认10')
@click.option('--codes', default=None, help='股票代码列表，逗号分隔。默认全市场')
@click.option('--all-code', is_flag=True, default=False, help='使用全市场所有股票（覆盖--codes）')
@click.option('--max-print', default=500, type=int, help='表格最多打印多少条，默认500')
@click.option('--end', 'end_date', default=None, help='分析截止日期 YYYY-MM-DD，默认今天')
@click.option('--no-volume', is_flag=True, default=False, help='关闭 MACD金叉策略的成交量过滤（默认开启）')
def analyze_macd_signals(days, codes, all_code, max_print, end_date, no_volume):
    """分析最近N天MACD金叉买入信号的收益情况"""
    from data_fetcher.baostock_fetcher import normalize_code
    from data_fetcher.stock_pool import get_stock_pool_from_db, get_stock_name_map
    from analysis.strategy_compare import analyze_recent_macd_signals

    # 确定股票代码
    if all_code:
        raw_codes = get_stock_pool_from_db()
        code_list = [normalize_code(c) for c in raw_codes]
    elif codes:
        code_list = [normalize_code(c.strip()) for c in codes.split(',') if c.strip()]
    else:
        pool = get_stock_pool_from_db()
        code_list = [normalize_code(c) for c in pool]

    # 名称映射（归一化格式 sh.600000）
    raw_map = get_stock_name_map()
    name_map = {normalize_code(k): v for k, v in raw_map.items()}

    analyze_recent_macd_signals(codes=code_list, days=days, name_map=name_map, max_print=max_print, end_date=end_date, check_volume=not no_volume)


@cli.command('macd-intraday-pnl-from-db')
@click.option('--start', default=None, help='起始日期 YYYY-MM-DD（筛选信号：signal_time >= start）')
@click.option('--end', default=None, help='结束日期 YYYY-MM-DD（筛选信号 + 持有到此日收盘卖出）')
@click.option('--codes', default=None, help='指定股票代码，逗号分隔。不指定则全部')
def macd_intraday_pnl_from_db(start, end, codes):
    """MACD 买入信号：信号日开盘买入 → 一直持有到 --end 日收盘卖出。"""
    from signals.signal_service import calc_hold_pnl_from_trade_signals
    from data_fetcher.stock_pool import get_stock_name_map

    code_list = [c.strip() for c in codes.split(',') if c.strip()] if codes else None

    items = calc_hold_pnl_from_trade_signals(
        strategy='MACD金叉',
        start_date=start,
        end_date=end,
        codes=code_list,
    )
    if not items:
        logger.info("没有可用于统计的信号")
        return

    # 简要统计
    positive = sum(1 for x in items if x['pnl_pct'] > 0)
    negative = sum(1 for x in items if x['pnl_pct'] < 0)
    zero = len(items) - positive - negative
    avg_pct = sum(x['pnl_pct'] for x in items) / len(items)
    max_pct = max(x['pnl_pct'] for x in items)
    min_pct = min(x['pnl_pct'] for x in items)

    logger.info("=" * 70)
    logger.info(
        f"共 {len(items)} 笔 MACD 买入信号（信号日下一交易日 open 买入 → {end or '最新'} close 卖出）"
    )
    logger.info(
        f"  盈利 {positive} / 亏损 {negative} / 持平 {zero} · 平均 {avg_pct:.2f}% "
        f"· 最高 {max_pct:.2f}% · 最低 {min_pct:.2f}%"
    )
    logger.info("-" * 70)

    name_map = get_stock_name_map()
    items_sorted = sorted(items, key=lambda x: x['pnl_pct'], reverse=True)

    for i, it in enumerate(items_sorted, 1):
        flag = '+' if it['pnl_pct'] > 0 else ('-' if it['pnl_pct'] < 0 else '=')
        name = name_map.get(it['code'], '')[:8]
        logger.info(
            f"{i:4d}. {it['buy_date']}→{it['sell_date']}({it['hold_days']:>3d}日) "
            f"{it['code']} {name} "
            f"buy={it['buy_price']:.3f} sell={it['sell_price']:.3f} "
            f"{flag}{abs(it['pnl_amount']):+.3f}  ({flag}{abs(it['pnl_pct']):.2f}%)"
        )

    logger.info("=" * 70)


@cli.command('verify-macd-prediction')
@click.option('--start', 'start_date', required=True, help='信号检查起始日期 YYYY-MM-DD（即 scan-market --date 的日期）')
@click.option('--end', 'end_date', default=None, help='（可选）信号检查结束日期 YYYY-MM-DD，不传则只检查 start 单日')
@click.option('--codes', default=None, help='股票代码列表，逗号分隔。不传则验证所有在该日有信号的股票')
@click.option('--window', 'check_window', default=5, type=int, help='向后检查多少个交易日内是否出现金叉/最高收益，默认 5')
@click.option('--max-print', default=500, type=int, help='单日明细最多打印多少行，默认 500')
def verify_macd_prediction(start_date, end_date, codes, check_window, max_print):
    """验证 MACD 预测金叉信号：从 trade_signals 读取指定日期信号，检查后续 N 天是否出现真正金叉，
    并统计窗口内最高价/最高收盘价收益出现的位置。"""
    from datetime import datetime, timedelta
    from data_fetcher.stock_pool import get_stock_name_map
    from analysis.strategy_compare import verify_macd_predictive_signals

    code_list = [c.strip() for c in codes.split(',') if c.strip()] if codes else None
    name_map = get_stock_name_map()

    # 枚举日期范围：只保留工作日（周一至周五）
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
    if end_date:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
    else:
        end_dt = start_dt

    if end_dt < start_dt:
        logger.error(f"起始日期 {start_date} 晚于结束日期 {end_date}")
        return

    dates = []
    cur = start_dt
    while cur <= end_dt:
        if cur.weekday() < 5:
            dates.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)

    if not dates:
        logger.warning(f"区间 {start_date} ~ {end_date} 没有有效的周一到周五")
        return

    # 多日范围：逐天跑一次，同时汇总「第几天收益最高」&「每日平均收益」
    if len(dates) > 1:
        logger.info("=" * 70)
        logger.info(f"批量验证 MACD 预测金叉：{dates[0]} ~ {dates[-1]} 共 {len(dates)} 个交易日 (window={check_window})")
        logger.info("=" * 70)

        # 用于整体汇总
        total_high_day_counts = {k: 0 for k in range(1, check_window + 1)}
        total_close_day_counts = {k: 0 for k in range(1, check_window + 1)}
        total_daily_ret = {k: [] for k in range(1, check_window + 1)}
        daily_summary = []
        total_signals = 0

        # 用 rich 表格打印每日期的窗口内收益汇总（每个日期一行）
        from rich.console import Console as _Console
        from rich.table import Table as _Table
        _console = _Console()

        for d in dates:
            logger.info(f"—— 验证 {d} ——")
            df = verify_macd_predictive_signals(
                codes=code_list,
                check_date=d,
                check_window=check_window,
                name_map=name_map,
                max_print=max_print,
                # 在范围模式下让单日内的打印更安静
                _batch_mode=True,
            )
            if df is None or df.empty:
                # 空日期仍然记录，避免表格行数错
                row_map = {
                    'date': d, 'signals': 0, 'avg_high': float('nan'),
                    'avg_close': float('nan'),
                }
                for k in range(1, check_window + 1):
                    row_map[f'day{k}'] = float('nan')
                    row_map[f'high_day{k}_cnt'] = 0
                    row_map[f'close_day{k}_cnt'] = 0
                daily_summary.append(row_map)
                continue

            valid_high = df[df['max_high_day'].notna() & (df['max_high_day'] != '')]
            valid_close = df[df['max_close_day'].notna() & (df['max_close_day'] != '')]

            # 统计 "第几天收益最高" 的分布（用 k≥2 的版本，实际上窗口里 max_high_day >=2，
            # 若 window=1 可能为空）
            high_day_cnt = {k: 0 for k in range(1, check_window + 1)}
            close_day_cnt = {k: 0 for k in range(1, check_window + 1)}
            for _, r in valid_high.iterrows():
                try:
                    k = int(r['max_high_day'])
                    if 1 <= k <= check_window:
                        total_high_day_counts[k] += 1
                        high_day_cnt[k] += 1
                except Exception:
                    pass
            for _, r in valid_close.iterrows():
                try:
                    k = int(r['max_close_day'])
                    if 1 <= k <= check_window:
                        total_close_day_counts[k] += 1
                        close_day_cnt[k] += 1
                except Exception:
                    pass

            # 每日平均收益（用收盘价）
            day_close_avg = {}
            for k in range(1, check_window + 1):
                col = f'ret_{k}d_pct'
                vals = df[col].dropna()
                if not vals.empty:
                    total_daily_ret[k].extend(vals.tolist())
                    day_close_avg[k] = vals.mean()
                else:
                    day_close_avg[k] = float('nan')

            day_total = len(df)
            day_avg_high = valid_high['max_high_pct'].mean() if not valid_high.empty else float('nan')
            day_avg_close = valid_close['max_close_pct'].mean() if not valid_close.empty else float('nan')

            # 当天最高收益出现最频繁的 day
            def _top_day(cnt_dict):
                best_k, best_c = None, 0
                for kk, cc in cnt_dict.items():
                    if cc > best_c:
                        best_k, best_c = kk, cc
                return best_k

            row_map = {
                'date': d, 'signals': day_total,
                'avg_high': day_avg_high, 'avg_close': day_avg_close,
                'top_high_day': _top_day(high_day_cnt),
                'top_close_day': _top_day(close_day_cnt),
            }
            for k in range(1, check_window + 1):
                row_map[f'day{k}'] = day_close_avg[k]
                row_map[f'high_day{k}_cnt'] = high_day_cnt[k]
                row_map[f'close_day{k}_cnt'] = close_day_cnt[k]
            daily_summary.append(row_map)
            total_signals += day_total

        # —— 打印整体汇总 ——
        logger.info("=" * 70)
        logger.info(f"【范围验证汇总】{dates[0]} ~ {dates[-1]} 共 {len(dates)} 个交易日，累计信号 {total_signals} 条")

        # 表格 1：每日信号数 + 每日收盘平均收益（1..window 日） + 窗口内最高收益平均
        title1 = (f"每日明细 · 信号数 & 每日收盘平均收益（相对信号日）& 窗口内最高收益平均 "
                  f"(k≥2)")
        table1 = _Table(title=title1, show_lines=False)
        table1.add_column("日期", justify="center", style="cyan")
        table1.add_column("信号数", justify="right")
        for k in range(1, check_window + 1):
            table1.add_column(f"{k}日%", justify="right")
        table1.add_column("最高(high)", justify="right", style="bold yellow")
        table1.add_column("最高(close)", justify="right", style="bold green")
        for row in daily_summary:
            cells = [row['date'], str(row['signals'])]
            for k in range(1, check_window + 1):
                v = row[f'day{k}']
                cells.append(f"{v:.2f}" if v == v else "—")  # NaN == NaN is False
            ah = row['avg_high']
            ac = row['avg_close']
            cells.append(f"{ah:.2f}%" if ah == ah else "—")
            cells.append(f"{ac:.2f}%" if ac == ac else "—")
            table1.add_row(*cells)
        _console.print(table1)

        # 表格 2：每日最高收益出现在第几天的分布（每日期一行，展示 high/close 各 day 的样本数）
        title2 = f"每日最高收益出现位置分布（仅 k≥2，high / close 各 day 样本数）"
        table2 = _Table(title=title2, show_lines=False)
        table2.add_column("日期", justify="center", style="cyan")
        table2.add_column("信号数", justify="right")
        for k in range(2, check_window + 1):
            table2.add_column(f"H-{k}日", justify="right")
        for k in range(2, check_window + 1):
            table2.add_column(f"C-{k}日", justify="right")
        table2.add_column("high最常", justify="center", style="bold yellow")
        table2.add_column("close最常", justify="center", style="bold green")
        for row in daily_summary:
            cells = [row['date'], str(row['signals'])]
            for k in range(2, check_window + 1):
                cells.append(str(row[f'high_day{k}_cnt']))
            for k in range(2, check_window + 1):
                cells.append(str(row[f'close_day{k}_cnt']))
            cells.append(f"第{row['top_high_day']}天" if row.get('top_high_day') else "—")
            cells.append(f"第{row['top_close_day']}天" if row.get('top_close_day') else "—")
            table2.add_row(*cells)
        _console.print(table2)

        # 第几天收益最高分布（整体汇总）
        logger.info("—— 汇总：窗口内最高收益出现在第几天（仅统计 k≥2 的卖出日）——")
        def _print_distribution(title, counts):
            total_c = sum(counts.values())
            if total_c == 0:
                logger.info(f"  {title}: 无可用数据")
                return
            # 找出现最多的 day
            most_day = max(counts, key=lambda k: counts[k])
            logger.info(f"  {title}: 共 {total_c} 条，最常出现在第 {most_day} 天 "
                        f"(占比 {counts[most_day] / total_c * 100:.1f}%)")
            # 柱状表示
            parts = []
            for k in range(2, check_window + 1):
                c = counts.get(k, 0)
                pct = c / total_c * 100
                bar = '█' * int(pct / 2)
                parts.append(f"第{k}天={c}({pct:.1f}%){bar}")
            logger.info("    " + "  ".join(parts))

        _print_distribution("最高(high)", total_high_day_counts)
        _print_distribution("最高(close)", total_close_day_counts)

        # 每日平均收盘收益走势（整体）
        logger.info("—— 汇总：每日收盘平均收益（相对信号日）——")
        avg_line_parts = []
        for k in range(1, check_window + 1):
            vals = total_daily_ret[k]
            if vals:
                avg = sum(vals) / len(vals)
                avg_line_parts.append(f"{k}日={avg:.2f}% (n={len(vals)})")
            else:
                avg_line_parts.append(f"{k}日=—")
        logger.info("  " + "  |  ".join(avg_line_parts))

        logger.info("=" * 70)
        return

    # 单日模式：原逻辑
    verify_macd_predictive_signals(
        codes=code_list,
        check_date=start_date,
        check_window=check_window,
        name_map=name_map,
        max_print=max_print,
    )


if __name__ == '__main__':
    cli()
