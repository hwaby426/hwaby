from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
import pandas as pd
import numpy as np
from loguru import logger

from config.settings import settings


@dataclass
class Trade:
    code: str
    trade_type: int
    trade_time: str
    price: float
    quantity: int
    amount: float
    commission: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    hold_days: int = 0


@dataclass
class BacktestResult:
    code: str
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
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = None,
        commission_rate: float = None,
        stamp_duty_rate: float = None,
        slippage_rate: float = None,
        min_commission: float = None,
    ):
        self.initial_capital = initial_capital or settings.INITIAL_CAPITAL
        self.commission_rate = commission_rate or settings.COMMISSION_RATE
        self.stamp_duty_rate = stamp_duty_rate or settings.STAMP_DUTY_RATE
        self.slippage_rate = slippage_rate or settings.SLIPPAGE_RATE
        self.min_commission = min_commission or settings.MIN_COMMISSION

    def _calc_buy_cost(self, price: float, quantity: int) -> float:
        amount = price * quantity
        commission = max(amount * self.commission_rate, self.min_commission)
        return amount + commission

    def _calc_sell_income(self, price: float, quantity: int) -> float:
        amount = price * quantity
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_duty = amount * self.stamp_duty_rate
        return amount - commission - stamp_duty

    def run(
        self,
        df: pd.DataFrame,
        code: str,
        strategy_name: str,
        period: str = 'daily',
        t0: bool = False,  # T+0模式：当天买入当天可卖出
        signal_col: str = 'signal',
        date_col: str = 'date',
        open_col: str = 'open',
        close_col: str = 'close',
        trade_start_date: Optional[str] = None,  # 该日期之前只计算指标，不开仓；用于预热期
    ) -> BacktestResult:
        if df.empty:
            return BacktestResult(
                code=code, strategy=strategy_name, period=period,
                start_date='', end_date='',
                initial_capital=self.initial_capital,
                final_capital=self.initial_capital,
                total_return=0, annual_return=0, max_drawdown=0,
                sharpe_ratio=0, win_rate=0, total_trades=0, profit_factor=0,
            )

        df = df.copy().reset_index(drop=True)
        n = len(df)

        # --------------------------------------------------------------
        # 关键修复：在数据末尾追加一根"下一交易日合成K线"
        #
        # 场景：最后一根真实K线上的买入/卖出信号（收盘后确认），按 shift(1) 规则
        # 应该在下一交易日开盘执行。但下一交易日不在数据范围内，信号会被截断丢失。
        #
        # 解决：追加合成K线（open = close = high = low = 最后一根真实K线的 close），
        # 让 shift(1) 把信号移到这根合成K线上，用合成K线的 open 作为执行价。
        # 这与 scan-market 的"合成今日K线"逻辑完全一致。
        # --------------------------------------------------------------
        synthetic_row = {}
        for col in df.columns:
            if col == date_col:
                # 日期用最后一行日期 + "合成"标记，不影响交易日判断（current_date > last_buy_date 仍成立）
                synthetic_row[col] = str(df[col].iloc[-1])
            elif col == signal_col:
                # 合成K线本身不产生信号（信号是由最后一根真实K线产生的）
                synthetic_row[col] = 0
            elif col in [open_col, close_col, 'high', 'low', 'open', 'close', 'High', 'Low', 'Open', 'Close']:
                # open / close / high / low 都用最后一根真实K线的 close 作为预估下一交易日开盘
                synthetic_row[col] = float(df[close_col].iloc[-1])
            elif col == 'volume' or col == 'Volume' or col == 'vol':
                synthetic_row[col] = 0
            else:
                # 其他列（技术指标等）：保持与最后一行相同，避免 NaN
                val = df[col].iloc[-1]
                synthetic_row[col] = val

        df_synthetic = pd.DataFrame([synthetic_row], columns=df.columns)
        df = pd.concat([df, df_synthetic], ignore_index=True)
        n = len(df)  # 更新 n（多了一行合成K线）

        # 信号滞后一日执行：MACD 信号依赖当日收盘价，收盘后才能确认
        # signal[i]（第i日收盘后产生的信号）→ 用 shift(1) 使其在第i+1日生效
        # 这样交易价格 = 第i+1日的开盘价，符合实盘"次日交易"逻辑
        # 最后一根真实K线上的信号 → 被移到合成K线上执行 = 下一交易日开盘
        if signal_col in df.columns:
            df[signal_col] = df[signal_col].astype(float).shift(1).fillna(0).astype(int)

        cash = self.initial_capital
        position = 0
        cost_price = 0.0
        buy_date_idx = -1
        last_buy_date = None

        trades: List[Trade] = []
        equity_list = []

        # 记录实际开始交易的日期（用于结果报告）
        actual_first_trade_date = None

        for i in range(n):
            row = df.iloc[i]
            signal = int(row.get(signal_col, 0))
            open_price = float(row[open_col])
            close_price = float(row[close_col])
            date_str = str(row[date_col])
            current_date = date_str[:10]

            # 预热期：trade_start_date 之前不开仓，只计算权益曲线
            in_warmup = trade_start_date is not None and current_date < trade_start_date

            # T+0: 当天买入当天可卖出; T+1: 当天买入次日才能卖出
            if t0:
                can_sell = position > 0
            else:
                can_sell = (
                    position > 0 and
                    last_buy_date is not None and
                    current_date > last_buy_date
                )

            if signal == -1 and can_sell:
                sell_price = open_price * (1 - self.slippage_rate)
                sell_amount = sell_price * position
                commission = max(sell_amount * self.commission_rate, self.min_commission)
                stamp = sell_amount * self.stamp_duty_rate
                income = sell_amount - commission - stamp
                pnl = income - (cost_price * position)
                pnl_pct = (sell_price / cost_price - 1) * 100
                hold_days = i - buy_date_idx

                logger.info(
                    f"[卖出] [{strategy_name}] {date_str} {code} "
                    f"价格={round(sell_price, 3)} 数量={position} "
                    f"金额={round(sell_amount, 2)} 盈亏={round(pnl, 2)}({round(pnl_pct, 2)}%) "
                    f"持有天数={hold_days}"
                )

                trades.append(Trade(
                    code=code,
                    trade_type=-1,
                    trade_time=date_str,
                    price=round(sell_price, 3),
                    quantity=position,
                    amount=round(sell_amount, 2),
                    commission=round(commission + stamp, 2),
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 2),
                    hold_days=hold_days,
                ))
                cash += income
                position = 0
                cost_price = 0.0
                last_buy_date = None

            # 预热期不开新仓
            if signal == 1 and position == 0 and cash > 0 and not in_warmup:
                buy_price = open_price * (1 + self.slippage_rate)
                max_quantity = int(cash / (buy_price * 100)) * 100
                if max_quantity > 0:
                    total_cost = self._calc_buy_cost(buy_price, max_quantity)
                    if total_cost <= cash:
                        commission = max(buy_price * max_quantity * self.commission_rate, self.min_commission)

                        logger.info(
                            f"[买入] [{strategy_name}] {date_str} {code} "
                            f"价格={round(buy_price, 3)} 数量={max_quantity} "
                            f"金额={round(buy_price * max_quantity, 2)} 手续费={round(commission, 2)}"
                        )

                        if actual_first_trade_date is None:
                            actual_first_trade_date = date_str

                        trades.append(Trade(
                            code=code,
                            trade_type=1,
                            trade_time=date_str,
                            price=round(buy_price, 3),
                            quantity=max_quantity,
                            amount=round(buy_price * max_quantity, 2),
                            commission=round(commission, 2),
                        ))
                        cash -= total_cost
                        position = max_quantity
                        cost_price = buy_price
                        buy_date_idx = i
                        last_buy_date = current_date

            equity = cash + position * close_price
            equity_list.append(equity)

        equity_series = pd.Series(equity_list, index=df[date_col] if date_col in df.columns else range(n))
        final_capital = equity_list[-1]

        from backtest.metrics import calc_all_metrics
        metrics = calc_all_metrics(equity_series, trades)

        # 报告的 start_date：如果指定了 trade_start_date，用它；否则用数据第一行
        reported_start = trade_start_date if trade_start_date else str(df[date_col].iloc[0])

        return BacktestResult(
            code=code,
            strategy=strategy_name,
            period=period,
            start_date=reported_start,
            end_date=str(df[date_col].iloc[-1]),
            initial_capital=self.initial_capital,
            final_capital=round(final_capital, 2),
            total_return=round(metrics['total_return'], 2),
            annual_return=round(metrics['annual_return'], 2),
            max_drawdown=round(metrics['max_drawdown'], 2),
            sharpe_ratio=round(metrics['sharpe_ratio'], 3),
            win_rate=round(metrics['win_rate'], 2),
            total_trades=metrics['total_trades'],
            profit_factor=round(metrics['profit_factor'], 3),
            trades=trades,
            equity_curve=equity_series,
        )
