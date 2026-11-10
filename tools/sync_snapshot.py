"""每天存一份腾讯快照里的「市值与股本」—— 补上库里唯一缺失的横截面基础量。

为什么需要这个表
----------------
项目目前算流通市值靠反推：`fcap = amount * 100 / turnover`。这个办法的精度
瓶颈不在公式，在 `turnover` 的存储类型 `decimal(6,2)`——大盘股真实换手 0.07%
时，两位小数带来的相对误差就有 10%；小盘次新股成交额小而换手高，误差更大。

腾讯快照的 `f[44]` 直接给流通市值（亿元），`f[45]` 给总市值，精确到分。
实测 2026-10-30（库内反推）vs 2026-11-02（腾讯），约 4900 只配对：

| 指标 | 结果 |
|---|---|
| 隐含股本比值（腾讯/反推） | 中位 ≈ 1.000，**98.3% 在 ±5% 内** |
| 漂移 >20% | 只有 0.1% |

剩下的 1.7% 恰恰是反推最不准的那批：

```
sz.301507  反推 40.54 亿  vs 腾讯 12.47 亿  (3.3 倍)
sz.301555  反推 13.36 亿  vs 腾讯 24.53 亿  (1.8 倍)
sz.301510  反推 102.8 亿  vs 腾讯 74.9 亿
```

所以腾讯这一档不是「多一个参考」，是**替换掉一个已知有 10% 噪声的量**。

为什么存「股本」而不只存「市值」
------------------------------
腾讯快照只能拿当天，历史补不了。但把当天市值除以当天收盘价就得到**流通股本**，
而股本是慢变量（除权才变）。于是历史市值可以算：

```
fcap_t = close_t(前复权) × float_shares(最近一次快照)
```

前复权价本身就是「按当前股本口径折算的历史价」，所以对纯送转是精确的；
现金分红会带来小幅偏差，但远小于 `turnover` 两位小数那 10%。

存股本的第二个好处：每天存一次，股本一旦因增发/回购/送转变动就能被捕捉到，
不必依赖单一时点的假设。

表结构
------
    stock_snapshot(
        code         VARCHAR(16)  -- 带点，与 daily_kline 一致
        trade_date   DATE
        close / limit_up / limit_down       -- 收盘价、涨跌停价
        float_mv / total_mv                 -- 流通市值、总市值（元）
        float_shares / total_shares         -- 流通股本、总股本（股）
        UNIQUE(code, trade_date)
    )

⚠ 市值单位：腾讯给的是**亿元**，这里统一乘 1e8 存成「元」，与 `neutralize.py`
  的 `fcap` 口径一致。别混。

用法
----
    python tools/sync_snapshot.py                        # 抓今天
    python tools/sync_snapshot.py --date 2026-11-02
    python tools/sync_snapshot.py --report               # 看库内积累情况
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import text

from db.database import engine
from data.universe import is_index_code

URL = 'http://qt.gtimg.cn/q='
BATCH = 200
UA = {'User-Agent': 'Mozilla/5.0'}

DDL = """
CREATE TABLE IF NOT EXISTS stock_snapshot (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    code          VARCHAR(16) NOT NULL COMMENT '带点：sh.600000',
    trade_date    DATE        NOT NULL,
    close         DOUBLE      NULL COMMENT '收盘价（不复权，与当日实际成交一致）',
    limit_up      DOUBLE      NULL COMMENT '涨停价',
    limit_down    DOUBLE      NULL COMMENT '跌停价',
    float_mv      DOUBLE      NULL COMMENT '流通市值（元）',
    total_mv      DOUBLE      NULL COMMENT '总市值（元）',
    float_shares  DOUBLE      NULL COMMENT '流通股本（股）= float_mv/close',
    total_shares  DOUBLE      NULL COMMENT '总股本（股）= total_mv/close',
    created_at    DATETIME    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date (code, trade_date),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='腾讯快照：市值/股本/涨跌停（BaoStock 封禁后新增维度）'
"""


def ensure_table() -> None:
    with engine.begin() as conn:
        conn.execute(text(DDL))


def fetch_batch(codes: List[str], retries: int = 3) -> Dict[str, List[str]]:
    q = ','.join(c.replace('.', '') for c in codes)
    last: Optional[Exception] = None
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
                if len(f) < 60 or len(f[2]) != 6:
                    continue
                out[f[2]] = f
            return out
        except Exception as e:
            last = e
            time.sleep(1 + attempt)
    logger.warning(f'批量拉取失败（{len(codes)} 只）: {last}')
    return {}


def to_num(s: str) -> Optional[float]:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v


def parse_row(code: str, f: List[str], trade_date: str) -> Optional[dict]:
    stamp = f[30] if len(f) > 30 else ''
    if not stamp.startswith(trade_date.replace('-', '')):
        return None
    close = to_num(f[3])
    if not close or close <= 0:
        return None
    float_mv = to_num(f[44])
    total_mv = to_num(f[45])
    return {
        'code': code,
        'trade_date': trade_date,
        'close': close,
        'limit_up': to_num(f[47]),
        'limit_down': to_num(f[48]),
        # 腾讯是「亿元」，统一存「元」
        'float_mv': float_mv * 1e8 if float_mv else None,
        'total_mv': total_mv * 1e8 if total_mv else None,
        'float_shares': (float_mv * 1e8 / close) if float_mv else None,
        'total_shares': (total_mv * 1e8 / close) if total_mv else None,
    }


def load_targets(trade_date: str) -> List[str]:
    with engine.connect() as c:
        codes = [r[0] for r in c.execute(text(
            'SELECT DISTINCT code FROM daily_kline WHERE trade_date = :d'),
            {'d': trade_date}).fetchall()]
    return [x for x in codes if not is_index_code(x)]


def write_rows(rows: List[dict]) -> int:
    if not rows:
        return 0
    clean = []
    for r in rows:
        item = {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                for k, v in r.items()}
        clean.append(item)
    sql = text(
        'INSERT INTO stock_snapshot '
        '(code, trade_date, close, limit_up, limit_down, float_mv, total_mv, '
        ' float_shares, total_shares) '
        'VALUES (:code, :trade_date, :close, :limit_up, :limit_down, :float_mv, '
        '        :total_mv, :float_shares, :total_shares) '
        'ON DUPLICATE KEY UPDATE '
        '  close=VALUES(close), limit_up=VALUES(limit_up), '
        '  limit_down=VALUES(limit_down), float_mv=VALUES(float_mv), '
        '  total_mv=VALUES(total_mv), float_shares=VALUES(float_shares), '
        '  total_shares=VALUES(total_shares)')
    n = 0
    with engine.begin() as conn:
        for i in range(0, len(clean), 2000):
            chunk = clean[i:i + 2000]
            conn.execute(sql, chunk)
            n += len(chunk)
    return n


def sync(trade_date: str) -> int:
    ensure_table()
    codes = load_targets(trade_date)
    logger.info(f'{trade_date} 目标 {len(codes)} 只正股')
    if not codes:
        logger.error(f'库里没有 {trade_date} 的日线，先跑 update_daily_tencent.py')
        return 0

    rows: List[dict] = []
    for i in range(0, len(codes), BATCH):
        got = fetch_batch(codes[i:i + BATCH])
        for code in codes[i:i + BATCH]:
            f = got.get(code.split('.')[1])
            if not f:
                continue
            r = parse_row(code, f, trade_date)
            if r:
                rows.append(r)
        time.sleep(0.15)

    n_mv = sum(1 for r in rows if r['float_mv'])
    logger.info(f'解析 {len(rows)} 只，其中带流通市值 {n_mv} 只')
    n = write_rows(rows)
    logger.info(f'写入 stock_snapshot {n} 行')
    return n


def report() -> None:
    ensure_table()
    with engine.connect() as c:
        df = pd.read_sql(text(
            'SELECT trade_date, COUNT(*) AS 行数, COUNT(float_mv) AS 有市值 '
            'FROM stock_snapshot GROUP BY trade_date ORDER BY trade_date DESC '
            'LIMIT 20'), c)
    if df.empty:
        logger.info('stock_snapshot 还没有数据')
        return
    logger.info('stock_snapshot 积累情况（最近 20 个日期）：')
    for _, r in df.iterrows():
        logger.info(f"  {r['trade_date']}  {int(r['行数']):5d} 行，"
                    f"有市值 {int(r['有市值'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description='抓腾讯快照的市值/股本存库')
    ap.add_argument('--date', default=None)
    ap.add_argument('--report', action='store_true')
    a = ap.parse_args()

    if a.report:
        report()
        return 0
    from datetime import date
    d = a.date or date.today().isoformat()
    return 0 if sync(d) > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
