import sys
import signal
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config.settings import settings
from scheduler.daily_job import daily_update_job
from scheduler.realtime_job import RealtimeMonitor
from data_fetcher.sina_fetcher import is_trading_time, is_market_open_day


def setup_scheduler():
    scheduler = BackgroundScheduler(timezone='Asia/Shanghai')

    hour, minute = settings.DAILY_UPDATE_TIME.split(':')
    scheduler.add_job(
        daily_update_job,
        CronTrigger(hour=int(hour), minute=int(minute), day_of_week='mon-fri'),
        id='daily_update',
        name='每日日线更新',
        misfire_grace_time=3600,
    )
    logger.info(f"已注册每日更新任务，时间: {settings.DAILY_UPDATE_TIME} (工作日)")

    return scheduler


def run_scheduler():
    logger.info("启动调度器...")
    scheduler = setup_scheduler()

    monitor = RealtimeMonitor()
    monitor_thread = None

    stock_pool = settings.get_stock_pool()
    logger.info(f"实时监控股票池 ({len(stock_pool)} 只):")
    for code in stock_pool:
        logger.info(f"  - {code}")

    def start_realtime():
        nonlocal monitor_thread
        if monitor_thread and monitor_thread.is_alive():
            logger.warning("实时监控已在运行中")
            return
        logger.info("开盘，启动实时监控")
        monitor_thread = threading.Thread(target=monitor.run, daemon=True)
        monitor_thread.start()

    def stop_realtime():
        logger.info("收盘，停止实时监控")
        monitor.stop()
        if monitor_thread:
            monitor_thread.join(timeout=10)

    scheduler.add_job(
        start_realtime,
        CronTrigger(hour=9, minute=25, day_of_week='mon-fri'),
        id='realtime_start',
        name='启动实时监控',
    )
    scheduler.add_job(
        stop_realtime,
        CronTrigger(hour=15, minute=5, day_of_week='mon-fri'),
        id='realtime_stop',
        name='停止实时监控',
    )

    def market_scan_job():
        logger.info("=== 开始14:30全市场扫描 ===")
        try:
            from scheduler.market_scan import scan_market_intraday
            scan_market_intraday(signal_type='buy')
        except Exception as e:
            logger.error(f"全市场扫描异常: {e}", exc_info=True)
        logger.info("=== 14:30全市场扫描完成 ===")

    scheduler.add_job(
        market_scan_job,
        CronTrigger(hour=14, minute=30, day_of_week='mon-fri'),
        id='market_scan_daily',
        name='14:30全市场扫描',
        misfire_grace_time=3600,
    )
    logger.info("已注册14:30全市场扫描任务 (工作日)")

    if is_market_open_day():
        now = datetime.now()
        t = now.time()
        morning_start = datetime.strptime('09:25', '%H:%M').time()
        morning_end = datetime.strptime('11:30', '%H:%M').time()
        afternoon_start = datetime.strptime('13:00', '%H:%M').time()
        afternoon_end = datetime.strptime('15:00', '%H:%M').time()

        if morning_start <= t <= morning_end or afternoon_start <= t <= afternoon_end:
            logger.info(f"当前时间 {now.strftime('%H:%M:%S')} 在交易时段内，立即启动实时监控")
            start_realtime()
        elif morning_end < t < afternoon_start:
            logger.info(f"当前时间 {now.strftime('%H:%M:%S')} 为午休时段，将在 13:00 启动实时监控")
            scheduler.add_job(
                start_realtime,
                'date',
                run_date=now.replace(hour=13, minute=0, second=0, microsecond=0),
                id='realtime_start_afternoon',
                name='下午开盘启动实时监控',
                replace_existing=True,
            )

    stop_event = threading.Event()

    def signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，正在停止调度器...")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except (ValueError, OSError):
        pass

    scheduler.start()
    logger.info("调度器已启动，按 Ctrl+C 停止")
    logger.info("已注册任务:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name} ({job.id})")

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，正在停止...")
        stop_event.set()

    logger.info("正在停止实时监控...")
    monitor.stop()
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=5)

    logger.info("正在停止调度器...")
    scheduler.shutdown(wait=True)
    logger.info("调度器已完全停止")
