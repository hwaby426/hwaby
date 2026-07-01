from typing import List, Optional
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
from loguru import logger

from backtest.engine import BacktestEngine, BacktestResult
from indicators.mytt_indicators import calc_all_indicators
from signals.manager import get_strategy


@dataclass
class PortfolioResult:
    strategy: str
    period: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    total_trades: int
    profit_factor: float
    individual_results: List[BacktestResult] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)


def run_single_backtest(
    df_daily: pd.DataFrame,
    code: str,
    strategy_name: str,
    initial_capital: float = 100000,
    period: str = 'daily',
    t0: bool = False,  # T+0模式
    trade_start_date: Optional[str] = None,  # 该日期之前不开仓，用于指标预热
    check_volume: bool = True,
) -> BacktestResult:
    if df_daily.empty:
        raise ValueError("K线数据为空")

    df = calc_all_indicators(df_daily)
    strategy = get_strategy(strategy_name, check_volume=check_volume)
    signals = strategy.generate_signals(df)
    df['signal'] = signals.values

    engine = BacktestEngine(initial_capital=initial_capital)
    result = engine.run(
        df,
        code=code,
        strategy_name=strategy_name,
        period=period,
        t0=t0,
        signal_col='signal',
        date_col='date',
        open_col='open',
        close_col='close',
        trade_start_date=trade_start_date,
    )
    return result


def run_portfolio_backtest(
    data_dict: dict,
    strategy_name: str,
    initial_capital: float = 100000,
    position_mode: str = 'equal_weight',
    period: str = 'daily',
    t0: bool = False,  # T+0模式
    trade_start_date: Optional[str] = None,  # 该日期之前不开仓，用于指标预热
    check_volume: bool = True,
) -> PortfolioResult:
    """
    多标的组合回测
    - data_dict: {code: df_daily}
    - position_mode: equal_weight 等权分配
    """
    codes = list(data_dict.keys())
    if not codes:
        raise ValueError("股票列表为空")

    n_stocks = len(codes)
    per_stock_capital = initial_capital / n_stocks if position_mode == 'equal_weight' else initial_capital

    results = []
    all_equities = {}

    for code, df in data_dict.items():
        try:
            result = run_single_backtest(
                df,
                code=code,
                strategy_name=strategy_name,
                initial_capital=per_stock_capital,
                period=period,
                t0=t0,
                trade_start_date=trade_start_date,
                check_volume=check_volume,
            )
            results.append(result)
            all_equities[code] = result.equity_curve
        except Exception as e:
            logger.error(f"{code} 回测失败: {e}")

    if not results:
        return PortfolioResult(
            strategy=strategy_name, period=period,
            start_date='', end_date='',
            initial_capital=initial_capital, final_capital=initial_capital,
            total_return=0, annual_return=0, max_drawdown=0,
            sharpe_ratio=0, win_rate=0, total_trades=0, profit_factor=0,
        )

    equity_df = pd.DataFrame(all_equities)
    equity_df = equity_df.ffill().bfill()
    portfolio_equity = equity_df.sum(axis=1)

    from backtest.metrics import calc_all_metrics
    all_trades = []
    for r in results:
        all_trades.extend(r.trades)
    metrics = calc_all_metrics(portfolio_equity, all_trades)

    return PortfolioResult(
        strategy=strategy_name,
        period=period,
        start_date=results[0].start_date,
        end_date=results[-1].end_date,
        initial_capital=initial_capital,
        final_capital=round(float(portfolio_equity.iloc[-1]), 2),
        total_return=round(metrics['total_return'], 2),
        annual_return=round(metrics['annual_return'], 2),
        max_drawdown=round(metrics['max_drawdown'], 2),
        sharpe_ratio=round(metrics['sharpe_ratio'], 3),
        win_rate=round(metrics['win_rate'], 2),
        total_trades=metrics['total_trades'],
        profit_factor=round(metrics['profit_factor'], 3),
        individual_results=results,
        equity_curve=portfolio_equity,
    )
