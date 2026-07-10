"""在相同 200 只样本上，测试不同的"条件2"变体"""
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
N = 200

all_codes = get_stock_pool_from_db()
codes = [c for c in all_codes if not _is_restricted(c)]
random.seed(7)
codes_sample = random.sample(codes, min(N, len(codes)))

# 收集 base pool（有数据 & 价格>=5）的数据
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
    ma20 = float(last['ma20']) if 'ma20' in df2.columns and not pd.isna(last['ma20']) else None
    vol = float(last['volume'])
    vol_ma5 = float(last['vol_ma5']) if 'vol_ma5' in df2.columns and not pd.isna(last['vol_ma5']) else None
    prev_close = float(df2['close'].iloc[-2])
    pool_data.append((code, close, mc, dif, ma20, vol, vol_ma5, prev_close))

base = len(pool_data)

def check_all(code, close, mc, dif, ma20, vol, vol_ma5, prev_close, *, c2_func):
    """固定条件1/3/4/5，只换条件2的实现"""
    if not (mc[-1] < 0):
        return False
    if not c2_func(mc):
        return False
    if not (len(dif) >= 3 and dif[-1] > dif[-2] and dif[-2] <= dif[-3]):
        return False
    if not (ma20 and close > ma20):
        return False
    if not (close > prev_close and vol_ma5 and vol_ma5 > 0 and vol > vol_ma5 * 1.1):
        return False
    return True

# 不同的条件2实现
variants = {
    "A) 仅 连续2日增大，不加加速要求":
        lambda mc: (len(mc) >= 3 and mc[-1] > mc[-2] > mc[-3]),
    "B) 仅 当日增大（macd[-1] > macd[-2]）":
        lambda mc: (len(mc) >= 2 and mc[-1] > mc[-2]),
    "C) 最近3日至少2日增大 且 总体上升":
        lambda mc: (len(mc) >= 3 and
                    (1 if mc[-1] > mc[-2] else 0) + (1 if mc[-2] > mc[-3] else 0) >= 1
                    and mc[-1] > mc[-3]),
    "D) 连续3日增大（更严格）":
        lambda mc: (len(mc) >= 4 and mc[-1] > mc[-2] > mc[-3] > mc[-4]),
    "当前版本：连续2日增大 + 增量也增大":
        lambda mc: (len(mc) >= 3 and mc[-1] > mc[-2] > mc[-3]
                    and (mc[-1] - mc[-2]) > (mc[-2] - mc[-3])),
}

print(f"样本 base = {base} 只股票\n")
print(f"{'条件2 版本':<45s}  :  命中  / {base}  =  占比    |  预计全市场")
print("-" * 95)
for name, fn in variants.items():
    hits = sum(1 for row in pool_data if check_all(*row, c2_func=fn))
    pct = hits / base * 100
    proj = round(hits / base * 4282)
    print(f"  {name:<40s} :  {hits:>4}  / {base}  =  {pct:>5.1f}%  |  ≈ {proj} 个信号")
