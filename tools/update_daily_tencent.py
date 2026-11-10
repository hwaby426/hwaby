"""腾讯通道一键日更 —— BaoStock 账号被封后的正式更新流程。

为什么需要这个脚本
------------------
2026-11-02 BaoStock 把本机账号永久拉黑（`10001011 黑名单用户`，挂 2 小时
每 5 分钟探一次全是拒绝），此后日线走 `fill_from_tencent.py`、估值走
`fill_valuation_tencent.py`。但那两个是**补漏工具**，日更需要把它们串起来，
并且补上一个它们都没有的东西：**漏更检测**。

腾讯通道的致命限制
------------------
`qt.gtimg.cn` 是**实时快照**，只有「当前」这一档。它没有 `date=` 参数，
不能拉历史。所以：

| | BaoStock | 腾讯快照 |
|---|---|---|
| 当日行情 | ✅ | ✅ |
| 回补昨天 | ✅ | ❌ **永久丢失** |
| 回补一个月前 | ✅ | ❌ |
| 账号/封禁 | 会封 | 无账号概念 |

**漏跑一天就是真的缺一天，事后补不回来。** 这是从 BaoStock 切到腾讯之后
唯一实质性的能力降级，所以本脚本把「体检」放在「更新」前面强制执行：

1. 今天是不是交易日（用快照时间戳回答，不依赖节假日表）
2. 是否已收盘（时间戳 < 15:00 时数据不全）
3. 最近 N 个交易日有没有缺口 / 行数异常
4. 估值覆盖率是否跟上日线

任何一项不合格都会在开头喊出来，让人先去处理，而不是闷头更新。

交易日怎么判定
-------------
不维护节假日表。腾讯快照的 `f[30]` 是成交时间戳，格式 `20261102161430`
（2026-11-02 16:14:30）。抓 30 只样本：

- 时间戳的日期 == 今天  → 今天是交易日（周末/节假日会停在上一交易日）
- 时分 < 15:00         → 还没收盘，数据不完整
- 日期 < 今天          → 今天不是交易日，直接退出

这个判定的好处是**数据源自证**：它和行情同源，不会出现「日历说是交易日
但数据源没数据」的分裂。

用法
----
    python tools/update_daily_tencent.py --check        # 只体检，不拉数据
    python tools/update_daily_tencent.py                # 体检 + 日线 + 估值 + 特征
    python tools/update_daily_tencent.py --skip-features
    python tools/update_daily_tencent.py --date 2026-11-02   # 指定日期（仅限当天，历史补不了）
"""

import argparse
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from loguru import logger
from sqlalchemy import text

from db.database import engine
from tools.fill_from_tencent import fetch_batch as fetch_kline
from tools.fill_from_tencent import fill as fill_kline
from tools.fill_valuation_tencent import fetch_batch as fetch_val
from tools.fill_valuation_tencent import fill as fill_val
from tools.fill_valuation_tencent import calibrate as calibrate_val

# 全市场正股约 4976 只；某天行数显著低于这个数就是补漏
EXPECT_MIN = 4800
LOOKBACK = 15          # 体检回看多少个交易日


# ────────────────────────── 体检 ──────────────────────────

def sample_codes(n: int = 30) -> List[str]:
    """取最近一个交易日里的 n 只票做探针（含大票和随机票）。"""
    with engine.connect() as c:
        rows = c.execute(text(
            'SELECT DISTINCT code FROM daily_kline '
            'WHERE trade_date = (SELECT MAX(trade_date) FROM daily_kline) '
            'ORDER BY RAND() LIMIT :n'), {'n': n}).fetchall()
    return [r[0] for r in rows]


def probe_market() -> Dict[str, object]:
    """抓一批快照，判定「今天是否交易日 / 是否已收盘」。"""
    codes = sample_codes(30)
    if not codes:
        return {'ok': False, 'reason': '库里没有日线数据'}
    got = fetch_kline(codes)
    stamps = [f[30] for f in got.values() if len(f) > 30 and len(f[30]) >= 14]
    if not stamps:
        return {'ok': False, 'reason': '腾讯一只都没返回（网络或接口变动）'}

    days = Counter(s[:8] for s in stamps)
    main_day, cnt = days.most_common(1)[0]
    hhmm = max(int(s[8:12]) for s in stamps if s[:8] == main_day)
    return {
        'ok': True,
        'trade_date': f'{main_day[:4]}-{main_day[4:6]}-{main_day[6:]}',
        'hhmm': hhmm,
        'votes': f'{cnt}/{len(stamps)}',
    }


def coverage_report(lookback: int = LOOKBACK) -> pd.DataFrame:
    """最近 N 个交易日的日线 / 估值覆盖情况。"""
    with engine.connect() as c:
        df = pd.read_sql(text(
            'SELECT d.trade_date, '
            '       COUNT(*) AS 日线行数, '
            '       SUM(CASE WHEN d.code NOT LIKE :idx_sz AND d.code NOT LIKE :idx_sh '
            '                THEN 1 ELSE 0 END) AS 正股, '
            '       (SELECT COUNT(*) FROM daily_valuation v '
            '         WHERE v.trade_date = d.trade_date) AS 估值行数, '
            '       (SELECT COUNT(ps_ttm) FROM daily_valuation v '
            '         WHERE v.trade_date = d.trade_date) AS 有PS '
            'FROM daily_kline d '
            'GROUP BY d.trade_date ORDER BY d.trade_date DESC LIMIT :n'),
            c, params={'idx_sz': 'sz.39%', 'idx_sh': 'sh.000%', 'n': lookback})
    return df.sort_values('trade_date').reset_index(drop=True)


def gaps(df: pd.DataFrame) -> List[str]:
    """找缺口：行数不足的日子、相邻交易日间隔过长（>10 自然日必有漏）。"""
    out: List[str] = []
    for _, r in df.iterrows():
        if r['正股'] < EXPECT_MIN:
            out.append(f"{r['trade_date']} 正股仅 {int(r['正股'])} 只"
                       f"（应 ≥{EXPECT_MIN}）→ 当天漏更或补得不全")
        if r['估值行数'] < r['正股'] * 0.9:
            out.append(f"{r['trade_date']} 估值 {int(r['估值行数'])} / 日线 {int(r['正股'])}"
                       f" → 估值没跟上")
    dates = [pd.Timestamp(x) for x in df['trade_date']]
    for a, b in zip(dates, dates[1:]):
        span = (b - a).days
        if span > 10:
            out.append(f'{a.date()} → {b.date()} 间隔 {span} 天'
                       f'（A股最长休市约 9 天）→ 中间可能整段漏了')
    return out


def diagnose(target: Optional[str] = None) -> bool:
    """打印体检表，返回是否可以继续更新。"""
    today = target or date.today().isoformat()
    logger.info('=' * 62)
    logger.info(f'数据体检（目标日 {today}）')

    p = probe_market()
    if not p.get('ok'):
        logger.error(f"市场探针失败：{p.get('reason')}")
        return False
    logger.info(f"腾讯快照最新成交日 {p['trade_date']}，时间 {p['hhmm']:04d}，"
                f"样本一致度 {p['votes']}")

    if p['trade_date'] != today:
        # 非交易日 = 正常情况，不是错误
        logger.warning(f"今天（{today}）不是交易日 —— 数据源停在 {p['trade_date']}，"
                       f"无需更新。若是节假日，这是预期行为。")
        return False
    if p['hhmm'] < 1500:
        logger.error(f"还没收盘（快照时间 {p['hhmm']:04d} < 1500），"
                     f"此时成交量/收盘价都不完整，拒绝写入。")
        return False

    df = coverage_report()
    logger.info(f'最近 {len(df)} 个交易日覆盖：')
    for _, r in df.iterrows():
        flag = '  ' if r['正股'] >= EXPECT_MIN else '❌'
        ps = f"{int(r['有PS'])} 有PS" if r['有PS'] else 'PS 空'
        logger.info(f"  {flag} {r['trade_date']}  日线 {int(r['日线行数']):5d}"
                    f"（正股 {int(r['正股']):4d}）  估值 {int(r['估值行数']):4d}  {ps}")

    problems = gaps(df)
    if problems:
        logger.warning('发现缺口（腾讯快照补不了历史，只能靠换源或记下来）：')
        for x in problems:
            logger.warning('  ⚠ ' + x)
    else:
        logger.info('最近 %d 个交易日无缺口' % len(df))

    latest = df['trade_date'].max()
    latest = latest.date() if hasattr(latest, 'date') else latest
    if str(latest) != today:
        logger.warning(f'库内最新只到 {latest}，今天是 {today} → 需要更新')
    else:
        logger.info(f'库内已是最新（{latest}）')
    logger.info('=' * 62)
    return True


# ────────────────────────── 更新 ──────────────────────────

def run_features(trade_date: str) -> bool:
    """只重建当天特征（builder 用 ON DUPLICATE KEY UPDATE，可增量）。

    `--append-parquet` 不能省：默认 save_parquet 是覆盖写，日更只构建一天就会把
    `.data_cache/features.parquet` 从 161 万行缩成 4976 行，而 `ml/dataset.py`
    读的正是这个文件——下次 ml-train 会静默只训练一天样本。
    """
    cmd = [sys.executable, 'main.py', 'build-features',
           '--start', trade_date, '--end', trade_date, '--append-parquet']
    logger.info('构建特征：' + ' '.join(cmd[1:]))
    r = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent))
    if r.returncode != 0:
        logger.error(f'特征构建失败（exit {r.returncode}）')
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description='腾讯通道一键日更')
    ap.add_argument('--date', default=None, help='目标日，默认今天（历史补不了）')
    ap.add_argument('--check', action='store_true', help='只体检')
    ap.add_argument('--skip-features', action='store_true')
    a = ap.parse_args()

    today = a.date or date.today().isoformat()
    if not diagnose(today):
        return 0 if a.check else 1
    if a.check:
        return 0

    # ── 日线 ──
    n_k = 0
    with engine.connect() as c:
        n_k = c.execute(text('SELECT COUNT(*) FROM daily_kline WHERE trade_date=:d'),
                        {'d': today}).scalar()
    if n_k >= EXPECT_MIN:
        logger.info(f'日线已有 {n_k} 行，跳过')
    else:
        logger.info(f'日线只有 {n_k} 行，开始从腾讯补')
        n_k = fill_kline(today)
        logger.info(f'日线补完 {n_k} 行')

    # ── 估值（先证明口径，再写）──
    if calibrate_val(today) is None:
        logger.error('估值口径检验未通过，跳过估值（日线已处理）')
    else:
        n_v = fill_val(today, missing_only=True, dry=False)
        logger.info(f'估值补完 {n_v} 行')

    # ── 快照（市值/股本，库里没有的横截面量）──
    # 必须每天存：腾讯只能拿当天，历史补不了。股本是慢变量，攒起来才能
    # 回溯历史市值（fcap_t = close_t 前复权 × float_shares）。
    from tools.sync_snapshot import sync as sync_snapshot
    try:
        sync_snapshot(today)
    except Exception as e:
        logger.error(f'快照写入失败（不影响日线与估值）：{e}')

    # ── 特征 ──
    if not a.skip_features:
        run_features(today)

    # ── 收尾：只核当日三表行数（不重跑完整体检，那要再花 20 秒）──
    with engine.connect() as c:
        rows = c.execute(text(
            'SELECT '
            '(SELECT COUNT(*) FROM daily_kline WHERE trade_date=:d), '
            '(SELECT COUNT(*) FROM daily_valuation WHERE trade_date=:d), '
            '(SELECT COUNT(*) FROM stock_features WHERE trade_date=:d)'),
            {'d': today}).fetchone()
    logger.info(f'{today} 收尾核对：日线 {rows[0]} / 估值 {rows[1]} / 特征 {rows[2]}')
    ok = rows[0] >= EXPECT_MIN and rows[2] >= EXPECT_MIN
    if not ok:
        logger.error('行数不足，请人工检查（腾讯快照补不了历史，拖到明天就永久缺）')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
