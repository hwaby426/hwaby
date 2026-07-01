import pandas as pd
import numpy as np
from signals.base import BaseStrategy


class KDJStrategy(BaseStrategy):
    name = "KDJ超买超卖"
    description = "KDJ超买超卖策略：低位金叉买入，高位死叉卖出"

    def __init__(self, overbought=80, oversold=20):
        self.overbought = overbought
        self.oversold = oversold

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 20:
            return signals
        k = df['k'].values
        d = df['d'].values

        k_prev = np.roll(k, 1)
        d_prev = np.roll(d, 1)
        k_prev[0] = np.nan
        d_prev[0] = np.nan

        buy = (
            (k < self.oversold) & (d < self.oversold + 10) &
            (k > d) & (k_prev <= d_prev)
        )
        sell = (
            (k > self.overbought) & (d > self.overbought - 10) &
            (k < d) & (k_prev >= d_prev)
        )

        signals[buy] = 1
        signals[sell] = -1
        return signals

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            s = 50.0
            row = df.iloc[i]
            if sig == 1:
                if pd.notna(row.get('rsi6')) and row['rsi6'] < 30:
                    s += 15
                if pd.notna(row.get('macd')) and row['macd'] > 0:
                    s += 10
                if pd.notna(row.get('j')) and row['j'] < 0:
                    s += 10
            else:
                if pd.notna(row.get('rsi6')) and row['rsi6'] > 70:
                    s += 15
                if pd.notna(row.get('macd')) and row['macd'] < 0:
                    s += 10
                if pd.notna(row.get('j')) and row['j'] > 100:
                    s += 10
            strength.iloc[i] = min(s, 100)
        return strength
