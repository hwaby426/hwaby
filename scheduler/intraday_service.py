from datetime import datetime
from typing import Optional
from loguru import logger
import pandas as pd

from data_fetcher.baostock_fetcher import get_daily_kline_df, normalize_code
from data_fetcher.sina_fetcher import get_realtime_quotes


def build_intraday_daily_df(code: str, quote: dict, history_days: int = 180) -> pd.DataFrame:
    """构建盘中日线DataFrame（历史数据 + 当日合成）

    Args:
        code: 股票代码
        quote: 实时行情数据
        history_days: 历史数据天数，默认180天（半年）
    """
    df_hist = get_daily_kline_df(code, start_date=None)
    if df_hist.empty:
        return pd.DataFrame()

    today = datetime.now().strftime('%Y-%m-%d')
    last_date = df_hist['date'].iloc[-1]

    if last_date == today:
        df_hist.loc[df_hist.index[-1], 'open'] = float(quote.get('open', 0))
        df_hist.loc[df_hist.index[-1], 'high'] = float(quote.get('high', 0))
        df_hist.loc[df_hist.index[-1], 'low'] = float(quote.get('low', 0))
        df_hist.loc[df_hist.index[-1], 'close'] = float(quote.get('price', 0))
        df_hist.loc[df_hist.index[-1], 'volume'] = int(quote.get('volume', 0))
        df_hist.loc[df_hist.index[-1], 'amount'] = float(quote.get('amount', 0))
    else:
        preclose = float(df_hist['close'].iloc[-1])
        price = float(quote.get('price', 0))
        pct_chg = ((price - preclose) / preclose * 100) if preclose > 0 else 0.0
        today_row = {
            'date': today,
            'code': code,
            'open': float(quote.get('open', 0)),
            'high': float(quote.get('high', 0)),
            'low': float(quote.get('low', 0)),
            'close': price,
            'volume': int(quote.get('volume', 0)),
            'amount': float(quote.get('amount', 0)),
            'pct_chg': pct_chg,
            'turnover': 0.0,
        }
        df_hist = pd.concat([df_hist, pd.DataFrame([today_row])], ignore_index=True)

    return df_hist


def build_historical_daily_df(code: str, target_date: str, history_days: int = 180) -> pd.DataFrame:
    """构建指定日期的历史日线DataFrame（无需实时行情，用于周末/盘后扫描）

    Args:
        code: 股票代码
        target_date: 目标日期 (YYYY-MM-DD)，扫描这一天的信号
        history_days: 历史数据天数，默认180天（半年）

    Returns:
        DataFrame，最后一行为 target_date 的K线。如果 target_date 没有交易数据，返回空
    """
    df_hist = get_daily_kline_df(code, start_date=None, end_date=target_date)
    if df_hist.empty:
        return pd.DataFrame()

    last_date = df_hist['date'].iloc[-1]
    if last_date != target_date:
        return pd.DataFrame()

    return df_hist
