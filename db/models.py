from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, DECIMAL, Text, JSON,
    BigInteger, SmallInteger, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class StockInfo(Base):
    __tablename__ = 'stock_info'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, unique=True, comment='股票代码 sh.600519')
    symbol = Column(String(10), comment='纯数字代码')
    name = Column(String(32), comment='股票名称')
    market = Column(String(4), comment='市场 sh/sz')
    industry = Column(String(32), comment='行业')
    list_date = Column(Date, comment='上市日期')
    is_st = Column(SmallInteger, default=0, comment='是否ST 1是 0否')
    status = Column(SmallInteger, default=1, comment='状态 1正常 0退市')
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class DailyKline(Base):
    __tablename__ = 'daily_kline'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    open = Column(DECIMAL(10, 3))
    high = Column(DECIMAL(10, 3))
    low = Column(DECIMAL(10, 3))
    close = Column(DECIMAL(10, 3))
    volume = Column(BigInteger, comment='成交量(手)')
    amount = Column(DECIMAL(18, 2), comment='成交额(元)')
    pct_chg = Column(DECIMAL(6, 2), comment='涨跌幅%')
    turnover = Column(DECIMAL(6, 2), comment='换手率%')
    obv = Column(DECIMAL(20, 2), comment='OBV能量潮')
    obv_ma = Column(DECIMAL(20, 2), comment='OBV均线')
    adjustflag = Column(SmallInteger, default=2, comment='复权 1后 2前 3不复权')
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'adjustflag', 'trade_date', name='uk_code_adjustflag_date'),
        Index('idx_code_adjustflag_date', 'code', 'adjustflag', 'trade_date'),
    )


class MinKline(Base):
    __tablename__ = 'min_kline'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    period = Column(String(6), nullable=False, index=True, comment='5min/15min')
    kline_time = Column(DateTime, nullable=False, index=True)
    open = Column(DECIMAL(10, 3))
    high = Column(DECIMAL(10, 3))
    low = Column(DECIMAL(10, 3))
    close = Column(DECIMAL(10, 3))
    volume = Column(BigInteger)
    amount = Column(DECIMAL(18, 2))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'period', 'kline_time', name='uk_code_period_time'),
        Index('idx_code_period_time', 'code', 'period', 'kline_time'),
    )


class TradeSignal(Base):
    __tablename__ = 'trade_signals'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    period = Column(String(10), nullable=False, index=True, comment='daily/5min/15min')
    strategy = Column(String(32), nullable=False, index=True, comment='策略名称')
    signal_type = Column(SmallInteger, nullable=False, index=True, comment='1买 -1卖 0观望')
    signal_time = Column(DateTime, nullable=False, index=True)
    price = Column(DECIMAL(10, 3), comment='信号价')
    signal_strength = Column(DECIMAL(5, 2), comment='信号强度 0-100')
    reason = Column(String(512), comment='信号触发原因')
    indicators = Column(JSON, comment='指标快照')
    description = Column(String(255), comment='描述')
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('idx_code_time', 'code', 'signal_time'),
        Index('idx_strategy_type', 'strategy', 'signal_type'),
    )


class Backtest(Base):
    __tablename__ = 'backtest'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(64), comment='回测名称')
    strategy = Column(String(32), comment='策略名称')
    period = Column(String(10), comment='周期')
    code = Column(String(10), comment='单标的代码')
    code_list = Column(Text, comment='多标的代码列表')
    start_date = Column(Date)
    end_date = Column(Date)
    initial_capital = Column(DECIMAL(15, 2))
    final_capital = Column(DECIMAL(15, 2))
    total_return = Column(DECIMAL(8, 2), comment='总收益率%')
    annual_return = Column(DECIMAL(8, 2), comment='年化%')
    max_drawdown = Column(DECIMAL(8, 2), comment='最大回撤%')
    sharpe_ratio = Column(DECIMAL(6, 3))
    win_rate = Column(DECIMAL(5, 2), comment='胜率%')
    total_trades = Column(Integer)
    profit_factor = Column(DECIMAL(6, 3), comment='盈亏比')
    commission = Column(DECIMAL(6, 4))
    slippage = Column(DECIMAL(6, 4))
    params = Column(JSON, comment='策略参数')
    created_at = Column(DateTime, default=datetime.now)


class BacktestTrade(Base):
    __tablename__ = 'backtest_trade'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    backtest_id = Column(BigInteger, nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    trade_type = Column(SmallInteger, comment='1买 -1卖')
    trade_time = Column(DateTime, nullable=False)
    price = Column(DECIMAL(10, 3))
    quantity = Column(Integer, comment='股数')
    amount = Column(DECIMAL(15, 2))
    commission = Column(DECIMAL(10, 2))
    pnl = Column(DECIMAL(15, 2), comment='盈亏')
    pnl_pct = Column(DECIMAL(8, 2), comment='盈亏%')
    hold_days = Column(Integer, comment='持仓天数')
    signal_id = Column(BigInteger, comment='关联信号')
    created_at = Column(DateTime, default=datetime.now)
