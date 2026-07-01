from datetime import datetime, date, timedelta
from typing import List, Optional
from loguru import logger
import pandas as pd

from db.database import session_scope
from db.models import TradeSignal, StockInfo


def scan_signals(
    trade_date: Optional[str] = None,
    period: str = 'daily',
    signal_type: int = None,
    strategy: str = None,
    min_strength: float = 0,
    limit: int = 100,
) -> pd.DataFrame:
    """
    扫描指定日期的买卖信号
    """
    if trade_date is None:
        trade_date = date.today().strftime('%Y-%m-%d')

    with session_scope() as session:
        query = session.query(
            TradeSignal, StockInfo.name.label('stock_name')
        ).outerjoin(
            StockInfo, TradeSignal.code == StockInfo.code
        ).filter(
            TradeSignal.period == period
        )

        if period == 'daily':
            query = query.filter(TradeSignal.signal_time >= f"{trade_date} 00:00:00")
            query = query.filter(TradeSignal.signal_time <= f"{trade_date} 23:59:59")
        else:
            query = query.filter(TradeSignal.signal_time >= f"{trade_date} 09:00:00")
            query = query.filter(TradeSignal.signal_time <= f"{trade_date} 15:30:00")

        if signal_type is not None:
            query = query.filter(TradeSignal.signal_type == signal_type)
        if strategy:
            query = query.filter(TradeSignal.strategy == strategy)
        if min_strength > 0:
            query = query.filter(TradeSignal.signal_strength >= min_strength)

        query = query.order_by(TradeSignal.signal_strength.desc()).limit(limit)
        rows = query.all()

        if not rows:
            return pd.DataFrame()

        data = []
        for ts, name in rows:
            data.append({
                'code': ts.code,
                'name': name or '',
                'period': ts.period,
                'strategy': ts.strategy,
                'signal_type': '买入' if ts.signal_type == 1 else '卖出',
                'signal_time': ts.signal_time.strftime('%Y-%m-%d %H:%M:%S'),
                'price': float(ts.price),
                'signal_strength': float(ts.signal_strength),
                'reason': ts.reason or '',
                'description': ts.description or '',
            })
        return pd.DataFrame(data)


def scan_buy_signals(
    trade_date: Optional[str] = None,
    period: str = 'daily',
    min_strength: float = 60,
    limit: int = 50,
) -> pd.DataFrame:
    return scan_signals(
        trade_date=trade_date,
        period=period,
        signal_type=1,
        min_strength=min_strength,
        limit=limit,
    )


def scan_sell_signals(
    trade_date: Optional[str] = None,
    period: str = 'daily',
    min_strength: float = 60,
    limit: int = 50,
) -> pd.DataFrame:
    return scan_signals(
        trade_date=trade_date,
        period=period,
        signal_type=-1,
        min_strength=min_strength,
        limit=limit,
    )


def multi_period_resonance(
    trade_date: Optional[str] = None,
    signal_type: int = 1,
    min_strength: float = 50,
) -> pd.DataFrame:
    """
    日线信号扫描（原多周期共振简化为仅日线）
    """
    daily_df = scan_signals(
        trade_date=trade_date, period='daily',
        signal_type=signal_type, min_strength=min_strength
    )
    return daily_df


def print_signal_report(df: pd.DataFrame, title: str = "信号扫描结果"):
    if df.empty:
        logger.info(f"{title}: 无信号")
        return
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table(title=title, show_lines=False)
        table.add_column("代码", style="cyan")
        table.add_column("名称", style="white")
        table.add_column("策略", style="magenta")
        table.add_column("类型", style="green" if 'buy' in title.lower() else "red")
        table.add_column("时间", style="yellow")
        table.add_column("价格", justify="right")
        table.add_column("强度", justify="right", style="bold")
        table.add_column("原因", style="dim")
        for _, row in df.iterrows():
            table.add_row(
                row['code'],
                str(row.get('name', '')),
                row['strategy'],
                row['signal_type'],
                row['signal_time'],
                f"{row['price']:.2f}",
                f"{row['signal_strength']:.1f}",
                str(row.get('reason', '')),
            )
        console.print(table)
    except ImportError:
        logger.info(f"{title}:\n{df.to_string()}")
