import time
import re
import math
from datetime import datetime
from typing import List, Dict, Optional
import requests
import pandas as pd
from loguru import logger

SINA_REALTIME_URL = "https://hq.sinajs.cn/etag.php"
SINA_KLINE_URL = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

SINA_HEADERS = {
    'Referer': 'https://finance.sina.com.cn',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}


def _convert_sina_code(code: str) -> str:
    code = code.lower()
    if code.startswith('sh.') or code.startswith('sz.'):
        return code.replace('.', '')
    if code.startswith('sh') or code.startswith('sz'):
        return code
    if len(code) == 6:
        if code.startswith('6'):
            return f'sh{code}'
        else:
            return f'sz{code}'
    return code


def _normalize_code(sina_code: str) -> str:
    """sh600519 -> sh.600519"""
    m = re.match(r'(sh|sz)(\d{6})', sina_code.lower())
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return sina_code


def get_realtime_quotes(codes: List[str]) -> pd.DataFrame:
    """
    批量获取实时行情（一次最多约80只，超过自动分批）
    返回 DataFrame 列: code,name,open,preclose,price,high,low,bid1,ask1,volume,amount,time
    """
    if not codes:
        return pd.DataFrame()

    sina_codes = [_convert_sina_code(c) for c in codes]
    batch_size = 80
    all_rows = []

    for i in range(0, len(sina_codes), batch_size):
        batch = sina_codes[i:i + batch_size]
        list_param = ','.join(batch)
        url = f"{SINA_REALTIME_URL}?list={list_param}"
        try:
            resp = requests.get(url, headers=SINA_HEADERS, timeout=5)
            resp.encoding = 'gbk'
            text = resp.text
            rows = _parse_sina_realtime(text)
            all_rows.extend(rows)
        except Exception as e:
            logger.error(f"获取新浪实时行情失败: {e}")
        time.sleep(0.2)

    if not all_rows:
        return pd.DataFrame()
    return pd.DataFrame(all_rows)


def _parse_sina_realtime(text: str) -> List[dict]:
    rows = []
    pattern = r'var hq_str_([a-z]{2}\d{6})="([^"]*)";'
    for m in re.finditer(pattern, text):
        sina_code = m.group(1)
        data = m.group(2).split(',')
        if len(data) < 32:
            continue
        try:
            code = _normalize_code(sina_code)
            name = data[0]
            open_price = float(data[1]) if data[1] else 0.0
            preclose = float(data[2]) if data[2] else 0.0
            price = float(data[3]) if data[3] else 0.0
            high = float(data[4]) if data[4] else 0.0
            low = float(data[5]) if data[5] else 0.0
            bid1 = float(data[6]) if data[6] else 0.0
            ask1 = float(data[7]) if data[7] else 0.0
            volume = int(float(data[8])) if data[8] else 0
            amount = float(data[9]) if data[9] else 0.0
            quote_time = data[31] if len(data) > 31 else ''
            rows.append({
                'code': code,
                'name': name,
                'open': open_price,
                'preclose': preclose,
                'price': price,
                'high': high,
                'low': low,
                'bid1': bid1,
                'ask1': ask1,
                'volume': volume,
                'amount': amount,
                'time': quote_time,
            })
        except (ValueError, IndexError) as e:
            logger.warning(f"解析 {sina_code} 失败: {e}")
    return rows


def get_minute_kline(
    code: str,
    scale: int = 5,
    datalen: int = 1023,
) -> pd.DataFrame:
    """
    获取分钟K线（新浪历史分钟数据）
    scale: 5=5min, 15=15min, 30=30min, 60=60min
    datalen: 数据条数，最大1023
    """
    sina_code = _convert_sina_code(code)
    params = {
        'symbol': sina_code,
        'scale': scale,
        'ma': 5,
        'datalen': datalen,
    }
    try:
        resp = requests.get(SINA_KLINE_URL, params=params, headers=SINA_HEADERS, timeout=10)
        resp.encoding = 'utf-8'
        data = resp.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.rename(columns={
            'day': 'kline_time',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume',
        })
        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0).astype('int64')
        df['amount'] = 0.0
        df['code'] = _normalize_code(sina_code)
        df = df.sort_values('kline_time').reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"获取{code} {scale}分钟K线失败: {e}")
        return pd.DataFrame()


def is_trading_time(now: Optional[datetime] = None) -> bool:
    if now is None:
        now = datetime.now()
    t = now.time()
    morning_start = datetime.strptime('09:30', '%H:%M').time()
    morning_end = datetime.strptime('11:30', '%H:%M').time()
    afternoon_start = datetime.strptime('13:00', '%H:%M').time()
    afternoon_end = datetime.strptime('15:00', '%H:%M').time()
    if morning_start <= t <= morning_end:
        return True
    if afternoon_start <= t <= afternoon_end:
        return True
    return False


def is_market_open_day(now: Optional[datetime] = None) -> bool:
    if now is None:
        now = datetime.now()
    return now.weekday() < 5
