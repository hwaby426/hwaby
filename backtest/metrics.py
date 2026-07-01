import pandas as pd
import numpy as np
from typing import List

from backtest.engine import Trade


def calc_total_return(equity_curve: pd.Series) -> float:
    if equity_curve.empty or equity_curve.iloc[0] == 0:
        return 0.0
    return (equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) * 100


def calc_annual_return(equity_curve: pd.Series, trading_days_per_year: int = 252) -> float:
    if equity_curve.empty or equity_curve.iloc[0] == 0:
        return 0.0
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1
    n = len(equity_curve)
    if n <= 1:
        return 0.0
    annualized = (1 + total_return) ** (trading_days_per_year / n) - 1
    return annualized * 100


def calc_max_drawdown(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    peak = equity_curve.expanding().max()
    drawdown = (equity_curve - peak) / peak * 100
    return float(abs(drawdown.min()))


def calc_sharpe_ratio(
    equity_curve: pd.Series,
    risk_free_rate: float = 0.025,
    trading_days_per_year: int = 252,
) -> float:
    if equity_curve.empty or len(equity_curve) < 2:
        return 0.0
    daily_returns = equity_curve.pct_change().dropna()
    if len(daily_returns) == 0 or daily_returns.std() == 0:
        return 0.0
    excess_daily = daily_returns - risk_free_rate / trading_days_per_year
    sharpe = excess_daily.mean() / daily_returns.std() * np.sqrt(trading_days_per_year)
    return float(sharpe)


def calc_win_rate(trades: List[Trade]) -> float:
    sell_trades = [t for t in trades if t.trade_type == -1]
    if not sell_trades:
        return 0.0
    wins = [t for t in sell_trades if t.pnl > 0]
    return len(wins) / len(sell_trades) * 100


def calc_profit_factor(trades: List[Trade]) -> float:
    sell_trades = [t for t in trades if t.trade_type == -1]
    if not sell_trades:
        return 0.0
    gross_profit = sum(t.pnl for t in sell_trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in sell_trades if t.pnl < 0))
    if gross_loss == 0:
        return float('inf') if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def calc_max_consecutive_losses(trades: List[Trade]) -> int:
    sell_trades = [t for t in trades if t.trade_type == -1]
    if not sell_trades:
        return 0
    max_loss = 0
    current = 0
    for t in sell_trades:
        if t.pnl < 0:
            current += 1
            max_loss = max(max_loss, current)
        else:
            current = 0
    return max_loss


def calc_all_metrics(equity_curve: pd.Series, trades: List[Trade]) -> dict:
    sell_trades = [t for t in trades if t.trade_type == -1]
    return {
        'total_return': calc_total_return(equity_curve),
        'annual_return': calc_annual_return(equity_curve),
        'max_drawdown': calc_max_drawdown(equity_curve),
        'sharpe_ratio': calc_sharpe_ratio(equity_curve),
        'win_rate': calc_win_rate(trades),
        'total_trades': len(sell_trades),
        'profit_factor': calc_profit_factor(trades),
        'max_consecutive_losses': calc_max_consecutive_losses(trades),
    }
