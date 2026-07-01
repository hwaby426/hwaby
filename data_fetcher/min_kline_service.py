from datetime import datetime, timedelta
from typing import List, Optional
import pandas as pd
from loguru import logger

from db.database import session_scope
from db.models import MinKline
from data_fetcher.min_kline_builder import KlineBar


def get_last_min_kline_time(code: str, period: str) -> Optional[str]:
    """
    获取某只股票某周期分钟K线的最后一条时间
    返回格式: 'YYYY-MM-DD HH:MM:SS'，没有数据返回None
    """
    with session_scope() as session:
        last = (
            session.query(MinKline)
            .filter_by(code=code, period=period)
            .order_by(MinKline.kline_time.desc())
            .first()
        )
        if last:
            return last.kline_time.strftime('%Y-%m-%d %H:%M:%S')
        return None


def save_min_klines(code: str, period: str, bars: List[KlineBar]):
    if not bars:
        return
    with session_scope() as session:
        for bar in bars:
            existing = (
                session.query(MinKline)
                .filter_by(code=code, period=period, kline_time=bar.kline_time)
                .first()
            )
            if existing:
                existing.open = bar.open
                existing.high = bar.high
                existing.low = bar.low
                existing.close = bar.close
                existing.volume = bar.volume
                existing.amount = bar.amount
            else:
                mk = MinKline(
                    code=code,
                    period=period,
                    kline_time=bar.kline_time,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    amount=bar.amount,
                )
                session.add(mk)


def get_min_kline_df(
    code: str,
    period: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 5000,
) -> pd.DataFrame:
    # 支持字符串日期参数
    if start_date and not start_time:
        start_time = datetime.strptime(start_date, '%Y-%m-%d')
    if end_date and not end_time:
        end_time = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(hours=23, minutes=59)

    with session_scope() as session:
        query = session.query(MinKline).filter_by(code=code, period=period)
        if start_time:
            query = query.filter(MinKline.kline_time >= start_time)
        if end_time:
            query = query.filter(MinKline.kline_time <= end_time)
        query = query.order_by(MinKline.kline_time.asc())
        if not start_time and not end_time:
            query = query.limit(limit)
        rows = query.all()
        if not rows:
            return pd.DataFrame()
        data = []
        for r in rows:
            data.append({
                'kline_time': r.kline_time.strftime('%Y-%m-%d %H:%M:%S'),
                'code': r.code,
                'open': float(r.open),
                'high': float(r.high),
                'low': float(r.low),
                'close': float(r.close),
                'volume': int(r.volume or 0),
                'amount': float(r.amount or 0),
            })
        return pd.DataFrame(data)
