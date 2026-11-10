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
    is_index = Column(SmallInteger, default=0,
                      comment='是否指数 1是 0否（库里混入约286个指数，见 data/universe.py）')
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
    exit_kind = Column(String(20), comment='平仓原因：signal/stop_loss/take_profit/trailing/time_stop/liquidation')
    reason = Column(String(255), comment='平仓说明')
    created_at = Column(DateTime, default=datetime.now)


# ======================================================================
# ML 特征 / 预测记录（2026-10-31 新增）
#
# 这三张表对应方案 4.3 的 stock_features / model_predictions /
# prediction_results。设计要点：
#   - stock_features 每 (code, trade_date) 一行，特征只用 t 日及之前的数据，
#     标签用 t+1..t+n，两者在同一行上并存，便于直接取 X/y；
#   - label_valid=0 表示未来数据不足或停牌导致标签不可用，
#     **不能当成「没涨」参与训练**（尾样本、停牌、退市都会落到这里）；
#   - model_predictions 按 (code, 日期, 周期, 模型版本) 唯一，
#     保证「同一天用同一模型重跑」是幂等的。
# ======================================================================
class StockFeature(Base):
    __tablename__ = 'stock_features'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='sh.600000（与 daily_kline 一致，带点）')
    trade_date = Column(Date, nullable=False)

    # ── 原始量额（**不是模型特征**，跨股票不可比） ──
    # 用途：停牌判定（volume=0）、流动性过滤（amount 太小买不进）、
    # 成交额约束。缩量类因子若不加这层过滤，会把停牌股当成「最缩量」选中。
    volume = Column(DECIMAL(20, 2), comment='成交量（股）')
    amount = Column(DECIMAL(20, 2), comment='成交额（元）')

    # ── 价格特征（只用 t 日及之前） ──
    ret_1 = Column(DECIMAL(12, 6), comment='1日收益率')
    ret_3 = Column(DECIMAL(12, 6))
    ret_5 = Column(DECIMAL(12, 6))
    ret_10 = Column(DECIMAL(12, 6))
    ret_20 = Column(DECIMAL(12, 6))
    ma5_bias = Column(DECIMAL(12, 6), comment='(close/ma5-1)')
    ma10_bias = Column(DECIMAL(12, 6))
    ma20_bias = Column(DECIMAL(12, 6))
    gap_open = Column(DECIMAL(12, 6), comment='开盘缺口 open/pre_close-1')
    close_pos = Column(DECIMAL(12, 6), comment='收盘在当日高低区间的位置 0~1')
    high_dist_20 = Column(DECIMAL(12, 6), comment='距20日最高 close/hhv20-1')

    # ── 成交量特征 ──
    vol_ratio_5 = Column(DECIMAL(12, 6), comment='volume/MA5(volume)')
    vol_ratio_5_20 = Column(DECIMAL(12, 6), comment='MA5(vol)/MA20(vol)')
    turnover = Column(DECIMAL(12, 6))
    vol_surge_up = Column(SmallInteger, comment='放量上涨 1/0')
    vol_surge_dn = Column(SmallInteger, comment='放量下跌 1/0')

    # ── 技术指标 ──
    rsi_12 = Column(DECIMAL(12, 6))
    kdj_k = Column(DECIMAL(12, 6))
    kdj_d = Column(DECIMAL(12, 6))
    kdj_j = Column(DECIMAL(12, 6))
    macd_dif = Column(DECIMAL(12, 6))
    macd_dea = Column(DECIMAL(12, 6))
    macd_hist = Column(DECIMAL(12, 6))
    boll_pos = Column(DECIMAL(12, 6), comment='(close-lower)/(upper-lower)')
    cci = Column(DECIMAL(12, 6))
    atr_pct = Column(DECIMAL(12, 6), comment='ATR/close')

    # ── 波动 / 相对强度 ──
    volatility_20 = Column(DECIMAL(12, 6), comment='20日收益率标准差')
    max_dd_20 = Column(DECIMAL(12, 6), comment='20日最大回撤')
    rel_strength_20 = Column(DECIMAL(12, 6), comment='ret_20 减同日全市场中位数')
    vol_obv_slope_5 = Column(DECIMAL(12, 6), comment='OBV 5日斜率')

    # ── 上下文 ──
    days_since_ipo = Column(Integer, comment='上市天数（自然日）')
    is_st = Column(SmallInteger, comment='当日是否ST')

    # ── 标签（用 t+1..t+n，仅 label_valid=1 时可信） ──
    fwd_ret_1 = Column(DECIMAL(12, 6), comment='close[t+1]/close[t]-1（收盘口径）')
    fwd_ret_3 = Column(DECIMAL(12, 6))
    fwd_ret_5 = Column(DECIMAL(12, 6))
    # 可执行口径：t+1 开盘买入 → t+n 收盘卖出。方案 10.4 要求不能假设按收盘价成交
    fwd_openret_1 = Column(DECIMAL(12, 6), comment='close[t+1]/open[t+1]-1')
    fwd_openret_3 = Column(DECIMAL(12, 6), comment='close[t+3]/open[t+1]-1')
    fwd_openret_5 = Column(DECIMAL(12, 6), comment='close[t+5]/open[t+1]-1')
    fwd_open_gap = Column(DECIMAL(12, 6), comment='open[t+1]/close[t]-1，信号后追高成本')
    fwd_up_1 = Column(SmallInteger, comment='1涨 0不涨 NULL无效')
    fwd_up_3 = Column(SmallInteger)
    fwd_up_5 = Column(SmallInteger)
    fwd_excess_5 = Column(DECIMAL(12, 6), comment='fwd_ret_5 减同期全市场中位数')
    fwd_tradable_1 = Column(SmallInteger, comment='t+1 可买入 1/0')
    fwd_tradable_3 = Column(SmallInteger)
    fwd_tradable_5 = Column(SmallInteger)
    label_valid = Column(SmallInteger, comment='1=1/3/5日标签齐全且可成交')

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'trade_date', name='uk_feat_code_date'),
        Index('idx_feat_date', 'trade_date'),
    )


class ModelPrediction(Base):
    __tablename__ = 'model_predictions'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    predict_date = Column(Date, nullable=False, comment='生成预测的交易日（收盘后）')
    horizon = Column(SmallInteger, nullable=False, comment='1/3/5 个交易日')
    prob_up = Column(DECIMAL(8, 6), comment='模型估计上涨概率')
    exp_return = Column(DECIMAL(12, 6), comment='模型估计收益率')
    score = Column(DECIMAL(12, 6), comment='综合评分')
    rank = Column(Integer, comment='当日同 horizon 内排名')
    model_version = Column(String(64), nullable=False, comment='模型名+版本，如 lgbm_v1')
    reasons = Column(JSON, comment='入选原因（特征贡献等）')
    risk = Column(String(512), comment='风险提示')
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'predict_date', 'horizon', 'model_version',
                         name='uk_pred_code_date_h_mv'),
        Index('idx_pred_date', 'predict_date'),
    )


class PremarketScore(Base):
    """盘前评分留档 —— 让「这套分到底准不准」变成可以回答的问题。

    盘前分析原本跑完就丢（只出 HTML 和邮件），于是「综合分 +30.5 = 偏多」
    到底意味着次日涨还是跌，没有任何记录可查。没有留档就没有准确率，
    没有准确率这套分只能当参考，不能拿去下注。

    这张表只做一件事：每次盘前跑完把四个分项和结论记一行，事后用
    daily_kline 回填实际涨跌，算命中率 / 分层胜率。

    注意：**历史补不回来**。消息面和外围都是时点数据，昨天抓到的公告
    就是昨天的截面，重跑也拿不到当时的样子。所以样本只能往后攒。
    """

    __tablename__ = 'premarket_scores'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, comment='带点格式 sh.600000')
    symbol = Column(String(10), comment='纯数字，便于按 symbol 关联 stock_info')
    name = Column(String(32))
    trade_date = Column(Date, nullable=False,
                        comment='评分针对的交易日（盘后跑则推下一个交易日）')
    run_at = Column(DateTime, comment='实际开始跑的时刻，与 trade_date 不同')

    # 分项。env_score 是市场级指标，同一批次里每只票都一样，冗余存是为了
    # 让这一行能独立解释自己，不必再去翻当天的日志。
    env_score = Column(DECIMAL(8, 1), comment='外围环境 -100~+100')
    news_score = Column(DECIMAL(8, 1), comment='个股消息面')
    sector_score = Column(DECIMAL(8, 1), comment='板块维度')
    tech_score = Column(DECIMAL(8, 1), comment='技术位')
    total_score = Column(DECIMAL(8, 1), comment='加权综合分')
    alpha_score = Column(DECIMAL(8, 1), comment='剔除外围后的个股分')

    conclusion = Column(String(16), comment='明显看多/偏多/中性/偏空/明显看空')
    strength = Column(String(4), comment='强/中/弱')
    industry = Column(String(32))

    direct_news = Column(SmallInteger, comment='直接相关消息条数')
    market_news = Column(SmallInteger, comment='大盘层面消息条数')
    use_llm = Column(SmallInteger, comment='1=消息面经大模型精判，0=纯规则')

    ok = Column(SmallInteger, default=1, comment='0=该票分析失败，无有效分')
    error = Column(String(255))

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('code', 'trade_date', name='uk_pm_code_date'),
        Index('idx_pm_date', 'trade_date'),
    )


class PredictionResult(Base):
    __tablename__ = 'prediction_results'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id = Column(BigInteger, nullable=False, index=True)
    actual_return = Column(DECIMAL(12, 6), comment='实际收益')
    actual_direction = Column(String(8), comment='up/down/flat')
    hit = Column(SmallInteger, comment='方向是否命中 1/0')
    evaluated_at = Column(DateTime, default=datetime.now)
    note = Column(String(255), comment='不可评估原因（停牌/退市/数据缺失）')

    __table_args__ = (
        UniqueConstraint('prediction_id', name='uk_result_pred'),
    )
