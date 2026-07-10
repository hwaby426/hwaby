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
import re
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
    check_date: str = None,
    check_window: int = 1,
    name_map: Optional[Dict[str, str]] = None,
    max_print: int = 500,
    _batch_mode: bool = False,
) -> pd.DataFrame:
    """
    验证「MACD预测金叉」策略的有效性。

    流程:
      1. 从 trade_signals 表查询 check_date 当天的「MACD预测金叉」买入信号
      2. 只对这些股票拉取K线数据
      3. 检查每只股票在信号日之后的 check_window 个交易日内是否出现真正的 MACD 金叉
         （真正金叉 = DIF 从下方穿过 DEA，即 dif[i] > dea[i] 且 dif[i-1] <= dea[i-1]）
      4. 输出汇总统计表（命中率）与每只股票的明细

    Args:
        codes: 股票代码列表，逗号分隔。None 时使用数据库中当天的信号股票
        check_date: 要检查的日期 YYYY-MM-DD（即 scan-market --date 的日期）
        check_window: 向后检查多少个交易日内是否出现金叉，默认 1
        name_map: 股票代码到名称的映射
        max_print: 明细最多打印多少行，默认 500

    Returns:
        包含每条信号验证结果的 DataFrame
    """
    from data_fetcher.baostock_fetcher import normalize_code, get_daily_kline_batch
    from signals.signal_service import TradeSignal
    from db.database import session_scope
    from config.settings import settings
    from sqlalchemy import func
    import datetime as _dt

    # 1. 日期处理
    if not check_date:
        check_date = (_dt.date.today() - _dt.timedelta(days=1)).strftime('%Y-%m-%d')
    ref_date = _dt.datetime.strptime(check_date, '%Y-%m-%d').date()
    ref_date_str = ref_date.strftime('%Y-%m-%d')
    fetch_start = (ref_date - _dt.timedelta(days=120)).strftime('%Y-%m-%d')

    logger.info(
        f"开始验证 MACD 预测金叉信号 · 信号日 = {check_date} · "
        f"向后检查 {check_window} 个交易日"
    )

    # 2. 从 trade_signals 表查询当天的「MACD预测金叉」买入信号
    signal_codes = []
    signal_info = {}
    with session_scope() as session:
        q = session.query(TradeSignal).filter(
            TradeSignal.strategy == 'MACD预测金叉',
            TradeSignal.signal_type == 1,
        )
        q = q.filter(func.date(TradeSignal.signal_time) == ref_date)
        raw_signals = q.all()

        for sig in raw_signals:
            code = sig.code
            signal_codes.append(code)
            signal_info[code] = {
                'price': float(sig.price) if sig.price is not None else None,
                'reason': sig.reason or '',
                'strength': float(sig.signal_strength) if sig.signal_strength is not None else None,
            }

    logger.info(f"从 trade_signals 表查到 {len(signal_codes)} 只股票在 {check_date} 有预测金叉信号")

    # 3. 如果用户指定了 --codes，优先用用户的列表
    if codes:
        user_codes = [normalize_code(c.strip()) for c in codes if c.strip()]
        code_list = [c for c in user_codes if c in signal_codes]
        logger.info(f"用户指定 {len(user_codes)} 只股票，其中 {len(code_list)} 只在 {check_date} 有信号")
    else:
        code_list = signal_codes

    # 3a. 剔除科创板/北交所（未开户无权限）
    from scheduler.market_scan import _is_restricted
    restricted_codes = [c for c in code_list if _is_restricted(c)]
    if restricted_codes:
        code_list = [c for c in code_list if not _is_restricted(c)]
        logger.info(f"剔除受限板块 {len(restricted_codes)} 只，剩余 {len(code_list)} 只股票待验证")

    if not code_list:
        logger.info("没有找到符合条件的股票。请先运行:")
        logger.info(f'  python main.py scan-market --strategies "MACD预测金叉" --date {check_date} --save')
        return pd.DataFrame()

    # 4. 名称映射
    nm = name_map or {}
    if not nm:
        from data_fetcher.stock_pool import get_stock_name_map
        nm = get_stock_name_map()

    # 5. 只对有信号的股票拉取K线数据
    code_dfs = get_daily_kline_batch(code_list, start_date=fetch_start)
    logger.info(f"数据加载完成：{len(code_dfs)} 只股票有K线数据")

    # 5a. 如果历史数据无法覆盖 check_date + check_window 个交易日，
    #     则用新浪实时行情补充一只"今日K线"（用当天收盘/实时价补齐窗口）
    today_str = _dt.date.today().strftime('%Y-%m-%d')
    realtime_codes = []

    for code in code_list:
        df = code_dfs.get(code)
        if df is None or len(df) < 35:
            # 历史数据不足 — 即使补了实时也未必能计算指标，但尽力补
            if df is not None and not df.empty:
                realtime_codes.append(code)
            continue

        last_date = str(df['date'].iloc[-1])
        # 如果最后一个交易日早于今天，说明历史数据还没更新到今天，
        # 需要拉取实时行情来补齐到今天（如果今天是交易日且有行情）
        if last_date < today_str:
            realtime_codes.append(code)

    # 实际判断是否需要补：逐只看窗口是否"已经被历史覆盖"
    # 只有当窗口内存在无法由历史数据覆盖的交易日时，才需要补充
    need_realtime = []
    for code in realtime_codes:
        df = code_dfs.get(code)
        if df is None or df.empty:
            continue
        # 找信号日索引
        sig_idx = None
        for i in range(len(df)):
            if str(df['date'].iloc[i]) == ref_date_str:
                sig_idx = i
                break
        if sig_idx is None:
            continue
        if sig_idx + check_window >= len(df):
            need_realtime.append(code)

    realtime_quote = None
    if need_realtime:
        logger.info(f"{len(need_realtime)} 只股票窗口数据不足，尝试用实时行情补齐...")
        from data_fetcher.sina_fetcher import get_realtime_quotes
        try:
            realtime_quote = get_realtime_quotes(need_realtime)
        except Exception as e:
            logger.error(f"拉取实时行情失败: {e}")
            realtime_quote = None

        if realtime_quote is not None and not realtime_quote.empty:
            for code in need_realtime:
                df = code_dfs.get(code)
                if df is None or df.empty:
                    continue
                q = realtime_quote[realtime_quote['code'] == code]
                if q.empty:
                    continue
                q = q.iloc[0]
                price = float(q.get('price', 0))
                if price <= 0:
                    continue
                last_date = str(df['date'].iloc[-1])
                preclose = float(df['close'].iloc[-1])
                # 如果今日交易日与最后一天同日 → 覆盖；否则追加
                if last_date == today_str:
                    idx = df.index[-1]
                    df = df.copy()
                    df.loc[idx, 'open'] = float(q.get('open', 0)) or float(df.loc[idx, 'open'])
                    df.loc[idx, 'high'] = float(q.get('high', 0)) or float(df.loc[idx, 'high'])
                    df.loc[idx, 'low'] = float(q.get('low', 0)) or float(df.loc[idx, 'low'])
                    df.loc[idx, 'close'] = price
                    df.loc[idx, 'volume'] = int(q.get('volume', 0)) or int(df.loc[idx, 'volume'])
                    df.loc[idx, 'amount'] = float(q.get('amount', 0)) or float(df.loc[idx, 'amount'])
                else:
                    pct = ((price - preclose) / preclose * 100.0) if preclose > 0 else 0.0
                    new_row = {
                        'date': today_str,
                        'code': code,
                        'open': float(q.get('open', 0)),
                        'high': float(q.get('high', 0)),
                        'low': float(q.get('low', 0)),
                        'close': price,
                        'volume': int(q.get('volume', 0)),
                        'amount': float(q.get('amount', 0)),
                        'pct_chg': pct,
                        'turnover': 0.0,
                    }
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                code_dfs[code] = df
            logger.info(f"实时行情补齐完成，共更新 {len(need_realtime)} 只股票")


    detail_rows = []
    skipped = 0

    for code in code_list:
        df = code_dfs.get(code)
        if df is None or len(df) < 35:
            skipped += 1
            continue

        # 用全量数据（包含 check_date 之后的K线）计算 MACD 指标
        df_ind = calc_all_indicators(df.copy())

        # 找到 check_date 在 df 中的索引位置
        signal_idx = None
        for i in range(len(df_ind)):
            if str(df_ind['date'].iloc[i]) == ref_date_str:
                signal_idx = i
                break

        if signal_idx is None:
            skipped += 1
            continue

        # —— 信号日信息 ——
        signal_date = ref_date_str
        close_i = float(df_ind['close'].iloc[signal_idx])
        macd_i = float(df_ind['macd'].values[signal_idx])
        dif_i = float(df_ind['dif'].values[signal_idx])
        dea_i = float(df_ind['dea'].values[signal_idx])

        # —— 向后检查金叉 / MACD柱增大 / 窗口日收益率 / 窗口内最高收益 ——
        macd_increase_next = False
        next_macd_val = float('nan')
        cross_day = None
        cross_date = None
        # ret_per_day[k] = 第 k 个交易日收盘价相对信号日的收益率 (k = 1..check_window)
        ret_per_day = {k: float('nan') for k in range(1, check_window + 1)}
        # 窗口内最高收益（用 high 计算，代表能吃到的最大涨幅）
        max_high_pct = float('nan')
        max_high_day = None
        # 窗口内最大收盘价收益（比 high 更保守，但不会误把集合竞价/尾盘冲高算入）
        max_close_pct = float('nan')
        max_close_day = None

        n = len(df_ind)
        high_arr = df_ind['high'].values
        close_arr = df_ind['close'].values

        for k in range(1, check_window + 1):
            j = signal_idx + k
            if j >= n:
                break

            macd_j = float(df_ind['macd'].values[j])
            dif_j = float(df_ind['dif'].values[j])
            dea_j = float(df_ind['dea'].values[j])
            close_j = float(close_arr[j])

            if k == 1:
                next_macd_val = macd_j
                if macd_j > macd_i:
                    macd_increase_next = True

            # 真正金叉：DIF 从下方穿过 DEA
            prev_dif = float(df_ind['dif'].values[j - 1])
            prev_dea = float(df_ind['dea'].values[j - 1])
            if (dif_j > dea_j) and (prev_dif <= prev_dea):
                if cross_day is None:
                    cross_day = k
                    cross_date = str(df_ind['date'].iloc[j])

            # 窗口内每个交易日的收盘价收益率（保留原逻辑）
            ret_per_day[k] = (close_j - close_i) / close_i * 100.0

            # —— 只有 k >= 2 才统计「最高收益」：信号日买入后，最早 T+1 日才能卖出
            if k >= 2:
                if j < len(high_arr):
                    high_j = float(high_arr[j])
                    high_ret = (high_j - close_i) / close_i * 100.0
                    if pd.isna(max_high_pct) or high_ret > max_high_pct:
                        max_high_pct = high_ret
                        max_high_day = k

                close_ret = ret_per_day[k]
                if not pd.isna(close_ret):
                    if pd.isna(max_close_pct) or close_ret > max_close_pct:
                        max_close_pct = close_ret
                        max_close_day = k

        info = signal_info.get(code, {})
        name = nm.get(code, '')
        row_dict = {
            'code': code,
            'name': name,
            'signal_date': signal_date,
            'signal_close': round(close_i, 3),
            'signal_price': info.get('price'),
            'strength': info.get('strength'),
            'reason': info.get('reason', ''),
            'macd_at_signal': round(macd_i, 4),
            'dif_at_signal': round(dif_i, 4),
            'dea_at_signal': round(dea_i, 4),
            'next_day_macd': round(next_macd_val, 4),
            'macd_increase_next': '✓' if macd_increase_next else '✗',
            'cross_within_window': '✓' if cross_day is not None else '✗',
            'cross_day': cross_day if cross_day is not None else '',
            'cross_date': cross_date or '',
            'max_high_pct': round(max_high_pct, 2),
            'max_high_day': max_high_day if max_high_day is not None else '',
            'max_close_pct': round(max_close_pct, 2),
            'max_close_day': max_close_day if max_close_day is not None else '',
        }
        for k in range(1, check_window + 1):
            row_dict[f'ret_{k}d_pct'] = round(ret_per_day[k], 2)
        detail_rows.append(row_dict)

    result_df = pd.DataFrame(detail_rows)
    if result_df.empty:
        logger.info("没有可用于验证的信号数据")
        return result_df

    # 解析每条信号的辅助指标：强度 strength / 量比 / 需涨% / 3日缩短%
    # 从 reason 文本中提取："3日缩短XX%" / "需涨XX.XX%" / "量比=XX.XX"
    def _parse_reason(r):
        if not r:
            return {}
        out = {}
        m1 = re.search(r'3日缩短([\d.]+)%', r)
        if m1:
            out['shrink_pct'] = float(m1.group(1))
        m2 = re.search(r'需涨([\d.]+)%', r)
        if m2:
            out['needed_pct'] = float(m2.group(1))
        m3 = re.search(r'量比=([\d.]+)', r)
        if m3:
            out['vol_ratio'] = float(m3.group(1))
        return out

    parsed_flags = []
    for _, row in result_df.iterrows():
        info = _parse_reason(row.get('reason', ''))
        try:
            info['strength'] = float(row['strength']) if row.get('strength') is not None else float('nan')
        except (ValueError, TypeError):
            info['strength'] = float('nan')
        parsed_flags.append(info)

    # 汇总统计
    total_signals = len(result_df)
    n_increase_next = sum(1 for r in result_df['macd_increase_next'] if r == '✓')
    n_cross = sum(1 for r in result_df['cross_within_window'] if r == '✓')

    # 按窗口内每个交易日计算平均涨幅和上涨概率
    day_stats = []
    for k in range(1, check_window + 1):
        col = f'ret_{k}d_pct'
        valid = result_df[result_df[col].notna()]
        avg = valid[col].mean() if not valid.empty else 0
        win = sum(1 for r in valid[col] if r > 0)
        day_stats.append((k, len(valid), avg, win))

    logger.info("=" * 70)
    logger.info(f"验证完成：共 {total_signals} 个「MACD预测金叉」信号")
    logger.info(f"  ┌─ 次日 MACD 柱继续增大: {n_increase_next}/{total_signals} "
                f"({n_increase_next / total_signals * 100:.1f}%)")
    logger.info(f"  ├─ {check_window} 日内出现 MACD 金叉: {n_cross}/{total_signals} "
                f"({n_cross / total_signals * 100:.1f}% 金叉命中率)")
    for idx, (k, cnt, avg, win) in enumerate(day_stats):
        if cnt == 0:
            continue
        prefix = '  ├─' if idx < len(day_stats) - 1 else '  └─'
        logger.info(f"{prefix} 第{k}日收盘平均涨幅: {avg:.2f}% · 上涨概率: "
                    f"{win / cnt * 100:.1f}% ({win}/{cnt})")

    # 窗口内「最高收益」汇总（high / close），
    # 注意：仅统计 k>=2 的交易日（信号日买入，T+1 才能卖）
    try:
        high_valid = result_df[result_df['max_high_pct'].notna()]
        close_valid = result_df[result_df['max_close_pct'].notna()]

        if not high_valid.empty:
            avg_high = high_valid['max_high_pct'].mean()
            med_high = high_valid['max_high_pct'].median()
            win_high = sum(1 for r in high_valid['max_high_pct'] if r > 0)
            pct3 = sum(1 for r in high_valid['max_high_pct'] if r >= 3.0)
            pct5 = sum(1 for r in high_valid['max_high_pct'] if r >= 5.0)
            logger.info(f"  ┌─ 窗口内最高收益(high, k≥2): 平均={avg_high:.2f}%  中位数={med_high:.2f}%  "
                        f">0%:{win_high}/{len(high_valid)}({win_high / len(high_valid) * 100:.1f}%)  "
                        f">=3%:{pct3}/{len(high_valid)}({pct3 / len(high_valid) * 100:.1f}%)  "
                        f">=5%:{pct5}/{len(high_valid)}({pct5 / len(high_valid) * 100:.1f}%)")

        if not close_valid.empty:
            avg_close = close_valid['max_close_pct'].mean()
            med_close = close_valid['max_close_pct'].median()
            win_close = sum(1 for r in close_valid['max_close_pct'] if r > 0)
            pct3_c = sum(1 for r in close_valid['max_close_pct'] if r >= 3.0)
            pct5_c = sum(1 for r in close_valid['max_close_pct'] if r >= 5.0)
            logger.info(f"  └─ 窗口内最高收益(close, k≥2): 平均={avg_close:.2f}%  中位数={med_close:.2f}%  "
                        f">0%:{win_close}/{len(close_valid)}({win_close / len(close_valid) * 100:.1f}%)  "
                        f">=3%:{pct3_c}/{len(close_valid)}({pct3_c / len(close_valid) * 100:.1f}%)  "
                        f">=5%:{pct5_c}/{len(close_valid)}({pct5_c / len(close_valid) * 100:.1f}%)")
    except Exception as _e:
        logger.debug(f"计算窗口内最高收益时发生异常: {_e}")

    if skipped > 0:
        logger.info(f"  (跳过 {skipped} 只无K线数据 / 信号日无数据的股票)")
    logger.info("-" * 70)

    # —— 按各参数阈值分层统计，帮助判断是否需要调整 ——
    #  batch 模式下跳过明细/分段统计，避免范围模式时输出过多
    if _batch_mode:
        # batch 模式下只返回 DataFrame 和基本汇总 logger 信息，不打印明细表
        return result_df

    # 使用最后一个窗口日作为"盈亏"基准
    metric_col = f'ret_{check_window}d_pct'
    has_metric = result_df[result_df[metric_col].notna()]
    if len(has_metric) == 0:
        metric_col = 'ret_1d_pct'
        has_metric = result_df[result_df[metric_col].notna()]

    def _segment_stats(key, buckets, label):
        """buckets = [(name, predicate(df_row, parsed_row))]"""
        lines = []
        for bname, pred in buckets:
            idxs = [i for i in range(len(result_df)) if pred(result_df.iloc[i], parsed_flags[i])]
            sub = result_df.iloc[idxs]
            sub_valid = sub[sub[metric_col].notna()]
            sub_cross = sum(1 for r in sub['cross_within_window'] if r == '✓')
            sub_avg = sub_valid[metric_col].mean() if not sub_valid.empty else float('nan')
            sub_win = sum(1 for r in sub_valid[metric_col] if r > 0)
            lines.append((bname, len(sub), sub_cross, sub_avg, sub_win, len(sub_valid)))
        logger.info(f"  — {label}（样本数 / {check_window}日金叉命中率 / 平均涨幅 / 上涨概率）—")
        for bname, cnt, cr, avg, win, vcnt in lines:
            cross_pct = (cr / cnt * 100) if cnt > 0 else 0.0
            avg_str = f"{avg:.2f}%" if pd.notna(avg) else "N/A"
            win_pct = f"{win / vcnt * 100:.1f}%" if vcnt > 0 else "N/A"
            logger.info(f"    {bname:<16} 样本={cnt:<4} 金叉命中={cr}/{cnt} "
                        f"({cross_pct:.1f}%)  平均涨幅={avg_str:<7}  上涨概率={win_pct} ({win}/{vcnt})")

    # 1) 信号强度分段
    _segment_stats('strength', [
        ('strength ≥ 85',   lambda r, p: pd.notna(p.get('strength')) and p['strength'] >= 85),
        ('80 ≤ strength<85', lambda r, p: pd.notna(p.get('strength')) and 80 <= p['strength'] < 85),
        ('70 ≤ strength<80', lambda r, p: pd.notna(p.get('strength')) and 70 <= p['strength'] < 80),
        ('strength < 70',   lambda r, p: pd.notna(p.get('strength')) and p['strength'] < 70),
        ('无强度数据',       lambda r, p: pd.isna(p.get('strength'))),
    ], '信号强度 strength')

    # 2) 量比分段
    _segment_stats('vol_ratio', [
        ('量比 ≥ 2.0', lambda r, p: pd.notna(p.get('vol_ratio')) and p['vol_ratio'] >= 2.0),
        ('1.5 ≤ 量比<2.0', lambda r, p: pd.notna(p.get('vol_ratio')) and 1.5 <= p['vol_ratio'] < 2.0),
        ('1.25 ≤ 量比<1.5', lambda r, p: pd.notna(p.get('vol_ratio')) and 1.25 <= p['vol_ratio'] < 1.5),
        ('1.0 ≤ 量比<1.25', lambda r, p: pd.notna(p.get('vol_ratio')) and 1.0 <= p['vol_ratio'] < 1.25),
        ('量比 < 1.0',  lambda r, p: pd.notna(p.get('vol_ratio')) and p['vol_ratio'] < 1.0),
        ('无量比数据', lambda r, p: pd.isna(p.get('vol_ratio'))),
    ], '量比（vol / vol_ma20）')

    # 3) 需涨幅度分段
    _segment_stats('needed_pct', [
        ('需涨 < 1%',   lambda r, p: pd.notna(p.get('needed_pct')) and p['needed_pct'] < 1.0),
        ('1% ≤ 需涨<2%', lambda r, p: pd.notna(p.get('needed_pct')) and 1.0 <= p['needed_pct'] < 2.0),
        ('2% ≤ 需涨<3%', lambda r, p: pd.notna(p.get('needed_pct')) and 2.0 <= p['needed_pct'] < 3.0),
        ('3% ≤ 需涨<5%', lambda r, p: pd.notna(p.get('needed_pct')) and 3.0 <= p['needed_pct'] < 5.0),
        ('无需涨数据',   lambda r, p: pd.isna(p.get('needed_pct'))),
    ], '需涨幅度（明日触发金叉所需涨幅）')

    # 4) 3日缩短比例分段
    _segment_stats('shrink_pct', [
        ('缩短 ≥ 70%', lambda r, p: pd.notna(p.get('shrink_pct')) and p['shrink_pct'] >= 70),
        ('50% ≤ 缩短<70%', lambda r, p: pd.notna(p.get('shrink_pct')) and 50 <= p['shrink_pct'] < 70),
        ('30% ≤ 缩短<50%', lambda r, p: pd.notna(p.get('shrink_pct')) and 30 <= p['shrink_pct'] < 50),
        ('缩短 < 30%', lambda r, p: pd.notna(p.get('shrink_pct')) and p['shrink_pct'] < 30),
        ('无缩短数据', lambda r, p: pd.isna(p.get('shrink_pct'))),
    ], '3日缩短比例')

    # 5) 信号日股价区间分段（使用 signal_close 列）
    # 在 parsed_flags 中预先提取价格值，避免 lambda 里反复容错
    for i, (_, row) in enumerate(result_df.iterrows()):
        if i >= len(parsed_flags):
            parsed_flags.append({})
        v = row.get('signal_close')
        try:
            parsed_flags[i]['price_val'] = float(v)
        except (ValueError, TypeError):
            parsed_flags[i]['price_val'] = float('nan')

    _segment_stats('price', [
        ('股价 < 5',    lambda r, p: pd.notna(p.get('price_val')) and p['price_val'] < 5),
        ('5 ≤ 股价 < 10', lambda r, p: pd.notna(p.get('price_val')) and 5 <= p['price_val'] < 10),
        ('10 ≤ 股价 < 20', lambda r, p: pd.notna(p.get('price_val')) and 10 <= p['price_val'] < 20),
        ('20 ≤ 股价 < 50', lambda r, p: pd.notna(p.get('price_val')) and 20 <= p['price_val'] < 50),
        ('股价 ≥ 50',   lambda r, p: pd.notna(p.get('price_val')) and p['price_val'] >= 50),
        ('无股价数据',  lambda r, p: pd.isna(p.get('price_val')) or p.get('price_val') is None),
    ], '信号日股价区间（元）')

    logger.info("-" * 70)

    # 打印明细表
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        title = (f"「MACD预测金叉」信号验证明细 · 共 {total_signals} 条 · "
                 f"显示前 {min(max_print, total_signals)} 条")
        table = Table(title=title, show_lines=False)
        table.add_column("#", justify="center", style="cyan", max_width=5)
        table.add_column("代码", style="magenta")
        table.add_column("信号日", justify="center")
        # 合并信号日MACD + 次日 + 是否增大为一列，让宽表格可读
        table.add_column("MACD(信号/次日)", justify="right")
        # 合并 "N日内是否有金叉 / 金叉日" 成一列
        table.add_column(f"{check_window}日内金叉", justify="center")
        # 窗口最高收益，同时用括号标出出现日，不再单列
        table.add_column("最高(high,k≥2)", justify="right", style="bold yellow")
        table.add_column("最高(close,k≥2)", justify="right", style="bold green")
        for k in range(1, check_window + 1):
            style = "bold green" if k == 1 else ""
            table.add_column(f"{k}日%", justify="right", style=style)

        for idx, row in result_df.head(max_print).iterrows():
            macd_signal = row['macd_at_signal']
            macd_next = row['next_day_macd']
            # 用简短符号表达「柱增大/减小」
            dir_sym = "↑" if row['macd_increase_next'] in ('✓', 'Y', 'y', 1, True) else "↓"
            macd_col = f"{macd_signal:.4f}→{macd_next:.4f}({dir_sym})"

            cross_col = (str(row['cross_date']) if row['cross_date'] else '—')
            if row.get('cross_within_window') in ('✓', 'Y', 'y', 1, True):
                cross_col = f"✓ {cross_col}"
            else:
                cross_col = f"✗ {cross_col}" if row['cross_date'] else '✗'

            high_pct = row['max_high_pct'] if not pd.isna(row['max_high_pct']) else None
            high_day = row.get('max_high_day')
            high_col = f"{high_pct:.2f}% @{high_day}" if high_pct is not None else '—'

            close_pct = row['max_close_pct'] if not pd.isna(row['max_close_pct']) else None
            close_day = row.get('max_close_day')
            close_col = f"{close_pct:.2f}% @{close_day}" if close_pct is not None else '—'

            cells = [
                str(idx + 1),
                row['code'],
                row['signal_date'],
                macd_col,
                cross_col,
                high_col,
                close_col,
            ]
            for k in range(1, check_window + 1):
                cells.append(f"{row[f'ret_{k}d_pct']:.2f}")
            table.add_row(*cells)
        console.print(table)
        if len(result_df) > max_print:
            logger.info(f"  表格只显示前 {max_print} 条，其余 {len(result_df) - max_print} 条省略")
    except Exception:
        for idx, row in result_df.head(max_print).iterrows():
            high_pct = row['max_high_pct'] if not pd.isna(row['max_high_pct']) else None
            high_day = row.get('max_high_day')
            close_pct = row['max_close_pct'] if not pd.isna(row['max_close_pct']) else None
            close_day = row.get('max_close_day')
            high_parts = f"最高(high)={high_pct:.2f}%@第{high_day}天" if high_pct is not None else "最高(high)=—"
            close_parts = f"最高(close)={close_pct:.2f}%@第{close_day}天" if close_pct is not None else "最高(close)=—"
            ret_parts = " ".join(
                f"{k}d={row[f'ret_{k}d_pct']:.2f}%" for k in range(1, check_window + 1)
            )
            logger.info(
                f"  {idx + 1:>3}. {row['code']} 信号:{row['signal_date']} "
                f"MACD={row['macd_at_signal']:.4f}→{row['next_day_macd']:.4f} "
                f"金叉={row['cross_within_window']}({row['cross_date'] or '—'}) "
                f"{high_parts} {close_parts} {ret_parts}"
            )

    logger.info("-" * 70)

    # 导出 CSV
    try:
        csv_path = settings.OUTPUT_DIR / f"macd_prediction_verify_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        cols = ['code', 'name', 'signal_date', 'signal_close', 'signal_price',
                'strength', 'reason', 'macd_at_signal',
                'dif_at_signal', 'dea_at_signal', 'next_day_macd',
                'macd_increase_next',
                'cross_within_window', 'cross_day', 'cross_date',
                'max_high_pct', 'max_high_day', 'max_close_pct', 'max_close_day']
        for k in range(1, check_window + 1):
            cols.append(f'ret_{k}d_pct')
        result_df[cols].to_csv(csv_path, index=False, encoding='utf-8-sig')
        logger.info(f"验证结果已导出 CSV: {csv_path} (共 {len(result_df)} 条)")
    except Exception as e:
        logger.warning(f"导出 CSV 失败: {e}")

    return result_df
