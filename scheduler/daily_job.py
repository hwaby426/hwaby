"""每日收盘后的全市场 K 线更新（腾讯通道）。

BaoStock 已永久封号，旧链路 update_all_daily_klines / calc_all_daily_signals
全部移除。这里只做一件事：调用 tools/update_daily_tencent.py，它内部依次
完成 日线 → 估值 → 快照 → 特征，并做四项前置检查与收尾行数核对。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

BASE_DIR = Path(__file__).resolve().parent.parent


def run_daily_update(date: str | None = None, check: bool = False) -> int:
    """跑一次腾讯日更，返回退出码。"""
    cmd = [sys.executable, 'tools/update_daily_tencent.py']
    if date:
        cmd += ['--date', date]
    if check:
        cmd += ['--check']
    logger.info('日更开始：' + ' '.join(cmd[1:]))
    return subprocess.run(cmd, cwd=str(BASE_DIR)).returncode


def daily_update_job(stock_pool=None, date: str | None = None) -> None:
    """调度器入口。stock_pool 保留兼容参数（日更是全市场，不按池子过滤）。"""
    start = datetime.now()
    logger.info('========== 开始每日全市场 K 线更新 ==========')
    try:
        rc = run_daily_update(date=date)
        if rc != 0:
            logger.error(f'日更脚本返回 {rc}，请查看上方日志（腾讯快照补不了历史）')
    except Exception as e:
        logger.error(f'每日更新任务异常: {e}', exc_info=True)
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f'========== 每日更新完成，耗时 {elapsed:.1f}s ==========')
