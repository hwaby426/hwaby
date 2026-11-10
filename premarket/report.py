"""盘前简报渲染 —— 终端版（快看）与 HTML 版（存档/细看）。

配色遵循 A 股习惯：**涨红跌绿**，与项目其他部分保持一致。
HTML 是自包含单文件（样式内联，无外部依赖），可以直接双击打开或转发。
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from premarket.analyzer import PremarketVerdict
from premarket.global_market import GlobalSnapshot
from premarket.news import NewsItem
from premarket.sector import SectorContext
from premarket.sentiment import SentimentResult
from premarket.stock_context import StockContext, format_flow, allowed_lag
from utils.html_style import email_palette, root_css

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'output')

# 配色统一由 utils.html_style 提供（与 review 共用同一套 :root）
_ROOT = root_css()


@dataclass
class StockReport:
    code: str
    name: str
    ctx: StockContext
    senti: SentimentResult
    verdict: PremarketVerdict
    sector: Optional[SectorContext] = None
    direct_items: List[NewsItem] = field(default_factory=list)
    market_items: List[NewsItem] = field(default_factory=list)


@dataclass
class PremarketReport:
    generated_at: str
    snap: GlobalSnapshot
    stocks: List[StockReport] = field(default_factory=list)

    @property
    def avg_score(self) -> float:
        vals = [s.verdict.total_score for s in self.stocks if s.verdict.ok]
        return round(sum(vals) / len(vals), 1) if vals else 0.0


# ----------------------------------------------------------------------
# 终端版
# ----------------------------------------------------------------------
def _bar(score: float, width: int = 20) -> str:
    """把 -100~+100 画成一条居中的条形图。"""
    half = width // 2
    n = int(round(abs(score) / 100 * half))
    n = min(n, half)
    if score >= 0:
        return ' ' * half + '█' * n + ' ' * (half - n)
    return ' ' * (half - n) + '█' * n + ' ' * half


def sector_peer_lines(sc: SectorContext, stale: bool = False) -> List[str]:
    """同业等权口径的说明行：等权/中位/σ、个股相对强弱、评分分母来源。

    为什么要把这些摊开：板块分从"固定 3% 分母"改成"2×板块自身波动率"之后，
    不写清分母是怎么来的，同一只票的分数变化就无从复核。

    stale=True 表示个股K线还没更新到板块指数那一天（收盘后忘了跑 update-daily）。
    这时同业口径是"前一日"、板块指数是"当日"，两者相差里混着日期差，
    背离提示必须关掉——否则会把"两天的差"误报成"指数稀释了板块压力"。
    """
    out: List[str] = []
    p = getattr(sc, 'peer', None)
    if p is not None and getattr(p, 'ok', False):
        bits = [f'同业等权({p.as_of})', f'{len(p.refs)}只',
                f'等权 {p.ew_pct:+.2f}%', f'中位 {p.median_pct:+.2f}%',
                f'σ {p.sigma:.2f}%']
        out.append('    ' + '  '.join(bits))
        if p.rel_pct is not None:
            out.append(f'    个股 {p.stock_pct:+.2f}%  相对同业 {p.rel_pct:+.2f}pct'
                       f' → {p.rel_label()}')
        # 指数 vs 同业背离：这是"板块压力被指数平均掉"的直接证据
        if sc.board_pct is not None and p.ew_pct is not None:
            gap = sc.board_pct - p.ew_pct
            if stale:
                out.append(f'    （本地K线未更新到板块当日，同业口径为 {p.as_of}，'
                           f'两者不同日，背离判断已关闭）')
            elif abs(gap) >= 1.5:
                if gap > 0:
                    why = '指数成分太杂，板块压力被平均掉了'
                else:
                    why = '少数权重股拖累指数，实际同业的跌幅没那么大'
                out.append(f'    ! 板块指数 {sc.board_pct:+.2f}% 与同业等权 {p.ew_pct:+.2f}% '
                           f'相差 {gap:+.2f}pct —— {why}')
    elif p is not None and p.note:
        out.append(f'    同业口径不可用：{p.note}')

    if sc.perf_pct is not None and sc.perf_denom:
        raw = sc.perf_pct / sc.perf_denom
        clipped = max(-1.0, min(1.0, raw))
        flag = '（已触顶）' if abs(raw) >= 1.0 else ''
        out.append(f'    评分口径：{sc.perf_src} {sc.perf_pct:+.2f}% ÷ 分母 {sc.perf_denom:.2f}%'
                   f' = {clipped:+.2f}{flag}')
    return out


def render_terminal(rep: PremarketReport) -> str:
    L = []
    W = 78
    L.append('=' * W)
    L.append(f'A股盘前简报    {rep.generated_at}')
    L.append('=' * W)

    # ---- 数据新鲜度总闸门 ----
    # 顶层先吼一声：只要有标的的数据落后于允许值，后面所有结论都不可信。
    _limit = allowed_lag()
    stale = [s for s in rep.stocks if getattr(s.ctx, 'data_lag_days', 0) > _limit]
    if stale:
        L.append('')
        L.append('!! 数据滞后警告 ' + '!' * (W - 12))
        for s in stale[:5]:
            L.append(f'   {s.name or s.code} 本地K线只到 {s.ctx.last_date}'
                     f'（滞后 {s.ctx.data_lag_days} 个交易日，当前时点允许 {_limit}）')
        L.append('   → 先跑 `python main.py update-daily`，否则下面的昨收/缺口/技术位全是旧数据')
        L.append('!' * W)

    # ---- 外围 ----
    s = rep.snap
    L.append('')
    L.append('【一】隔夜外围环境')
    L.append('-' * W)
    order = ['a50', 'golden_dragon', 'hsi', 'hstech', 'ixic', 'inx', 'dji', 'usdcny', 'gold', 'oil']
    for key in order:
        q = s.quotes.get(key)
        if not q:
            continue
        pct = f'{q.pct:+.2f}%' if q.pct is not None else '  解析失败'
        L.append(f'  {q.name:<16}{pct:>10}   {q.timing}')
    L.append('-' * W)
    L.append(f'  环境分 {s.env_score:+.1f}   {s.env_verdict}')
    L.append(f'  {_bar(s.env_score)}')
    for b in s.bullets[:6]:
        L.append(f'    · {b}')
    for h in s.sector_hints:
        L.append(f'    ▶ {h}')

    # ---- 个股 ----
    for i, st in enumerate(rep.stocks, 1):
        L.append('')
        L.append(f'【{i + 1}】{st.name}  {st.code}')
        L.append('-' * W)
        v = st.verdict
        if not v.ok or st.ctx.error:
            L.append(f'  数据不可用：{st.ctx.error or v.error}')
            continue

        c = st.ctx
        L.append(f'  昨收 {c.close:.2f} ({c.pct_chg:+.2f}%)  '
                 f'{c.last_date}   量比 {c.volume_ratio:.2f}   '
                 f'主力净流入 {format_flow(c.main_net_flow)}')

        L.append('')
        L.append(f'  综合分 {v.total_score:+.1f}   结论：{v.conclusion}（{v.strength}）'
                 f'    个股alpha分 {v.alpha_score:+.1f}'
                 + (f'    行业：{v.industry}' if v.industry else ''))
        L.append(f'  {_bar(v.total_score)}')
        if st.sector and st.sector.ok:
            L.append(f'    消息面 {v.news_score:+6.1f}  ×0.40')
            L.append(f'    外围   {v.env_score:+6.1f}  ×0.25')
            L.append(f'    板块   {v.sector_score:+6.1f}  ×0.20')
            L.append(f'    技术位 {v.tech_score:+6.1f}  ×0.15')
        else:
            L.append(f'    消息面 {v.news_score:+6.1f}  ×0.45')
            L.append(f'    外围   {v.env_score:+6.1f}  ×0.35')
            L.append(f'    技术位 {v.tech_score:+6.1f}  ×0.20')

        # 板块
        if st.sector and st.sector.ok:
            sc = st.sector
            L.append('')
            L.append('  -- 板块情况 --')
            bp = f'{sc.board_pct:+.2f}%' if sc.board_pct is not None else '—'
            L.append(f'    {sc.industry}（板块{sc.board_name or "?"} 昨日{bp}）  板块分 {sc.score:+.1f}')
            if sc.chain and len(sc.chain) > 1:
                L.append(f'    行业链：{" → ".join(sc.chain)}')
            if sc.themes:
                th = '、'.join(
                    f'{t["name"]}{(" %+.2f%%" % t["pct"]) if t["pct"] is not None else ""}'
                    for t in sc.themes)
                L.append(f'    核心题材：{th}')
                vals = [t['pct'] for t in sc.themes if t['pct'] is not None]
                if vals and sc.board_pct is not None and min(vals) < sc.board_pct - 1.0:
                    L.append(f'    ! 题材里最弱的一档比行业板块还低 {min(vals) - sc.board_pct:+.2f}pct'
                             f'，板块内部分化明显')
                elif vals and sc.board_pct is not None and max(vals) > sc.board_pct + 1.0:
                    L.append(f'    ! 题材全线好于行业板块 {max(vals) - sc.board_pct:+.2f}pct'
                             f'，行业指数可能在拖后腿')
            L.extend(sector_peer_lines(sc, stale=getattr(st.ctx, 'data_lag_days', 0) > 0))
            if sc.news:
                n_wide = sum(1 for x in sc.news
                             if sc.news_kind.get(x.title) == 'wide')
                L.append(f'    关联消息 {len(sc.news)} 条'
                         f'（板块级 {n_wide} / 个股 {len(sc.news) - n_wide}）')
                for x in sc.news[:6]:
                    hit = '、'.join((sc.news_hits.get(x.title) or [])[:2])
                    kind = '板块' if sc.news_kind.get(x.title) == 'wide' else '个股'
                    L.append(f'    · [{kind}] {x.title[:48]}'
                             + (f'  ←命中:{hit}' if hit else ''))
            else:
                L.append('    （无板块相关消息）')

        # 消息
        L.append('')
        L.append('  -- 直接相关消息 --')
        if st.direct_items:
            for x in st.direct_items[:8]:
                vd = next((z for z in st.senti.verdicts if z.item is x), None)
                tag = f'{vd.category[:16]:<16}{vd.polarity:+.1f}' if vd else f'{"":<16}     '
                L.append(f'    [{tag}] {x.title[:44]}')
                if vd and vd.used_llm:
                    L.append(f'              大模型：{vd.llm_label} {vd.llm_score:+.1f}｜{vd.llm_reason[:40]}')
        else:
            L.append('    （无直接相关消息）')
        if st.senti.llm_note:
            L.append(f'    备注：{st.senti.llm_note}')

        # 技术位
        L.append('')
        L.append('  -- 盘前状态 --')
        for n in c.notes:
            L.append(f'    · {n}')

        # 开盘预案
        L.append('')
        L.append('  -- 开盘应对预案 --')
        for a in v.action_plan:
            L.append(f'    ▸ {a}')

        # 风险
        if v.risks:
            L.append('')
            L.append('  -- 这套分析可能错在哪 --')
            for r in v.risks:
                L.append(f'    ! {r}')

    # ---- 大盘背景 ----
    bg = []
    for st in rep.stocks:
        for x in st.market_items:
            bg.append(x)
    if bg:
        seen, uniq = set(), []
        for x in bg:
            if x.title in seen:
                continue
            seen.add(x.title)
            uniq.append(x)
        L.append('')
        L.append('【附】昨日市场背景（不进个股打分）')
        L.append('-' * W)
        for x in uniq[:8]:
            L.append(f'    · {x.title[:62]}')

    L.append('')
    L.append('=' * W)
    L.append('免责：本简报是信息聚合与规则化解读，不是投资建议。')
    L.append('      开盘前请以交易所公告为准，A50 在 9:15 前仍在变动。')
    L.append('=' * W)
    return '\n'.join(L)


# ----------------------------------------------------------------------
# HTML 版
# ----------------------------------------------------------------------
_CSS = _ROOT + """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",Roboto,sans-serif;
  font-size:14px;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 12px;padding-left:10px;border-left:3px solid var(--up)}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin-bottom:14px}
.score-big{font-size:34px;font-weight:700;line-height:1.1}
.verdict{font-size:16px;font-weight:600;margin-top:2px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--dim)} .warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;background:var(--panel2);border-radius:4px;position:relative;overflow:hidden;margin:6px 0 12px}
.bar .mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line)}
.bar .fill{position:absolute;top:0;bottom:0;border-radius:3px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:10px 0}
.kv{background:var(--panel2);border-radius:6px;padding:10px 12px}
.kv .k{color:var(--dim);font-size:12px}
.kv .v{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
ul{margin:6px 0;padding-left:18px} li{margin:3px 0}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:12px;
  background:var(--panel2);color:var(--dim);margin-right:6px}
.tag.a{background:#3d1d1d;color:#ff9d95} .tag.n{background:#1d2f1d;color:#7ee787}
.tag.f{background:#1d2733;color:#79c0ff} .tag.l{background:#33291d;color:#e3b341}
.tag.wid{background:#2a2338;color:#c297ff}
.foot{color:var(--dim);font-size:12px;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}
.hint{color:var(--dim);font-size:12.5px;line-height:1.7;margin:6px 0 2px}
.item{border-bottom:1px dashed var(--line);padding:7px 0}
.item:last-child{border-bottom:none}
.item .t{font-size:13px}
.item .m{color:var(--dim);font-size:12px;margin-top:2px}
@media(max-width:720px){.grid{grid-template-columns:1fr 1fr}}
"""

# ---------------------------------------------------------------------------
# 邮件版样式
# ---------------------------------------------------------------------------
# 邮件客户端（QQ邮箱/Outlook/Gmail）与浏览器的差异，逐条抹平：
#   1. 不认 CSS 变量 `var(--x)`            → 全部写成字面量颜色
#   2. 不认 `display:grid`                 → 用 inline-block + 32% 宽自动换行
#   3. 会剥掉/忽略 `<style>` 里的 body 背景 → 外层再套一层带 bgcolor 的 table
#   4. `<style>` 本身可能被部分客户端丢弃   → 关键颜色在 render_email_html 里
#                                            再对内联 style 的 var() 做一次展开
_EMAIL_VAR = email_palette()

_EMAIL_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;
  font-family:-apple-system,"Segoe UI","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif;
  font-size:14px;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 40px}
h1,h2,h3{color:#e6edf3}
h1{font-size:21px;margin:0 0 4px}
h2{font-size:16px;margin:26px 0 12px;padding-left:10px;border-left:3px solid #f85149}
.sub{color:#8b949e;font-size:13px;margin-bottom:18px}
.panel{background:#161b22;border:1px solid #2a313c;border-radius:8px;padding:14px 16px;margin-bottom:14px}
.score-big{font-size:32px;font-weight:700;line-height:1.1}
.verdict{font-size:16px;font-weight:600;margin-top:2px}
.up{color:#f85149} .down{color:#3fb950} .flat{color:#8b949e} .warn{color:#d29922}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 9px;text-align:left;border-bottom:1px solid #2a313c;color:#e6edf3}
th{color:#8b949e;font-weight:500;font-size:12px}
td.num,th.num{text-align:right}
.grid{font-size:0;margin:8px 0}
.kv{display:inline-block;width:31.5%;vertical-align:top;background:#1c2330;
  border-radius:6px;padding:9px 11px;margin:0 1.4% 9px 0;font-size:14px}
.kv .k{color:#8b949e;font-size:12px}
.kv .v{font-size:16px;font-weight:600}
ul{margin:6px 0;padding-left:18px} li{margin:3px 0}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:12px;
  background:#1c2330;color:#8b949e;margin-right:6px}
.tag.a{background:#3d1d1d;color:#ff9d95} .tag.n{background:#1d2f1d;color:#7ee787}
.tag.f{background:#1d2733;color:#79c0ff} .tag.l{background:#33291d;color:#e3b341}
.tag.wid{background:#2a2338;color:#c297ff}
.foot{color:#8b949e;font-size:12px;margin-top:26px;border-top:1px solid #2a313c;padding-top:12px}
.item{border-bottom:1px dashed #2a313c;padding:6px 0}
.item .t{font-size:13px}
.item .m{color:#8b949e;font-size:12px;margin-top:2px}
"""


def _cls(x: float) -> str:
    return 'up' if x > 0 else ('down' if x < 0 else 'flat')


def _esc(s: str) -> str:
    return html.escape(str(s or ''))


def render_html(rep: PremarketReport, for_email: bool = False) -> str:
    """HTML 版简报。

    Args:
        for_email: True 时输出**邮件安全版**：CSS 不用变量、grid 改 inline-block、
                   body 背景写内联，并在外层套一层带 bgcolor 的 table。
                   直接双击打开用默认（False）即可，浏览器里观感更好。
    """
    s = rep.snap
    _css = _EMAIL_CSS if for_email else _CSS
    parts = [f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<title>A股盘前简报 {rep.generated_at}</title><style>{_css}</style></head>'
             f'<body><div class="wrap">']

    parts.append(f'<h1>A股盘前简报</h1><div class="sub">生成于 {rep.generated_at}'
                 f'　|　{len(rep.stocks)} 只标的　|　综合均分 '
                 f'<span class="{_cls(rep.avg_score)}">{rep.avg_score:+.1f}</span></div>')

    # ---- 数据新鲜度闸门（HTML） ----
    _limit = allowed_lag()
    _stale = [x for x in rep.stocks if getattr(x.ctx, 'data_lag_days', 0) > _limit]
    if _stale:
        rows = ''.join(
            f'<li>{_esc(x.name or x.code)} 本地K线只到 <b>{x.ctx.last_date}</b>'
            f'（滞后 {x.ctx.data_lag_days} 个交易日，当前时点允许 {_limit}）</li>'
            for x in _stale[:8])
        parts.append(
            '<div style="border-left:4px solid #d29922;background:rgba(210,153,34,.12);'
            'border-radius:0 8px 8px 0;padding:12px 16px;margin:14px 0">'
            '<b style="color:#e3b341">⚠ 数据滞后，下面所有结论都不可信</b>'
            f'<ul style="margin:8px 0 0">{rows}</ul>'
            '<div style="margin-top:8px;font-size:12.5px;opacity:.85">'
            '先跑 <code>python main.py update-daily</code> 再重新出报告 —— '
            '昨收价、开盘缺口、均线位置都会跟着变。</div></div>')

    # ---- 外围环境 ----
    parts.append('<h2>隔夜外围环境</h2><div class="panel">')
    parts.append('<div class="grid">')
    parts.append(f'<div class="kv"><div class="k">环境分</div>'
                 f'<div class="v {_cls(s.env_score)}">{s.env_score:+.1f}</div></div>')
    parts.append(f'<div class="kv"><div class="k">判定</div><div class="v">{_esc(s.env_verdict)}</div></div>')
    parts.append(f'<div class="kv"><div class="k">抓取时间</div>'
                 f'<div class="v" style="font-size:13px">{_esc(s.fetched_at)}</div></div>')
    parts.append('</div>')
    parts.append(_env_bar(s.env_score, email=for_email))

    parts.append('<table><thead><tr><th>标的</th><th class="num">现价</th>'
                 '<th class="num">涨跌幅</th><th>口径</th></tr></thead><tbody>')
    order = ['a50', 'golden_dragon', 'hsi', 'hstech', 'ixic', 'inx', 'dji', 'usdcny', 'gold', 'oil']
    for key in order:
        q = s.quotes.get(key)
        if not q:
            continue
        price = f'{q.price:,.2f}' if q.price is not None else '—'
        if q.pct is None:
            pct_s, c = ('解析失败', 'flat')
        else:
            pct_s, c = f'{q.pct:+.2f}%', _cls(q.pct)
        parts.append(f'<tr><td>{_esc(q.name)}</td><td class="num">{price}</td>'
                     f'<td class="num {c}">{pct_s}</td><td class="flat">{_esc(q.timing)}</td></tr>')
    parts.append('</tbody></table>')

    if s.bullets:
        parts.append('<ul style="margin-top:12px">')
        for b in s.bullets[:8]:
            parts.append(f'<li class="flat">{_esc(b)}</li>')
        parts.append('</ul>')
    if s.sector_hints:
        parts.append('<div style="margin-top:10px">')
        for h in s.sector_hints:
            parts.append(f'<div class="warn">▶ {_esc(h)}</div>')
        parts.append('</div>')
    parts.append('</div>')

    # ---- 个股 ----
    for st in rep.stocks:
        v, c, se = st.verdict, st.ctx, st.senti
        parts.append(f'<h2>{_esc(st.name)}　<span class="flat" style="font-size:13px">'
                     f'{_esc(st.code)}</span></h2><div class="panel">')
        if not v.ok or c.error:
            parts.append(f'<div class="warn">数据不可用：{_esc(c.error or v.error)}</div></div>')
            continue

        parts.append('<div class="grid">')
        parts.append(f'<div class="kv"><div class="k">综合分</div>'
                     f'<div class="v {_cls(v.total_score)}">{v.total_score:+.1f}</div></div>')
        parts.append(f'<div class="kv"><div class="k">结论</div>'
                     f'<div class="v {_cls(v.total_score)}">{_esc(v.conclusion)}</div></div>')
        parts.append(f'<div class="kv"><div class="k">个股alpha分（剔除外围）</div>'
                     f'<div class="v {_cls(v.alpha_score)}">{v.alpha_score:+.1f}</div></div>')
        parts.append(f'<div class="kv"><div class="k">昨收</div>'
                     f'<div class="v {_cls(c.pct_chg)}">{c.close:.2f}'
                     f'<span style="font-size:12px"> {c.pct_chg:+.2f}%</span></div></div>')
        parts.append(f'<div class="kv"><div class="k">行业</div>'
                     f'<div class="v" style="font-size:14px">{_esc(v.industry or "—")}</div></div>')
        parts.append(f'<div class="kv"><div class="k">量比 / MA20乖离</div>'
                     f'<div class="v" style="font-size:14px">{c.volume_ratio:.2f} / {c.bias20:+.1f}%</div></div>')
        parts.append('</div>')
        parts.append(_env_bar(v.total_score, email=for_email))

        # 四维度
        parts.append('<table><thead><tr><th>维度</th><th class="num">得分</th>'
                     '<th class="num">权重</th><th>说明</th></tr></thead><tbody>')
        has_sector = bool(st.sector and st.sector.ok)
        if has_sector:
            sc = st.sector
            bp = f'{sc.board_pct:+.2f}%' if sc.board_pct is not None else '—'
            chain_txt = ' → '.join(sc.chain) if len(sc.chain or []) > 1 else sc.industry
            theme_txt = ''
            if sc.themes:
                theme_txt = '｜题材 ' + '、'.join(
                    f'{t["name"]}{(" %+.2f%%" % t["pct"]) if t["pct"] is not None else ""}'
                    for t in sc.themes)
            peer_txt = ''
            p = getattr(sc, 'peer', None)
            if p is not None and getattr(p, 'ok', False):
                peer_txt = f'｜同业等权 {p.ew_pct:+.2f}%'
                if p.rel_pct is not None:
                    peer_txt += f'（个股相对 {p.rel_pct:+.2f}pct）'
            rows = [
                ('消息面', v.news_score, 0.40, se.verdict),
                ('外围环境', v.env_score, 0.25, s.env_verdict),
                ('板块', v.sector_score, 0.20,
                 f'{chain_txt} 昨日{bp}{theme_txt}{peer_txt}，板块消息{len(sc.news)}条'),
                ('技术位', v.tech_score, 0.15, f'MA20乖离{c.bias20:+.1f}%'),
            ]
        else:
            rows = [
                ('消息面', v.news_score, 0.45, se.verdict),
                ('外围环境', v.env_score, 0.35, s.env_verdict),
                ('技术位', v.tech_score, 0.20, f'MA20乖离{c.bias20:+.1f}%'),
            ]
        for nm, sc_, w, note in rows:
            parts.append(f'<tr><td>{nm}</td><td class="num {_cls(sc_)}">{sc_:+.1f}</td>'
                         f'<td class="num flat">×{w:.2f}</td><td class="flat">{_esc(note)}</td></tr>')
        parts.append('</tbody></table>')
        if has_sector:
            hint = [x.strip() for x in
                    sector_peer_lines(st.sector, stale=getattr(c, 'data_lag_days', 0) > 0)]
            if hint:
                parts.append('<div class="hint">'
                             + '<br>'.join(_esc(x) for x in hint) + '</div>')

        # 板块消息
        if has_sector and st.sector.news:
            sc = st.sector
            n_wide = sum(1 for x in sc.news if sc.news_kind.get(x.title) == 'wide')
            parts.append('<div style="margin-top:12px"><div style="color:var(--dim);font-size:12px">'
                         f'板块消息（{_esc(sc.industry)}，共{len(sc.news)}条：'
                         f'板块级{n_wide} / 个股{len(sc.news) - n_wide}）</div>')
            for x in sc.news[:6]:
                kind = sc.news_kind.get(x.title, 'wide')
                tag = 'wid' if kind == 'wide' else 'f'
                label = '板块' if kind == 'wide' else '个股'
                hit = '、'.join((sc.news_hits.get(x.title) or [])[:2])
                hit_txt = (f'<span class="flat"> 命中：{_esc(hit)}</span>'
                           if hit else '')
                parts.append(f'<div class="item"><div class="t">'
                             f'<span class="tag {tag}">{label}</span>{_esc(x.title)}</div>'
                             f'<div class="m">{_esc(x.published)}{hit_txt}</div></div>')
            parts.append('</div>')

        # 关键位
        parts.append('<div class="grid" style="margin-top:14px">')
        for label, val in [('量比', f'{c.volume_ratio:.2f}'),
                           ('MA20乖离', f'{c.bias20:+.1f}%'),
                           ('60日位置', f'{c.pos_in_range:.0f}%'),
                           ('ATR14', f'{c.atr14:.2f}' if c.atr14 else '—'),
                           ('日均波幅', f'{c.atr_pct:.1f}%' if c.atr_pct else '—'),
                           ('主力净流入', format_flow(c.main_net_flow))]:
            parts.append(f'<div class="kv"><div class="k">{label}</div><div class="v">{val}</div></div>')
        parts.append('</div>')

        if c.limit_event:
            parts.append(f'<div class="warn" style="margin-top:8px">昨日{_esc(c.limit_event)}</div>')

        # 消息
        parts.append('<div style="margin-top:16px"><div class="k" style="color:var(--dim);font-size:12px">'
                     '直接相关消息</div>')
        if st.direct_items:
            for x in st.direct_items[:10]:
                vd = next((z for z in se.verdicts if z.item is x), None)
                src_cls = {'announcement': 'a', 'news': 'n', 'flash': 'f', 'local': 'l'}.get(x.source, '')
                src_cn = {'announcement': '公告', 'news': '新闻', 'flash': '快讯',
                          'local': '手工'}.get(x.source, x.source)
                pol = ''
                if vd:
                    pol = (f'<span class="{_cls(vd.polarity)}">{vd.polarity:+.1f}</span>'
                           f'　<span class="flat">{_esc(vd.category)}</span>')
                parts.append('<div class="item">')
                parts.append(f'<div class="t"><span class="tag {src_cls}">{src_cn}</span>'
                             f'{_esc(x.title)}</div>')
                parts.append(f'<div class="m">{_esc(x.published)}　{pol}</div>')
                if vd and vd.used_llm:
                    parts.append(f'<div class="m">大模型复核：{_esc(vd.llm_label)} '
                                 f'{vd.llm_score:+.1f}｜{_esc(vd.llm_reason)}</div>')
                parts.append('</div>')
        else:
            parts.append('<div class="flat" style="padding:8px 0">（无直接相关消息）</div>')
        parts.append('</div>')
        if se.llm_note:
            parts.append(f'<div class="warn" style="font-size:12px;margin-top:6px">'
                         f'备注：{_esc(se.llm_note)}</div>')

        # 预案
        parts.append('<div style="margin-top:16px"><div style="color:var(--dim);font-size:12px">'
                     '开盘应对预案</div><ul>')
        for a in v.action_plan:
            parts.append(f'<li>{_esc(a)}</li>')
        parts.append('</ul></div>')

        # 盘前状态
        if c.notes:
            parts.append('<div style="margin-top:12px"><div style="color:var(--dim);font-size:12px">'
                         '盘前状态</div><ul>')
            for n in c.notes:
                parts.append(f'<li class="flat">{_esc(n)}</li>')
            parts.append('</ul></div>')

        # 风险
        if v.risks:
            parts.append('<div style="margin-top:12px"><div style="color:var(--warn);font-size:12px">'
                         '这套分析可能错在哪</div><ul>')
            for r in v.risks:
                parts.append(f'<li class="warn">{_esc(r)}</li>')
            parts.append('</ul></div>')

        parts.append('</div>')

    # ---- 背景 ----
    seen, uniq = set(), []
    for st in rep.stocks:
        for x in st.market_items:
            if x.title not in seen:
                seen.add(x.title)
                uniq.append(x)
    if uniq:
        parts.append('<h2>昨日市场背景（不进个股打分）</h2><div class="panel"><ul>')
        for x in uniq[:12]:
            parts.append(f'<li class="flat">{_esc(x.title)}</li>')
        parts.append('</ul></div>')

    parts.append('<div class="foot">本简报由规则引擎自动聚合生成，是信息整理与规则化解读，'
                 '<b>不是投资建议</b>。开盘前请以交易所与公司正式公告为准；'
                 'A50 期货在 9:15 集合竞价前仍在变动，请在开盘前刷新。</div>')
    parts.append('</div></body></html>')
    return ''.join(parts)


def _env_bar(score: float, email: bool = False) -> str:
    """居中条形图（以 50% 为零轴）。

    Args:
        email: True 时改用 table 布局 —— 邮件客户端不支持 `position:absolute`，
               原来用绝对定位的实现会退化成"整条色块"，误导性很强。
    """
    pct = max(-100.0, min(100.0, score))
    w = abs(pct) / 2.0                      # 半宽 50%
    color = 'var(--up)' if pct > 0 else ('var(--down)' if pct < 0 else 'var(--dim)')

    if email:
        c = _EMAIL_VAR['up'] if pct > 0 else (_EMAIL_VAR['down'] if pct < 0 else _EMAIL_VAR['dim'])
        cell = f'<div style="height:8px;width:{w:.2f}%;background:{c};border-radius:3px"></div>'
        left = cell if pct < 0 else ''
        right = cell if pct > 0 else ''
        # td 必须显式 padding/border 归零：全局 th,td 规则会把它撑成 20px 高并多一条下边框
        td = 'height:8px;padding:0;border:none;line-height:8px'
        return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
                f' bgcolor="{_EMAIL_VAR["panel2"]}" style="height:8px;margin:6px 0 12px;'
                'border-collapse:collapse">'
                f'<tr><td width="50%" align="right" style="{td}">{left}</td>'
                '<td width="1" bgcolor="#3a424e" style="height:8px;width:1px;padding:0;'
                'border:none;line-height:8px">&nbsp;</td>'
                f'<td width="50%" align="left" style="{td}">{right}</td></tr></table>')

    left = '50%' if pct >= 0 else f'{50 - w}%'
    return (f'<div class="bar"><div class="mid"></div>'
            f'<div class="fill" style="left:{left};width:{w}%;background:{color}"></div></div>')


def save_html(rep: PremarketReport, path: Optional[str] = None) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if not path:
        ts = datetime.now().strftime('%Y%m%d_%H%M')
        path = os.path.join(OUTPUT_DIR, f'premarket_{ts}.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(render_html(rep))
    return path


_VAR_RE = re.compile(r'var\(--([a-z0-9]+)\)')

# 注意用单引号：要放进 style="..." 属性里，双引号会把属性提前闭合
_EMAIL_FONT = "-apple-system,'Segoe UI','Microsoft YaHei',Roboto,Helvetica,Arial,sans-serif"


def render_email_html(rep: PremarketReport) -> str:
    """邮件正文专用的 HTML。

    与 `render_html` 的差别全部是邮件客户端的兼容处理：
      1. 内联 style 里的 `var(--dim)` 展开成字面量颜色（客户端不认 CSS 变量）
      2. 外层套一层带 bgcolor 的 table —— Gmail/Outlook 会丢弃 body 的背景色，
         不套这层的话深色主题会变成"白底 + 浅灰字"，几乎看不见
      3. 外层 td 直接带上 font-family —— 部分客户端会整个剥掉 `<style>`，
         那时只有内联样式还活着
    """
    doc = render_html(rep, for_email=True)
    doc = _VAR_RE.sub(lambda m: _EMAIL_VAR.get(m.group(1), _EMAIL_VAR['dim']), doc)
    doc = doc.replace(
        '<body><div class="wrap">',
        f'<body style="margin:0;padding:0;background:{_EMAIL_VAR["bg"]};'
        f'font-family:{_EMAIL_FONT}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
        f' bgcolor="{_EMAIL_VAR["bg"]}"><tr>'
        f'<td align="center" style="padding:0;border:none;font-family:{_EMAIL_FONT}">'
        '<div class="wrap">', 1)
    doc = doc.replace('</div></body></html>', '</div></td></tr></table></body></html>', 1)
    return doc
