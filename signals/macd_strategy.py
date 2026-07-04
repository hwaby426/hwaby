import pandas as pd
import numpy as np
from signals.base import BaseStrategy


class MACDCrossStrategy(BaseStrategy):
    name = "MACD金叉"

    def __init__(
        self,
        check_volume: bool = True,
        volume_ratio_low: float = 1.25,
        volume_ratio_high: float = 1.50,
    ):
        """
        MACD金叉死叉+零轴穿越策略。

        Args:
            check_volume: 买入信号是否要求放量（默认 True）。
                          传 False 则完全忽略成交量门槛。
            volume_ratio_low: 买入信号的最低放量倍数（成交量 / 5日均量，默认 1.25）。
                              仅当 check_volume=True 时生效。
            volume_ratio_high: 强度加分的放量倍数阈值（默认 1.50）。
                               仅当 check_volume=True 时生效。
        """
        self.check_volume = check_volume
        self.volume_ratio_low = volume_ratio_low
        self.volume_ratio_high = volume_ratio_high
        if check_volume:
            desc_vol = f"买入信号需当日成交量 >= {volume_ratio_low:.2f} × 5日均量"
        else:
            desc_vol = "不检查成交量"
        self.description = (
            f"MACD金叉死叉+零轴穿越策略：DIF上穿DEA或上穿零轴买入，"
            f"下穿DEA或下穿零轴卖出；{desc_vol}"
        )

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 35:
            return signals

        dif = df['dif'].values.astype(float)
        dea = df['dea'].values.astype(float)

        dif_prev = np.roll(dif, 1)
        dea_prev = np.roll(dea, 1)
        dif_prev[0] = np.nan
        dea_prev[0] = np.nan

        dif_cross_up = (dif > dea) & (dif_prev <= dea_prev)
        dif_cross_down = (dif < dea) & (dif_prev >= dea_prev)

        zero_cross_up = (dif > 0) & (dif_prev <= 0)
        zero_cross_down = (dif < 0) & (dif_prev >= 0)

        buy_signals = dif_cross_up | zero_cross_up
        sell_signals = dif_cross_down | zero_cross_down

        # 买入信号附加：当日成交量 ≥ volume_ratio_low × 5日均量
        if self.check_volume and 'volume' in df.columns and 'vol_ma5' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma5 = df['vol_ma5'].values.astype(float)
            with np.errstate(invalid='ignore', divide='ignore'):
                valid_vol = np.where(
                    (vol_ma5 > 0) & (~np.isnan(vol)) & (~np.isnan(vol_ma5)),
                    vol >= self.volume_ratio_low * vol_ma5,
                    False,
                )
            buy_signals = buy_signals & valid_vol

        signals[buy_signals] = 1
        signals[sell_signals] = -1
        return signals

    def calc_reason(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        reasons = pd.Series([''] * len(df), index=df.index)
        if len(df) < 35:
            return reasons

        dif = df['dif'].values.astype(float)
        dea = df['dea'].values.astype(float)

        dif_prev = np.roll(dif, 1)
        dea_prev = np.roll(dea, 1)
        dif_prev[0] = np.nan
        dea_prev[0] = np.nan

        # 量比文本仅在 check_volume=True 时添加，避免"只显示量比却没过滤"的混淆
        vol_ratio_str = [''] * len(df)
        if self.check_volume and 'volume' in df.columns and 'vol_ma5' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma5 = df['vol_ma5'].values.astype(float)
            with np.errstate(invalid='ignore', divide='ignore'):
                ratio = np.where(
                    (vol_ma5 > 0) & (~np.isnan(vol)) & (~np.isnan(vol_ma5)),
                    vol / vol_ma5,
                    np.nan,
                )
            for i in range(len(df)):
                if np.isnan(ratio[i]):
                    continue
                vol_ratio_str[i] = f"量比{ratio[i]:.2f}"

        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            reason_list = []
            if sig == 1:
                if dif[i] > dea[i] and dif_prev[i] <= dea_prev[i]:
                    reason_list.append('MACD金叉')
                if dif[i] > 0 and dif_prev[i] <= 0:
                    reason_list.append('MACD上穿零轴')
                if vol_ratio_str[i]:
                    reason_list.append(vol_ratio_str[i])
            else:
                if dif[i] < dea[i] and dif_prev[i] >= dea_prev[i]:
                    reason_list.append('MACD死叉')
                if dif[i] < 0 and dif_prev[i] >= 0:
                    reason_list.append('MACD下穿零轴')
                if vol_ratio_str[i]:
                    reason_list.append(vol_ratio_str[i])
            reasons.iloc[i] = '; '.join(reason_list)
        return reasons

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        vol_ratio = None
        if self.check_volume and 'volume' in df.columns and 'vol_ma5' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma5 = df['vol_ma5'].values.astype(float)
            with np.errstate(invalid='ignore', divide='ignore'):
                vol_ratio = np.where(
                    (vol_ma5 > 0) & (~np.isnan(vol)) & (~np.isnan(vol_ma5)),
                    vol / vol_ma5,
                    np.nan,
                )

        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            s = 55.0
            row = df.iloc[i]
            reason = ''
            if hasattr(self, 'calc_reason'):
                reason = str(
                    self.calc_reason(
                        df.loc[df.index[i:i+1]], signals.loc[signals.index[i:i+1]]
                    ).iloc[0]
                )
            if sig == 1:
                if 'MACD上穿零轴' in reason:
                    s += 15
                if pd.notna(row.get('ma20')) and float(row['close']) > float(row['ma20']):
                    s += 10
                if pd.notna(row.get('k')) and pd.notna(row.get('d')):
                    if float(row['k']) > float(row['d']):
                        s += 10
                if pd.notna(row.get('rsi6')) and 30 < float(row['rsi6']) < 70:
                    s += 10
                # 成交量放大加分：越放量强度越高（仅当 check_volume=True 时）
                if vol_ratio is not None:
                    if not np.isnan(vol_ratio[i]):
                        if vol_ratio[i] >= self.volume_ratio_high:
                            s += 10
                        elif vol_ratio[i] >= self.volume_ratio_low:
                            s += 5
            else:
                if 'MACD下穿零轴' in reason:
                    s += 15
                if pd.notna(row.get('ma20')) and float(row['close']) < float(row['ma20']):
                    s += 10
                if pd.notna(row.get('k')) and pd.notna(row.get('d')):
                    if float(row['k']) < float(row['d']):
                        s += 10
                if pd.notna(row.get('rsi6')) and float(row['rsi6']) > 70:
                    s += 10
            strength.iloc[i] = min(s, 100)
        return strength


class MACDPredictiveCrossStrategy(BaseStrategy):
    """MACD预测金叉策略（底部预判型）

    买入条件（全部满足）：
      1. MACD柱 < 0              → 当前仍处于绿柱区（DIF < DEA）
      2. MACD柱连续3天增大        → MACD[i] > MACD[i-1] > MACD[i-2] > MACD[i-3]
      3. DIF拐头向上              → DIF[i] > DIF[i-1] 且 DIF[i-1] <= DIF[i-2]
      4. 当日成交量 ≥ 1.25 × 20日均量  → volume[i] >= 1.25 * vol_ma20[i]
    """

    name = "MACD预测金叉"
    description = "MACD绿柱区连续2日收敛30%+ + DIF向上 + 放量 + 明日涨幅<5%即可金叉"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 35:
            return signals

        dif = df['dif'].values.astype(float)
        dea = df['dea'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)
        ema12 = df['ema12'].values.astype(float) if 'ema12' in df.columns else None
        ema26 = df['ema26'].values.astype(float) if 'ema26' in df.columns else None

        vol = None
        vol_ma20 = None
        if 'volume' in df.columns and 'vol_ma20' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma20 = df['vol_ma20'].values.astype(float)

        alpha12 = 2.0 / 13.0
        alpha26 = 2.0 / 27.0
        dif_alpha = alpha12 - alpha26  # ≈ 0.0798

        n = len(df)
        for i in range(3, n):  # 至少需要 i-3
            # 条件 1：MACD 柱 < 0（仍在绿柱区，未金叉）
            if not (macd_col[i] < 0):
                continue

            # 条件 2：MACD 柱连续 2 天增大（3 天序列递增）
            if not (macd_col[i] > macd_col[i-1] > macd_col[i-2]):
                continue

            # 条件 3：3 天内 MACD 柱绝对值缩短 ≥ 30%
            if np.isnan(macd_col[i-3]) or abs(macd_col[i-3]) == 0:
                continue
            if not (abs(macd_col[i]) < 0.7 * abs(macd_col[i-3])):
                continue

            # 条件 4：DIF 今日向上（趋势变好）
            if not (dif[i] > dif[i-1]):
                continue

            # 条件 5：当日成交量 ≥ 1.25 × 20 日均量（放量确认）
            if vol is not None and vol_ma20 is not None:
                if np.isnan(vol[i]) or np.isnan(vol_ma20[i]) or vol_ma20[i] <= 0:
                    continue
                if not (vol[i] >= 1.25 * vol_ma20[i]):
                    continue

            # 条件 6：明天涨幅 < 5% 即可出现金叉
            if ema12 is not None and ema26 is not None and close[i] > 0:
                # DIF_next = dif_alpha * P + (1-alpha12)*ema12[i] - (1-alpha26)*ema26[i]
                # 临界条件：DIF_next >= DEA[i]
                const_term = (1 - alpha12) * ema12[i] - (1 - alpha26) * ema26[i]
                p_cross = (dea[i] - const_term) / dif_alpha
                needed_pct = (p_cross - close[i]) / close[i] * 100.0
                if np.isnan(needed_pct) or needed_pct >= 5.0:
                    continue
                if needed_pct < 0:
                    continue  # 低于当前价 → 已金叉不需预测
                signals.iloc[i] = 1
            else:
                continue

        return signals

    def calc_reason(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        reasons = pd.Series([''] * len(df), index=df.index)
        if len(df) < 35:
            return reasons

        dif = df['dif'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)
        ema12 = df['ema12'].values.astype(float) if 'ema12' in df.columns else None
        ema26 = df['ema26'].values.astype(float) if 'ema26' in df.columns else None
        vol = None
        vol_ma20 = None
        if 'volume' in df.columns and 'vol_ma20' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma20 = df['vol_ma20'].values.astype(float)

        alpha12 = 2.0 / 13.0
        alpha26 = 2.0 / 27.0
        dif_alpha = alpha12 - alpha26

        for i in range(len(df)):
            if int(signals.iloc[i]) != 1:
                continue
            parts = []
            parts.append(f"MACD柱={macd_col[i]:.4f}")
            if i >= 3 and not np.isnan(macd_col[i-3]) and abs(macd_col[i-3]) > 0:
                shrink_pct = (1 - abs(macd_col[i]) / abs(macd_col[i-3])) * 100
                parts.append(f"3日缩短{shrink_pct:.0f}%")
            parts.append(f"DIF={dif[i]:.4f}(向上)")

            if ema12 is not None and ema26 is not None and close[i] > 0:
                const_term = (1 - alpha12) * ema12[i] - (1 - alpha26) * ema26[i]
                p_cross = (df['dea'].values.astype(float)[i] - const_term) / dif_alpha
                needed_pct = (p_cross - close[i]) / close[i] * 100.0
                if 0 <= needed_pct < 5:
                    parts.append(f"需涨{needed_pct:.2f}%")

            if vol is not None and vol_ma20 is not None and not np.isnan(vol[i]) and vol_ma20[i] > 0:
                ratio = vol[i] / vol_ma20[i]
                parts.append(f"量比={ratio:.2f}")
            reasons.iloc[i] = " | ".join(parts)
        return reasons

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        if len(df) < 35:
            return strength

        dif = df['dif'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)
        ema12 = df['ema12'].values.astype(float) if 'ema12' in df.columns else None
        ema26 = df['ema26'].values.astype(float) if 'ema26' in df.columns else None
        vol = None
        vol_ma20 = None
        if 'volume' in df.columns and 'vol_ma20' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma20 = df['vol_ma20'].values.astype(float)

        alpha12 = 2.0 / 13.0
        alpha26 = 2.0 / 27.0
        dif_alpha = alpha12 - alpha26

        for i in range(len(df)):
            if int(signals.iloc[i]) != 1:
                continue
            s = 55.0

            # MACD柱3日缩短比例加分（缩得越多越强）
            if i >= 3 and not np.isnan(macd_col[i-3]) and abs(macd_col[i-3]) > 0:
                shrink_pct = (1 - abs(macd_col[i]) / abs(macd_col[i-3])) * 100
                if shrink_pct >= 80:
                    s += 20
                elif shrink_pct >= 70:
                    s += 15
                elif shrink_pct >= 50:
                    s += 10

            # 需涨幅度加分（涨越少越强）
            if ema12 is not None and ema26 is not None and close[i] > 0:
                const_term = (1 - alpha12) * ema12[i] - (1 - alpha26) * ema26[i]
                p_cross = (df['dea'].values.astype(float)[i] - const_term) / dif_alpha
                needed_pct = (p_cross - close[i]) / close[i] * 100.0
                if 0 <= needed_pct < 5:
                    if needed_pct < 0.5:
                        s += 20
                    elif needed_pct < 1.0:
                        s += 15
                    elif needed_pct < 2.0:
                        s += 10
                    else:
                        s += 5

            # 量比加分
            if vol is not None and vol_ma20 is not None and not np.isnan(vol[i]) and vol_ma20[i] > 0:
                ratio = vol[i] / vol_ma20[i]
                if ratio >= 2.0:
                    s += 15
                elif ratio >= 1.5:
                    s += 10
                elif ratio > 1.0:
                    s += 5

            # 价格在 MA20 上方加分（趋势健康）
            row = df.iloc[i]
            if pd.notna(row.get('ma20')) and float(row['close']) > float(row['ma20']):
                s += 10

            strength.iloc[i] = min(s, 100)
        return strength
