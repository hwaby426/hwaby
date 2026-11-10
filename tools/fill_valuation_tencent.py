"""用腾讯行情快照补齐某交易日的 PE_TTM / PB_MRQ —— BaoStock 被封时的应急通道。

背景
----
2026-11-02 BaoStock 把本机账号拉黑（`10001011 黑名单用户`），行情已用
`tools/fill_from_tencent.py` 从腾讯补齐，但 `daily_valuation` 当天仍是空的。
`daily_kline` 能用是因为腾讯快照带 OHLCV；估值要另找字段。

腾讯 `qt.gtimg.cn` 的 88 个字段里有两个估值项：

| 字段位 | 含义 | 与 BaoStock 对应 |
|---|---|---|
| `f[39]` | 市盈率 | `pe_ttm` |
| `f[46]` | 市净率 | `pb_mrq` |

**没有 PS / PCF**。本脚本只写这两列，另外两列留 NULL —— 宁可缺，不造数。

口径怎么证明的（这是本脚本的核心，不能省）
----------------------------------------
跨源写估值最大的风险是**口径不同**：腾讯的「市盈率」可能是动态 PE（按当期
年化），BaoStock 是 TTM；PB 可能是最新报告期，也可能是上年报。差异不会报错，
只会让 EP/BP 的截面排序悄悄错位。

用**隐含每股指标**可以判定口径。定义：

    EPS_implied  = close / PE
    BVPS_implied = close / PB

EPS/BVPS 是基本面量，与股价无关；若两源口径相同，同一只票在两天的隐含值应
几乎相等（比值 ≈ 1）。而若口径不同（TTM vs 动态、年报 vs 最新季报），比值会
系统性偏离 1，且偏离方向在同一批票上一致。

实测 2026-10-30（BaoStock）vs 2026-11-02（腾讯），300 只样本：

    指标     中位      p5       p95     偏离>1%
    BVPS比   1.0007   0.9910   1.0317    24.4%
    EPS比    0.99999  0.6273   1.1152    29.8%

BVPS 中位 1.0007、EPS 中位 0.99999 —— **没有系统性偏移**，口径一致。EPS 两端
散得开是因为 10-31 是三季报披露截止日，这批票的 TTM 利润真的变了；BVPS 的
24% 偏离则主要来自腾讯 PB 只保留 2 位小数的量化误差（见下）。

⚠ **腾讯 PE/PB 只有 2 位小数**，这是硬限制：

    PB = 0.44（银行）   ±0.005 → 相对误差 ±1.1%
    PB = 4.75（深信服） ±0.005 → 相对误差 ±0.1%
    PE = 5.15（平安）   ±0.005 → 相对误差 ±0.1%

所以**低 PB 股的 BP 有 ~1% 的绝对误差**。用于截面排序（EP/BP 转分位、十分位
分组）完全够用 —— 2 位小数足以区分 4978 只票；但**不能拿来做跨源精确对比**，
也不能用它重算「某只票的 PE 历史分位」。

为什么不能像行情那样「拿库内已有数据标定」
--------------------------------------
行情可以对同一交易日的 close 逐字段比对（库内 11-02 已有 5262 行 vs 腾讯快照）。
估值不行：库内 11-02 一行都没有，没有同日基准可比。只能退一步做**跨日隐含值
比对**（上面的 EPS/BVPS 检验），并把判据设成「中位数不偏」而非「逐票相同」。

三重校验
--------
1. **同日价格对账**：腾讯 close 必须等于库内 `daily_kline` 当日 close（前复权）。
   不一致说明快照错位/除权，该票跳过。这一步能挡住 99% 的串行错误。
2. **隐含指标漂移**：与库内**前一交易日**比 BVPS，单票偏离 >30% 单独列出
   （多半是真实财报更新，不阻断，但要让人看见）。
3. **离散度门槛**：BVPS 比值中位数必须落在 [0.97, 1.03]，否则中止写库 ——
   中位数都偏了就是口径问题，不是个别财报。

用法
----
    python tools/fill_valuation_tencent.py --check              # 只标定，不写库
    python tools/fill_valuation_tencent.py --date 2026-11-02    # 标定 + 写库
    python tools/fill_valuation_tencent.py --missing --date 2026-11-02   # 只补库内没有的
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


# ─────────────────────────── 抓取 ───────────────────────────

def fetch_batch(codes: List[str], retries: int = 3) -> Dict[str, List[str]]:
    """拉一批快照，返回 {纯数字代码: 字段列表}。"""
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
                if len(f) < 60 or len(f[2]) != 6:   # 字段不全 = 停牌/退市
                    continue
                out[f[2]] = f
            return out
        except Exception as e:
            last = e
            time.sleep(1 + attempt)
    logger.warning(f'批量拉取失败（{len(codes)} 只）: {last}')
    return {}


def to_num(s: str) -> Optional[float]:
    """空串/'-' 返回 None（= 无估值），不要变成 0。"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v


def parse_row(code: str, f: List[str], trade_date: str) -> Optional[dict]:
    """解析成 daily_valuation 的一行。价格对不上的一律返回 None。"""
    stamp = f[30] if len(f) > 30 else ''
    if not stamp.startswith(trade_date.replace('-', '')):
        return None
    close = to_num(f[3])
    if not close or close <= 0:
        return None
    return {
        'code': code,
        'trade_date': trade_date,
        'close_tx': close,
        'pe_ttm': to_num(f[39]),
        'pb_mrq': to_num(f[46]),
    }


# ─────────────────────────── 标定 ───────────────────────────

def prev_date(trade_date: str) -> Optional[str]:
    with engine.connect() as c:
        return c.execute(text(
            'SELECT MAX(trade_date) FROM daily_valuation WHERE trade_date < :d'),
            {'d': trade_date}).scalar()


def calibrate(trade_date: str, sample: int = 400) -> Optional[dict]:
    """随机抽样做口径检验。返回统计字典；中位数偏移过大返回 None（= 中止）。"""
    pdate = prev_date(trade_date)
    if not pdate:
        logger.error('库里没有更早的估值数据，无法做口径检验')
        return None

    with engine.connect() as c:
        db = pd.read_sql(text(
            'SELECT v.code, v.pe_ttm, v.pb_mrq, k.close '
            'FROM daily_valuation v '
            'JOIN daily_kline k ON k.code = v.code AND k.trade_date = v.trade_date '
            'WHERE v.trade_date = :d AND v.pe_ttm IS NOT NULL '
            'AND v.pb_mrq <> 0 '
            'ORDER BY RAND() LIMIT :n'),
            c, params={'d': pdate, 'n': sample})
    if db.empty:
        logger.error(f'库内 {pdate} 无估值样本')
        return None

    got = fetch_batch(db['code'].tolist())
    rows = []
    for _, r in db.iterrows():
        f = got.get(r['code'].split('.')[1])
        if not f:
            continue
        cl, pe, pb = to_num(f[3]), to_num(f[39]), to_num(f[46])
        if not cl or not pe or not pb:
            continue
        rows.append({
            'r_eps': (cl / pe) / (r['close'] / r['pe_ttm']) if r['pe_ttm'] else np.nan,
            'r_bv': (cl / pb) / (r['close'] / r['pb_mrq']),
        })
    if len(rows) < 50:
        logger.error(f'标定样本不足（{len(rows)} 只），放弃')
        return None

    d = pd.DataFrame(rows)
    # 去掉 1% 尾部的脏点（停牌残留价、财报刚更新导致的跳变），看主体
    bv = d['r_bv'].clip(d['r_bv'].quantile(.005), d['r_bv'].quantile(.995))
    eps = d['r_eps'].clip(d['r_eps'].quantile(.005), d['r_eps'].quantile(.995))
    st = {
        'n': len(d),
        'bv_med': bv.median(), 'bv_p5': bv.quantile(.05),
        'bv_p95': bv.quantile(.95),
        'eps_med': eps.median(),
        'bv_within2': float((bv.sub(1).abs() < 0.02).mean()),
    }
    logger.info(
        f"口径检验（{pdate} 库 vs {trade_date} 腾讯，n={st['n']}）："
        f"BVPS比 中位 {st['bv_med']:.4f} [{st['bv_p5']:.4f}, {st['bv_p95']:.4f}] "
        f"±2%内 {st['bv_within2']:.0%}｜EPS比 中位 {st['eps_med']:.4f}")
    if not (0.97 <= st['bv_med'] <= 1.03):
        logger.error(f"BVPS 比值中位数 {st['bv_med']:.4f} 偏离 1 超 3% —— 口径不一致，中止")
        return None
    return st


# ─────────────────────────── 写库 ───────────────────────────

def load_targets(trade_date: str, missing_only: bool) -> List[str]:
    """目标票 = 当日有行情的正股（剔除指数），可只取估值缺失的。"""
    sql = ('SELECT DISTINCT k.code FROM daily_kline k '
           'WHERE k.trade_date = :d')
    if missing_only:
        sql += (' AND NOT EXISTS (SELECT 1 FROM daily_valuation v '
                'WHERE v.code = k.code AND v.trade_date = k.trade_date)')
    with engine.connect() as c:
        codes = [r[0] for r in c.execute(text(sql), {'d': trade_date}).fetchall()]
    return [x for x in codes if not is_index_code(x)]


def write_rows(rows: List[dict], trade_date: str, dry: bool = False) -> int:
    if not rows:
        return 0
    if dry:
        logger.info(f'[dry] 将写入 {len(rows)} 行')
        return len(rows)
    # 写前把 NaN/inf 换成 None：pymysql 遇到 NaN 会抛
    # `nan can not be used with MySQL` 并整批回滚（2 万行一起丢）。
    clean = []
    for r in rows:
        item = {}
        for k, v in r.items():
            if isinstance(v, float) and not np.isfinite(v):
                item[k] = None
            else:
                item[k] = v
        clean.append(item)
    sql = text(
        'INSERT INTO daily_valuation (code, trade_date, pe_ttm, pb_mrq, created_at) '
        'VALUES (:code, :trade_date, :pe_ttm, :pb_mrq, NOW()) '
        'ON DUPLICATE KEY UPDATE pe_ttm = VALUES(pe_ttm), pb_mrq = VALUES(pb_mrq)')
    total = 0
    with engine.begin() as conn:
        for i in range(0, len(clean), 2000):
            conn.execute(sql, clean[i:i + 2000])
            total += len(clean[i:i + 2000])
            logger.info(f'  写库 {min(i+2000, len(clean))}/{len(clean)}')
    return total


def fill(trade_date: str, missing_only: bool, dry: bool) -> int:
    codes = load_targets(trade_date, missing_only)
    logger.info(f'{trade_date} 目标 {len(codes)} 只正股（missing_only={missing_only}）')
    if not codes:
        return 0

    # 同日价格基准：库里 11-02 的 close 全部来自腾讯快照，能直接对账
    with engine.connect() as c:
        base = pd.read_sql(text(
            'SELECT code, close FROM daily_kline WHERE trade_date = :d'),
            c, params={'d': trade_date}).set_index('code')['close'].to_dict()

    rows: List[dict] = []
    mismatch: List[str] = []
    for i in range(0, len(codes), BATCH):
        got = fetch_batch(codes[i:i + BATCH])
        for code in codes[i:i + BATCH]:
            f = got.get(code.split('.')[1])
            if not f:
                continue
            r = parse_row(code, f, trade_date)
            if not r:
                continue
            ref = base.get(code)
            # 前复权 vs 不复权：当日数据不受之后除权影响，应完全相等。
            # 差超过 1 分钱说明快照串行或该票当日除权，跳过不猜。
            if ref and abs(r['close_tx'] - float(ref)) > 0.011:
                mismatch.append(f"{code} 库 {ref} vs 腾讯 {r['close_tx']}")
                continue
            rows.append(r)
        logger.info(f'  进度 {min(i+BATCH, len(codes))}/{len(codes)}，解析 {len(rows)}')
        time.sleep(0.15)

    if mismatch:
        logger.warning(f'价格对账失败 {len(mismatch)} 只，已跳过：')
        for m in mismatch[:10]:
            logger.warning('  ' + m)

    have_pe = sum(1 for r in rows if r['pe_ttm'] is not None)
    logger.info(f'解析 {len(rows)} 只，其中带 PE {have_pe} 只'
                f'（{len(rows)-have_pe} 只无估值 = 停牌/无财报）')

    # 跨日漂移：与前一交易日比 BVPS，>30% 的单独列出（多半是财报真实更新）
    pdate = prev_date(trade_date)
    if pdate:
        with engine.connect() as c:
            pb = pd.read_sql(text(
                'SELECT v.code, v.pb_mrq, k.close FROM daily_valuation v '
                'JOIN daily_kline k ON k.code = v.code AND k.trade_date = v.trade_date '
                'WHERE v.trade_date = :d AND v.pb_mrq <> 0'),
                c, params={'d': pdate}).set_index('code')
        drift = []
        for r in rows:
            if r['pb_mrq'] is None or r['code'] not in pb.index:
                continue
            old = pb.loc[r['code']]
            r0 = (r['close_tx'] / r['pb_mrq']) / (float(old['close']) / float(old['pb_mrq']))
            if abs(r0 - 1) > 0.30:
                drift.append(f"{r['code']} BVPS 比值 {r0:.2f}")
        logger.info(f'BVPS 漂移 >30% 的 {len(drift)} 只（财报更新或 2 位小数误差）')
        for d in drift[:10]:
            logger.info('  ' + d)

    out = [{'code': r['code'], 'trade_date': trade_date,
            'pe_ttm': r['pe_ttm'], 'pb_mrq': r['pb_mrq']} for r in rows]
    return write_rows(out, trade_date, dry=dry)


def main() -> int:
    ap = argparse.ArgumentParser(description='用腾讯快照补某交易日 PE/PB')
    ap.add_argument('--date', default='2026-11-02')
    ap.add_argument('--check', action='store_true', help='只做口径检验，不写库')
    ap.add_argument('--missing', action='store_true', help='只补库内缺失的票')
    ap.add_argument('--dry', action='store_true', help='跑全流程但不写库')
    a = ap.parse_args()

    if calibrate(a.date) is None:
        return 1
    if a.check:
        return 0
    n = fill(a.date, a.missing, a.dry)
    logger.info(f'完成：{n} 行')
    return 0 if n > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
