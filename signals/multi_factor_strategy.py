import pandas as pd
import numpy as np
from signals.base import BaseStrategy


class MultiFactorScoreStrategy(BaseStrategy):
    name = "多因子打分"
    description = "多因子综合打分策略：7个维度加权打分，超过阈值发出信号"

    def __init__(self, buy_threshold=70, sell_threshold=30):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold

    def calc_factor_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        n = len(df)
        scores = pd.DataFrame(index=df.index)

        ma_trend = np.zeros(n)
        for i in range(n):
            row = df.iloc[i]
            if pd.notna(row.get('ma5')) and pd.notna(row.get('ma10')) and pd.notna(row.get('ma20')) and pd.notna(row.get('ma60')):
                if row['ma5'] > row['ma10'] > row['ma20'] > row['ma60']:
                    ma_trend[i] = 100
                elif row['ma5'] > row['ma10'] > row['ma20']:
                    ma_trend[i] = 75
                elif row['ma5'] > row['ma10']:
                    ma_trend[i] = 50
                elif row['ma5'] < row['ma10']:
                    ma_trend[i] = 25
                else:
                    ma_trend[i] = 40
            else:
                ma_trend[i] = 50
        scores['ma_trend'] = ma_trend

        macd_score = np.zeros(n)
        for i in range(n):
            row = df.iloc[i]
            if pd.notna(row.get('dif')) and pd.notna(row.get('dea')) and pd.notna(row.get('macd')):
                if row['dif'] > row['dea'] and row['macd'] > 0:
                    macd_score[i] = 100
                elif row['dif'] > row['dea'] and row['macd'] <= 0:
                    macd_score[i] = 70
                elif row['dif'] < row['dea'] and row['macd'] < 0:
                    macd_score[i] = 20
                elif row['dif'] < row['dea']:
                    macd_score[i] = 40
                else:
                    macd_score[i] = 50
            else:
                macd_score[i] = 50
        scores['macd'] = macd_score

        kdj_score = np.zeros(n)
        for i in range(n):
            row = df.iloc[i]
            if pd.notna(row.get('k')) and pd.notna(row.get('d')) and pd.notna(row.get('j')):
                if row['k'] < 20 and row['k'] > row['d']:
                    kdj_score[i] = 100
                elif row['k'] < 40 and row['k'] > row['d']:
                    kdj_score[i] = 80
                elif row['k'] > row['d'] and 40 <= row['k'] <= 70:
                    kdj_score[i] = 60
                elif row['k'] > 80 and row['k'] < row['d']:
                    kdj_score[i] = 10
                elif row['k'] > 60 and row['k'] < row['d']:
                    kdj_score[i] = 30
                else:
                    kdj_score[i] = 50
            else:
                kdj_score[i] = 50
        scores['kdj'] = kdj_score

        rsi_score = np.zeros(n)
        for i in range(n):
            row = df.iloc[i]
            if pd.notna(row.get('rsi6')):
                rsi = row['rsi6']
                if 40 <= rsi <= 60:
                    rsi_score[i] = 80
                elif 30 <= rsi < 40:
                    rsi_score[i] = 100
                elif 60 < rsi <= 70:
                    rsi_score[i] = 60
                elif rsi < 30:
                    rsi_score[i] = 90
                elif rsi > 70:
                    rsi_score[i] = 20
                else:
                    rsi_score[i] = 50
            else:
                rsi_score[i] = 50
        scores['rsi'] = rsi_score

        boll_score = np.zeros(n)
        for i in range(n):
            row = df.iloc[i]
            if pd.notna(row.get('close')) and pd.notna(row.get('upper')) and pd.notna(row.get('mid')) and pd.notna(row.get('lower')):
                c = row['close']
                if c >= row['mid'] and c <= row['upper']:
                    boll_score[i] = 85
                elif c > row['upper']:
                    boll_score[i] = 40
                elif c < row['lower']:
                    boll_score[i] = 70
                elif c < row['mid']:
                    boll_score[i] = 45
                else:
                    boll_score[i] = 60
            else:
                boll_score[i] = 50
        scores['boll'] = boll_score

        vol_score = np.zeros(n)
        for i in range(n):
            row = df.iloc[i]
            if pd.notna(row.get('volume')) and pd.notna(row.get('vol_ma5')) and row['vol_ma5'] > 0:
                ratio = row['volume'] / row['vol_ma5']
                if 1.2 <= ratio <= 2.5:
                    vol_score[i] = 85
                elif ratio > 2.5:
                    vol_score[i] = 60
                elif 0.8 <= ratio < 1.2:
                    vol_score[i] = 60
                elif ratio < 0.5:
                    vol_score[i] = 40
                else:
                    vol_score[i] = 50
            else:
                vol_score[i] = 50
        scores['volume'] = vol_score

        return scores

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 65:
            return signals
        scores = self.calc_factor_scores(df)
        weights = {
            'ma_trend': 0.20,
            'macd': 0.25,
            'kdj': 0.20,
            'rsi': 0.14,
            'boll': 0.10,
            'volume': 0.11,
        }
        total_score = pd.Series(np.zeros(len(df)), index=df.index)
        for col, w in weights.items():
            total_score += scores[col] * w

        prev_score = total_score.shift(1)

        buy = (
            (total_score >= self.buy_threshold) &
            (prev_score < self.buy_threshold)
        )
        sell = (
            (total_score <= self.sell_threshold) &
            (prev_score > self.sell_threshold)
        )

        signals[buy] = 1
        signals[sell] = -1
        self._total_score = total_score
        return signals

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        total_score = getattr(self, '_total_score', None)
        if total_score is None:
            return strength
        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            s = float(total_score.iloc[i])
            strength.iloc[i] = max(0, min(100, s))
        return strength
