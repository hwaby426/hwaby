from datetime import datetime, timedelta
from typing import List, Optional
from loguru import logger
import baostock as bs
import pandas as pd

from db.database import get_session
from db.models import StockInfo


def get_all_a_stocks(target_date: Optional[str] = None) -> pd.DataFrame:
    if target_date is None:
        target_date = datetime.now().strftime('%Y-%m-%d')

    lg = bs.login()
    if lg.error_code != '0':
        logger.error(f"BaoStock登录失败: {lg.error_msg}")
        return pd.DataFrame()

    try:
        rs = bs.query_all_stock(day=target_date)
        if rs.error_code != '0':
            logger.error(f"获取股票列表失败: {rs.error_msg}")
            return pd.DataFrame()

        data_list = []
        while (rs.error_code == '0') & rs.next():
            data_list.append(rs.get_row_data())

        df = pd.DataFrame(data_list, columns=rs.fields)
        logger.info(f"BaoStock返回 {len(df)} 条记录 (日期: {target_date})")
        return df
    finally:
        bs.logout()


def filter_a_stocks(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    if 'tradeStatus' in df.columns:
        df = df[df['tradeStatus'] == '1'].copy()

    df['code'] = df['code'].apply(lambda x: x.replace('.', '').lower() if '.' in x else x.lower())

    def get_market(code):
        if code.startswith('sh'):
            return 'sh'
        elif code.startswith('sz'):
            return 'sz'
        return ''

    df['market'] = df['code'].apply(get_market)

    df = df[df['market'].isin(['sh', 'sz'])].copy()

    sh_main = (df['market'] == 'sh') & df['code'].str.match(r'^sh6\d{5}$')
    sz_main = (df['market'] == 'sz') & df['code'].str.match(r'^sz0\d{5}$')
    sz_sme = (df['market'] == 'sz') & df['code'].str.match(r'^sz00[2-9]\d{3}$')
    sz_gem = (df['market'] == 'sz') & df['code'].str.match(r'^sz3\d{5}$')
    sh_star = (df['market'] == 'sh') & df['code'].str.match(r'^sh68\d{4}$')

    df = df[sh_main | sz_main | sz_sme | sz_gem | sh_star].copy()

    def is_st_name(name):
        if not name:
            return False
        name = str(name).upper()
        return 'ST' in name or '退' in name

    df['is_st'] = df['code_name'].apply(is_st_name).astype(int)

    df['symbol'] = df['code'].apply(lambda x: x[2:])

    df = df.rename(columns={'code_name': 'name'})

    return df[['code', 'symbol', 'name', 'market', 'is_st']].reset_index(drop=True)


def update_stock_info(target_date: Optional[str] = None) -> int:
    if target_date is None:
        for i in range(7):
            d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            df = get_all_a_stocks(d)
            if not df.empty:
                break
    else:
        df = get_all_a_stocks(target_date)

    if df.empty:
        logger.error("未获取到股票列表数据")
        return 0

    df_filtered = filter_a_stocks(df)
    logger.info(f"过滤后剩余 {len(df_filtered)} 只A股")

    df_filtered = df_filtered[df_filtered['is_st'] == 0].copy()
    logger.info(f"排除ST后剩余 {len(df_filtered)} 只股票")

    session = get_session()
    count = 0

    try:
        for _, row in df_filtered.iterrows():
            existing = session.query(StockInfo).filter(StockInfo.code == row['code']).first()

            if existing:
                existing.name = row['name']
                existing.symbol = row['symbol']
                existing.market = row['market']
                existing.is_st = row['is_st']
                existing.status = 1
            else:
                stock = StockInfo(
                    code=row['code'],
                    symbol=row['symbol'],
                    name=row['name'],
                    market=row['market'],
                    is_st=row['is_st'],
                    status=1,
                )
                session.add(stock)

            count += 1
            if count % 500 == 0:
                session.commit()
                logger.info(f"已处理 {count} 只股票")

        session.commit()
        logger.info(f"股票列表更新完成，共 {count} 只")
    except Exception as e:
        session.rollback()
        logger.error(f"更新股票列表失败: {e}")
        count = 0
    finally:
        session.close()

    return count


def get_stock_pool_from_db(
    market: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[str]:
    session = get_session()
    try:
        query = session.query(StockInfo.code).filter(StockInfo.status == 1).filter(StockInfo.is_st == 0)

        if market:
            query = query.filter(StockInfo.market == market)

        query = query.order_by(StockInfo.code)

        if limit:
            query = query.limit(limit)

        codes = [row[0] for row in query.all()]
        return codes
    finally:
        session.close()


def get_stock_name_map() -> dict:
    """获取股票代码到名称的映射字典"""
    session = get_session()
    try:
        rows = session.query(StockInfo.code, StockInfo.name).filter(StockInfo.status == 1).all()
        return {row[0]: row[1] for row in rows}
    finally:
        session.close()
