"""ML 特征与前瞻标签。

模块边界
--------
- `labels`  1/3/5 日前瞻标签、涨跌停判定、可成交性（最容易做错的一层）
- `builder` 特征构造、横截面补齐、parquet / 数据库落盘

用法
----
    from features.builder import build_all, save_parquet, save_to_db
    feat = build_all(start='2025-07-01')          # 全市场特征 + 标签
    save_parquet(feat)                             # -> .data_cache/features.parquet
    save_to_db(feat)                               # -> stock_features 表
"""

from features.builder import (  # noqa: F401
    FEATURE_COLUMNS, X_COLUMNS,
    build_all, build_one, load_daily, load_features, load_meta,
    save_parquet, save_to_db, create_table,
)
from features.labels import build_labels, limit_pct  # noqa: F401
