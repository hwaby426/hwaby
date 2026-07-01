import pandas as pd
import numpy as np
from signals.base import BaseStrategy


class MACrossStrategy(BaseStrategy):
    name = "均线金叉"
    description = "均线金叉死叉策略：MA5/MA10/MA20/MA60多周期金叉买入，死叉卖出"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 62:
            return signals

        ma5 = df['ma5'].values
        ma10 = df['ma10'].values
        ma20 = df['ma20'].values
        ma60 = df['ma60'].values

        ma5_prev = np.roll(ma5, 1)
        ma10_prev = np.roll(ma10, 1)
        ma20_prev = np.roll(ma20, 1)
        ma60_prev = np.roll(ma60, 1)
        ma5_prev[0] = np.nan
        ma10_prev[0] = np.nan
        ma20_prev[0] = np.nan
        ma60_prev[0] = np.nan

        ma5_cross_up = (ma5 > ma10) & (ma5_prev <= ma10_prev)
        ma5_cross_down = (ma5 < ma10) & (ma5_prev >= ma10_prev)

        ma10_cross_up = (ma10 > ma20) & (ma10_prev <= ma20_prev)
        ma10_cross_down = (ma10 < ma20) & (ma10_prev >= ma20_prev)

        ma20_cross_up = (ma20 > ma60) & (ma20_prev <= ma60_prev)
        ma20_cross_down = (ma20 < ma60) & (ma20_prev >= ma60_prev)

        buy_signals = ma5_cross_up | ma10_cross_up | ma20_cross_up
        sell_signals = ma5_cross_down | ma10_cross_down | ma20_cross_down

        signals[buy_signals] = 1
        signals[sell_signals] = -1
        return signals

    def calc_reason(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        reasons = pd.Series([''] * len(df), index=df.index)
        if len(df) < 62:
            return reasons

        ma5 = df['ma5'].values
        ma10 = df['ma10'].values
        ma20 = df['ma20'].values
        ma60 = df['ma60'].values

        ma5_prev = np.roll(ma5, 1)
        ma10_prev = np.roll(ma10, 1)
        ma20_prev = np.roll(ma20, 1)
        ma60_prev = np.roll(ma60, 1)
        ma5_prev[0] = np.nan
        ma10_prev[0] = np.nan
        ma20_prev[0] = np.nan
        ma60_prev[0] = np.nan

        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            reason_list = []
            if sig == 1:
                if ma5[i] > ma10[i] and ma5_prev[i] <= ma10_prev[i]:
                    reason_list.append('MA5金叉MA10')
                if ma10[i] > ma20[i] and ma10_prev[i] <= ma20_prev[i]:
                    reason_list.append('MA10金叉MA20')
                if ma20[i] > ma60[i] and ma20_prev[i] <= ma60_prev[i]:
                    reason_list.append('MA20金叉MA60')
            else:
                if ma5[i] < ma10[i] and ma5_prev[i] >= ma10_prev[i]:
                    reason_list.append('MA5死叉MA10')
                if ma10[i] < ma20[i] and ma10_prev[i] >= ma20_prev[i]:
                    reason_list.append('MA10死叉MA20')
                if ma20[i] < ma60[i] and ma20_prev[i] >= ma60_prev[i]:
                    reason_list.append('MA20死叉MA60')
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
                if 'MA20金叉MA60' in reason:
                    s += 20
                elif 'MA10金叉MA20' in reason:
                    s += 10
                if pd.notna(row.get('ma60')) and row['close'] > row['ma60']:
                    s += 10
                if pd.notna(row.get('vol_ma5')) and pd.notna(row.get('volume')):
                    if row['volume'] > row['vol_ma5'] * 1.2:
                        s += 10
                if pd.notna(row.get('macd')) and row['macd'] > 0:
                    s += 10
            else:
                if 'MA20死叉MA60' in reason:
                    s += 20
                elif 'MA10死叉MA20' in reason:
                    s += 10
                if pd.notna(row.get('ma60')) and row['close'] < row['ma60']:
                    s += 10
                if pd.notna(row.get('vol_ma5')) and pd.notna(row.get('volume')):
                    if row['volume'] > row['vol_ma5'] * 1.2:
                        s += 10
                if pd.notna(row.get('macd')) and row['macd'] < 0:
                    s += 10
            strength.iloc[i] = min(s, 100)
        return strength
