"""只要求 MACD 柱 < 0 且 当日 > 昨日，再看各条件能筛选出多少"""
import sys
sys.path.insert(0, '.')
import random
import numpy as np
import pandas as pd

from data_fetcher.baostock_fetcher import get_daily_kline_df, normalize_code
from indicators.mytt_indicators import calc_all_indicators
from data_fetcher.stock_pool import get_stock_pool_from_db
from scheduler.market_scan import _is_restricted

TARGET = '2026-06-29'
N = 400

all_codes = get_stock_pool_from_db()
codes = [c for c in all_codes if not _is_restricted(c)]
random.seed(11)
codes_sample = random.sample(codes, min(N, len(codes)))

pool_data = []
for code_raw in codes_sample:
    code = normalize_code(code_raw)
    df = get_daily_kline_df(code, start_date=None, end_date=TARGET)
    if df.empty or len(df) < 35 or str(df['date'].iloc[-1])[:10] != TARGET:
        continue
    df2 = calc_all_indicators(df)
    last = df2.iloc[-1]
    close = float(last['close'])
    if np.isnan(close) or close < 5.0:
        continue
    mc = df2['macd'].values.astype(float)
    dif = df2['dif'].values.astype(float)
    ma10 = float(last['ma10']) if 'ma10' in df2.columns and not pd.isna(last['ma10']) else None
    ma20 = float(last['ma20']) if 'ma20' in df2.columns and not pd.isna(last['ma20']) else None
    vol = float(last['volume'])
    vol_ma5 = float(last['vol_ma5']) if 'vol_ma5' in df2.columns and not pd.isna(last['vol_ma5']) else None
    prev_close = float(df2['close'].iloc[-2])
    pool_data.append((code, close, mc, dif, ma10, ma20, vol, vol_ma5, prev_close))

base = len(pool_data)
print(f"base = {base} 只有效股票\n")

# 第一层：macd < 0 且 macd[-1] > macd[-2]（简化版的"绿柱收缩"）
sub = []
for row in pool_data:
    code, close, mc, dif, ma10, ma20, vol, vol_ma5, prev_close = row
    if mc[-1] < 0 and len(mc) >= 2 and mc[-1] > mc[-2]:
        sub.append(row)
print(f"① MACD<0 且 当日 > 昨日 → {len(sub)} / {base}  =  {len(sub)/base*100:.1f}%")

# 第二层：再筛选不同的条件组合
tests = [
    ("DIF 拐头向上 (dif[-1]>dif[-2] & dif[-2]<=dif[-3])",
        lambda r: (len(r[3]) >= 3 and r[3][-1] > r[3][-2] and r[3][-2] <= r[3][-3])),
    ("收盘 > MA20",
        lambda r: (r[5] and r[1] > r[5])),
    ("收盘 > MA10",
        lambda r: (r[4] and r[1] > r[4])),
    ("阳线 + 量比 > 1.1",
        lambda r: (r[1] > r[8] and r[7] and r[7] > 0 and r[6] > r[7] * 1.1)),
    ("(收盘 > MA10) AND (阳线 + 量比 > 1.1)",
        lambda r: (r[4] and r[1] > r[4] and r[1] > r[8] and r[7] and r[7] > 0 and r[6] > r[7] * 1.1)),
    ("(收盘 > MA20) AND (阳线 + 量比 > 1.1)",
        lambda r: (r[5] and r[1] > r[5] and r[1] > r[8] and r[7] and r[7] > 0 and r[6] > r[7] * 1.1)),
    ("(收盘 > MA20) AND (阳线 + 量比 > 1.1) AND DIF拐头",
        lambda r: (r[5] and r[1] > r[5] and r[1] > r[8] and r[7] and r[7] > 0 and r[6] > r[7] * 1.1
                    and len(r[3]) >= 3 and r[3][-1] > r[3][-2] and r[3][-2] <= r[3][-3])),
    ("(收盘 > MA10) AND (阳线 + 量比 > 1.1) AND DIF拐头",
        lambda r: (r[4] and r[1] > r[4] and r[1] > r[8] and r[7] and r[7] > 0 and r[6] > r[7] * 1.1
                    and len(r[3]) >= 3 and r[3][-1] > r[3][-2] and r[3][-2] <= r[3][-3])),
    ("去掉 MA：仅(阳线 + 量比 > 1.1) AND DIF拐头",
        lambda r: (r[1] > r[8] and r[7] and r[7] > 0 and r[6] > r[7] * 1.1
                    and len(r[3]) >= 3 and r[3][-1] > r[3][-2] and r[3][-2] <= r[3][-3])),
]

print(f"\n在上述 {len(sub)} 只基础上，再叠加条件：")
for name, fn in tests:
    hits = sum(1 for r in sub if fn(r))
    pct = hits / base * 100
    proj = round(hits / base * 4282)
    print(f"  {name:<45s} : {hits:>4}/{len(sub):<4}  ≈ {hits/len(sub)*100:>5.1f}%  → 全市场 ≈ {proj} 只")
