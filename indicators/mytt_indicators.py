import sys
import pandas as pd
import numpy as np

from config.settings import settings, BASE_DIR

sys.path.insert(0, str(BASE_DIR / 'lib'))
from MyTT import (
    MA, EMA, MACD, KDJ, RSI, BOLL, CCI, ATR, BIAS, WR,
    HHV, LLV, REF, CROSS,
)


def calc_ma(df: pd.DataFrame) -> pd.DataFrame:
    CLOSE = df['close'].values
    df['ma5'] = MA(CLOSE, 5)
    df['ma10'] = MA(CLOSE, 10)
    df['ma20'] = MA(CLOSE, 20)
    df['ma60'] = MA(CLOSE, 60)
    return df


def calc_macd(df: pd.DataFrame, short=12, long=26, mid=9) -> pd.DataFrame:
    CLOSE = df['close'].values
    dif, dea, macd = MACD(CLOSE, SHORT=short, LONG=long, M=mid)
    df['dif'] = dif
    df['dea'] = dea
    df['macd'] = macd
    df['ema12'] = EMA(CLOSE, short)
    df['ema26'] = EMA(CLOSE, long)
    return df


def calc_kdj(df: pd.DataFrame, n=9, m1=3, m2=3) -> pd.DataFrame:
    CLOSE = df['close'].values
    HIGH = df['high'].values
    LOW = df['low'].values
    k, d, j = KDJ(CLOSE, HIGH, LOW, N=n, M1=m1, M2=m2)
    df['k'] = k
    df['d'] = d
    df['j'] = j
    return df


def calc_rsi(df: pd.DataFrame) -> pd.DataFrame:
    CLOSE = df['close'].values
    df['rsi6'] = RSI(CLOSE, 6)
    df['rsi12'] = RSI(CLOSE, 12)
    df['rsi24'] = RSI(CLOSE, 24)
    return df


def calc_boll(df: pd.DataFrame, n=20, k=2) -> pd.DataFrame:
    CLOSE = df['close'].values
    upper, mid, lower = BOLL(CLOSE, N=n, P=k)
    df['upper'] = upper
    df['mid'] = mid
    df['lower'] = lower
    return df


def calc_cci(df: pd.DataFrame, n=14) -> pd.DataFrame:
    CLOSE = df['close'].values
    HIGH = df['high'].values
    LOW = df['low'].values
    df['cci'] = CCI(CLOSE, HIGH, LOW, N=n)
    return df


def calc_atr(df: pd.DataFrame, n=14) -> pd.DataFrame:
    CLOSE = df['close'].values
    HIGH = df['high'].values
    LOW = df['low'].values
    df['atr'] = ATR(CLOSE, HIGH, LOW, N=n)
    return df


def calc_bias(df: pd.DataFrame) -> pd.DataFrame:
    CLOSE = df['close'].values
    bias1, bias2, bias3 = BIAS(CLOSE, L1=6, L2=12, L3=24)
    df['bias6'] = bias1
    df['bias12'] = bias2
    df['bias24'] = bias3
    return df


def calc_wr(df: pd.DataFrame) -> pd.DataFrame:
    CLOSE = df['close'].values
    HIGH = df['high'].values
    LOW = df['low'].values
    wr1, wr2 = WR(CLOSE, HIGH, LOW, N=10, N1=6)
    df['wr10'] = wr1
    df['wr6'] = wr2
    return df


def calc_volume_ma(df: pd.DataFrame) -> pd.DataFrame:
    VOLUME = df['volume'].values.astype(float)
    df['vol_ma5'] = MA(VOLUME, 5)
    df['vol_ma10'] = MA(VOLUME, 10)
    df['vol_ma20'] = MA(VOLUME, 20)
    return df


def calc_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df = calc_ma(df)
    df = calc_macd(df)
    df = calc_kdj(df)
    df = calc_rsi(df)
    df = calc_boll(df)
    df = calc_cci(df)
    df = calc_atr(df)
    df = calc_bias(df)
    df = calc_wr(df)
    df = calc_volume_ma(df)
    return df
