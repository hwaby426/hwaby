import pandas as pd
import numpy as np
from signals.base import BaseStrategy


class BOLLStrategy(BaseStrategy):
    name = "BOLL突破"
    description = "BOLL突破策略：中轨突破+上下轨支撑压力"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 25:
            return signals
        close = df['close'].values
        mid = df['mid'].values
        upper = df['upper'].values
        lower = df['lower'].values

        close_prev = np.roll(close, 1)
        mid_prev = np.roll(mid, 1)
        upper_prev = np.roll(upper, 1)
        lower_prev = np.roll(lower, 1)
        close_prev[0] = np.nan
        mid_prev[0] = np.nan
        upper_prev[0] = np.nan
        lower_prev[0] = np.nan

        mid_cross_up = (close > mid) & (close_prev <= mid_prev)
        mid_cross_down = (close < mid) & (close_prev >= mid_prev)

        lower_support = (
            (close < lower) & (close_prev < lower_prev) &
            (close > close_prev)
        )
        upper_pressure = (
            (close > upper) & (close_prev > upper_prev) &
            (close < close_prev)
        )

        buy_signals = mid_cross_up | lower_support
        sell_signals = mid_cross_down | upper_pressure

        signals[buy_signals] = 1
        signals[sell_signals] = -1
        return signals

    def calc_reason(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        reasons = pd.Series([''] * len(df), index=df.index)
        if len(df) < 25:
            return reasons

        close = df['close'].values
        mid = df['mid'].values
        upper = df['upper'].values
        lower = df['lower'].values

        close_prev = np.roll(close, 1)
        mid_prev = np.roll(mid, 1)
        upper_prev = np.roll(upper, 1)
        lower_prev = np.roll(lower, 1)
        close_prev[0] = np.nan
        mid_prev[0] = np.nan
        upper_prev[0] = np.nan
        lower_prev[0] = np.nan

        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            reason_list = []
            if sig == 1:
                if close[i] > mid[i] and close_prev[i] <= mid_prev[i]:
                    reason_list.append('上穿中轨')
                if close[i] < lower[i] and close_prev[i] < lower_prev[i] and close[i] > close_prev[i]:
                    reason_list.append('下轨支撑')
            else:
                if close[i] < mid[i] and close_prev[i] >= mid_prev[i]:
                    reason_list.append('下穿中轨')
                if close[i] > upper[i] and close_prev[i] > upper_prev[i] and close[i] < close_prev[i]:
                    reason_list.append('上轨压力')
            reasons.iloc[i] = '; '.join(reason_list)
        return reasons

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            s = 50.0
            row = df.iloc[i]
            reason = ''
            if hasattr(self, 'calc_reason'):
                reason = str(self.calc_reason(df.loc[df.index[i:i+1]], signals.loc[signals.index[i:i+1]]).iloc[0])
            if sig == 1:
                if '下轨支撑' in reason:
                    s += 15
                if pd.notna(row.get('ma20')) and row['ma20'] > 0:
                    if row['close'] > row['ma20']:
                        s += 10
                if pd.notna(row.get('macd')) and row['macd'] > 0:
                    s += 10
                if pd.notna(row.get('vol_ma5')) and pd.notna(row.get('volume')):
                    if row['volume'] > row['vol_ma5'] * 1.3:
                        s += 15
            else:
                if '上轨压力' in reason:
                    s += 15
                if pd.notna(row.get('ma20')) and row['close'] < row['ma20']:
                    s += 15
                if pd.notna(row.get('macd')) and row['macd'] < 0:
                    s += 10
                if pd.notna(row.get('vol_ma5')) and pd.notna(row.get('volume')):
                    if row['volume'] > row['vol_ma5'] * 1.3:
                        s += 15
            strength.iloc[i] = min(s, 100)
        return strength
