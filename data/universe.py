"""标的池口径：区分「个股」与「指数」。

## 为什么必须有这个模块

本地库的 `daily_kline` / `stock_info` 里混着 **286 个指数**（深证成指、创业板指、
深证 1000 等），而且 `status = 1`，看起来和个股完全一样：

```
stock_info:  ('sz399001', '399001', '深证成份指数(价格)', 'sz')
daily_kline: code='sz.399001'  ← 同一只，code 带点
```

它们会被当成股票：进入「全市场广度」的分母、被全市场回测遍历、
被选股逻辑"买入深证成指"。指数没有涨跌停，还会让 `tradability` 的
一字板判断失真。

## 实测影响

286 个指数占 4.9% 的行。对「全市场等权涨跌」影响很小（日均差 +0.0015pct），
但**方向是系统性低估风险**，因为指数波动天然小于个股：

```
指标      仅个股    含指数    差
dn5 均值   4.07%    3.88%    +0.19pct
2026-10-30  27.3%    26.1%    +1.2pct
sigma       3.37     3.29     +0.08
```

量级不大，但「全市场 27% 的股票跌超 5%」和「26%」是两句话，
而且把一个非股票对象放进股票池本身就是口径错误。

## 判定规则

不靠 name 里的「指数」字样（会被「指数基金」类命名误伤），靠代码段：

| 前缀 | 含义 |
|---|---|
| `sz.399*` | 深证指数系列 |
| `sz.395*` | 深证指数（旧段） |
| `sh.000*` | 上证指数系列（上交所个股都是 60/68 开头） |
| `sh.950*` | 中证/其他指数 |

**注意必须带市场前缀判断**：`sz.000001` 是平安银行（个股），
`sh.000001` 是上证指数。只看数字段会把平安银行误杀。
"""
from __future__ import annotations

from typing import Iterable, List

# (市场前缀, 代码段前缀)
INDEX_PREFIXES = (
    ('sz.', '399'),
    ('sz.', '395'),
    ('sh.', '000'),
    ('sh.', '950'),
)


def is_index_code(code: str) -> bool:
    """判断是否为指数代码。接受 `sz.399001` / `sz399001` / `399001` 三种写法。

    无市场前缀的纯数字段有歧义（000001 可能是平安银行），此时按「上交所
    才是指数」的规则无法判断，保守地返回 False —— 宁可漏判也不误杀个股。
    """
    if not code:
        return False
    c = str(code).strip().lower()
    if '.' in c:
        mkt, _, num = c.partition('.')
        # 常量里写的是 'sz.'（带点），而 partition 切出来的市场段是 'sz'（不带点）。
        # 直接比较永远不相等，函数会恒返回 False —— 这个 bug 被「抽样只取到个股」
        # 的检查掩盖过，所以这里的归一化不能省。
        return any(mkt == m.rstrip('.') and num.startswith(p)
                   for m, p in INDEX_PREFIXES)
    # 无点：sz399001 这类
    for mkt, num_prefix in INDEX_PREFIXES:
        short = mkt.rstrip('.')
        if c.startswith(short + num_prefix):
            return True
    return False


def filter_stocks(codes: Iterable[str]) -> List[str]:
    """从代码列表里剔除指数。"""
    return [c for c in codes if not is_index_code(c)]


def stock_pool(market: str = None, limit: int = None) -> List[str]:
    """股票池的**推荐入口**：`get_stock_pool_from_db` 的剔除指数版。

    为什么需要包装：`data_fetcher.stock_pool.get_stock_pool_from_db()` 只过滤
    `status=1` 和 `is_st=0`，而库里 286 个指数的这两列与个股完全相同 ——
    全市场回测会把「深证成指」当股票买入。

    `data_fetcher/` 属于数据源层，本项目约定不修改它，所以在这一层包。
    **新的全市场命令请直接用这个函数**，不要再直连 get_stock_pool_from_db。
    """
    from data_fetcher.stock_pool import get_stock_pool_from_db
    return filter_stocks(get_stock_pool_from_db(market=market, limit=limit))


def stock_where(alias: str = '') -> str:
    """生成「只要个股」的 SQL 片段，供手写 SQL 拼接。

    >>> f"select ... from daily_kline where adjustflag=2 and {stock_where()}"
    """
    prefix = f'{alias}.' if alias else ''
    conds = ' and '.join(
        f"{prefix}code not like '{m}{p}%'" for m, p in INDEX_PREFIXES)
    return f'({conds})'


def index_where(alias: str = '') -> str:
    """生成「只要指数」的 SQL 片段。"""
    prefix = f'{alias}.' if alias else ''
    conds = ' or '.join(
        f"{prefix}code like '{m}{p}%'" for m, p in INDEX_PREFIXES)
    return f'({conds})'
