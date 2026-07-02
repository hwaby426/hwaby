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

_request_interval = 0.2
_last_request_time = 0.0
_min_interval = 0.1
_max_interval = 0.5
_base_interval = 0.2


def _rate_limit():
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < _request_interval:
        time.sleep(_request_interval - elapsed)
    _last_request_time = time.time()


def _slow_down():
    """遭遇限流/服务端错误时减速（幅度温和，避免卡死）"""
    global _request_interval
    _request_interval = min(_max_interval, _request_interval * 1.15)
    logger.debug(f"请求降速，间隔调整为 {_request_interval:.2f}s")


def _speed_up():
    """请求成功时快速回到基础间隔"""
    global _request_interval
    _request_interval = max(_min_interval, _request_interval * 0.90)


def _reset_interval():
    """重登录后重置请求间隔"""
    global _request_interval
    _request_interval = _base_interval


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
        # 需要触发重连的错误：网络断开 / session 失效
        if any(k in err_msg for k in ["网络接收错误", "10054", "连接被重置", "用户未登录", "you don't login", "login"]):
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


def get_last_kline_dates_batch(codes: List[str]) -> Dict[str, str]:
    """一次性查询所有股票的最后K线日期（代替 N 次单表查询）

    Args:
        codes: 股票代码列表，支持带点("sz.002624")或不带点("sz002624")格式

    Returns:
        {code_in_dot_format: last_date_str} — 仅包含有数据的股票
    """
    if not codes:
        return {}

    # 统一规范化为带点格式
    norm_codes = [normalize_code(c) for c in codes]

    with session_scope() as session:
        from sqlalchemy import func, text
        results = session.query(
            DailyKline.code,
            func.max(DailyKline.trade_date).label('last_date')
        ).filter(
            DailyKline.code.in_(norm_codes),
            DailyKline.adjustflag == int(ADJUST_FLAG),
        ).group_by(DailyKline.code).all()

        return {r.code: r.last_date.strftime('%Y-%m-%d') for r in results if r.last_date}


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


def save_daily_klines_batch(rows: List[dict], batch_size: int = 3000) -> int:
    """批量插入多只股票的K线数据

    Args:
        rows: 字典列表，每 dict 包含 code, trade_date, open, high, low, close, volume, amount, pct_chg, turnover, adjustflag
        batch_size: 每批插入的行数，避免 SQL 过长

    Returns:
        实际插入的总行数
    """
    if not rows:
        return 0

    from sqlalchemy import text

    total = 0
    for start_i in range(0, len(rows), batch_size):
        batch = rows[start_i:start_i + batch_size]
        with session_scope() as session:
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
            session.execute(sql, batch)
        total += len(batch)
        logger.info(f"[批量存储] 已写入 {total}/{len(rows)} 条")

    return total


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
    """批量更新全市场日线数据

    优化点：
    1. 一次性查询所有股票的最后日期（1 条 SQL）
    2. 仅对需要更新的股票调用 API
    3. 收集所有新 K 线到内存，最后批量插入数据库

    支持 CTRL+C 中断，已获取的数据仍会被写入
    """
    if not bs_login():
        return
    try:
        if not stock_pool:
            stock_pool = settings.get_stock_pool()
        total_codes = len(stock_pool)
        logger.info(f"开始更新 {total_codes} 只股票的日线数据")

        end_date = datetime.now().strftime('%Y-%m-%d')
        default_start = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

        # ── 1. 一次性查询所有股票的最后K线日期 ─────
        last_dates = get_last_kline_dates_batch(stock_pool)
        logger.info(f"[步骤1] 已查询 {len(last_dates)} 只股票的本地最后日期")

        # ── 2. 筛选需要更新的股票 ──────────────
        to_update = []  # [(norm_code, start_date, end_date), ...]
        already_latest = 0
        for code in stock_pool:
            norm_code = normalize_code(code)
            last_date = last_dates.get(norm_code)
            if last_date:
                start_date = (
                    datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
                ).strftime('%Y-%m-%d')
                if start_date > end_date:
                    already_latest += 1
                    continue
                to_update.append((norm_code, start_date, end_date))
            else:
                # 数据库中没有这只股票的数据，获取最近一年
                to_update.append((norm_code, default_start, end_date))

        logger.info(
            f"[步骤2] 需要更新: {len(to_update)} 只, 已是最新: {already_latest} 只"
        )

        # ── 3. 批量获取K线数据并收集到内存 ──────
        all_rows = []  # 所有股票的新 K 线
        total_klines = 0
        failed = 0
        login_count = 1  # 已登录次数（用于周期性重登录）
        RELogIN_INTERVAL = 100  # 每处理 N 只股票自动重登录一次（session约100次请求后失效）
        start_time = datetime.now()

        for i, (norm_code, start_date, code_end) in enumerate(to_update, 1):
            retry_success = False
            # 每只股票最多重试 2 次（每次失败都先重登录）
            for attempt in range(2):
                try:
                    df = fetch_daily_kline(norm_code, start_date, code_end)
                    if not df.empty:
                        adjust_flag = int(ADJUST_FLAG)
                        for _, row in df.iterrows():
                            all_rows.append({
                                'code': norm_code,
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
                        total_klines += len(df)
                    _speed_up()  # 成功则逐步降低延迟
                    retry_success = True
                    break

                except Exception as e:
                    err_msg = str(e)
                    # 识别 session 相关错误，触发重登录（session 失效≠被限流，不减速）
                    if any(k in err_msg for k in [
                        "10054", "连接被重置", "远程主机强迫关闭",
                        "网络接收错误", "用户未登录", "you don't login", "login",
                    ]):
                        logger.warning(f"{norm_code} session失效({err_msg})，重新登录 BaoStock...")
                        try:
                            bs_logout()
                        except Exception:
                            pass
                        time.sleep(1)  # 缩短等待，尽快恢复
                        if bs_login():
                            login_count += 1
                            _reset_interval()  # 重登录成功后重置请求间隔
                            continue  # 重登录成功，重试当前股票
                        time.sleep(3)
                    else:
                        # 非 session 错误（如数据格式问题），才考虑减速
                        _slow_down()
                    # 非 session 问题，或重登录失败，记录后放弃
                    failed += 1
                    if failed <= 5:
                        logger.error(f"{norm_code} 失败(尝试{attempt+1}次): {e}")
                    break

            if not retry_success and failed <= 5:
                logger.error(f"{norm_code} 最终失败，跳过")

            # 进度日志
            if i % 200 == 0:
                elapsed = (datetime.now() - start_time).total_seconds()
                speed = i / elapsed if elapsed > 0 else 0
                remaining = (len(to_update) - i) / speed if speed > 0 else 0
                logger.info(
                    f"[进度] {i:>5}/{len(to_update)} "
                    f"已获取K线:{total_klines:>6} "
                    f"速度:{speed:.1f}只/秒 "
                    f"预计剩余:{remaining/60:.0f}分钟"
                )

            # 每 N 只股票自动重登录一次，防止 session 过期
            if i % RELogIN_INTERVAL == 0:
                logger.info(f"[连接维护] 已处理 {i} 只，自动重登录 BaoStock...")
                try:
                    bs_logout()
                except Exception:
                    pass
                time.sleep(1)  # 缩短等待
                if bs_login():
                    login_count += 1
                    _reset_interval()  # 重置请求间隔
                else:
                    logger.warning("重登录失败，继续尝试...")
                    time.sleep(3)
                    bs_login()
                    login_count += 1
                    _reset_interval()

        logger.info(f"[步骤3] 数据获取完成，共 {total_klines} 条新K线，失败 {failed} 只, 重登录 {login_count-1} 次")

        # ── 4. 批量写入数据库 ──────────────────
        if all_rows:
            logger.info(f"[步骤4] 开始批量写入 {len(all_rows)} 条K线数据...")
            saved = save_daily_klines_batch(all_rows, batch_size=3000)
            logger.info(f"[步骤4] 批量写入完成: {saved} 条K线")
        else:
            logger.info("[步骤4] 没有新数据需要写入")

        logger.info(f"日线更新完成，共新增/更新 {total_klines} 条K线")
        return total_klines

    except KeyboardInterrupt:
        logger.info("收到中断信号，写入已获取的数据后停止...")
        if 'all_rows' in dir() and all_rows:
            logger.info(f"写入已获取的 {len(all_rows)} 条K线...")
            save_daily_klines_batch(all_rows, batch_size=3000)
        return total_klines if 'total_klines' in dir() else 0
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
