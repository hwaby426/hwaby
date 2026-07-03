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
@click.option('--date', 'scan_date', default=None, help='指定扫描日期 YYYY-MM-DD，不指定则为盘中实时扫描')
@click.option('--codes', default=None, help='指定股票代码，逗号分隔，如 --codes sh.600519,sz.000001。不指定则扫描全市场')
@click.option('--no-volume', is_flag=True, default=False, help='关闭 MACD金叉策略的成交量过滤（默认开启）')
def scan_market(strategies, signal_type, min_price, max_price, no_save, scan_date, codes, no_volume):
    """全市场日线信号扫描 —— 盘中扫描或扫描指定日期(支持周末/节假日)"""
    from scheduler.market_scan import scan_market_intraday

    strategy_list = strategies.split(',') if strategies else None
    code_list = [c.strip() for c in codes.split(',') if c.strip()] if codes else None

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
def compare(code, strategies, start, end, capital, period, sort_by, t0):
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
                                      trade_start_date=start)
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


@cli.command('macd-predict')
@click.option('--date', 'signal_date', default=None, help='信号日 YYYY-MM-DD。不传=今天盘中扫描（实时行情）')
@click.option('--codes', default=None, help='指定股票代码，逗号分隔。不传=全市场扫描')
@click.option('--no-volume', is_flag=True, default=False, help='关闭成交量过滤（默认开启）')
@click.option('--forward-days', default=1, type=int, help='向后验证多少个交易日，默认5天。--no-verify 时无效')
@click.option('--no-verify', is_flag=True, default=False, help='只检测信号，不做后续金叉验证')
def macd_predict_cmd(signal_date, codes, no_volume, forward_days, no_verify):
    """【MACD预测金叉 · 专用命令】扫描 → 逐行打印信号 → 验证命中率。

    典型用法:
      python main.py macd-predict --date 2026-07-02              # 全市场 + 自动验证
      python main.py macd-predict --date 2026-07-02 --no-verify  # 只扫描不验证
      python main.py macd-predict --date 2026-07-02 --codes sh.600006,sh.600048  # 指定股票
      python main.py macd-predict                                 # 盘中实时扫描
    """
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_df
    from data_fetcher.stock_pool import get_stock_pool_from_db, get_stock_name_map
    from data_fetcher.sina_fetcher import get_realtime_quotes
    from scheduler.intraday_service import build_intraday_daily_df, build_historical_daily_df
    from indicators.mytt_indicators import calc_all_indicators
    from signals.manager import get_strategy
    from datetime import datetime, timedelta
    import time as _time

    # --- 确定扫描范围 ---
    if codes:
        pool = [normalize_code(c.strip()) for c in codes.split(',') if c.strip()]
    else:
        raw = get_stock_pool_from_db()
        pool = [normalize_code(c) for c in raw]
    if not pool:
        logger.error("股票池为空，先运行 update-stock-list")
        return

    name_map = get_stock_name_map()
    strategy = get_strategy('MACD预测金叉', check_volume=not no_volume)

    # --- 打印表头 ---
    logger.info("=" * 95)
    if signal_date:
        logger.info(f"  【MACD 预测金叉】历史扫描 — 信号日: {signal_date}  股票: {len(pool)} 只")
    else:
        logger.info(f"  【MACD 预测金叉】盘中扫描 — {datetime.now().strftime('%Y-%m-%d')}  股票: {len(pool)} 只")
    logger.info(f"  策略条件: 绿柱 + 连续2日增大 + 3日缩柱>=30% + DIF拐头向上 + 量>20日均量 + 需涨<5%即可金叉")
    logger.info(f"  成交量过滤: {'关闭' if no_volume else '开启'}   验证: {'关闭' if no_verify else str(forward_days) + '日'}")
    logger.info("=" * 95)

    # --- Phase 1: 逐只扫描（与 scan_market 相同的 [信号] 格式逐行打印）---
    all_signals = []
    total_scanned = 0
    start_time = datetime.now()

    intraday_mode = signal_date is None
    if intraday_mode:
        logger.info(f"  正在获取 {len(pool)} 只股票的实时行情...")
        realtime_map = {}
        batch_size = 200
        for bi in range(0, len(pool), batch_size):
            batch = pool[bi:bi + batch_size]
            try:
                qdf = get_realtime_quotes(batch)
                if qdf is not None and not qdf.empty:
                    for _, row in qdf.iterrows():
                        rc = str(row.get('code', ''))
                        price = float(row.get('price', 0))
                        if rc and price > 0:
                            realtime_map[rc] = price
            except Exception as e:
                logger.warning(f"  第{bi//batch_size+1}批实时行情获取失败: {e}")
                continue
            _time.sleep(0.2)
        logger.info(f"  实时行情: 成功 {len(realtime_map)} 只")

    for code in pool:
        total_scanned += 1
        try:
            if intraday_mode:
                price = realtime_map.get(code, 0)
                if price <= 0:
                    continue
                df = build_intraday_daily_df(code, {'code': code, 'price': price})
            else:
                df = build_historical_daily_df(code, signal_date)

            if df.empty or len(df) < 35:
                continue

            df_ind = calc_all_indicators(df)
            if df_ind.empty:
                continue

            last_idx = len(df_ind) - 1
            last = df_ind.iloc[last_idx]
            sig_series = strategy.generate_signals(df_ind)
            if int(sig_series.iloc[last_idx]) != 1:
                continue

            reason_val = str(strategy.calc_reason(df_ind, sig_series).iloc[last_idx] or '')
            strength_val = float(strategy.calc_strength(df_ind, sig_series).iloc[last_idx] or 0)
            price_val = float(last.get('close', 0))
            sig_time = str(df_ind['date'].iloc[last_idx])
            name = name_map.get(code, '')

            # 打印：跟 scan_market 完全一致的格式
            logger.info(
                f"[信号] {sig_time} {code} {name} 买入 "
                f"策略=MACD预测金叉 价格={price_val:.2f} "
                f"强度={strength_val:.0f} 原因={reason_val}"
            )

            all_signals.append({
                'code': code, 'name': name, 'signal_time': sig_time,
                'price': price_val, 'close_on_signal': price_val,
                'dif_on_signal': float(last.get('dif', 0)),
                'dea_on_signal': float(last.get('dea', 0)),
                'macd_on_signal': float(last.get('macd', 0)),
                'reason': reason_val, 'strength': strength_val,
            })
        except Exception:
            continue

    # --- Phase 1 汇总 ---
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("-" * 95)
    if all_signals:
        logger.info(f"  共 {len(all_signals)} 只股票触发 MACD 预测金叉信号（扫描耗时 {elapsed:.0f}s）")
    else:
        logger.info(f"  未发现任何 MACD 预测金叉信号（扫描耗时 {elapsed:.0f}s）")
        return

    # --- Phase 2: 验证（如需要）---
    if no_verify:
        logger.info("  （--no-verify 已设置，跳过后续金叉验证）")
        return

    logger.info("")
    logger.info("=" * 95)
    logger.info(f"  【验证报告】检查后续 {forward_days} 个交易日中是否真的形成金叉")
    logger.info("=" * 95)

    try:
        base_dt = datetime.strptime(
            signal_date or datetime.now().strftime('%Y-%m-%d'), '%Y-%m-%d'
        )
    except ValueError:
        logger.error("日期格式错误")
        return

    start_lookup = (base_dt - timedelta(days=60)).strftime('%Y-%m-%d')
    end_lookup = (base_dt + timedelta(days=forward_days + 10)).strftime('%Y-%m-%d')

    cross_1d = cross_3d = cross_5d = 0
    bar_ok_1d = 0
    pct_1d_vals, pct_3d_vals, pct_5d_vals = [], [], []
    unverifiable = 0
    detail_rows = []

    for sig in all_signals:
        code = sig['code']
        df_fwd = get_daily_kline_df(code, start_date=start_lookup, end_date=end_lookup)
        if df_fwd.empty or len(df_fwd) < 35:
            unverifiable += 1
            detail_rows.append({**sig, 'note': '无后续K线数据'})
            continue

        df_ind = calc_all_indicators(df_fwd)
        if df_ind.empty:
            unverifiable += 1
            detail_rows.append({**sig, 'note': '无后续K线数据'})
            continue

        # 定位信号日在后续扩展K线中的索引
        sig_day = sig['signal_time']
        sig_idx = None
        for i in range(len(df_ind)):
            if str(df_ind['date'].iloc[i]) == sig_day:
                sig_idx = i
                break
        if sig_idx is None:
            sig_idx = len(df_ind) - 1

        max_fwd = min(len(df_ind) - sig_idx - 1, forward_days)
        if max_fwd <= 0:
            unverifiable += 1
            detail_rows.append({**sig, 'note': '无后续K线数据'})
            continue

        sig_price = sig['close_on_signal']
        dif_on_sig = sig['dif_on_signal']
        dea_on_sig = sig['dea_on_signal']
        macd_on_sig = sig['macd_on_signal']

        # --- 金叉检测：DIF 从 < DEA 变成 >= DEA ---
        days_to_cross = None
        for j in range(1, max_fwd + 1):
            cur_idx = sig_idx + j
            dif_j = float(df_ind['dif'].iloc[cur_idx])
            dea_j = float(df_ind['dea'].iloc[cur_idx])
            prev_dif = dif_on_sig if j == 1 else float(df_ind['dif'].iloc[cur_idx - 1])
            prev_dea = dea_on_sig if j == 1 else float(df_ind['dea'].iloc[cur_idx - 1])
            if (prev_dif < prev_dea) and (dif_j >= dea_j):
                days_to_cross = j
                break

        # --- MACD柱次日继续增大 ---
        bar_ok = None
        if max_fwd >= 1:
            bar_ok = float(df_ind['macd'].iloc[sig_idx + 1]) > macd_on_sig

        # --- 涨跌幅 ---
        def _pct(offset):
            if offset > max_fwd or sig_idx + offset >= len(df_ind):
                return None
            cl = float(df_ind['close'].iloc[sig_idx + offset])
            return round((cl - sig_price) / sig_price * 100, 2) if sig_price else None

        p1, p3, p5 = _pct(1), _pct(3), _pct(5)

        if days_to_cross is not None:
            if days_to_cross <= 1: cross_1d += 1
            if days_to_cross <= 3: cross_3d += 1
            if days_to_cross <= 5: cross_5d += 1
        if bar_ok:
            bar_ok_1d += 1
        if p1 is not None: pct_1d_vals.append(p1)
        if p3 is not None: pct_3d_vals.append(p3)
        if p5 is not None: pct_5d_vals.append(p5)

        detail_rows.append({**sig, 'days_to_cross': days_to_cross, 'bar_continued_1d': bar_ok,
                             'pct_1d': p1, 'pct_3d': p3, 'pct_5d': p5})

    # --- 汇总 ---
    verifiable = len(all_signals) - unverifiable
    def _avg(vals): return round(sum(vals) / len(vals), 2) if vals else None

    logger.info(f"  可验证: {verifiable} 只   无后续数据: {unverifiable} 只")
    logger.info("-" * 50)
    logger.info(f"  金叉形成:")
    logger.info(f"    次日金叉: {cross_1d} 只   命中率: {round(cross_1d / verifiable * 100, 1) if verifiable else '-'}%")
    logger.info(f"    3日内金叉: {cross_3d} 只   命中率: {round(cross_3d / verifiable * 100, 1) if verifiable else '-'}%")
    logger.info(f"    5日内金叉: {cross_5d} 只   命中率: {round(cross_5d / verifiable * 100, 1) if verifiable else '-'}%")
    logger.info(f"  MACD柱次日继续增大: {bar_ok_1d} 只   命中率: {round(bar_ok_1d / verifiable * 100, 1) if verifiable else '-'}%")
    logger.info(f"  股价表现: 次日均价={_avg(pct_1d_vals)}%   3日均价={_avg(pct_3d_vals)}%   5日均价={_avg(pct_5d_vals)}%")
    logger.info("=" * 95)

    logger.info(f"  {'序号':>3} {'代码':<12} {'名称':<10} {'信号价':>8} {'几日金叉':>8} {'次日柱增大':>10} {'1日%':>7} {'3日%':>7} {'5日%':>7}  备注")
    logger.info("-" * 130)
    for i, d in enumerate(detail_rows, 1):
        cross_flag = str(d['days_to_cross']) if d.get('days_to_cross') is not None else '未金叉'
        bar_flag = 'OK' if d.get('bar_continued_1d') is True else ('--' if d.get('bar_continued_1d') is False else '-')
        p1 = f"{d['pct_1d']:.2f}" if d.get('pct_1d') is not None else '-'
        p3 = f"{d['pct_3d']:.2f}" if d.get('pct_3d') is not None else '-'
        p5 = f"{d['pct_5d']:.2f}" if d.get('pct_5d') is not None else '-'
        note = d.get('note', '')[:30]
        logger.info(f"  {i:>3} {d['code']:<12} {d.get('name', ''):<10} {d['close_on_signal']:>8.2f} "
                    f"{cross_flag:>8} {bar_flag:>10} {p1:>7} {p3:>7} {p5:>7}  {note}")
    logger.info("=" * 95)




if __name__ == '__main__':
    cli()
