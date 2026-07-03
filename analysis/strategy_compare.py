"""
策略对比分析模块
- 单股票多策略对比
- 多股票多策略综合对比
- 收益率排名
- 策略推荐
"""
from typing import List, Dict, Optional
from dataclasses import dataclass, field
import pandas as pd
from loguru import logger

from backtest.engine import BacktestResult
from backtest.portfolio import run_single_backtest
from signals.manager import get_strategy_names, get_strategy
from indicators.mytt_indicators import calc_all_indicators


@dataclass
class StrategyCompareResult:
    code: str
    period: str
    start_date: str
    end_date: str
    initial_capital: float
    results: List[BacktestResult] = field(default_factory=list)
    rankings: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def get_ranking_by(self, metric: str = 'total_return', ascending: bool = False) -> List[dict]:
        ranked = sorted(
            [{'strategy': r.strategy, metric: getattr(r, metric), 'result': r} for r in self.results],
            key=lambda x: x[metric],
            reverse=not ascending
        )
        return ranked

    def get_best_strategy(self, metric: str = 'total_return') -> Optional[BacktestResult]:
        ranked = self.get_ranking_by(metric=metric)
        return ranked[0]['result'] if ranked else None


def run_strategy_compare(
    df: pd.DataFrame,
    code: str,
    strategy_names: List[str] = None,
    initial_capital: float = 100000,
    period: str = 'daily',
    t0: bool = False,
    date_col: str = 'date',
    trade_start_date: Optional[str] = None,  # 该日期之前不开仓，用于指标预热
    check_volume: bool = True,
) -> StrategyCompareResult:
    """
    单只股票多策略对比回测

    Args:
        df: K线数据
        code: 股票代码
        strategy_names: 策略名称列表，None表示全部策略
        initial_capital: 初始资金
        period: K线周期
        t0: T+0模式（当天买入当天可卖出）
        date_col: 日期列名
        check_volume: 买入信号是否要求放量（MACD金叉策略专用，默认 True）

    Returns:
        StrategyCompareResult
    """
    if strategy_names is None:
        strategy_names = get_strategy_names()

    if df.empty:
        return StrategyCompareResult(
            code=code, period=period,
            start_date='', end_date='',
            initial_capital=initial_capital,
        )

    df_ind = calc_all_indicators(df.copy())
    results = []

    for strategy_name in strategy_names:
        try:
            strategy = get_strategy(strategy_name, check_volume=check_volume)
            signals = strategy.generate_signals(df_ind)
            df_ind_tmp = df_ind.copy()
            df_ind_tmp['signal'] = signals.values

            from backtest.engine import BacktestEngine
            engine = BacktestEngine(initial_capital=initial_capital)
            result = engine.run(
                df_ind_tmp,
                code=code,
                strategy_name=strategy_name,
                period=period,
                t0=t0,
                signal_col='signal',
                date_col=date_col,
                open_col='open',
                close_col='close',
                trade_start_date=trade_start_date,
            )
            results.append(result)
            logger.debug(f"[{code}] {strategy_name}: 总收益={result.total_return}% 交易次数={result.total_trades}")
        except Exception as e:
            logger.warning(f"[{code}] {strategy_name} 回测失败: {e}")

    start_date = str(df[date_col].iloc[0]) if not df.empty else ''
    end_date = str(df[date_col].iloc[-1]) if not df.empty else ''

    return StrategyCompareResult(
        code=code,
        period=period,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        results=results,
    )


def run_multi_stock_compare(
    data_dict: Dict[str, pd.DataFrame],
    strategy_names: List[str] = None,
    initial_capital: float = 100000,
    period: str = 'daily',
    t0: bool = False,  # T+0模式
    date_col: str = 'date',
    trade_start_date: Optional[str] = None,  # 该日期之前不开仓，用于指标预热
    check_volume: bool = True,
) -> (pd.DataFrame, pd.DataFrame):
    """
    多股票多策略综合对比

    Args:
        data_dict: {code: df}
        strategy_names: 策略名称列表，None表示全部策略
        initial_capital: 初始资金
        period: K线周期
        t0: T+0模式（当天买入当天可卖出）
        date_col: 日期列名
        check_volume: 买入信号是否要求放量（MACD金叉策略专用，默认 True）

    Returns:
        (summary_df, detail_df)
        summary_df: 策略汇总表
            columns: strategy, avg_return, avg_annual, avg_max_dd, avg_sharpe,
                     avg_winrate, avg_trades, win_stocks, total_stocks, avg_profit_factor, win_ratio
        detail_df: 每只股票每策略明细表
            columns: code, strategy, total_return, annual_return, max_drawdown,
                     sharpe_ratio, win_rate, total_trades, profit_factor
    """
    if strategy_names is None:
        strategy_names = get_strategy_names()

    all_results = {}
    for strategy_name in strategy_names:
        all_results[strategy_name] = []

    detail_rows = []
    for code, df in data_dict.items():
        if df.empty:
            continue
        compare = run_strategy_compare(
            df, code, strategy_names=strategy_names,
            initial_capital=initial_capital, period=period,
            t0=t0, date_col=date_col,
            trade_start_date=trade_start_date,
            check_volume=check_volume,
        )
        for result in compare.results:
            all_results[result.strategy].append(result)
            detail_rows.append({
                'code': code,
                'strategy': result.strategy,
                'total_return': result.total_return,
                'annual_return': result.annual_return,
                'max_drawdown': result.max_drawdown,
                'sharpe_ratio': round(result.sharpe_ratio, 3),
                'win_rate': result.win_rate,
                'total_trades': result.total_trades,
                'profit_factor': round(result.profit_factor, 3),
            })

    summary_rows = []
    for strategy_name, results in all_results.items():
        if not results:
            continue
        n_stocks = len(results)
        avg_return = sum(r.total_return for r in results) / n_stocks
        avg_annual = sum(r.annual_return for r in results) / n_stocks
        avg_max_dd = sum(r.max_drawdown for r in results) / n_stocks
        avg_sharpe = sum(r.sharpe_ratio for r in results) / n_stocks
        avg_winrate = sum(r.win_rate for r in results) / n_stocks
        avg_trades = sum(r.total_trades for r in results) / n_stocks
        avg_pf = sum(r.profit_factor for r in results if r.profit_factor != float('inf')) / n_stocks
        win_stocks = sum(1 for r in results if r.total_return > 0)

        summary_rows.append({
            'strategy': strategy_name,
            'avg_return': round(avg_return, 2),
            'avg_annual': round(avg_annual, 2),
            'avg_max_dd': round(avg_max_dd, 2),
            'avg_sharpe': round(avg_sharpe, 3),
            'avg_winrate': round(avg_winrate, 2),
            'avg_trades': round(avg_trades, 1),
            'avg_profit_factor': round(avg_pf, 3),
            'win_stocks': win_stocks,
            'total_stocks': n_stocks,
            'win_ratio': round(win_stocks / n_stocks * 100, 1) if n_stocks > 0 else 0,
        })

    df = pd.DataFrame(summary_rows)
    df = df.sort_values('avg_return', ascending=False).reset_index(drop=True)
    detail_df = pd.DataFrame(detail_rows)
    return df, detail_df


def print_compare_table(compare_result: StrategyCompareResult, sort_by: str = 'total_return'):
    """
    打印单股票多策略对比表
    """
    if not compare_result.results:
        logger.warning("无回测结果")
        return

    ranked = compare_result.get_ranking_by(metric=sort_by)

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        title = f"策略对比 - {compare_result.code} | {compare_result.period} | {compare_result.start_date} ~ {compare_result.end_date}"
        table = Table(title=title, show_lines=False)
        table.add_column("排名", justify="center", style="cyan")
        table.add_column("策略", style="magenta")
        table.add_column("总收益%", justify="right", style="bold green")
        table.add_column("年化%", justify="right")
        table.add_column("最大回撤%", justify="right", style="red")
        table.add_column("夏普", justify="right")
        table.add_column("胜率%", justify="right")
        table.add_column("交易次数", justify="right")
        table.add_column("盈亏比", justify="right")

        for idx, item in enumerate(ranked, 1):
            r = item['result']
            table.add_row(
                str(idx),
                r.strategy,
                f"{r.total_return:.2f}",
                f"{r.annual_return:.2f}",
                f"{r.max_drawdown:.2f}",
                f"{r.sharpe_ratio:.3f}",
                f"{r.win_rate:.2f}",
                str(r.total_trades),
                f"{r.profit_factor:.3f}",
            )
        console.print(table)
    except ImportError:
        logger.info(f"策略对比结果 - {compare_result.code}:")
        for idx, item in enumerate(ranked, 1):
            r = item['result']
            logger.info(
                f"  {idx}. {r.strategy}: 总收益={r.total_return}% "
                f"年化={r.annual_return}% 回撤={r.max_drawdown}% "
                f"夏普={r.sharpe_ratio} 胜率={r.win_rate}% "
                f"交易={r.total_trades}次"
            )


def print_multi_compare_table(df: pd.DataFrame, title: str = "多股票策略综合对比"):
    """
    打印多股票综合对比表
    """
    if df.empty:
        logger.warning("无对比数据")
        return

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table(title=title, show_lines=False)
        table.add_column("排名", justify="center", style="cyan")
        table.add_column("策略", style="magenta")
        table.add_column("平均收益%", justify="right", style="bold green")
        table.add_column("平均年化%", justify="right")
        table.add_column("平均回撤%", justify="right", style="red")
        table.add_column("平均夏普", justify="right")
        table.add_column("平均胜率%", justify="right")
        table.add_column("盈利股票数", justify="right")
        table.add_column("胜率%", justify="right")

        for idx, row in df.iterrows():
            table.add_row(
                str(idx + 1),
                row['strategy'],
                f"{row['avg_return']:.2f}",
                f"{row['avg_annual']:.2f}",
                f"{row['avg_max_dd']:.2f}",
                f"{row['avg_sharpe']:.3f}",
                f"{row['avg_winrate']:.2f}",
                f"{row['win_stocks']}/{row['total_stocks']}",
                f"{row['win_ratio']:.1f}",
            )
        console.print(table)
    except ImportError:
        logger.info(title)
        for idx, row in df.iterrows():
            logger.info(
                f"  {idx+1}. {row['strategy']}: 平均收益={row['avg_return']}% "
                f"盈利股票={row['win_stocks']}/{row['total_stocks']} "
                f"平均夏普={row['avg_sharpe']}"
            )


def print_stock_returns(detail_df: pd.DataFrame, title: str = "每只股票收益明细", max_print: int = 1000):
    """
    打印每只股票收益明细（按策略分组）

    Args:
        detail_df: 明细表 DataFrame
        title: 表格标题
        max_print: 每个策略最多打印多少只股票，默认1000
    """
    if detail_df.empty:
        logger.warning("无明细数据")
        return

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()

        strategies = sorted(detail_df['strategy'].unique())
        for strategy in strategies:
            subset = detail_df[detail_df['strategy'] == strategy].copy()
            subset = subset.sort_values('total_return', ascending=False).reset_index(drop=True)
            win_count = sum(1 for r in subset['total_return'] if r > 0)
            total_count = len(subset)
            avg_return = subset['total_return'].mean()

            table_title = f"{title} - {strategy} | 平均: {avg_return:.2f}% | 盈利: {win_count}/{total_count} | 显示前{min(max_print, total_count)}只"
            table = Table(title=table_title, show_lines=False)
            table.add_column("排名", justify="center", style="cyan")
            table.add_column("股票代码", style="magenta")
            table.add_column("总收益%", justify="right", style="bold green")
            table.add_column("最大回撤%", justify="right", style="red")
            table.add_column("夏普", justify="right")
            table.add_column("胜率%", justify="right")
            table.add_column("交易次数", justify="right")
            table.add_column("盈亏比", justify="right")

            for idx, row in subset.head(max_print).iterrows():
                ret_str = f"{float(row['total_return']):.2f}"
                dd_str = f"{float(row['max_drawdown']):.2f}"
                sharpe_str = f"{float(row['sharpe_ratio']):.3f}"
                winrate_str = f"{float(row['win_rate']):.2f}"
                trades_str = str(int(float(row['total_trades'])))
                pf_str = f"{float(row['profit_factor']):.3f}"

                table.add_row(
                    str(idx + 1),
                    row['code'],
                    ret_str,
                    dd_str,
                    sharpe_str,
                    winrate_str,
                    trades_str,
                    pf_str,
                )

            if total_count > max_print:
                logger.info(f"  [ {strategy} ] 共 {total_count} 只股票，只显示前 {max_print} 只，剩余 {total_count - max_print} 只省略")
            console.print(table)
    except ImportError:
        strategies = sorted(detail_df['strategy'].unique())
        for strategy in strategies:
            subset = detail_df[detail_df['strategy'] == strategy]
            subset = subset.sort_values('total_return', ascending=False)
            logger.info(f"{title} - {strategy}:")
            for idx, (_, row) in enumerate(subset.head(max_print).iterrows(), 1):
                logger.info(
                    f"  {idx}. {row['code']}: 收益={row['total_return']:.2f}% "
                    f"回撤={row['max_drawdown']:.2f}% 交易={row['total_trades']}次"
                )
            if len(subset) > max_print:
                logger.info(f"  [ {strategy} ] 共 {len(subset)} 只，只显示前 {max_print} 只，剩余 {len(subset) - max_print} 只省略")


def recommend_strategies(
    compare_df: pd.DataFrame,
    top_n: int = 3,
    metrics: List[str] = None,
) -> List[dict]:
    """
    综合推荐策略
    综合考虑：收益率、夏普比率、胜率、盈利股票占比

    Args:
        compare_df: 多股票综合对比DataFrame
        top_n: 返回前N个推荐策略
        metrics: 评分指标和权重，默认 ['avg_return', 'avg_sharpe', 'win_ratio', 'avg_winrate']

    Returns:
        推荐策略列表，每项包含 strategy, score, rank
    """
    if compare_df.empty:
        return []

    if metrics is None:
        metrics = ['avg_return', 'avg_sharpe', 'win_ratio', 'avg_winrate']

    weights = {
        'avg_return': 0.35,
        'avg_sharpe': 0.25,
        'win_ratio': 0.25,
        'avg_winrate': 0.15,
    }

    df = compare_df.copy()
    for m in metrics:
        if m in df.columns:
            col_min = df[m].min()
            col_max = df[m].max()
            if col_max - col_min == 0:
                df[f'{m}_score'] = 50.0
            else:
                df[f'{m}_score'] = (df[m] - col_min) / (col_max - col_min) * 100

    df['total_score'] = 0.0
    for m in metrics:
        if f'{m}_score' in df.columns:
            df['total_score'] += df[f'{m}_score'] * weights.get(m, 0.25)

    df['total_score'] = df['total_score'].round(2)
    df = df.sort_values('total_score', ascending=False).reset_index(drop=True)

    recommendations = []
    for idx, row in df.head(top_n).iterrows():
        recommendations.append({
            'rank': idx + 1,
            'strategy': row['strategy'],
            'score': row['total_score'],
            'avg_return': row['avg_return'],
            'avg_sharpe': row['avg_sharpe'],
            'win_ratio': row['win_ratio'],
            'avg_winrate': row['avg_winrate'],
        })
    return recommendations


def analyze_recent_macd_signals(
    codes: Optional[List[str]] = None,
    days: int = 10,
    name_map: Optional[Dict[str, str]] = None,
    max_print: int = 500,
    end_date: Optional[str] = None,
    check_volume: bool = True,
) -> pd.DataFrame:
    """
    分析最近 N 天内 MACD金叉买入信号的收益情况

    - 对每只股票计算 MACD 信号（与 MACDCrossStrategy 相同逻辑）
    - 找到最近 N 个交易日内的买入信号
    - 如果后续出现卖出信号：计算从买入到卖出的收益率
    - 如果还没出现卖出信号：计算从买入到今日收盘的浮盈浮亏

    价格规则（与回测引擎一致，信号日收盘确认、次一交易日开盘成交）：
    - 买入价 = 买入信号日的**下一日开盘价**
    - 卖出价 = 卖出信号日的**下一日开盘价**（未平仓时 = 最近一根K线收盘价）

    Args:
        codes: 股票代码列表。None 时使用全市场股票
        days: 最近多少个交易日的买入信号需要分析，默认 10
        name_map: 股票代码到名称的映射，用于打印名称
        max_print: 表格最多打印多少行，默认 500（返回的 DataFrame 仍包含全部）
        end_date: 分析截止日期 YYYY-MM-DD，None 则用今天
        check_volume: 买入信号是否要求放量（MACD金叉策略专用，默认 True）

    Returns:
        DataFrame: 列包括 code, name, buy_date, buy_price, sell_date, sell_price,
                   status('已平仓'/'持仓中'), hold_days, return_pct
    """
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_batch
    from data_fetcher.stock_pool import get_stock_pool_from_db
    from signals.macd_strategy import MACDCrossStrategy
    from config.settings import settings
    import datetime as _dt

    # 1. 确定股票池
    if codes:
        code_list = [normalize_code(c.strip()) for c in codes if c.strip()]
    else:
        raw_codes = get_stock_pool_from_db()
        code_list = [normalize_code(c) for c in raw_codes]

    if not code_list:
        logger.error("股票池为空")
        return pd.DataFrame()

    # 2. 策略与名称映射
    strategy = MACDCrossStrategy(check_volume=check_volume)
    logger.info(f"MACD 成交量过滤: {'开启' if check_volume else '关闭'}")
    strategy_name = strategy.name
    nm = name_map or {}

    # 3. 批量获取日线数据 —— 一次 SQL 查询代替 N 次查询
    #    MACD 只需要 35 天预热，因此只拉取最近 days + 70 日历天
    ref_date = _dt.datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else _dt.date.today()
    start_date = (ref_date - _dt.timedelta(days=days + 70)).strftime('%Y-%m-%d')
    end_label = end_date or "今日"
    logger.info(f"开始分析最近 {days} 天 MACD 买入信号的收益，共 {len(code_list)} 只股票 (截止: {end_label}, 数据起始日: {start_date})")

    code_dfs = get_daily_kline_batch(code_list, start_date=start_date)
    logger.info(f"数据加载完成：{len(code_dfs)} 只股票有K线数据")

    rows = []
    skipped = 0
    signal_records = []

    for code in code_list:
        df = code_dfs.get(code)
        if df is None or len(df) < 35:
            skipped += 1
            continue

        # 如果指定了 end_date，只保留该日及之前的数据
        if end_date:
            df = df[df['date'] <= end_date].copy()
            if len(df) < 35:
                skipped += 1
                continue

        df = calc_all_indicators(df)

        # 计算信号、信号原因和信号强度
        signals = strategy.generate_signals(df)
        reasons = strategy.calc_reason(df, signals)
        strengths = strategy.calc_strength(df, signals)
        df['signal'] = signals.values
        df['signal_reason'] = reasons.values

        n = len(df)
        # 最近 N 个交易日的起始索引
        recent_start = max(0, n - days)

        # 遍历最近 N 天找买入信号
        for i in range(recent_start, n):
            if int(df['signal'].iloc[i]) != 1:
                continue

            buy_date = str(df['date'].iloc[i])
            buy_reason = str(df['signal_reason'].iloc[i])
            # MACD 信号收盘后才能确认，买入价取下一日开盘价
            buy_price = float(df['open'].iloc[i + 1]) if i + 1 < n else float(df['open'].iloc[i])

            # 在买入日之后找第一个卖出信号
            sell_idx = None
            for j in range(i + 1, n):
                if int(df['signal'].iloc[j]) == -1:
                    sell_idx = j
                    break

            if sell_idx is not None:
                sell_date = str(df['date'].iloc[sell_idx])
                sell_reason = str(df['signal_reason'].iloc[sell_idx])
                # 卖出信号同样收盘后确认，卖出价取下一日开盘价
                sell_price = float(df['open'].iloc[sell_idx + 1]) if sell_idx + 1 < n else float(df['open'].iloc[sell_idx])
                status = "已平仓"
                hold_days = sell_idx + 1 - (i + 1) if (i + 1 < n and sell_idx + 1 < n) else sell_idx - i
                ret = (sell_price - buy_price) / buy_price * 100
            else:
                sell_date = ""
                sell_reason = ""
                # 未平仓：按最近一根K线收盘价估值
                sell_price = float(df['close'].iloc[-1])
                status = "持仓中"
                hold_days = n - 1 - (i + 1) if (i + 1 < n) else n - 1 - i
                ret = (sell_price - buy_price) / buy_price * 100

            name = nm.get(code, '')
            rows.append({
                'code': code,
                'name': name,
                'buy_date': buy_date,
                'buy_reason': buy_reason,
                'buy_price': round(buy_price, 3),
                'sell_date': sell_date,
                'sell_reason': sell_reason,
                'sell_price': round(sell_price, 3),
                'status': status,
                'hold_days': hold_days,
                'return_pct': round(ret, 2),
            })

            # 同时把买入信号存入 trade_signals（次日开盘价）
            signal_records.append({
                'code': code,
                'period': 'daily',
                'strategy': strategy_name,
                'signal_type': 1,
                'signal_time': buy_date,
                'price': round(buy_price, 3),
                'signal_strength': float(strengths.iloc[i]),
                'reason': buy_reason,
                'indicators': strategy.get_indicator_snapshot(df, i),
                'description': f"{strategy_name} 买入信号",
            })

    # 保存买入信号到 trade_signals 表
    if signal_records:
        try:
            from db.database import session_scope
            from db.models import TradeSignal
            from datetime import datetime

            with session_scope() as session:
                saved = 0
                for sig in signal_records:
                    try:
                        sig_time = datetime.strptime(sig['signal_time'][:10], '%Y-%m-%d')
                    except (ValueError, KeyError):
                        continue

                    existing = (
                        session.query(TradeSignal)
                        .filter_by(
                            code=sig['code'],
                            period=sig['period'],
                            strategy=sig['strategy'],
                            signal_type=sig['signal_type'],
                        )
                        .filter(TradeSignal.signal_time == sig_time)
                        .first()
                    )
                    if existing:
                        existing.price = sig['price']
                        existing.signal_strength = sig['signal_strength']
                        existing.reason = sig['reason']
                        existing.indicators = sig['indicators']
                        existing.description = sig['description']
                    else:
                        ts = TradeSignal(
                            code=sig['code'],
                            period=sig['period'],
                            strategy=sig['strategy'],
                            signal_type=sig['signal_type'],
                            signal_time=sig_time,
                            price=sig['price'],
                            signal_strength=sig['signal_strength'],
                            reason=sig['reason'],
                            indicators=sig['indicators'],
                            description=sig['description'],
                        )
                        session.add(ts)
                        saved += 1
                logger.info(f"已保存 {saved} 个新的买入信号到 trade_signals 表")
        except Exception as e:
            logger.warning(f"保存信号到 trade_signals 失败: {e}")

    result_df = pd.DataFrame(rows)
    if result_df.empty:
        logger.info("最近 N 天内未发现 MACD 买入信号")
        return result_df

    result_df = result_df.sort_values('return_pct', ascending=False).reset_index(drop=True)

    # 汇总打印
    closed = result_df[result_df['status'] == '已平仓']
    holding = result_df[result_df['status'] == '持仓中']
    avg_closed = closed['return_pct'].mean() if not closed.empty else 0
    avg_holding = holding['return_pct'].mean() if not holding.empty else 0
    win_closed = sum(1 for r in closed['return_pct'] if r > 0)
    total_closed = len(closed)
    win_rate_closed = round(win_closed / total_closed * 100, 2) if total_closed > 0 else 0
    win_holding = sum(1 for r in holding['return_pct'] if r > 0)
    total_holding = len(holding)
    win_rate_holding = round(win_holding / total_holding * 100, 2) if total_holding > 0 else 0

    logger.info(f"分析完成：共 {len(result_df)} 个买入信号（已平仓 {total_closed}，持仓中 {total_holding}）")
    logger.info(f"  已平仓: 平均收益 {avg_closed:.2f}%  胜率 {win_rate_closed:.1f}%")
    logger.info(f"  持仓中: 平均收益 {avg_holding:.2f}%  浮盈占比 {win_rate_holding:.1f}%")
    if skipped > 0:
        logger.info(f"  跳过 {skipped} 只无K线数据的股票")

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()

        table = Table(title=f"最近 {days} 天 MACD 金叉买入信号 收益分析（共 {len(result_df)} 个，显示前 {min(max_print, len(result_df))} 个）")
        table.add_column("排名", justify="center", style="cyan")
        table.add_column("代码", style="magenta")
        table.add_column("名称")
        table.add_column("买入日", justify="center")
        table.add_column("买入原因", justify="center")
        table.add_column("买入价", justify="right")
        table.add_column("卖出日", justify="center")
        table.add_column("卖出原因", justify="center")
        table.add_column("卖出/当前价", justify="right")
        table.add_column("状态", justify="center")
        table.add_column("天数", justify="right")
        table.add_column("收益率%", justify="right", style="bold green")

        for idx, row in result_df.head(max_print).iterrows():
            sell_date_str = row['sell_date'] if row['sell_date'] else '—'
            sell_reason_str = row['sell_reason'] if row['sell_reason'] else '—'
            table.add_row(
                str(idx + 1),
                row['code'],
                row['name'],
                row['buy_date'],
                row['buy_reason'],
                f"{row['buy_price']:.3f}",
                sell_date_str,
                sell_reason_str,
                f"{row['sell_price']:.3f}",
                row['status'],
                str(row['hold_days']),
                f"{row['return_pct']:.2f}",
            )
        console.print(table)

        if len(result_df) > max_print:
            logger.info(f"  表格只显示前 {max_print} 个，其余 {len(result_df) - max_print} 个省略")
    except Exception as e:
        # 降级：用 logger 打印
        for idx, row in result_df.head(max_print).iterrows():
            sell_date_str = row['sell_date'] if row['sell_date'] else '持仓中'
            sell_reason_str = row['sell_reason'] if row['sell_reason'] else '—'
            logger.info(
                f"  {idx+1:>3}. {row['code']} {row['name']} "
                f"买入:{row['buy_date']}[{row['buy_reason']}] @{row['buy_price']:.3f} "
                f"卖出:{sell_date_str}[{sell_reason_str}] @{row['sell_price']:.3f} "
                f"[{row['status']}] {row['hold_days']}天 "
                f"收益={row['return_pct']:.2f}%"
            )
        if len(result_df) > max_print:
            logger.info(f"  只显示前 {max_print} 个，其余 {len(result_df) - max_print} 个省略")

    # 导出到 CSV
    if not result_df.empty:
        try:
            csv_path = settings.OUTPUT_DIR / f"macd_signals_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            csv_path.parent.mkdir(parents=True, exist_ok=True)

            # 整理列顺序，写入完整数据（不受 max_print 限制）
            cols = ['code', 'name', 'buy_date', 'buy_reason', 'buy_price',
                    'sell_date', 'sell_reason', 'sell_price', 'status',
                    'hold_days', 'return_pct']
            result_df[cols].to_csv(csv_path, index=False, encoding='utf-8-sig')
            logger.info(f"信号数据已导出 CSV: {csv_path} (共 {len(result_df)} 条)")
        except Exception as e:
            logger.warning(f"导出 CSV 失败: {e}")

    return result_df


# ============================================================
# MACD 预测金叉信号验证
# ============================================================

def verify_macd_predictive_signals(
    codes: Optional[List[str]] = None,
    start_date: str = None,
    end_date: str = None,
    check_window: int = 1,
    name_map: Optional[Dict[str, str]] = None,
    max_print: int = 500,
    no_volume: bool = False,
) -> pd.DataFrame:
    """
    验证「MACD预测金叉」策略的有效性。

    流程:
      1. 对每只股票，加载到数据库最新一天为止的全量日线
      2. 在 [start_date, end_date] 区间内，运行 MACDPredictiveCrossStrategy
         找出所有「预测金叉」买入信号
      3. 对每个信号日 i，在后续 check_window 个交易日内检查:
           - MACD 柱是否继续增大 (macd[i+1] > macd[i])
           - 是否出现真正的 MACD 金叉 (DIF 上穿 DEA)
           - 1日/3日/5日 涨跌幅
      4. 输出汇总统计表与明细

    Args:
        codes: 股票代码列表，None 时使用全市场股票
        start_date: 分析起始日期 YYYY-MM-DD
        end_date: 分析截止日期 YYYY-MM-DD
        check_window: 向后检查多少个交易日内是否出现金叉，默认 1
        name_map: 股票代码到名称的映射
        max_print: 明细最多打印多少行
        no_volume: 是否关闭成交量过滤，默认 False（即开启量比过滤）

    Returns:
        包含每条信号验证结果的 DataFrame
    """
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_batch
    from data_fetcher.stock_pool import get_stock_pool_from_db
    from signals.macd_strategy import MACDPredictiveCrossStrategy
    from config.settings import settings
    import datetime as _dt

    # 1. 确定股票池
    if codes:
        code_list = [normalize_code(c.strip()) for c in codes if c.strip()]
    else:
        raw_codes = get_stock_pool_from_db()
        code_list = [normalize_code(c) for c in raw_codes]

    if not code_list:
        logger.error("股票池为空")
        return pd.DataFrame()

    # 2. 策略与名称
    strategy = MACDPredictiveCrossStrategy()
    nm = name_map or {}

    # 3. 日期处理
    ref_end = _dt.datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else _dt.date.today()
    ref_start = _dt.datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else (ref_end - _dt.timedelta(days=60))
    # 往前多拉 120 天作为 MACD 预热期
    fetch_start = (ref_start - _dt.timedelta(days=120)).strftime('%Y-%m-%d')

    logger.info(
        f"开始验证 MACD 预测金叉信号 · 共 {len(code_list)} 只股票 · "
        f"寻找信号区间 {ref_start.strftime('%Y-%m-%d')} ~ {ref_end.strftime('%Y-%m-%d')} · "
        f"向后检查 {check_window} 个交易日 · 成交量过滤: {'关闭' if no_volume else '开启'}"
    )
    logger.info(f" （注意：只在 [start, end] 内寻找预测信号，但会用到 end_date 之后的K线做金叉验证）")

    # 4. 批量拉取日线（不截断到 end_date，这样 end_date 之后的数据可用于验证金叉）
    code_dfs = get_daily_kline_batch(code_list, start_date=fetch_start)
    logger.info(f"数据加载完成：{len(code_dfs)} 只股票有K线数据")

    detail_rows = []
    skipped = 0
    ref_start_str = ref_start.strftime('%Y-%m-%d')
    ref_end_str = ref_end.strftime('%Y-%m-%d')

    for code in code_list:
        df = code_dfs.get(code)
        if df is None or len(df) < 35:
            skipped += 1
            continue

        # 关键：用全量数据计算 MACD 等指标（不截断到 end_date）
        # 这样信号日 i 的后续 k 天数据都在 df_ind 中，可以用来验证金叉
        df_ind = calc_all_indicators(df.copy())

        # 在全量数据上生成完整的 0/1 信号序列
        pred_signals = strategy.generate_signals(df_ind)
        df_ind['signal'] = pred_signals.values

        n = len(df_ind)

        # 只在 [start_date, end_date] 区间内寻找买入信号
        for i in range(n):
            row_date = str(df_ind['date'].iloc[i])
            if row_date < ref_start_str:
                continue
            if row_date > ref_end_str:
                continue

            if int(df_ind['signal'].iloc[i]) != 1:
                continue

            # —— 发现一个预测金叉信号 ——
            signal_date = row_date
            close_i = float(df_ind['close'].iloc[i])
            macd_i = float(df_ind['macd'].values[i]) if 'macd' in df_ind.columns else float('nan')
            dif_i = float(df_ind['dif'].values[i])
            dea_i = float(df_ind['dea'].values[i])

            macd_increase_next = False
            macd_increase_any = False
            next_macd_val = float('nan')
            cross_day = None
            cross_date = None
            ret_1d = float('nan')
            ret_3d = float('nan')
            ret_5d = float('nan')

            for k in range(1, check_window + 1):
                j = i + k
                if j >= n:
                    break

                macd_j = float(df_ind['macd'].values[j])
                dif_j = float(df_ind['dif'].values[j])
                dea_j = float(df_ind['dea'].values[j])
                close_j = float(df_ind['close'].iloc[j])

                if k == 1:
                    next_macd_val = macd_j
                    if macd_j > macd_i:
                        macd_increase_next = True
                    ret_1d = (close_j - close_i) / close_i * 100.0

                if macd_j > macd_i:
                    macd_increase_any = True

                # 金叉判断：DIF 从下方向上穿过 DEA
                prev_dif = float(df_ind['dif'].values[j - 1])
                prev_dea = float(df_ind['dea'].values[j - 1])
                if (dif_j > dea_j) and (prev_dif <= prev_dea):
                    if cross_day is None:
                        cross_day = k
                        cross_date = str(df_ind['date'].iloc[j])

                if k == 3:
                    ret_3d = (close_j - close_i) / close_i * 100.0
                if k == 5:
                    ret_5d = (close_j - close_i) / close_i * 100.0

            # 记录
            name = nm.get(code, '')
            detail_rows.append({
                'code': code,
                'name': name,
                'signal_date': signal_date,
                'signal_close': round(close_i, 3),
                'macd_at_signal': round(macd_i, 4),
                'dif_at_signal': round(dif_i, 4),
                'dea_at_signal': round(dea_i, 4),
                'next_day_macd': round(next_macd_val, 4),
                'macd_increase_next': '✓' if macd_increase_next else '✗',
                'macd_increase_any': '✓' if macd_increase_any else '✗',
                'cross_within_window': '✓' if cross_day is not None else '✗',
                'cross_day': cross_day if cross_day is not None else '',
                'cross_date': cross_date or '',
                'ret_1d_pct': round(ret_1d, 2),
                'ret_3d_pct': round(ret_3d, 2),
                'ret_5d_pct': round(ret_5d, 2),
            })

    result_df = pd.DataFrame(detail_rows)
    if result_df.empty:
        logger.info("在指定区间内未发现「MACD预测金叉」信号")
        return result_df

    # 汇总统计
    total_signals = len(result_df)
    n_increase_next = sum(1 for r in result_df['macd_increase_next'] if r == '✓')
    n_increase_any = sum(1 for r in result_df['macd_increase_any'] if r == '✓')
    n_cross = sum(1 for r in result_df['cross_within_window'] if r == '✓')
    valid_1d = result_df[result_df['ret_1d_pct'].notna()]
    valid_3d = result_df[result_df['ret_3d_pct'].notna()]
    valid_5d = result_df[result_df['ret_5d_pct'].notna()]
    avg_1d = valid_1d['ret_1d_pct'].mean() if not valid_1d.empty else 0
    avg_3d = valid_3d['ret_3d_pct'].mean() if not valid_3d.empty else 0
    avg_5d = valid_5d['ret_5d_pct'].mean() if not valid_5d.empty else 0
    win_1d = sum(1 for r in valid_1d['ret_1d_pct'] if r > 0)
    win_3d = sum(1 for r in valid_3d['ret_3d_pct'] if r > 0)
    win_5d = sum(1 for r in valid_5d['ret_5d_pct'] if r > 0)

    logger.info("=" * 70)
    logger.info(f"验证完成：共 {total_signals} 个「MACD预测金叉」信号")
    logger.info(f"  ┌─ 次日 MACD 柱继续增大: {n_increase_next}/{total_signals} "
                f"({n_increase_next / total_signals * 100:.1f}% 命中率)")
    logger.info(f"  ├─ {check_window} 日内 MACD 柱曾增大: {n_increase_any}/{total_signals} "
                f"({n_increase_any / total_signals * 100:.1f}%)")
    logger.info(f"  ├─ {check_window} 日内出现 MACD 金叉: {n_cross}/{total_signals} "
                f"({n_cross / total_signals * 100:.1f}% 金叉命中率)")
    if len(valid_1d):
        logger.info(f"  ├─ 次日平均涨幅: {avg_1d:.2f}% · 上涨概率: "
                    f"{win_1d / len(valid_1d) * 100:.1f}% ({win_1d}/{len(valid_1d)})")
    if len(valid_3d):
        logger.info(f"  ├─ 3日平均涨幅: {avg_3d:.2f}% · 上涨概率: "
                    f"{win_3d / len(valid_3d) * 100:.1f}% ({win_3d}/{len(valid_3d)})")
    if len(valid_5d):
        logger.info(f"  └─ 5日平均涨幅: {avg_5d:.2f}% · 上涨概率: "
                    f"{win_5d / len(valid_5d) * 100:.1f}% ({win_5d}/{len(valid_5d)})")
    if skipped > 0:
        logger.info(f"  (跳过 {skipped} 只无K线数据的股票)")
    logger.info("-" * 70)

    # 打印明细表
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        title = (f"「MACD预测金叉」信号验证明细 · 共 {total_signals} 条 · "
                 f"显示前 {min(max_print, total_signals)} 条")
        table = Table(title=title, show_lines=False)
        table.add_column("#", justify="center", style="cyan")
        table.add_column("代码", style="magenta")
        table.add_column("名称")
        table.add_column("信号日", justify="center")
        table.add_column("信号日MACD", justify="right")
        table.add_column("次日MACD", justify="right")
        table.add_column("次日柱增大", justify="center")
        table.add_column(f"{check_window}日内金叉", justify="center")
        table.add_column("金叉日", justify="center")
        table.add_column("1日%", justify="right", style="bold green")
        table.add_column("3日%", justify="right")
        table.add_column("5日%", justify="right")

        for idx, row in result_df.head(max_print).iterrows():
            table.add_row(
                str(idx + 1),
                row['code'],
                str(row['name']),
                row['signal_date'],
                f"{row['macd_at_signal']:.4f}",
                f"{row['next_day_macd']:.4f}",
                row['macd_increase_next'],
                row['cross_within_window'],
                row['cross_date'] if row['cross_date'] else '—',
                f"{row['ret_1d_pct']:.2f}",
                f"{row['ret_3d_pct']:.2f}",
                f"{row['ret_5d_pct']:.2f}",
            )
        console.print(table)
        if len(result_df) > max_print:
            logger.info(f"  表格只显示前 {max_print} 条，其余 {len(result_df) - max_print} 条省略")
    except Exception as e:
        for idx, row in result_df.head(max_print).iterrows():
            logger.info(
                f"  {idx + 1:>3}. {row['code']} {row['name']} "
                f"信号:{row['signal_date']}[MACD={row['macd_at_signal']:.4f}] "
                f"次日MACD={row['next_day_macd']:.4f} "
                f"柱增大={row['macd_increase_next']} "
                f"{check_window}日内金叉={row['cross_within_window']} "
                f"({row['cross_date'] or '—'}) "
                f"1日={row['ret_1d_pct']:.2f}% 3日={row['ret_3d_pct']:.2f}% 5日={row['ret_5d_pct']:.2f}%"
            )

    logger.info("-" * 70)

    # 导出 CSV（完整数据）
    try:
        csv_path = settings.OUTPUT_DIR / f"macd_prediction_verify_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        cols = ['code', 'name', 'signal_date', 'signal_close', 'macd_at_signal',
                'dif_at_signal', 'dea_at_signal', 'next_day_macd',
                'macd_increase_next', 'macd_increase_any',
                'cross_within_window', 'cross_day', 'cross_date',
                'ret_1d_pct', 'ret_3d_pct', 'ret_5d_pct']
        result_df[cols].to_csv(csv_path, index=False, encoding='utf-8-sig')
        logger.info(f"验证结果已导出 CSV: {csv_path} (共 {len(result_df)} 条)")
    except Exception as e:
        logger.warning(f"导出 CSV 失败: {e}")

    return result_df
