from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any
import pandas as pd
import numpy as np


@dataclass
class SignalRecord:
    code: str
    period: str
    strategy: str
    signal_type: int
    signal_time: str
    price: float
    signal_strength: float = 0.0
    reason: str = ""
    indicators: Dict[str, Any] = field(default_factory=dict)
    description: str = ""


class BaseStrategy(ABC):
    name: str = "base"
    description: str = ""

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """
        输入包含指标列的 DataFrame
        返回 signal 序列: 1=买入, -1=卖出, 0=观望
        """
        raise NotImplementedError

    def calc_strength(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        """
        计算信号强度，默认返回 50
        """
        strength = pd.Series(np.zeros(len(df)), index=df.index)
        strength[signals != 0] = 50.0
        return strength

    def calc_reason(self, df: pd.DataFrame, signals: pd.Series) -> pd.Series:
        """
        计算信号原因，默认返回空字符串
        """
        reason = pd.Series([''] * len(df), index=df.index)
        return reason

    def get_indicator_snapshot(self, df: pd.DataFrame, idx: int) -> Dict[str, Any]:
        """
        获取指定索引处的关键指标快照
        """
        row = df.iloc[idx]
        cols = [
            'close', 'ma5', 'ma10', 'ma20', 'ma60',
            'dif', 'dea', 'macd',
            'k', 'd', 'j',
            'rsi6', 'rsi12', 'rsi24',
            'upper', 'mid', 'lower',
            'cci', 'atr',
            'bias6', 'bias12', 'bias24',
            'wr10', 'wr6',
            'volume', 'vol_ma5', 'vol_ma10', 'vol_ma20',
        ]
        snap = {}
        for c in cols:
            if c in row and pd.notna(row[c]):
                val = float(row[c])
                snap[c] = round(val, 4) if abs(val) < 1e6 else round(val, 0)
        # 计算量比（当日成交量 / 5日均量），用于判断放量
        if 'volume' in snap and 'vol_ma5' in snap:
            if snap['vol_ma5'] and snap['vol_ma5'] > 0:
                snap['volume_ratio'] = round(snap['volume'] / snap['vol_ma5'], 3)
        return snap

    def generate_signal_records(
        self,
        df: pd.DataFrame,
        code: str,
        period: str,
        time_col: str = 'date',
    ) -> List[SignalRecord]:
        if df.empty:
            return []
        signals = self.generate_signals(df)
        strengths = self.calc_strength(df, signals)
        reasons = self.calc_reason(df, signals)
        records = []
        for i in range(len(df)):
            sig = int(signals.iloc[i])
            if sig == 0:
                continue
            row = df.iloc[i]
            records.append(SignalRecord(
                code=code,
                period=period,
                strategy=self.name,
                signal_type=sig,
                signal_time=str(row[time_col]),
                price=float(row['close']),
                signal_strength=float(strengths.iloc[i]),
                reason=str(reasons.iloc[i]) if reasons.iloc[i] else "",
                indicators=self.get_indicator_snapshot(df, i),
                description=f"{self.name} {'买入' if sig == 1 else '卖出'}信号",
            ))
        return records
