import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import baostock as bs
import pandas as pd
from loguru import logger
from tqdm import tqdm

from db.database import session_scope
from db.models import StockInfo, DailyKline
from config.settings import settings, BASE_DIR

sys.path.insert(0, str(BASE_DIR))

ADJUST_FLAG = '2'

BS_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,"
    "turn,pctChg,isST"
)

_request_interval = 0.3
_last_request_time = 0.0
_min_interval = 0.2
_max_interval = 2.0


def _rate_limit():
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < _request_interval:
        time.sleep(_request_interval - elapsed)
    _last_request_time = time.time()


def _slow_down():
    global _request_interval
    _request_interval = min(_max_interval, _request_interval * 1.5)
    logger.debug(f"请求降速，间隔调整为 {_request_interval:.2f}s")


def _speed_up():
    global _request_interval
    _request_interval = max(_min_interval, _request_interval * 0.95)


def normalize_code(code: str) -> str:
    """将股票代码规范化为 BaoStock 格式（sh.600519）"""
    code = code.strip().lower()
    if not code:
        return ''
    # 已经带点的格式，直接返回
    if '.' in code:
        return code
    # 没有点，添加点
    if code.startswith('sh') or code.startswith('sz'):
        return f"{code[:2]}.{code[2:]}"
    # 只有数字，根据前缀添加市场
    if code.startswith('6'):
        return f"sh.{code}"
    elif code.startswith('0') or code.startswith('3'):
        return f"sz.{code}"
    return code


def bs_login(max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            lg = bs.login()
            if lg.error_code == '0':
                logger.info("BaoStock 登录成功")
                return True
            err_msg = lg.error_msg or ''
            logger.warning(f"BaoStock 登录失败(第{attempt+1}次): {err_msg}")
            if "网络接收错误" in err_msg or "10054" in err_msg or "连接" in err_msg:
                time.sleep(2 + attempt * 3)
                continue
            return False
        except Exception as e:
            logger.warning(f"BaoStock 登录异常(第{attempt+1}次): {e}")
            time.sleep(2 + attempt * 3)
    logger.error("BaoStock 登录失败，已达最大重试次数")
    return False


def bs_logout():
    try:
        bs.logout()
        logger.info("BaoStock 登出")
    except Exception as e:
        logger.debug(f"BaoStock 登出异常: {e}")


def fetch_stock_list(day: Optional[str] = None) -> List[dict]:
    if day is None:
        day = datetime.now().strftime('%Y-%m-%d')
    rs = bs.query_all_stock(day=day)
    if rs.error_code != '0':
        logger.error(f"获取股票列表失败: {rs.error_msg}")
        return []
    df = rs.get_data()
    if df.empty:
        logger.warning(f"股票列表为空: {day}")
        return []
    stocks = []
    for _, row in df.iterrows():
        code = row['code']
        if not (code.startswith('sh.') or code.startswith('sz.')):
            continue
        if len(code.split('.')[-1]) != 6:
            continue
        stocks.append({
            'code': code,
            'symbol': code.split('.')[-1],
            'name': row.get('code_name', ''),
            'market': code.split('.')[0],
            'status': int(row.get('tradeStatus', 1)),
        })
    logger.info(f"获取到 {len(stocks)} 只 A 股股票")
    return stocks


def sync_stock_info(stocks: List[dict]):
    with session_scope() as session:
        for s in stocks:
            existing = session.query(StockInfo).filter_by(code=s['code']).first()
            if existing:
                existing.name = s['name']
                existing.status = s['status']
            else:
                info = StockInfo(
                    code=s['code'],
                    symbol=s['symbol'],
                    name=s['name'],
                    market=s['market'],
                    status=s['status'],
                )
                session.add(info)
    logger.info(f"股票列表同步完成，共 {len(stocks)} 只")


def fetch_daily_kline(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str = 'd',
    adjustflag: str = ADJUST_FLAG,
) -> pd.DataFrame:
    _rate_limit()
    rs = bs.query_history_k_data_plus(
        code,
        BS_FIELDS,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        adjustflag=adjustflag,
    )
    if rs.error_code != '0':
        err_msg = rs.error_msg or ''
        if "网络接收错误" in err_msg or "10054" in err_msg or "连接被重置" in err_msg:
            raise ConnectionError(f"{code} 获取K线失败: {err_msg}")
        logger.error(f"{code} 获取K线失败: {err_msg}")
        return pd.DataFrame()
    df = rs.get_data()
    if df.empty:
        return df
    df['open'] = pd.to_numeric(df['open'], errors='coerce')
    df['high'] = pd.to_numeric(df['high'], errors='coerce')
    df['low'] = pd.to_numeric(df['low'], errors='coerce')
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['preclose'] = pd.to_numeric(df['preclose'], errors='coerce')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0).astype('int64')
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0)
    df['turn'] = pd.to_numeric(df['turn'], errors='coerce')
    df['pctChg'] = pd.to_numeric(df['pctChg'], errors='coerce')
    df['isST'] = pd.to_numeric(df['isST'], errors='coerce').fillna(0).astype(int)
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df = df.sort_values('date').reset_index(drop=True)
    return df


def get_last_kline_date(code: str) -> Optional[str]:
    with session_scope() as session:
        last = (
            session.query(DailyKline)
            .filter_by(code=code, adjustflag=int(ADJUST_FLAG))
            .order_by(DailyKline.trade_date.desc())
            .first()
        )
        if last:
            return last.trade_date.strftime('%Y-%m-%d')
    return None


def save_daily_klines(code: str, df: pd.DataFrame):
    if df.empty:
        return
    from sqlalchemy import text
    with session_scope() as session:
        adjust_flag = int(ADJUST_FLAG)
        rows = []
        for _, row in df.iterrows():
            rows.append({
                'code': code,
                'trade_date': row['date'],
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': int(row['volume']),
                'amount': float(row['amount']),
                'pct_chg': float(row['pctChg']) if pd.notna(row['pctChg']) else None,
                'turnover': float(row['turn']) if pd.notna(row['turn']) else None,
                'adjustflag': adjust_flag,
            })

        sql = text("""
            INSERT INTO daily_kline 
                (code, trade_date, open, high, low, close, volume, amount, pct_chg, turnover, adjustflag, created_at)
            VALUES 
                (:code, :trade_date, :open, :high, :low, :close, :volume, :amount, :pct_chg, :turnover, :adjustflag, NOW())
            ON DUPLICATE KEY UPDATE
                open = VALUES(open),
                high = VALUES(high),
                low = VALUES(low),
                close = VALUES(close),
                volume = VALUES(volume),
                amount = VALUES(amount),
                pct_chg = VALUES(pct_chg),
                turnover = VALUES(turnover)
        """)
        session.execute(sql, rows)


def update_single_stock(code: str, end_date: str = None) -> int:
    code = normalize_code(code)
    if end_date is None:
        end_date = datetime.now().strftime('%Y-%m-%d')
    last_date = get_last_kline_date(code)
    if last_date:
        start_date = (
            datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
        ).strftime('%Y-%m-%d')
        if start_date > end_date:
            return 0
    else:
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    for attempt in range(3):
        try:
            df = fetch_daily_kline(code, start_date, end_date)
            if df.empty:
                return 0
            save_daily_klines(code, df)
            return len(df)
        except Exception as e:
            err_msg = str(e)
            if "10054" in err_msg or "连接被重置" in err_msg or "远程主机强迫关闭" in err_msg or "网络接收错误" in err_msg:
                logger.warning(f"{code} 连接被重置，重新登录 BaoStock...")
                try:
                    bs_logout()
                except Exception:
                    pass
                time.sleep(2 + attempt * 2)
                if not bs_login():
                    time.sleep(5)
                continue
            logger.warning(f"{code} 第{attempt+1}次失败: {e}")
            time.sleep(1 + attempt * 2)
    logger.error(f"{code} 更新失败，已重试3次")
    return 0


def update_all_daily_klines(
    stock_pool: Optional[List[str]] = None,
):
    """顺序更新所有股票的日线数据，支持CTRL+C中断"""
    if not bs_login():
        return
    try:
        if not stock_pool:
            stock_pool = settings.get_stock_pool()
        codes = stock_pool
        logger.info(f"开始更新 {len(codes)} 只股票的日线数据")
        total = 0
        for code in tqdm(codes, desc="更新日线"):
            try:
                cnt = update_single_stock(code)
                total += cnt
            except Exception as e:
                logger.error(f"{code} 异常: {e}")
        logger.info(f"日线更新完成，共新增/更新 {total} 条K线")
        return total
    except KeyboardInterrupt:
        logger.info("收到中断信号，停止更新...")
        return total if 'total' in dir() else 0
    finally:
        bs_logout()


def init_all_stocks(stock_pool: Optional[List[str]] = None):
    update_all_daily_klines(stock_pool=stock_pool)


def init_market_all(
    batch_size: int = 100,
    max_stocks: Optional[int] = None,
):
    """初始化全市场股票的日线数据，从stock_info表读取股票列表"""
    from data_fetcher.stock_pool import get_stock_pool_from_db
    from datetime import datetime

    stock_pool = get_stock_pool_from_db()
    if not stock_pool:
        logger.error("股票池为空，请先运行 update-stock-list 更新股票列表")
        return

    if max_stocks:
        stock_pool = stock_pool[:max_stocks]

    total = len(stock_pool)
    logger.info("=" * 60)
    logger.info(f"全市场日线数据初始化")
    logger.info(f"股票总数: {total} 只")
    logger.info(f"批次大小: {batch_size}")
    logger.info("=" * 60)

    success = 0
    failed = 0
    total_klines = 0
    skipped = 0
    start_time = datetime.now()

    if not bs_login():
        return

    try:
        for i, code in enumerate(stock_pool, 1):
            try:
                cnt = update_single_stock(code)
                total_klines += cnt
                success += 1

                if cnt == 0:
                    skipped += 1

                if i % 50 == 0:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    speed = i / elapsed if elapsed > 0 else 0
                    remaining = (total - i) / speed if speed > 0 else 0
                    eta_min = remaining / 60
                    logger.info(
                        f"[进度] {i:>5}/{total} ({i*100//total:>3}%) "
                        f"成功:{success:>4} 失败:{failed:>3} 跳过:{skipped:>4} "
                        f"K线:{total_klines:>6} "
                        f"速度:{speed:.1f}只/秒 "
                        f"预计剩余:{eta_min:.0f}分钟"
                    )

            except KeyboardInterrupt:
                logger.info("-" * 60)
                logger.info(f"收到中断信号，停止更新")
                break
            except Exception as e:
                failed += 1
                if failed <= 5:
                    logger.error(f"{code} 失败: {e}")
                elif failed == 6:
                    logger.warning("... 后续失败将不再打印详细信息")

            if i % batch_size == 0:
                logger.info("-" * 60)
                logger.info(
                    f"[批次完成] {i}/{total} ({i*100//total}%) "
                    f"成功:{success} 失败:{failed} 新增K线:{total_klines}"
                )

            if i % 1000 == 0:
                logger.info("[连接维护] 重新登录 BaoStock...")
                bs_logout()
                time.sleep(2)
                if not bs_login():
                    logger.error("重新登录失败，中止")
                    break

        elapsed = (datetime.now() - start_time).total_seconds()
        speed = success / elapsed if elapsed > 0 else 0
        logger.info("=" * 60)
        logger.info(f"全市场初始化完成")
        logger.info(f"总耗时: {elapsed:.0f}秒 ({elapsed/60:.1f}分钟)")
        logger.info(f"处理股票: {total} 只")
        logger.info(f"成功: {success} 只")
        logger.info(f"失败: {failed} 只")
        logger.info(f"跳过(无新数据): {skipped} 只")
        logger.info(f"新增K线: {total_klines} 条")
        logger.info(f"平均速度: {speed:.1f} 只/秒")
        logger.info("=" * 60)
    finally:
        bs_logout()


def get_daily_kline_df(
    code: str,
    start_date: str = None,
    end_date: str = None,
    adjustflag: int = 2,
) -> pd.DataFrame:
    with session_scope() as session:
        query = session.query(DailyKline).filter_by(code=code, adjustflag=adjustflag)
        if start_date:
            query = query.filter(DailyKline.trade_date >= start_date)
        if end_date:
            query = query.filter(DailyKline.trade_date <= end_date)
        query = query.order_by(DailyKline.trade_date.asc())
        rows = query.all()
        if not rows:
            return pd.DataFrame()
        data = []
        for r in rows:
            data.append({
                'date': r.trade_date.strftime('%Y-%m-%d'),
                'code': r.code,
                'open': float(r.open),
                'high': float(r.high),
                'low': float(r.low),
                'close': float(r.close),
                'volume': int(r.volume or 0),
                'amount': float(r.amount or 0),
                'pct_chg': float(r.pct_chg) if r.pct_chg else 0.0,
                'turnover': float(r.turnover) if r.turnover else 0.0,
            })
        return pd.DataFrame(data)


def get_daily_kline_batch(
    code_list: List[str],
    start_date: str = None,
    end_date: str = None,
    adjustflag: int = 2,
    batch_size: int = 1000,
) -> Dict[str, pd.DataFrame]:
    """批量获取多只股票的日线数据

    Args:
        code_list: 股票代码列表（数据库中的格式，如 sh.600000）
        start_date: 起始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        adjustflag: 复权类型，默认后复权(2)
        batch_size: 每批查询的股票数量，默认 1000

    Returns:
        {code: DataFrame} —— 每只股票一个 DataFrame（已按 code 分组、排序）
    """
    if not code_list:
        return {}

    result = {}
    # 分批查询，避免 SQL IN 子句过长
    for start_i in range(0, len(code_list), batch_size):
        batch = code_list[start_i:start_i + batch_size]
        with session_scope() as session:
            q = session.query(
                DailyKline.code,
                DailyKline.trade_date,
                DailyKline.open,
                DailyKline.high,
                DailyKline.low,
                DailyKline.close,
                DailyKline.volume,
                DailyKline.amount,
                DailyKline.pct_chg,
                DailyKline.turnover,
            ).filter(
                DailyKline.code.in_(batch),
                DailyKline.adjustflag == adjustflag,
            )
            if start_date:
                q = q.filter(DailyKline.trade_date >= start_date)
            if end_date:
                q = q.filter(DailyKline.trade_date <= end_date)
            q = q.order_by(DailyKline.code, DailyKline.trade_date.asc())
            rows = q.all()

            if not rows:
                continue

            # 一次性构建大 DataFrame，再按 code 分组
            data = [{
                'date': str(r.trade_date),
                'code': r.code,
                'open': float(r.open),
                'high': float(r.high),
                'low': float(r.low),
                'close': float(r.close),
                'volume': int(r.volume or 0),
                'amount': float(r.amount or 0),
                'pct_chg': float(r.pct_chg) if r.pct_chg else 0.0,
                'turnover': float(r.turnover) if r.turnover else 0.0,
            } for r in rows]

            big_df = pd.DataFrame(data)
            for c, sub in big_df.groupby('code'):
                result[c] = sub.sort_values('date').reset_index(drop=True)

    return result
