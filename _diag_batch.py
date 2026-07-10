"""批量诊断 2026-06-29：100 只股票统计各条件通过率"""
import sys
sys.path.insert(0, '.')
import random
import numpy as np
import pandas as pd

from data_fetcher.baostock_fetcher import get_daily_kline_df, normalize_code
from indicators.mytt_indicators import calc_all_indicators
from signals.macd_strategy import MACDPredictiveCrossStrategy
from data_fetcher.stock_pool import get_stock_pool_from_db
from scheduler.market_scan import _is_restricted

TARGET = '2026-06-29'
N = 200

all_codes = get_stock_pool_from_db()
codes = [c for c in all_codes if not _is_restricted(c)]
random.seed(7)
codes_sample = random.sample(codes, min(N, len(codes)))

strategy = MACDPredictiveCrossStrategy()

stats = {
    'total': 0,
    'has_data': 0,
    'close_ge_5': 0,
    'c1_macd_neg': 0,
    'c2_rise_accel': 0,
    'c3_dif_turnup': 0,
    'c4_close_above_ma20': 0,
    'c5_bull_vol': 0,
    'signal': 0,
}

for code_raw in codes_sample:
    code = normalize_code(code_raw)
    df = get_daily_kline_df(code, start_date=None, end_date=TARGET)
    if df.empty or len(df) < 35 or str(df['date'].iloc[-1])[:10] != TARGET:
        continue
    stats['total'] += 1
    df2 = calc_all_indicators(df)
    last = df2.iloc[-1]
    close = float(last['close'])
    if np.isnan(close) or close < 5.0:
        continue
    stats['close_ge_5'] += 1

    mc = df2['macd'].values.astype(float)
    dif = df2['dif'].values.astype(float)
    ma20 = float(last['ma20']) if 'ma20' in df2.columns and not pd.isna(last['ma20']) else None
    vol = float(last['volume'])
    vol_ma5 = float(last['vol_ma5']) if 'vol_ma5' in df2.columns and not pd.isna(last['vol_ma5']) else None
    prev_close = float(df2['close'].iloc[-2])

    if mc[-1] < 0:
        stats['c1_macd_neg'] += 1
    if len(mc) >= 3 and mc[-1] > mc[-2] > mc[-3] and (mc[-1] - mc[-2]) > (mc[-2] - mc[-3]):
        stats['c2_rise_accel'] += 1
    if len(dif) >= 3 and dif[-1] > dif[-2] and dif[-2] <= dif[-3]:
        stats['c3_dif_turnup'] += 1
    if ma20 and close > ma20:
        stats['c4_close_above_ma20'] += 1
    if close > prev_close and vol_ma5 and vol_ma5 > 0 and vol > vol_ma5 * 1.1:
        stats['c5_bull_vol'] += 1

    signals = strategy.generate_signals(df2)
    if int(signals.iloc[-1]) == 1:
        stats['signal'] += 1

print(f"样本: {len(codes_sample)} 只 → 有完整日线 & 日期匹配: {stats['total']} 只")
print(f"  价格 >= 5元                     : {stats['close_ge_5']}")
base = stats['close_ge_5']
print(f"\n以 price>=5 为基数 {base} 只的通过率:")
labels = [
    ('c1_macd_neg',            "条件1: MACD柱 < 0"),
    ('c2_rise_accel',         "条件2: 连续2日增大 + 加速"),
    ('c3_dif_turnup',         "条件3: DIF 拐头向上"),
    ('c4_close_above_ma20',   "条件4: 收盘 > MA20"),
    ('c5_bull_vol',           "条件5: 阳线 + 量比 > 1.1"),
]
for key, text in labels:
    print(f"  {text:<35s} : {stats[key]:>4} / {base}  =  {stats[key]/base*100:.1f}%")
print(f"\n最终信号: {stats['signal']} / {base} = {stats['signal']/base*100:.1f}%")
print(f"预计全市场 4282 只有效数据: 约 {round(stats['signal']/base*4282)} 个信号")
