import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import pandas as pd
from loguru import logger

PERIOD_MINUTES = {
    '5min': 5,
    '15min': 15,
    '30min': 30,
    '60min': 60,
}

MORNING_START = (9, 30)
MORNING_END = (11, 30)
AFTERNOON_START = (13, 0)
AFTERNOON_END = (15, 0)


def get_kline_start_time(dt: datetime, period_minutes: int) -> datetime:
    total_minutes = dt.hour * 60 + dt.minute
    start_total = (total_minutes // period_minutes) * period_minutes
    return dt.replace(
        hour=start_total // 60,
        minute=start_total % 60,
        second=0,
        microsecond=0,
    )


def is_within_trading_hours(dt: datetime) -> bool:
    t = (dt.hour, dt.minute)
    if MORNING_START <= t <= MORNING_END:
        return True
    if AFTERNOON_START <= t <= AFTERNOON_END:
        return True
    return False


@dataclass
class KlineBar:
    code: str
    period: str
    kline_time: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    amount: float = 0.0
    closed: bool = False


class MinKlineBuilder:
    def __init__(self, code: str, periods: List[str] = None):
        self.code = code
        self.periods = periods or ['5min', '15min']
        self._current_bars: Dict[str, Optional[KlineBar]] = {p: None for p in self.periods}
        self._closed_bars: Dict[str, List[KlineBar]] = {p: [] for p in self.periods}
        self._last_volume = 0
        self._last_amount = 0.0
        self._initialized = False

    def init_from_history(self, history_dfs: Dict[str, pd.DataFrame]):
        for period, df in history_dfs.items():
            if df.empty:
                continue
            for _, row in df.iterrows():
                bar = KlineBar(
                    code=self.code,
                    period=period,
                    kline_time=pd.to_datetime(row['kline_time']),
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=int(row.get('volume', 0)),
                    amount=float(row.get('amount', 0)),
                    closed=True,
                )
                self._closed_bars[period].append(bar)
        self._initialized = True
        logger.debug(f"{self.code} 分钟K线初始化完成，5min: {len(self._closed_bars.get('5min', []))} 根")

    def on_tick(self, tick: dict) -> Dict[str, List[KlineBar]]:
        price = float(tick.get('price', 0))
        volume = int(tick.get('volume', 0))
        amount = float(tick.get('amount', 0))
        tick_time_str = tick.get('time', '')

        if not tick_time_str:
            now = datetime.now()
        else:
            try:
                now = datetime.strptime(tick_time_str, '%H:%M:%S')
                now = datetime.combine(datetime.now().date(), now.time())
            except ValueError:
                now = datetime.now()

        if price <= 0:
            return {}

        vol_diff = max(0, volume - self._last_volume)
        amt_diff = max(0.0, amount - self._last_amount)
        if self._last_volume == 0:
            vol_diff = 0
            amt_diff = 0.0
        self._last_volume = volume
        self._last_amount = amount

        newly_closed = {}

        for period in self.periods:
            period_min = PERIOD_MINUTES[period]
            bar_start = get_kline_start_time(now, period_min)

            current = self._current_bars[period]

            if current is None or current.kline_time != bar_start:
                if current is not None and not current.closed:
                    current.closed = True
                    self._closed_bars[period].append(current)
                    newly_closed.setdefault(period, []).append(current)

                current = KlineBar(
                    code=self.code,
                    period=period,
                    kline_time=bar_start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=vol_diff,
                    amount=amt_diff,
                    closed=False,
                )
                self._current_bars[period] = current
            else:
                current.high = max(current.high, price)
                current.low = min(current.low, price)
                current.close = price
                current.volume += vol_diff
                current.amount += amt_diff

        return newly_closed

    def close_current_bars(self) -> Dict[str, KlineBar]:
        result = {}
        for period in self.periods:
            current = self._current_bars[period]
            if current is not None and not current.closed:
                current.closed = True
                self._closed_bars[period].append(current)
                result[period] = current
                self._current_bars[period] = None
        return result

    def get_recent_bars(self, period: str, n: int = 100) -> pd.DataFrame:
        bars = list(self._closed_bars.get(period, []))
        current = self._current_bars.get(period)
        if current is not None:
            bars.append(current)
        if not bars:
            return pd.DataFrame()
        bars = bars[-n:]
        data = []
        for bar in bars:
            data.append({
                'code': bar.code,
                'period': bar.period,
                'kline_time': bar.kline_time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'amount': bar.amount,
                'closed': bar.closed,
            })
        return pd.DataFrame(data)

    def get_current_bar(self, period: str) -> Optional[KlineBar]:
        return self._current_bars.get(period)
