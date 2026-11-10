"""用腾讯行情快照补齐某交易日的日线 —— BaoStock 被封时的应急通道。

背景：2026-11-02 全市场更新时，BaoStock 在 5262 次请求后把本机拉黑
（`10001011 黑名单用户`），导致 2171 只（40%）缺当日数据，其中创业板
1354 只**全部**缺失。逐只重试只会延长封禁，所以改用腾讯的批量快照接口
`qt.gtimg.cn`：一次 200 只、0.14 秒，没有账号概念。

⚠ 跨源写入必须证明口径一致，否则是**静默污染**（库里看不出来，分析全错）。
本脚本强制做三件事：

1. **标定**：先用库里已有当日数据的 N 只票，逐字段比对（OHLC 必须完全相同，
   volume 比值必须稳定，否则中止）。
2. **成交量自校准**：腾讯的 volume 单位**按板块不同**——主板/创业板是「手」，
   科创板（688）是「股」。用 `成交额/成交量/价格` 判定：≈100 是手、≈1 是股。
   实测中芯国际若统一 ×100 会放大 100 倍。
3. **行内一致性**：`high >= max(open, close)`、`low <= min(open, close)`，
   停牌（价格或成交量为 0）一律跳过，宁缺不假。

用法：
    python tools/fill_from_tencent.py --check          # 只标定，不写库
    python tools/fill_from_tencent.py --date 2026-11-02
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from loguru import logger
from sqlalchemy import text

from db.database import engine
from tools.sync_kline import load_missing

URL = 'http://qt.gtimg.cn/q='
BATCH = 200
UA = {'User-Agent': 'Mozilla/5.0'}


def fetch_batch(codes: List[str], retries: int = 3) -> Dict[str, List[str]]:
    """拉一批快照，返回 {纯数字代码: 字段列表}。"""
    q = ','.join(c.replace('.', '') for c in codes)
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(URL + q, headers=UA), timeout=20
            ).read().decode('gbk', errors='ignore')
            out: Dict[str, List[str]] = {}
            for line in raw.strip().split(';'):
                if '="' not in line:
                    continue
                f = line.split('="', 1)[1].rstrip('"').split('~')
                if len(f) < 46:          # 字段不全 = 无效行（停牌/已退市）
                    continue
                out[f[2]] = f
            return out
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    logger.warning(f'批量拉取失败（{len(codes)} 只）: {last_err}')
    return {}


def to_num(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse_row(code: str, f: List[str], trade_date: str,
              is_index: bool = False) -> Optional[dict]:
    """把腾讯字段解析成 daily_kline 的一行；不合格返回 None。"""
    # f[30] 是 '20261102161430'，前 8 位是日期。不是目标日就是没交易/停牌。
    stamp = f[30] if len(f) > 30 else ''
    if not stamp.startswith(trade_date.replace('-', '')):
        return None
    close, pre, op = to_num(f[3]), to_num(f[4]), to_num(f[5])
    high, low = to_num(f[33]), to_num(f[34])
    vol_raw, amt_wan = to_num(f[6]), to_num(f[37])
    if close <= 0 or vol_raw <= 0 or high <= 0 or low <= 0:
        return None                      # 停牌 / 无成交
    if high < max(op, close) - 1e-6 or low > min(op, close) + 1e-6:
        logger.warning(f'{code} OHLC 不自洽，丢弃: {op}/{high}/{low}/{close}')
        return None

    # ── 成交量单位自校准 ────────────────────────────────
    # 腾讯给的是「手」还是「股」按板块不同（688 是股）。
    # 用成交额反推均价再比一次：amount/(vol*100) 落在收盘价附近 => 手。
    #
    # ⚠ 指数不能这么判：它的 close 是**点位**（如深证成指 11010），
    # amount/vol/close 会算出 0.13 这种无意义的数。指数一律按「手」处理。
    amount = amt_wan * 1e4
    if is_index:
        vol = int(vol_raw * 100)
    else:
        ref = close if close > 0 else (high + low) / 2
        k = amount / vol_raw / ref       # ≈100 → 手；≈1 → 股
        vol = int(vol_raw * 100) if k > 10 else int(vol_raw)

    pct = to_num(f[32])
    if pct == 0 and pre > 0:             # f[32] 偶发缺省，用昨收自己算
        pct = (close / pre - 1) * 100

    return {
        'code': code,
        'trade_date': trade_date,
        'open': op,
        'high': high,
        'low': low,
        'close': close,
        'volume': vol,
        'amount': amount,
        'pct_chg': round(pct, 4),
        'turnover': to_num(f[38]) or None,
        'adjustflag': 2,                 # 与库内全表一致（前复权）
    }


def calibrate(trade_date: str, sample: int = 40) -> bool:
    """用库内已有当日数据的票做逐字段标定。返回 True = 通过。

    这一步不能省：腾讯的 volume 单位是「按板块」变化的，只按经验写死 ×100
    会在科创板上错 100 倍，而错误数据写进库之后完全看不出来。
    """
    with engine.connect() as conn:
        db = pd.read_sql(text(
            'SELECT code, open, high, low, close, volume, amount, turnover '
            'FROM daily_kline WHERE trade_date = :d '
            'ORDER BY RAND() LIMIT :n'), conn, params={'d': trade_date, 'n': sample})
    if db.empty:
        logger.error(f'库内没有 {trade_date} 的数据，无法标定')
        return False

    got = fetch_batch(db['code'].tolist())
    ok = bad = vol_bad = 0
    ratios = []
    for _, r in db.iterrows():
        f = got.get(r['code'].split('.')[1])
        if not f:
            continue
        op, hi, lo, cl = (to_num(f[5]), to_num(f[33]), to_num(f[34]), to_num(f[3]))
        if max(abs(op - r['open']), abs(hi - r['high']), abs(lo - r['low']),
               abs(cl - r['close'])) < 0.02:
            ok += 1
            v = to_num(f[6])
            if v > 0:
                ratios.append(int(r['volume']) / v)
        else:
            bad += 1
            logger.warning(f"{r['code']} 价格不一致: 库 {r['close']} vs 腾讯 {cl}")
    if ratios:
        s = pd.Series(ratios)
        # 比值应聚在 1 或 100 上；分散说明单位判定逻辑有问题
        near1 = ((s - 1).abs() < 0.05).mean()
        near100 = ((s - 100).abs() < 5).mean()
        logger.info(f'成交量比值：≈1 占 {near1:.0%}、≈100 占 {near100:.0%}、'
                    f'其它 {1-near1-near100:.0%}（中位 {s.median():.0f}）')
        # 「其它」才是异常：比值既不像 1 也不像 100，说明既不是股也不是手
        vol_bad = int(round((1 - near1 - near100) * len(s)))

    logger.info(f'标定：价格一致 {ok} / 不一致 {bad} / 成交量比值异常 {vol_bad}')
    if bad > 0 or ok < 10:
        logger.error('标定未通过，中止（价格口径不一致时写入会污染全表）')
        return False
    return True


def fill(trade_date: str) -> int:
    codes = load_missing(trade_date)
    # 指数（sz.39xxxx / sh.000xxx）腾讯同样能查，但它的 close 是点位，
    # volume 单位判定要走另一条路（见 parse_row 的 is_index）。
    idx_set = {c for c in codes if c.startswith('sz.39') or c.startswith('sh.000')}
    logger.info(f'{trade_date} 待补 {len(codes)} 只'
                f'（其中指数 {len(idx_set)} 只、正股 {len(codes)-len(idx_set)} 只）')
    if not codes:
        return 0

    rows: List[dict] = []
    for i in range(0, len(codes), BATCH):
        got = fetch_batch(codes[i:i + BATCH])
        for code in codes[i:i + BATCH]:
            f = got.get(code.split('.')[1])
            if not f:
                continue
            r = parse_row(code, f, trade_date, is_index=code in idx_set)
            if r:
                rows.append(r)
        logger.info(f'  进度 {min(i+BATCH, len(codes))}/{len(codes)}，已解析 {len(rows)}')
        time.sleep(0.15)

    if not rows:
        logger.error('一只都没解析出来')
        return 0
    # 落库走 data_fetcher 现成的批量 upsert（口径、唯一键与全表一致）
    from data_fetcher.baostock_fetcher import save_daily_klines_batch
    save_daily_klines_batch(rows, batch_size=3000, log_prefix='[腾讯补] ')
    logger.info(f'完成：解析 {len(rows)} 只，未解析 {len(codes)-len(rows)} 只（停牌或已退市）')
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description='用腾讯快照补某交易日日线')
    ap.add_argument('--date', default='2026-11-02')
    ap.add_argument('--check', action='store_true', help='只做标定，不写库')
    a = ap.parse_args()

    if not calibrate(a.date):
        return 1
    if a.check:
        return 0
    n = fill(a.date)
    return 0 if n > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
