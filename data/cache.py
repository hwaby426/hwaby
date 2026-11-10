"""本地行情缓存层。

为什么需要它
-------------
全市场回测 / walk-forward / 参数敏感性网格，本质上都是「同一批历史数据被
反复读取几千次」。原实现每次都要走：

    MySQL → SQLAlchemy ORM 逐行构造 dict → DataFrame

5264 只票共 175 万行，全市场加载要几十秒。这个成本直接决定了「你跑不跑得起
参数网格」，也就决定了策略验证质量的上限。

本模块在不改动 data_fetcher 的前提下加一层 Parquet 缓存：
    - 首次调用时把整张 daily_kline（指定 adjustflag）快照到本地 Parquet
    - 之后所有 get / get_batch 都走本地内存/列式读取
    - 采用「按需回源」的失效策略：数据有更新时不会立刻重建整个缓存，
      只有当请求超出缓存覆盖范围时才回退到 MySQL（见 `_covers`）
    - 支持 build(force=True) 强制全量重建

注意：这层只做读取加速，不产生任何新数据，数据源仍然是 MySQL。
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime
import json

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import text

from config.settings import BASE_DIR
from db.database import engine


CACHE_DIR = BASE_DIR / '.data_cache'
CACHE_FILE = 'daily_kline'
MANIFEST = 'manifest'

_COLUMNS = ['date', 'code', 'open', 'high', 'low', 'close',
            'volume', 'amount', 'pct_chg', 'turnover']

_DTYPE_MAP = {
    'open': 'float64', 'high': 'float64', 'low': 'float64', 'close': 'float64',
    'volume': 'int64', 'amount': 'float64', 'pct_chg': 'float64', 'turnover': 'float64',
}


def _norm(code: str) -> str:
    """统一代码格式为 sh.600000 / sz.000001，兼容 sh600000 / 600000 等写法。"""
    c = str(code or '').strip().lower().replace(' ', '')
    if '.' in c:
        return c
    if c.startswith(('sh', 'sz', 'bj')):
        return f"{c[:2]}.{c[2:]}"
    if c.isdigit() and len(c) == 6:
        prefix = 'sh' if c[0] in ('6', '9') else ('sz' if c[0] in ('0', '2', '3') else 'bj')
        return f'{prefix}.{c}'
    return c


class DailyCache:
    """日线数据的本地 Parquet 缓存。"""

    def __init__(self, adjustflag: int = 2, cache_dir: Optional[Path] = None):
        self.adjustflag = adjustflag
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.parquet_path = self.cache_dir / f'{CACHE_FILE}_{adjustflag}.parquet'
        self.pickle_path = self.cache_dir / f'{CACHE_FILE}_{adjustflag}.pkl'
        self.manifest_path = self.cache_dir / f'{MANIFEST}_{adjustflag}.json'
        self._memory: Optional[pd.DataFrame] = None
        self._loaded = False
        # 按代码切分的内存索引：一次构建，之后 O(1) 取用
        self._index: Optional[Dict[str, pd.DataFrame]] = None
        self._cov: Optional[dict] = None

    # ------------------------------------------------------------------
    # 指纹与失效检测
    # ------------------------------------------------------------------
    def _db_fingerprint(self) -> dict:
        """一次轻量查询拿到 (行数, 最新交易日, 股票数)。"""
        sql = text(
            "SELECT COUNT(*), MAX(trade_date), COUNT(DISTINCT code) "
            "FROM daily_kline WHERE adjustflag = :af"
        )
        with engine.connect() as conn:
            row = conn.execute(sql, {'af': self.adjustflag}).fetchone()
        cnt, max_date, n_codes = row
        return {
            'rows': int(cnt or 0),
            'max_date': str(max_date) if max_date else '',
            'codes': int(n_codes or 0),
        }

    def _load_manifest(self) -> Optional[dict]:
        if not self.manifest_path.exists():
            return None
        try:
            return json.loads(self.manifest_path.read_text(encoding='utf-8'))
        except Exception:
            return None

    def _write_manifest(self, fp: dict):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(fp)
        payload['built_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    def cached_coverage(self) -> dict:
        """缓存自身的覆盖范围，内部 memoize，避免重复计算。"""
        if self._cov is not None:
            return self._cov
        manifest = self._load_manifest() or {}
        self._cov = {
            'rows': int(manifest.get('rows', 0)),
            'max_date': str(manifest.get('max_date', '')),
            'codes': set(self._index_by_code().keys()),
        }
        return self._cov

    def _has_file(self) -> bool:
        return self.parquet_path.exists() or self.pickle_path.exists()

    def _reset_index(self):
        self._index = None
        self._cov = None

    def _index_by_code(self) -> Dict[str, pd.DataFrame]:
        """按股票代码切分的内存索引。构建一次，之后所有查询 O(1)。"""
        if self._index is not None:
            return self._index
        df = self._load_local()
        if df.empty:
            self._index = {}
            return self._index
        idx: Dict[str, pd.DataFrame] = {}
        codes = df['code'].astype(str).to_numpy()
        dates = df['date'].astype(str).to_numpy()
        # 数据已按 (code, date) 排序 → 用边界切分，避免 groupby 全表扫描
        bounds = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
        bounds = np.r_[bounds, len(codes)]
        arr_start = 0
        for bidx in range(len(bounds) - 1):
            s, e = bounds[bidx], bounds[bidx + 1]
            idx[str(codes[s])] = df.iloc[s:e]
        self._index = idx
        return idx

    def is_stale(self) -> Tuple[bool, str]:
        """严格判断缓存是否与数据库完全一致。"""
        if not self._has_file():
            return True, '本地无缓存'
        manifest = self._load_manifest()
        if manifest is None:
            return True, '缺少 manifest'
        current = self._db_fingerprint()
        for key in ('rows', 'max_date', 'codes'):
            if str(manifest.get(key)) != str(current.get(key)):
                return True, f'数据已变更: {key} {manifest.get(key)} → {current.get(key)}'
        return False, ''

    def _covers(self, codes: Set[str], end_date: Optional[str]) -> bool:
        """判断缓存能否满足本次请求——比 is_stale 宽松，避免数据一更新就整体重建。"""
        cov = self.cached_coverage()
        if cov['rows'] == 0 or not cov['codes']:
            return False
        if codes and not codes.issubset(cov['codes']):
            return False
        if end_date and cov['max_date'] and end_date > cov['max_date']:
            return False
        return True

    # ------------------------------------------------------------------
    # 构建（用 read_sql 批量读，比 ORM 逐行快一个数量级）
    # ------------------------------------------------------------------
    def build(self, force: bool = False) -> int:
        if not force:
            stale, why = self.is_stale()
            if not stale:
                return int((self._load_manifest() or {}).get('rows', 0))

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info('正在构建日线本地缓存...')
        started = datetime.now()

        sql = text(
            "SELECT trade_date AS date, code, `open`, high, low, close, "
            "volume, amount, pct_chg, turnover "
            "FROM daily_kline WHERE adjustflag = :af"
        )
        chunks = []
        with engine.connect() as conn:
            conn = conn.execution_options(stream_results=True)
            for part in pd.read_sql(sql, conn, params={'af': self.adjustflag},
                                     chunksize=200_000):
                chunks.append(part)
        if not chunks:
            logger.warning('未读到任何日线数据')
            return 0

        df = pd.concat(chunks, ignore_index=True)
        df['date'] = df['date'].astype(str)
        df['code'] = df['code'].astype(str)
        for col, dt in _DTYPE_MAP.items():
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype(dt)
        df = df.sort_values(['code', 'date']).reset_index(drop=True)

        try:
            df.to_parquet(self.parquet_path, index=False, engine='pyarrow')
        except Exception as e:
            logger.warning(f'Parquet 写入失败({e})，回退 pickle')
            df.to_pickle(self.pickle_path)

        fp = self._db_fingerprint()
        self._write_manifest(fp)
        self._memory = None
        self._loaded = False
        size_mb = (self.parquet_path.stat().st_size / 1024 / 1024
                   if self.parquet_path.exists() else 0)
        logger.info(
            f'缓存构建完成: {len(df)} 行 / {fp["codes"]} 只票 / {size_mb:.1f} MB / '
            f'{(datetime.now() - started).total_seconds():.1f}s'
        )
        return len(df)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _load_local(self) -> pd.DataFrame:
        if self._loaded and self._memory is not None:
            return self._memory
        if not self._has_file():
            self.build()
        try:
            if self.parquet_path.exists():
                df = pd.read_parquet(self.parquet_path, engine='pyarrow')
            else:
                df = pd.read_pickle(self.pickle_path)
        except Exception as e:
            logger.warning(f'缓存读取失败({e})，重建中...')
            self.build()
            df = (pd.read_parquet(self.parquet_path, engine='pyarrow')
                  if self.parquet_path.exists()
                  else pd.read_pickle(self.pickle_path))
        self._memory = df
        self._loaded = True
        self._reset_index()
        return df

    def _from_db(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """回源到 MySQL（缓存未覆盖时）。"""
        from data_fetcher.baostock_fetcher import get_daily_kline_df
        return get_daily_kline_df(code, start_date=start_date, end_date=end_date,
                                  adjustflag=self.adjustflag)

    def get(self, code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        code = _norm(code)
        if not self._covers({code}, end_date):
            logger.debug(f'{code} 超出缓存范围，回源 MySQL')
            return self._from_db(code, start_date, end_date)
        grp = self._index_by_code().get(code)
        if grp is None or grp.empty:
            return self._from_db(code, start_date, end_date)
        sel = grp
        if start_date:
            sel = sel[sel['date'] >= start_date]
        if end_date:
            sel = sel[sel['date'] <= end_date]
        if sel.empty:
            return self._from_db(code, start_date, end_date)
        return sel.drop(columns=['code'], errors='ignore').reset_index(drop=True)

    def get_batch(
        self,
        code_list: List[str],
        start_date: str = None,
        end_date: str = None,
    ) -> Dict[str, pd.DataFrame]:
        result: Dict[str, pd.DataFrame] = {}
        if not code_list:
            return result

        wanted = {_norm(c) for c in code_list}
        mapping = {_norm(c): c for c in code_list}

        covered = wanted
        if not self._covers(wanted, end_date):
            cov = self.cached_coverage()
            missing = set()
            if end_date and cov['max_date'] and end_date > cov['max_date']:
                missing |= wanted            # 请求超出缓存日期范围 → 全部回源
            elif codes_not_covered := (wanted - cov['codes']):
                missing |= codes_not_covered
            if missing:
                logger.debug(f'{len(missing)} 只票未命中缓存，回源 MySQL')
                from data_fetcher.baostock_fetcher import get_daily_kline_batch
                old = get_daily_kline_batch(
                    [mapping.get(c, c) for c in missing],
                    start_date=start_date, end_date=end_date,
                    adjustflag=self.adjustflag,
                )
                result.update({_norm(k): v for k, v in old.items()})
            covered = wanted - missing

        if covered:
            index = self._index_by_code()
            for code in covered:
                grp = index.get(code)
                if grp is None or grp.empty:
                    continue
                sel = grp
                if start_date:
                    sel = sel[sel['date'] >= start_date]
                if end_date:
                    sel = sel[sel['date'] <= end_date]
                if sel.empty:
                    continue
                out = sel.drop(columns=['code'], errors='ignore').reset_index(drop=True)
                # 数据源可能存在重复交易日，去重保证下游 concat/reindex 不炸
                if out['date'].duplicated().any():
                    out = out.drop_duplicates('date', keep='last').reset_index(drop=True)
                result[code] = out
        return result

    def all_codes(self) -> List[str]:
        return sorted(self._index_by_code().keys()) if self._has_file() or self._loaded else []

    def clear(self):
        for p in (self.parquet_path, self.pickle_path, self.manifest_path):
            if Path(p).exists():
                Path(p).unlink()
        self._memory = None
        self._loaded = False
        logger.info('缓存已清除')


_DEFAULT_CACHE: Optional[DailyCache] = None


def get_cache(adjustflag: int = 2) -> DailyCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None or _DEFAULT_CACHE.adjustflag != adjustflag:
        _DEFAULT_CACHE = DailyCache(adjustflag=adjustflag)
    return _DEFAULT_CACHE
