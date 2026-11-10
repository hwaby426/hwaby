"""固定进程：定时盘前分析 + 定时日更 + 交易时段启停 1min MACD 盯盘。

    python main.py scheduler

注册 4 类任务（均为工作日）：
  · PREMARKET_TIME（默认 08:30）盘前分析 .env 里的 STOCK_POOL，出分数/
    行业/消息面/技术位，落库 + HTML + 邮件
  · DAILY_UPDATE_TIME（默认 16:30）收盘后腾讯通道全市场日更
  · 09:25 启动盯盘 / 15:05 停止盯盘（进程内线程，重启自动接上）
  · 启动时若正处于交易时段，立即拉起盯盘；午休则排一个 13:00 的启动

⚠ 日更任务与 WorkBuddy 外部 16:30 自动化是**二选一**：两边都开会重复跑
（幂等 UPSERT，不会写坏数据，但白跑 5~6 分钟）。
"""
from __future__ import annotations

import os
import signal
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config.settings import settings
from data_fetcher.sina_fetcher import is_market_open_day, is_trading_time
from scheduler.daily_job import daily_update_job
from scheduler.macd_monitor import MacdMonitor


def _premarket_job():
    """定时盘前：分析配置的股票池当天的走势应对预案。"""
    logger.info('=== 开始定时盘前分析 ===')
    try:
        from premarket.report import render_terminal, save_html
        from premarket.service import run_premarket, save_scores, send_report_email

        pool = settings.get_stock_pool()
        if not pool:
            logger.warning('STOCK_POOL 为空，跳过盘前分析')
            return

        t_run = datetime.now()
        rep = run_premarket(list(pool), use_network=True, use_llm=True, news_hours=16.0)
        print(render_terminal(rep))

        # 评分必须落库，否则「这套分准不准」永远无法回答。
        save_scores(rep, use_llm=True, run_at=t_run)
        try:
            path = save_html(rep)
            logger.info(f'盘前 HTML 报告: {path}')
        except Exception as e:
            logger.warning(f'HTML 生成失败: {e}')
            path = ''
        send_report_email(rep, path)
    except Exception as e:
        logger.error(f'盘前分析异常: {e}', exc_info=True)
    logger.info('=== 定时盘前分析完成 ===')


def setup_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone='Asia/Shanghai')

    # 1) 定时盘前
    pre_time = os.getenv('PREMARKET_TIME', '08:30')
    ph, pm = pre_time.split(':')
    scheduler.add_job(
        _premarket_job,
        CronTrigger(hour=int(ph), minute=int(pm), day_of_week='mon-fri'),
        id='premarket_analysis', name='定时盘前分析（配置股票池）',
        misfire_grace_time=3600,
    )

    # 2) 定时日更（收盘后）
    daily_time = settings.DAILY_UPDATE_TIME
    dh, dm = daily_time.split(':')
    scheduler.add_job(
        daily_update_job,
        CronTrigger(hour=int(dh), minute=int(dm), day_of_week='mon-fri'),
        id='daily_update', name='每日全市场 K 线更新',
        misfire_grace_time=3600,
    )
    return scheduler


def run_scheduler() -> None:
    logger.info('启动固定进程...')
    scheduler = setup_scheduler()

    monitor = MacdMonitor()
    monitor_thread: threading.Thread | None = None
    lock = threading.Lock()

    logger.info(f'配置股票池 ({len(monitor.codes)} 只): {", ".join(monitor.codes)}')

    def start_realtime():
        nonlocal monitor_thread
        with lock:
            if monitor_thread and monitor_thread.is_alive():
                logger.warning('盯盘线程已在运行')
                return
            logger.info('开盘，启动 1min MACD 盯盘')
            # 复用同一实例：run() 开头会 clear 停止信号，收盘后可直接再启动
            monitor_thread = threading.Thread(target=monitor.run, daemon=True)
            monitor_thread.start()

    def stop_realtime():
        logger.info('收盘，停止盯盘')
        monitor.stop()

    scheduler.add_job(start_realtime,
                      CronTrigger(hour=9, minute=25, day_of_week='mon-fri'),
                      id='realtime_start', name='启动盯盘')
    scheduler.add_job(stop_realtime,
                      CronTrigger(hour=15, minute=5, day_of_week='mon-fri'),
                      id='realtime_stop', name='停止盯盘')

    if is_market_open_day() and is_trading_time():
        logger.info(f'当前 {datetime.now():%H:%M:%S} 在交易时段内，立即启动盯盘')
        start_realtime()

    stop_event = threading.Event()

    def _on_signal(signum, frame):
        logger.info(f'收到信号 {signum}，正在停止...')
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass

    scheduler.start()
    logger.info('已注册任务：')
    for job in scheduler.get_jobs():
        logger.info(f'  - {job.name} ({job.id})')

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        stop_event.set()

    logger.info('正在停止盯盘...')
    monitor.stop()
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=5)
    scheduler.shutdown(wait=True)
    logger.info('固定进程已完全停止')
