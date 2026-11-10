"""分片并行补日线 —— 只**调用** data_fetcher，不修改它。

为什么需要这个：`main.py update-market` 是串行全市场（5262 只），实测
**1.1 只/秒 ≈ 80 分钟**。瓶颈不是限速：`_request_interval` 早就降到了下限
0.01s，那 0.9 秒/只全花在 BaoStock 服务端往返上，串行就只能是这个速度。

`save_daily_klines_batch` 用的是 `ON DUPLICATE KEY UPDATE`，所以多进程写同一批
日期是安全的（重复写只是覆盖，不会报错、不会产生重复行）。

用法：
    python tools/sync_kline.py --shard 1/6
    # 6 个后台进程，每个跑 ~877 只，合计约 13 分钟

    python tools/sync_kline.py --report     # 只看库里现状，不拉

注意：这里刻意不复用 `daily_update_market_all()`，因为它在结束时还会触发信号
计算；补历史数据时那一步是不需要的，也会让并行分片互相覆盖信号表。
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import baostock as bs
import pandas as pd
from loguru import logger
from sqlalchemy import text

from db.database import engine
from data_fetcher.baostock_fetcher import update_all_daily_klines
from data_fetcher.stock_pool import get_stock_pool_from_db


def load_missing(trade_date: str) -> List[str]:
    """返回该交易日**库里还没有日线**的 code（按前一日有数据的票为准）。

    为什么要这个：`update_all_daily_klines` 判断的是「本地最后日期 < 今天」，
    所以一旦某只票今天被限流返回空，它明天会被再次纳入更新范围——但**今天
    这次就是缺的**，而库里看不出来（没有行 ≠ 出错，停牌也没有行）。
    只有拿「前一交易日有的票」做全集，减去「今天已有的」，才得到真正的缺口。
    """
    with engine.connect() as conn:
        prev = pd.read_sql(text(
            'SELECT DISTINCT code FROM daily_kline WHERE trade_date = '
            '(SELECT MAX(trade_date) FROM daily_kline WHERE trade_date < :d)'),
            conn, params={'d': trade_date})
        have = set(pd.read_sql(text(
            'SELECT DISTINCT code FROM daily_kline WHERE trade_date = :d'),
            conn, params={'d': trade_date})['code'])
    all_codes = set(prev['code'])
    return sorted(all_codes - have)


def _rows_from_df(code: str, df: pd.DataFrame) -> list:
    """把 fetch_daily_kline 的返回转成 save_daily_klines_batch 需要的 dict 列表。

    字段口径照抄 data_fetcher.update_all_daily_klines 里的那段（不能改它，
    但必须和它写得一模一样，否则同一个 UNIQUE(code, trade_date) 会被两种
    单位覆盖——volume 差 100 倍这类错误在库里是看不出来的）。
    """
    from data_fetcher.baostock_fetcher import ADJUST_FLAG
    rows = []
    for _, row in df.iterrows():
        rows.append({
            'code': code,
            'trade_date': row['date'],
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': int(row['volume']),
            'amount': float(row['amount']),
            'pct_chg': float(row['pctChg']) if pd.notna(row['pctChg']) else None,
            'turnover': float(row['turn']) if pd.notna(row['turn']) else None,
            'adjustflag': int(ADJUST_FLAG),
        })
    return rows


def fill_missing(trade_date: str, sleep: float = 0.0,
                 max_empty_streak: int = 20, backoff: int = 60) -> int:
    """补某交易日缺失的日线，带**空返回退避**。

    `update_all_daily_klines` 没有这个保护：BaoStock 限流时用「空结果」表达，
    `error_code` 仍是 '0'，所以它既不重试也不减速，静默跳过。今天 6 片并行
    跑完只拿到 58.7%（空 41%），就是栽在这里。

    判据（MEMENTO 已记）：**空比例 > 15% 一定是限流**，真实退市/未上市远不到。
    """
    from data_fetcher.baostock_fetcher import (
        bs_login, fetch_daily_kline, save_daily_klines_batch)

    codes = load_missing(trade_date)
    if not codes:
        logger.info(f'{trade_date} 已无缺失')
        return 0
    logger.info(f'{trade_date} 待补 {len(codes)} 只')

    if not bs_login():
        raise SystemExit('BaoStock 登录失败（若提示「黑名单用户」，说明请求被风控拉黑，'
                         '需要等解封或换账号）')
    n_ok = n_empty = 0
    streak = 0                 # 连续空计数
    buf: List[dict] = []
    try:
        for i, code in enumerate(codes, 1):
            try:
                df = fetch_daily_kline(code, trade_date, trade_date)
            except Exception as e:                      # 单只失败不影响整体
                logger.warning(f'{code} 异常: {e}')
                df = pd.DataFrame()
            if df is None or df.empty:
                n_empty += 1
                streak += 1
                if streak >= max_empty_streak:
                    logger.warning(
                        f'连续 {streak} 只返回空（已空 {n_empty}/{i}），判定限流，'
                        f'暂停 {backoff}s 后退避重试')
                    time.sleep(backoff)
                    bs_login()          # 重登录换 session
                    streak = 0
            else:
                n_ok += 1
                streak = 0
                buf.extend(_rows_from_df(code, df))
            if len(buf) >= 500:
                save_daily_klines_batch(buf, batch_size=3000, log_prefix='[补漏] ')
                buf = []
            if sleep:
                time.sleep(sleep)
            if i % 200 == 0:
                logger.info(f'[补漏进度] {i}/{len(codes)} 成功 {n_ok} 空 {n_empty}')
        if buf:
            save_daily_klines_batch(buf, batch_size=3000, log_prefix='[补漏] ')
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    logger.info(f'补漏完成：{len(codes)} 只 / 成功 {n_ok} / 空 {n_empty}')
    return n_ok


def report() -> None:
    with engine.connect() as conn:
        d = pd.read_sql(text(
            'SELECT COUNT(*) n, COUNT(DISTINCT code) c, '
            'MIN(trade_date) d0, MAX(trade_date) d1 FROM daily_kline'), conn).iloc[0]
        # 最近 5 个交易日各有多少只
        tail = pd.read_sql(text(
            'SELECT trade_date, COUNT(*) n, COUNT(DISTINCT code) c '
            'FROM daily_kline GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5'),
            conn)
    print(f'\ndaily_kline：{int(d["n"]):,} 行 / {int(d["c"]):,} 只 / '
          f'{d["d0"]} ~ {d["d1"]}')
    print('\n最近 5 个交易日：')
    print(tail.to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description='分片并行补日线（只调用 data_fetcher）')
    ap.add_argument('--shard', default=None, metavar='I/N',
                    help='只跑第 I 片（1-based），共 N 片，如 1/6')
    ap.add_argument('--limit', type=int, default=None, help='本片最多处理多少只')
    ap.add_argument('--report', action='store_true', help='只报告库内现状')
    ap.add_argument('--missing', action='store_true',
                    help='补缺失：只拉前一交易日有、但 --date 这天库里没有的票')
    ap.add_argument('--date', default=None,
                    help='补漏的目标交易日，默认库内最新日期')
    ap.add_argument('--sleep', type=float, default=0.0,
                    help='每只之间的额外停顿（补漏时建议 0.1~0.3，避开风控）')
    ap.add_argument('--backoff', type=int, default=60,
                    help='连续 20 只返回空后暂停的秒数')
    a = ap.parse_args()

    if a.report:
        report()
        return 0

    if a.missing:
        target = a.date
        if target is None:
            with engine.connect() as conn:
                target = str(pd.read_sql(text(
                    'SELECT MAX(trade_date) FROM daily_kline'), conn).iloc[0, 0])
        fill_missing(target, sleep=a.sleep, backoff=a.backoff)
        return 0

    codes: List[str] = get_stock_pool_from_db()
    if not codes:
        logger.error('股票池为空，先跑 update-stock-list')
        return 1

    if a.shard:
        try:
            i, n = (int(x) for x in a.shard.split('/'))
        except ValueError:
            logger.error('--shard 格式应为 I/N，如 1/6')
            return 1
        if not (1 <= i <= n):
            logger.error(f'--shard 序号越界：{i}/{n}')
            return 1
        # 用步长切片而不是连续切片：连续切会让每片都撞到同一批行业的连号
        # 代码段，遇到某个板块整体停牌时各片进度差异被放大。
        codes = codes[i - 1::n]
        logger.info(f'分片 {i}/{n}：{len(codes)} 只')

    if a.limit:
        codes = codes[:a.limit]

    t0 = datetime.now()
    n_rows = update_all_daily_klines(stock_pool=codes) or 0
    logger.info(f'分片完成：{len(codes)} 只 / {n_rows} 条K线 / '
                f'{(datetime.now() - t0).total_seconds():.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
