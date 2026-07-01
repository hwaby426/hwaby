from datetime import datetime, date
from typing import List
from loguru import logger

from data_fetcher.baostock_fetcher import (
    update_all_daily_klines,
    get_daily_kline_df,
    bs_login, bs_logout,
    fetch_stock_list, sync_stock_info,
)
from signals.signal_service import generate_daily_signals_for_stock, save_signals
from signals.manager import get_strategy_names
from config.settings import settings


def daily_update_job(stock_pool: List[str] = None):
    logger.info("========== 开始每日日线更新任务 ==========")
    start = datetime.now()
    kline_count = 0
    signal_count = 0
    try:
        kline_count = update_all_daily_klines(stock_pool=stock_pool) or 0
        signal_count = calc_all_daily_signals(stock_pool=stock_pool) or 0
    except Exception as e:
        logger.error(f"每日更新任务异常: {e}", exc_info=True)
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"========== 每日更新任务完成，新增K线 {kline_count} 条，新增信号 {signal_count} 条，耗时 {elapsed:.1f}s ==========")


def calc_all_daily_signals(stock_pool: List[str] = None):
    if not stock_pool:
        stock_pool = settings.get_stock_pool()
    if not stock_pool:
        logger.warning("股票池为空，跳过信号计算")
        return
    logger.info(f"开始计算日线买卖信号，共 {len(stock_pool)} 只股票")
    total_signals = 0
    for i, code in enumerate(stock_pool, 1):
        try:
            df = get_daily_kline_df(code)
            if df.empty or len(df) < 35:
                continue
            records = generate_daily_signals_for_stock(df, code)
            if records:
                save_signals(records)
                total_signals += len(records)
        except Exception as e:
            logger.error(f"{code} 信号计算失败: {e}")

        if i % 100 == 0:
            logger.info(f"信号计算进度: {i}/{len(stock_pool)} ({i*100//len(stock_pool)}%)  已生成 {total_signals} 条信号")

    logger.info(f"日线信号计算完成，共生成 {total_signals} 条信号")
    return total_signals


def daily_update_market_all():
    """全市场每日更新：仅更新日线数据"""
    from data_fetcher.stock_pool import get_stock_pool_from_db

    logger.info("========== 开始全市场每日更新 ==========")
    start = datetime.now()

    kline_count = 0
    try:
        stock_pool = get_stock_pool_from_db()
        if not stock_pool:
            logger.error("股票池为空，请先运行 update-stock-list 更新股票列表")
            return

        logger.info(f"全市场股票池: {len(stock_pool)} 只")

        kline_count = update_all_daily_klines(stock_pool=stock_pool) or 0
    except KeyboardInterrupt:
        logger.info("收到中断信号，停止更新")
    except Exception as e:
        logger.error(f"全市场更新异常: {e}", exc_info=True)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"========== 全市场更新完成，新增K线 {kline_count} 条，耗时 {elapsed:.1f}s ==========")
