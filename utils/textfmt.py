"""终端文本排版的共用工具。

目前只有一个需求：**中文表格对齐**。

pandas 默认按 Python 字符数计算列宽，而一个汉字在终端里占 **2 列**。
于是「恐慌」（2 字，占 4 列）和「趋势下行」（4 字，占 8 列）在
`to_string` 里被当成 2 和 4 去补空格，表头与数据行错位 1~2 字符。
报告里带中文状态列的地方全都会歪，且不报错、只是「看着别扭」，
很容易被忽略成终端字体问题。

pandas 其实有开关，只是默认关闭：

    pd.set_option('display.unicode.east_asian_width', True)

这里把它包成上下文管理器，避免全局副作用（别的模块可能在写 CSV，
   全局打开会让它对齐到东亚宽度）。

注意：**CSV / 文件导出不要用这个**。它只影响给人看的终端输出，
   对写入文件的字节没有任何影响，别指望它修好了导出文件的对齐。
"""
from __future__ import annotations

from contextlib import contextmanager

import pandas as pd


@contextmanager
def cn_table():
    """在 with 块内让 pandas 按东亚字符显示宽度对齐列。"""
    with pd.option_context('display.unicode.east_asian_width', True):
        yield


def render(df: pd.DataFrame, index: bool = False, **kwargs) -> str:
    """把 DataFrame 渲染成对齐好的终端表格字符串。

    Args:
        index: 是否显示索引（日期列之类通常已经单独成列，默认关掉）
        **kwargs: 透传给 `DataFrame.to_string`
    """
    with cn_table():
        return df.to_string(index=index, **kwargs)
