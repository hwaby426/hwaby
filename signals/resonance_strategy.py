import pandas as pd
import numpy as np
from signals.base import BaseStrategy


class MACDKDJResonanceStrategy(BaseStrategy):
    name = "MACD+KDJ共振"
    description = "MACD+KDJ共振策略：多指标同时满足条件才发出信号"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 35:
            return signals
        dif = df['dif'].values
        dea = df['dea'].values
        macd = df['macd'].values
        k = df['k'].values
        d = df['d'].values
        close = df['close'].values
        ma20 = df['ma20'].values

        dif_prev = np.roll(dif, 1)
        dea_prev = np.roll(dea, 1)
        macd_prev = np.roll(macd, 1)
        k_prev = np.roll(k, 1)
        d_prev = np.roll(d, 1)
        close_prev = np.roll(close, 1)
        ma20_prev = np.roll(ma20, 1)
        for arr in [dif_prev, dea_prev, macd_prev, k_prev, d_prev, close_prev, ma20_prev]:
            arr[0] = np.nan

        macd_buy = (
            ((dif > dea) & (dif_prev <= dea_prev)) |
            ((dif > dea) & (dif_prev < dea_prev) & (macd > macd_prev))
        )
        kdj_buy = (k > d) & (k_prev <= d_prev) & (k < 55)
        ma_buy = (close > ma20) & (close_prev <= ma20_prev) | (close > ma20)

        buy = macd_buy & kdj_buy & (close > ma20)

        macd_sell = (
            ((dif < dea) & (dif_prev >= dea_prev)) |
            ((dif < dea) & (dif_prev > dea_prev) & (macd < macd_prev))
        )
        kdj_sell = (k < d) & (k_prev >= d_prev) & (k > 75)
        ma_sell = close < ma20

        sell = macd_sell & kdj_sell & ma_sell

        signals[buy] = 1
        signals[sell] = -1
        return signals

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            s = 60.0
            row = df.iloc[i]
            if sig == 1:
                if pd.notna(row.get('rsi6')) and row['rsi6'] > 50:
                    s += 10
                if pd.notna(row.get('vol_ma5')) and pd.notna(row.get('volume')):
                    if row['volume'] > row['vol_ma5'] * 1.2:
                        s += 10
                if pd.notna(row.get('upper')) and pd.notna(row.get('close')):
                    if row['close'] > row['mid']:
                        s += 10
                if pd.notna(row.get('ma60')) and row['close'] > row['ma60']:
                    s += 10
            else:
                if pd.notna(row.get('rsi6')) and row['rsi6'] < 50:
                    s += 10
                if pd.notna(row.get('vol_ma5')) and pd.notna(row.get('volume')):
                    if row['volume'] > row['vol_ma5'] * 1.2:
                        s += 10
                if pd.notna(row.get('lower')) and pd.notna(row.get('close')):
                    if row['close'] < row['mid']:
                        s += 10
                if pd.notna(row.get('ma60')) and row['close'] < row['ma60']:
                    s += 10
            strength.iloc[i] = min(s, 100)
        return strength
