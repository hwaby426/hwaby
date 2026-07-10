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

    买入触发条件（全部满足，缺一不可）：
      1. 位置确认：MACD 柱 < 0（处于绿柱区，满足左侧预判）
      2. 加速确认：从前天 (i-2) 到今天，MACD 柱回升 ≥ 30%（仅看两天跨度的收窄幅度，不要求连续增大）
      3. 方向确认：DIF[i] > DIF[i-1] 且 DIF[i-1] <= DIF[i-2]（DIF 拐头向上）
      4. 趋势过滤：收盘价 > MA10 / MA20 中任意一条（站上 10 日或 20 日均线，避免下跌中继）
      5. 资金确认：收阳线 且 成交量 > 5日均量 * 1.1（真金白银温和放量）
    """

    name = "MACD预测金叉"
    description = "MACD绿柱加速收缩 + DIF拐头 + 股价站上MA20 + 放量阳线"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        signals = pd.Series(np.zeros(len(df)), index=df.index, dtype=int)
        if len(df) < 35:
            return signals

        dif = df['dif'].values.astype(float)
        dea = df['dea'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)
        high = df['high'].values.astype(float) if 'high' in df.columns else None
        low = df['low'].values.astype(float) if 'low' in df.columns else None
        ma20 = df['ma20'].values.astype(float) if 'ma20' in df.columns else None

        vol = None
        vol_ma5 = None
        if 'volume' in df.columns and 'vol_ma5' in df.columns:
            vol = df['volume'].values.astype(float)
            vol_ma5 = df['vol_ma5'].values.astype(float)

        # 预计算"近6个交易日内是否存在涨停（涨幅>=9.5%）"
        limit_up = None
        if len(df) > 1 and 'close' in df.columns:
            prev_close = np.roll(close, 1)
            prev_close[0] = np.nan
            with np.errstate(invalid='ignore', divide='ignore'):
                daily_pct = np.where(
                    (prev_close > 0) & (~np.isnan(prev_close)) & (~np.isnan(close)),
                    (close - prev_close) / prev_close * 100.0,
                    0.0,
                )
            limit_up_window = np.zeros(len(df), dtype=bool)
            for i in range(len(df)):
                start = max(0, i - 5)
                window_max = np.nanmax(daily_pct[start:i + 1])
                if not np.isnan(window_max) and window_max >= 9.5:
                    limit_up_window[i] = True
            limit_up = limit_up_window

        n = len(df)
        for i in range(3, n):
            # 过滤 1：股价 < 5 元的低价股不参与预测金叉
            if np.isnan(close[i]) or close[i] < 5.0:
                continue

            # 过滤 2：近一周（含当日，向前 6 个交易日）出现过涨停（涨幅>=9.5%）
            if limit_up is not None and limit_up[i]:
                continue

            # 条件 1：位置确认 —— MACD 柱 < 0（处于绿柱区）
            if not (macd_col[i] < 0):
                continue

            # 条件 2：加速确认 —— 从前天 (i-2) 到今天，MACD 柱从低点回升 ≥ 30%
            #   （不再看3天内最低点，也不要求逐日递增，仅看两天跨度的收窄幅度）
            if i < 2:
                continue
            if np.isnan(macd_col[i-2]) or np.isnan(macd_col[i]):
                continue
            if macd_col[i-2] >= 0 or macd_col[i] >= 0:
                continue
            # 比例：(macd[i] - macd[i-2]) / |macd[i-2]|
            # 例：前天=-0.8，今天=-0.4，比例 = 0.4/0.8 = 0.5 = 50%
            ratio_two_day = (macd_col[i] - macd_col[i-2]) / abs(macd_col[i-2])
            if ratio_two_day < 0.30:
                continue

            # 条件 3：方向确认 —— DIF 拐头向上
            if not (dif[i] > dif[i-1]):
                continue
            if not (dif[i-1] <= dif[i-2]):
                continue

            # 条件 4：趋势过滤 —— 收盘价 > MA10 / MA20 中任意一条
            ma10 = df['ma10'].values.astype(float) if 'ma10' in df.columns else None
            ma20_cond = (ma20 is not None and not np.isnan(ma20[i]) and close[i] > ma20[i])
            ma10_cond = (ma10 is not None and not np.isnan(ma10[i]) and close[i] > ma10[i])
            if not (ma20_cond or ma10_cond):
                continue

            # 条件 5：资金确认 —— 收阳线 且 成交量 > 5日均量 * 1.1
            if high is not None and low is not None:
                body_real = (close[i] > close[i-1])  # 收阳线（相对昨日收盘价）
            else:
                body_real = (close[i] > close[i-1])
            if not body_real:
                continue
            if vol is not None and vol_ma5 is not None:
                if np.isnan(vol[i]) or np.isnan(vol_ma5[i]) or vol_ma5[i] <= 0:
                    continue
                if not (vol[i] > vol_ma5[i] * 1.1):
                    continue

            signals.iloc[i] = 1

        return signals

    def calc_reason(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        reasons = pd.Series([''] * len(df), index=df.index)
        if len(df) < 35:
            return reasons

        dif = df['dif'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)

        for i in range(len(df)):
            if int(signals.iloc[i]) != 1:
                continue
            parts = []
            parts.append(f"MACD柱={macd_col[i]:.4f}")
            if i >= 2 and not np.isnan(macd_col[i-2]) and macd_col[i-2] < 0 and macd_col[i] < 0:
                ratio_two_day = (macd_col[i] - macd_col[i-2]) / abs(macd_col[i-2])
                parts.append(f"从前天回升={ratio_two_day * 100:.1f}%")
            parts.append(f"DIF={dif[i]:.4f}(拐头向上)")
            # 报告能站上哪条均线就显示哪条（MA10 / MA20）
            for col, label in (('ma10', 'MA10'), ('ma20', 'MA20')):
                if col in df.columns:
                    val = float(df[col].iloc[i])
                    if not np.isnan(val) and val > 0 and close[i] > val:
                        parts.append(f"收>{label}({close[i]:.2f}>{val:.2f})")
                        break
            if 'volume' in df.columns and 'vol_ma5' in df.columns:
                vi = float(df['volume'].iloc[i])
                vm5 = float(df['vol_ma5'].iloc[i])
                if vm5 > 0:
                    parts.append(f"量比={vi / vm5:.2f}")
            reasons.iloc[i] = " | ".join(parts)
        return reasons

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        if len(df) < 35:
            return strength

        dif = df['dif'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)

        for i in range(len(df)):
            if int(signals.iloc[i]) != 1:
                continue
            s = 60.0

            # 1) MACD 柱增量放大 → 加分
            if i >= 2:
                delta_i = macd_col[i] - macd_col[i-1]
                delta_i_1 = macd_col[i-1] - macd_col[i-2]
                if delta_i_1 > 0 and delta_i / delta_i_1 > 1.5:
                    s += 15
                elif delta_i > delta_i_1:
                    s += 8

            # 2) DIF 拐头角度
            if i >= 2:
                turn = (dif[i] - dif[i-1]) + max(0, dif[i-1] - dif[i-2])
                if turn > 0.05:
                    s += 10
                elif turn > 0:
                    s += 5

            # 3) 收 > MA20 距离
            if 'ma20' in df.columns:
                ma20 = float(df['ma20'].iloc[i]) if not pd.isna(df['ma20'].iloc[i]) else None
                if ma20 is not None and ma20 > 0:
                    bias = (close[i] - ma20) / ma20
                    if bias > 0.03:
                        s += 10
                    elif bias > 0:
                        s += 5

            # 4) 量比（温和放量）
            if 'volume' in df.columns and 'vol_ma5' in df.columns:
                vi = float(df['volume'].iloc[i])
                vm5 = float(df['vol_ma5'].iloc[i])
                if vm5 > 0:
                    ratio = vi / vm5
                    if ratio >= 2.5:
                        s += 15
                    elif ratio >= 2.0:
                        s += 10
                    elif ratio > 1.2:
                        s += 5

            strength.iloc[i] = min(s, 100)
        return strength

    def diagnose_last_row(self, df: pd.DataFrame) -> Dict[str, Any]:
        """对最后一根K线逐条检查新的 6 个买入条件，返回详细诊断"""
        n = len(df)
        base = super().diagnose_last_row(df)
        if n < 35:
            return base

        conditions = []

        dif = df['dif'].values.astype(float)
        macd_col = df['macd'].values.astype(float)
        close = df['close'].values.astype(float)
        ma20 = df['ma20'].values.astype(float) if 'ma20' in df.columns else None
        vol = df['volume'].values.astype(float) if 'volume' in df.columns else None
        vol_ma5 = df['vol_ma5'].values.astype(float) if 'vol_ma5' in df.columns else None

        i = n - 1

        # 过滤 1：价格
        ok = not (np.isnan(close[i]) or close[i] < 5.0)
        conditions.append({
            'name': '价格 >= 5元',
            'ok': bool(ok),
            'detail': f"当前价={close[i]:.2f}" if ok else f"当前价={close[i]:.2f}，低于5元阈值",
        })

        # 过滤 2：近 6 天是否有涨停
        wmax = 0.0
        limit_up_hit = False
        if n > 1:
            prev_close = np.roll(close, 1)
            prev_close[0] = np.nan
            with np.errstate(invalid='ignore', divide='ignore'):
                daily_pct = np.where(
                    (prev_close > 0) & (~np.isnan(prev_close)) & (~np.isnan(close)),
                    (close - prev_close) / prev_close * 100.0,
                    0.0,
                )
            start = max(0, i - 5)
            seg = daily_pct[start:i + 1]
            all_nan = bool(np.all(np.isnan(seg))) if seg.size else True
            wmax = 0.0 if all_nan else float(np.nanmax(seg))
            limit_up_hit = (not np.isnan(wmax)) and wmax >= 9.5
        conditions.append({
            'name': '近1周未涨停',
            'ok': bool(not limit_up_hit),
            'detail': f"近6日最高涨幅={wmax:.2f}%" if n > 1 else "数据不足",
        })

        # 条件 1：MACD 柱 < 0（绿柱）
        ok = macd_col[i] < 0
        conditions.append({
            'name': 'MACD绿柱（MACD < 0）',
            'ok': bool(ok),
            'detail': f"MACD={macd_col[i]:.4f}",
        })

        # 条件 2：从前天 (i-2) 到今天，MACD 柱回升 ≥ 30%
        if i >= 2 and not np.isnan(macd_col[i-2]) and not np.isnan(macd_col[i]) \
                and macd_col[i-2] < 0 and macd_col[i] < 0:
            ratio_two_day = (macd_col[i] - macd_col[i-2]) / abs(macd_col[i-2])
            ok = ratio_two_day >= 0.30
            detail = (f"macd[i]={macd_col[i]:.4f}, macd[i-2]={macd_col[i-2]:.4f}, "
                      f"从前天回升={ratio_two_day * 100:.1f}%（阈值30%）")
        else:
            ok, detail = False, "MACD柱数据不满足负数条件或索引不足"
        conditions.append({
            'name': 'MACD柱从前天回升≥30%',
            'ok': bool(ok),
            'detail': detail,
        })

        # 条件 3：DIF 拐头向上
        if i >= 2:
            ok = (dif[i] > dif[i-1]) and (dif[i-1] <= dif[i-2])
            conditions.append({
                'name': 'DIF拐头向上',
                'ok': bool(ok),
                'detail': f"DIF[{i}]={dif[i]:.4f}, [{i-1}]={dif[i-1]:.4f}, [{i-2}]={dif[i-2]:.4f}",
            })
        else:
            conditions.append({'name': 'DIF拐头向上', 'ok': False, 'detail': '索引不足'})

        # 条件 4：趋势过滤 —— 收盘价 > MA10 / MA20 中任意一条
        if ma20 is not None and not np.isnan(ma20[i]) and ma20[i] > 0:
            ma10 = df['ma10'].values.astype(float) if 'ma10' in df.columns else None
            ma20_cond = close[i] > ma20[i]
            ma10_cond = (ma10 is not None and not np.isnan(ma10[i]) and close[i] > ma10[i])
            ok = ma20_cond or ma10_cond
            detail_parts = []
            if ma10 is not None and not np.isnan(ma10[i]):
                detail_parts.append(f"close>ma10={ma10_cond}({close[i]:.2f} vs {ma10[i]:.2f})")
            detail_parts.append(f"close>ma20={ma20_cond}({close[i]:.2f} vs {ma20[i]:.2f})")
            conditions.append({
                'name': '收盘价 > MA10/MA20 中任意一条',
                'ok': bool(ok),
                'detail': '；'.join(detail_parts),
            })
        else:
            conditions.append({
                'name': '收盘价 > MA10/MA20 中任意一条',
                'ok': False,
                'detail': 'MA20 数据缺失',
            })

        # 条件 5：资金确认 —— 收阳线 且 量比 > 1.1
        if i >= 1:
            body_ok = close[i] > close[i-1]
        else:
            body_ok = False
        if vol is not None and vol_ma5 is not None and not np.isnan(vol[i]) and vol_ma5[i] > 0:
            ratio = vol[i] / vol_ma5[i]
            vol_ok = ratio > 1.1
            detail_vol = f"量比={ratio:.2f}（volume={vol[i]:.0f}, vol_ma5={vol_ma5[i]:.0f}）"
        else:
            vol_ok = False
            detail_vol = "成交量/5日均量 数据缺失"
        ok = body_ok and vol_ok
        conditions.append({
            'name': '收阳线 且 成交量 > 5日均量 * 1.1',
            'ok': bool(ok),
            'detail': f"阳线={'是' if body_ok else '否'} | {detail_vol}",
        })

        ok_all = all(c['ok'] for c in conditions)
        lines = [f"  {'✅' if c['ok'] else '❌'}  {c['name']} → {c['detail']}" for c in conditions]
        return {
            'ok': bool(ok_all),
            'conditions': conditions,
            'text': '\n'.join(lines),
        }
