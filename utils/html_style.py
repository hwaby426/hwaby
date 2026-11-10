"""HTML 报告的共享配色。

**为什么单独抽出来**：`premarket/report.py` 和 `review/render.py` 原本各写一份
`:root`，注释都写着"保持同一套变量"，但实测已经漂移：

```
              --bg        --panel2    --line      --up
premarket     #0d1117     #1c2330     #2a313c     #f85149
review        #0e1116     #1c2230     #2a3140     #ff5c5c
```

同一份报告体系两种底色、两种红，两份并排看会明显不一致。配色属于**跨模块契约**，
只要写两份就一定会漂——注释管不住它，只能收敛到一处。改这里，两边同时生效。

A股约定：**涨=红（--up）、跌=绿（--down）**，与欧美相反。写反了每一屏都是错的。

注意这里只管**配色（:root 变量）**。布局类样式（.panel / .card / table）
两份报告本来就长得不一样，各自保留，不要为了"统一"硬合并——那样只会把
两个都改坏。
"""

from __future__ import annotations

from typing import Dict, Optional

# 深色底（#0d1117 系，GitHub Dark 血统）。两份报告共用这一套。
PALETTE: Dict[str, str] = {
    'bg': '#0d1117',      # 页面底
    'panel': '#161b22',   # 卡片
    'panel2': '#1c2330',  # 卡片内嵌块 / 表格斑马
    'line': '#2a313c',    # 分隔线、边框
    'txt': '#e6edf3',     # 正文
    'dim': '#8b949e',     # 次要文字
    'up': '#f85149',      # 涨 = 红（A股）
    'down': '#3fb950',    # 跌 = 绿（A股）
    'warn': '#d29922',    # 提醒
}


def root_css(extra: Optional[Dict[str, str]] = None) -> str:
    """生成 `:root{...}` 块。

    Args:
        extra: 该报告额外需要、或需要覆盖的变量。例如复盘要 `--accent`，
            盘前的 `--warn` 用的是另一档黄。覆盖只影响调用方，不改全局。
    """
    p = dict(PALETTE)
    if extra:
        p.update(extra)
    pairs = ' '.join(f'--{k}:{v};' for k, v in p.items())
    return f':root{{\n  {pairs}\n}}\n'


def email_palette() -> Dict[str, str]:
    """邮件版用字面量颜色（邮件客户端不认 `var(--x)`）。

    返回的是不带 `--` 前缀的字典，键名与 `PALETTE` 相同，方便
    `render_email_html` 里对内联 `style` 的 `var()` 做一次展开。
    """
    return dict(PALETTE)
