"""个股公告查询 —— 深市走深交所官方接口，沪市走巨潮资讯。

**用途是归因，不是预测。** 回答「这天到底有没有公告」，用于个股异动复盘：
一只票单日逆势放量跌 4%，是公告驱动还是纯资金行为，查一次就知道。

为什么不用东财：`np-anotice-stock.eastmoney.com` 按 IP 限流，全量补历史会被封
（见 news_collect.py 的教训）。但**单票查询只有 1~2 个请求**，官方源完全够用，
且深交所的 `publishTime` 精确到秒。

深市：POST szse.cn/api/disc/announcement/annList，参数 `stock=["300454"]`
      （注意不是 `secCode`，传错会静默返回全市场数据）
沪市：巨潮 hisAnnouncement/query，需要先查 `orgId`（本地缓存，一次查永久用）

用法：
    python tools/announce_lookup.py --code 300454 --date 2026-11-04
    python tools/announce_lookup.py --code 300454 --days 10
    python tools/announce_lookup.py --code 300454 688030 --start 2026-10-01
    python tools/announce_lookup.py --code 300454 --days 30 --save
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

SZ_API = 'https://www.szse.cn/api/disc/announcement/annList?random={r}'
SZ_REF = 'https://www.szse.cn/disclosure/listed/notice/index.html'
CN_QUERY = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
CN_REF = 'http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice'
CN_ORG = 'http://www.cninfo.com.cn/new/information/topSearch/query'
CN_ORG_REF = 'http://www.cninfo.com.cn/new/index'
CN_CACHE = Path('.data_cache/cninfo_orgid.json')

SZ_PREFIX = ('000', '001', '002', '003', '300', '301')
SH_PREFIX = ('600', '601', '603', '605', '688', '689')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'


# ── HTTP ────────────────────────────────────────────────────────────────────

def _post_json(url: str, body: dict, ref: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method='POST',
        headers={'User-Agent': UA, 'Referer': ref,
                 'Content-Type': 'application/json',
                 'X-Request-Type': 'ajax',
                 'X-Requested-With': 'XMLHttpRequest'})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')
    return json.loads(raw)


def _post_form(url: str, data: dict, ref: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method='POST',
        headers={'User-Agent': UA, 'Referer': ref,
                 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')
    return json.loads(raw)


# ── 深市：深交所官方 ────────────────────────────────────────────────────────

def fetch_sz(code: str, start: str, end: str, max_pages: int = 6) -> List[dict]:
    """深交所公告。`stock` 参数是关键，写错会静默返回全市场。"""
    out: List[dict] = []
    for page in range(1, max_pages + 1):
        body = {'seDate': [start, end], 'channelCode': ['listedNotice_disc'],
                'stock': [code], 'pageSize': 50, 'pageNum': page}
        j = _post_json(SZ_API.format(r=round(random.random(), 6)), body, SZ_REF)
        rows = j.get('data') or []
        total = j.get('announceCount') or 0
        if page == 1 and total == 0:
            return []
        for a in rows:
            title = (a.get('title') or '').strip()
            # 标题形如「深信服：关于xxx的公告」，前缀就是简称
            short = title.split('：')[0] if '：' in title else ''
            out.append(dict(
                art_code='sz:' + str(a.get('id') or a.get('annId')),
                notice_date=(a.get('publishTime') or '')[:10],
                publish_time=a.get('publishTime') or '',
                stock_code=code,
                short_name=short[:20],
                title=title[:512],
                column_name=(a.get('bigCategoryName') or '')[:64],
                ann_type='szse',
                source='szse',
            ))
        if len(rows) < 50 or page * 50 >= total:
            break
        time.sleep(0.4)
    return out


# ── 沪市：巨潮资讯 ──────────────────────────────────────────────────────────

def _load_cache() -> Dict[str, str]:
    if CN_CACHE.exists():
        try:
            return json.loads(CN_CACHE.read_text(encoding='utf-8'))
        except Exception:                       # noqa: BLE001
            return {}
    return {}


def _save_cache(c: Dict[str, str]) -> None:
    CN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CN_CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding='utf-8')


def cninfo_orgid(code: str) -> Optional[str]:
    """巨潮内部 orgId。查一次缓存到本地，之后不再请求。"""
    cache = _load_cache()
    if code in cache:
        return cache[code]
    try:
        j = _post_form(CN_ORG, {'keyWord': code, 'maxNum': 10}, CN_ORG_REF)
    except Exception as e:                      # noqa: BLE001
        logger.warning(f'{code} orgId 查询失败: {e}')
        return None
    for x in (j if isinstance(j, list) else []):
        if str(x.get('code')) == code:
            orgid = x.get('orgId')
            if orgid:
                cache[code] = orgid
                _save_cache(cache)
            return orgid
    return None


def fetch_sh(code: str, start: str, end: str, max_pages: int = 6) -> List[dict]:
    """沪市公告（巨潮）。pageSize 硬限 30，但单票区间通常远小于 30 条。"""
    orgid = cninfo_orgid(code)
    if not orgid:
        logger.warning(f'{code} 拿不到 orgId，跳过')
        return []
    out: List[dict] = []
    for page in range(1, max_pages + 1):
        data = {'pageNum': page, 'pageSize': 30, 'column': 'sse', 'tabName': 'fulltext',
                'stock': f'{code},{orgid}', 'seDate': f'{start}~{end}',
                'isHLtitle': 'true', 'sortName': '', 'sortType': '', 'plate': ''}
        j = _post_form(CN_QUERY, data, CN_REF)
        rows = j.get('announcements') or []
        total = j.get('totalAnnouncement') or 0
        for a in rows:
            ts = a.get('announcementTime')
            try:
                dt = datetime.fromtimestamp(int(ts) / 1000)
            except Exception:                   # noqa: BLE001
                dt = None
            out.append(dict(
                art_code='cn:' + str(a.get('announcementId')),
                notice_date=dt.strftime('%Y-%m-%d') if dt else '',
                publish_time=dt.strftime('%Y-%m-%d %H:%M:%S') if dt else '',
                stock_code=code,
                short_name=(a.get('secName') or '')[:20],
                title=(a.get('announcementTitle') or '').strip()[:512],
                column_name=(a.get('announcementTypeName') or '')[:64],
                ann_type='sse',
                source='cninfo',
            ))
        if not rows or page * 30 >= total:
            break
        time.sleep(0.4)
    return out


# ── 入口 ────────────────────────────────────────────────────────────────────

def normalize_code(code: str) -> Optional[str]:
    """接受 300454 / sz300454 / sz.300454 / SZ.300454，返回 6 位。"""
    c = code.strip().upper().replace('SZ.', '').replace('SH.', '')
    c = c.replace('SZ', '').replace('SH', '')
    c = c.strip('. ')
    return c if len(c) == 6 and c.isdigit() else None


def market_of(code: str) -> Optional[str]:
    if code.startswith(SZ_PREFIX):
        return 'sz'
    if code.startswith(SH_PREFIX):
        return 'sh'
    return None


def lookup(codes: List[str], start: str, end: str) -> pd.DataFrame:
    rows: List[dict] = []
    for raw in codes:
        code = normalize_code(raw)
        if not code:
            logger.warning(f'{raw} 不是 6 位代码，跳过')
            continue
        mk = market_of(code)
        try:
            if mk == 'sz':
                got = fetch_sz(code, start, end)
            elif mk == 'sh':
                got = fetch_sh(code, start, end)
            else:
                logger.warning(f'{code} 不在沪深主板/创业板/科创板范围（北交所暂不支持）')
                continue
        except Exception as e:                  # noqa: BLE001
            logger.error(f'{code} 抓取失败: {e}')
            continue
        logger.info(f'{code} {mk} → {len(got)} 条公告')
        rows.extend(got)
    if not rows:
        return pd.DataFrame(columns=['notice_date', 'publish_time', 'stock_code',
                                     'short_name', 'title', 'column_name', 'source'])
    df = pd.DataFrame(rows)
    df['notice_date'] = pd.to_datetime(df['notice_date'], errors='coerce')
    return df.sort_values(['stock_code', 'publish_time']).reset_index(drop=True)


def save(df: pd.DataFrame) -> int:
    """写入 announcements 表（art_code 唯一，重复自动跳过）。"""
    if df.empty:
        return 0
    cols = ['art_code', 'notice_date', 'stock_code', 'short_name', 'title',
            'column_name', 'ann_type']
    d = df[cols].copy()
    d['notice_date'] = d['notice_date'].dt.strftime('%Y-%m-%d')
    sql = ('INSERT IGNORE INTO announcements '
           '(art_code,notice_date,stock_code,short_name,title,column_name,ann_type) '
           'VALUES (%s,%s,%s,%s,%s,%s,%s)')
    from db.database import get_connection
    cn = get_connection()
    try:
        cur = cn.cursor()
        cur.executemany(sql, [tuple(None if pd.isna(v) else v for v in r)
                              for r in d.itertuples(index=False, name=None)])
        cn.commit()
        n = cur.rowcount
    finally:
        cn.close()
    return n


def render(df: pd.DataFrame) -> None:
    if df.empty:
        print('\n  该区间没有公告。\n')
        return
    for code, g in df.groupby('stock_code'):
        name = g.short_name.iloc[0] or ''
        print(f'\n{"=" * 76}')
        print(f'{code} {name}　{len(g)} 条公告　'
              f'{g.notice_date.min().date()} ~ {g.notice_date.max().date()}')
        print('=' * 76)
        for r in g.itertuples(index=False):
            cat = f'[{r.column_name}] ' if r.column_name else ''
            print(f'  {r.notice_date.date()}  {cat}{r.title}')


def main() -> int:
    ap = argparse.ArgumentParser(description='个股公告查询（深交所官方 + 巨潮），用于异动归因')
    ap.add_argument('--code', nargs='+', required=True, help='6 位代码，可多个')
    ap.add_argument('--date', default=None, help='单日 YYYY-MM-DD')
    ap.add_argument('--start', default=None, help='起始日 YYYY-MM-DD')
    ap.add_argument('--end', default=None, help='结束日，默认今天')
    ap.add_argument('--days', type=int, default=None, help='最近 N 自然日（含今天）')
    ap.add_argument('--save', action='store_true', help='写入 announcements 表')
    a = ap.parse_args()

    if a.date:
        start = end = a.date
    else:
        end = a.end or date.today().isoformat()
        if a.start:
            start = a.start
        elif a.days:
            start = (datetime.strptime(end, '%Y-%m-%d').date()
                     - timedelta(days=a.days - 1)).isoformat()
        else:
            start = (datetime.strptime(end, '%Y-%m-%d').date()
                     - timedelta(days=30)).isoformat()

    print(f'\n区间 {start} ~ {end}')
    df = lookup(a.code, start, end)
    render(df)
    if a.save and not df.empty:
        n = save(df)
        logger.info(f'入库 {n} 条（重复自动跳过）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
