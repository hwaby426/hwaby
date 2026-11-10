"""1 分钟 MACD 信号判据 + 分时数据归档（盯盘核心）。

只做三件事：取 1min K 线、算 DIF/DEA 与信号、幂等写 min_kline。
不含回测/安慰剂/参数网格——那些是研究工具，已随精简移除。

口径（与精简前 tools/minute_macd.py 的 signed 口径一致）：
  · EMA12/26/9，种子=序列首值（跨日连续，不按日重置）
  · 卖出：DIF>0 且 DEA>0 且 DIF 处「截至当根」滚动 60 根分位 ≥0.7 → 死叉
  · 买入：DIF<0 且 DEA<0 且 DEA 处「截至当根」滚动 60 根分位 ≤0.3 → 金叉
  · 高低位**必须**用滚动分位：用全天/全序列分位会把尾盘信息泄进早盘，是未来函数
  · 前 35 根为 EMA 预热期，不出信号
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import numpy as np
import pandas as pd
import requests

SINA_URL = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x_/'
            'CN_MarketDataService.getKLineData?symbol={code}&scale={scale}&ma=no&datalen={n}')
TENCENT_HOSTS = ('https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},{p},,800',
                 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline?param={code},{p},,800')
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'}

SCALES = {1: '1min', 5: '5min', 15: '15min', 30: '30min', 60: '60min'}
WARMUP = 35          # EMA26 + DEA9 预热
LOOKBACK = 60        # 滚动分位窗口
HI_Q = 0.7           # DIF 高位阈值
LO_Q = 0.3           # DEA 低位阈值


def norm_code(code: str) -> str:
    """统一成新浪/腾讯用的无点格式：sz.300454 / 300454 → sz300454。"""
    c = code.strip().lower().replace('.', '')
    if c.isdigit():
        return ('sh' if c.startswith(('6', '9')) else 'sz') + c
    return c


def db_code(code: str) -> str:
    """统一成 min_kline / daily_kline 用的带点格式：sz300454 → sz.300454。

    ⚠ 全项目只用这一种格式写 min_kline。旧 realtime_job 写的是不带点的
    sz002624，同表会出现两份数据，回测按带点查就会漏掉实时那份。
    """
    c = norm_code(code)
    return f'{c[:2]}.{c[2:]}'


def fetch_sina(code: str, scale: int = 1, n: int = 1023) -> pd.DataFrame:
    r = requests.get(SINA_URL.format(code=norm_code(code), scale=scale, n=n),
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = 'utf-8'
    m = re.search(r'\((.*)\)', r.text, re.S)
    if not m:
        raise ValueError('新浪返回不是 JSONP，接口可能改版')
    rows = json.loads(m.group(1))
    if not rows:
        raise ValueError('新浪返回空列表')
    df = pd.DataFrame(rows)
    df['dt'] = pd.to_datetime(df['day'])
    for col in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['date'] = df['dt'].dt.date
    return df[['dt', 'date', 'open', 'high', 'low', 'close',
               'volume', 'amount']].reset_index(drop=True)


def fetch_tencent(code: str, scale: int = 1) -> pd.DataFrame:
    """备源。腾讯字段序是 [时间, 开, 收, 高, 低, 量, ...]，必须显式重排。"""
    p = f'm{scale}'
    for tpl in TENCENT_HOSTS:
        try:
            r = requests.get(tpl.format(code=norm_code(code), p=p),
                             headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
            r.raise_for_status()
            raw = r.json()['data'][norm_code(code)].get(p) or []
            if not raw:
                continue
            df = pd.DataFrame(
                [[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                  float(x[5]) if len(x) > 5 else 0.0] for x in raw],
                columns=['ts', 'open', 'close', 'high', 'low', 'volume'])
            df['dt'] = pd.to_datetime(df['ts'], format='%Y%m%d%H%M')
            df['date'] = df['dt'].dt.date
            df['amount'] = 0.0
            return df[['dt', 'date', 'open', 'high', 'low', 'close',
                       'volume', 'amount']].reset_index(drop=True)
        except Exception:
            continue
    raise ValueError('腾讯分钟线两个域名均不可达')


def is_fresh(df: pd.DataFrame, max_days: int = 15,
             now: datetime | None = None) -> bool:
    """免费源对不支持的标的不报错，而是返回过期数据。必须校验最新一根时点。"""
    if df.empty:
        return False
    return ((now or datetime.now()).date() - df['dt'].max().date()).days <= max_days


def load(code: str, scale: int = 1) -> tuple[pd.DataFrame, str]:
    """新浪优先，失败退腾讯；两源都要过新鲜度校验。"""
    try:
        df = fetch_sina(code, scale)
        if is_fresh(df):
            return df, 'sina'
    except Exception as e:
        print(f'  [warn] 新浪 {scale}分钟取数失败：{type(e).__name__} {e}')
    df = fetch_tencent(code, scale)
    if not is_fresh(df):
        raise ValueError(f'备源 {scale} 分钟数据已过期，拒绝使用')
    return df, 'tencent'


def macd(close: pd.Series, short: int = 12, long: int = 26, mid: int = 9):
    """EMA 种子=首值（ewm adjust=False），与 features/builder.py 的 mytt 口径一致。"""
    dif = close.ewm(span=short, adjust=False).mean() - close.ewm(span=long, adjust=False).mean()
    dea = dif.ewm(span=mid, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def compute_signals(df: pd.DataFrame, hi_q: float = HI_Q, lo_q: float = LO_Q,
                    lookback: int = LOOKBACK, warmup: int = WARMUP) -> pd.DataFrame:
    """逐根算出 DIF/DEA/柱、滚动分位与 sell/buy 标记。

    返回的 DataFrame 与 df 等长、按时间升序；调用方取新增的尾部行即可。
    """
    close = df['close'].reset_index(drop=True)
    dif, dea, hist = macd(close)
    gap = dif - dea
    gold = ((gap.shift(1) <= 0) & (gap > 0)).to_numpy()
    dead = ((gap.shift(1) >= 0) & (gap < 0)).to_numpy()

    # 截至当根的滚动分位（rank(pct=True) 是 1-based，窗口内自身也参与排名）
    p_dif = dif.rolling(lookback, min_periods=20).rank(pct=True).to_numpy()
    p_dea = dea.rolling(lookback, min_periods=20).rank(pct=True).to_numpy()
    dv, ev = dif.to_numpy(), dea.to_numpy()
    valid = np.arange(len(df)) >= warmup

    hi_cond = (dv > 0) & (ev > 0) & (p_dif >= hi_q)   # 双正 + DIF 偏大
    lo_cond = (dv < 0) & (ev < 0) & (p_dea <= lo_q)   # 双负 + DEA 偏小

    out = pd.DataFrame({
        'dt': df['dt'].reset_index(drop=True),
        'close': close,
        'dif': dif.to_numpy(),
        'dea': dea.to_numpy(),
        'hist': hist.to_numpy(),
        'pct_dif': p_dif,
        'pct_dea': p_dea,
        'sell': dead & hi_cond & valid,
        'buy': gold & lo_cond & valid,
    })
    return out


def archive_rows(df: pd.DataFrame, code: str, scale: int = 1) -> list[dict]:
    """把 K 线转成 UPSERT 参数（纯函数，便于校验格式与幂等键）。"""
    return [dict(code=db_code(code), period=SCALES[scale], t=str(r.dt),
                 o=float(r.open), h=float(r.high), l=float(r.low),
                 cl=float(r.close), v=int(r.volume or 0), a=float(r.amount or 0))
            for r in df.itertuples(index=False)]


def archive(df: pd.DataFrame, code: str, scale: int = 1) -> int:
    """幂等写入 min_kline（唯一键 code+period+kline_time），code 统一带点。"""
    from db.database import engine
    from sqlalchemy import text

    rows = archive_rows(df, code, scale)
    if not rows:
        return 0
    sql = text("""INSERT INTO min_kline
                  (code,period,kline_time,open,high,low,close,volume,amount)
                  VALUES (:code,:period,:t,:o,:h,:l,:cl,:v,:a)
                  ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),
                    low=VALUES(low),close=VALUES(close),volume=VALUES(volume),
                    amount=VALUES(amount)""")
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)
