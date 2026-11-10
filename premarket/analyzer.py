"""盘前综合研判 —— 把「外围环境 / 消息面 / 技术位」三路信息合成一个结论。

权重怎么定的
------------
盘前只有一件事要判断：**明天开盘这只票大概率往哪走、我该怎么办**。
三类信息的边际价值完全不同：

| 维度 | 权重 | 理由 |
|---|---|---|
| 消息面 | 0.40 | 个股**独有**的边际信息。别人没有、你有，这才是 alpha 的来源 |
| 外围环境 | 0.25 | 全市场 beta，决定开盘缺口的方向和大小，但对你我他都一样 |
| 板块 | 0.20 | "准 beta"：行业政策/景气度对个股的影响常常比美股还直接 |
| 技术位 | 0.15 | 不产生新信息，只告诉你"消息落地后价格大概走到哪"，用来定关键位 |

刻意不给技术位高权重——很多人盘前盯着 K 线形态猜方向，那是把 beta 当 alpha 用。

除综合分外另输出 **alpha 分**：只含消息/板块/技术三维（剔除人人平等的外围），
用于多只股票之间横向比较——大涨日外围分会把所有票一起推高，排名必须看 alpha。

输出什么
--------
不是"买/卖"这种拍板的结论（机器不该替你拍板），而是三件事：
1. 综合方向与强度
2. **开盘应对预案**：高开怎么办、低开怎么办（盘前准备的核心价值在这里）
3. 风险提示：哪些地方这套分析可能出错
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from premarket.global_market import GlobalSnapshot
from premarket.sector import SectorContext
from premarket.sentiment import SentimentResult
from premarket.stock_context import StockContext, tech_score

W_NEWS = 0.40
W_ENV = 0.25
W_SECTOR = 0.20
W_TECH = 0.15


@dataclass
class PremarketVerdict:
    code: str
    name: str = ''

    env_score: float = 0.0
    news_score: float = 0.0
    sector_score: float = 0.0
    tech_score: float = 0.0
    total_score: float = 0.0
    alpha_score: float = 0.0    # 剔除外围后的个股分：消息/板块/技术按权重归一
    industry: str = ''

    conclusion: str = ''
    strength: str = ''          # 强 / 中 / 弱
    action_plan: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    key_levels: List[str] = field(default_factory=list)
    ok: bool = True
    error: str = ''


def _band(s: float) -> tuple:
    """把 -100~+100 的分映射到 (结论, 强度)。"""
    a = abs(s)
    if s >= 35:
        label = '明显看多'
    elif s >= 12:
        label = '偏多'
    elif s > -12:
        label = '中性'
    elif s > -35:
        label = '偏空'
    else:
        label = '明显看空'
    strength = '强' if a >= 35 else ('中' if a >= 12 else '弱')
    return label, strength


def analyze(
    code: str,
    name: str,
    snap: GlobalSnapshot,
    senti: SentimentResult,
    ctx: StockContext,
    sector: Optional[SectorContext] = None,
) -> PremarketVerdict:
    v = PremarketVerdict(code=code, name=name)

    v.env_score = snap.env_score
    v.news_score = senti.news_score
    v.tech_score = tech_score(ctx)

    has_sector = bool(sector and sector.ok)
    v.sector_score = sector.score if has_sector else 0.0
    v.industry = sector.industry if sector else ''

    if has_sector:
        v.total_score = round(
            W_NEWS * v.news_score + W_ENV * v.env_score
            + W_SECTOR * v.sector_score + W_TECH * v.tech_score, 1)
    else:
        # 板块维度缺失时权重让回消息/外围/技术（按原 0.45/0.35/0.20 归一）
        v.total_score = round(
            0.45 * v.news_score + 0.35 * v.env_score + 0.20 * v.tech_score, 1)

    # alpha 分：剔除外围（人人平等），只留个股相关三维，归一化到同等量级
    parts = [(W_NEWS, v.news_score), (W_TECH, v.tech_score)]
    if has_sector:
        parts.append((W_SECTOR, v.sector_score))
    wsum = sum(w for w, _ in parts)
    v.alpha_score = round(sum(w * s for w, s in parts) / wsum, 1) if wsum else 0.0

    v.conclusion, v.strength = _band(v.total_score)
    _build_action_plan(v, snap, senti, ctx)
    _build_risks(v, snap, senti, ctx, sector)
    return v


def _build_action_plan(v: PremarketVerdict, snap: GlobalSnapshot,
                       senti: SentimentResult, ctx: StockContext) -> None:
    """开盘应对预案。这是盘前准备真正值钱的部分——
    盘前把各种情况想清楚，开盘才不会手忙脚乱。"""
    plan = v.action_plan

    if not ctx.ok:
        plan.append('技术位不可用（K线数据缺失），只能依据外围与消息面判断')
        return

    atr = ctx.atr14
    prev_close = ctx.close

    # ---- 关键位 ----
    v.key_levels.append(f'昨收 {prev_close:.2f}')
    v.key_levels.append(f'昨高 {ctx.high:.2f}（突破=强势确认）')
    v.key_levels.append(f'昨低 {ctx.low:.2f}（跌破=走弱确认）')
    if ctx.ma5:
        v.key_levels.append(f'MA5 {ctx.ma5:.2f}')
    if atr:
        v.key_levels.append(f'ATR14 {atr:.2f}（约 {ctx.atr_pct:.1f}%，日内波动基准）')

    # ---- 分情形预案 ----
    if v.total_score >= 12:
        plan.append(
            f'偏多情形：若高开 ≤2%，可观察回踩昨收({prev_close:.2f})附近是否企稳，'
            f'企稳是较舒服的介入点')
        plan.append(
            f'若高开 >4%，属于情绪透支区——A股高开高走概率本就不高，'
            f'不要在高开瞬间追，等 9:45 后看是否守住开盘价')
        if atr:
            plan.append(f'止损参考：跌破介入价 {atr:.2f} 元（1×ATR）或跌破昨低 {ctx.low:.2f}')
    elif v.total_score <= -12:
        plan.append(
            f'偏空情形：若低开，先看能否收回昨低({ctx.low:.2f})上方，'
            f'收不回则延续弱势')
        plan.append(
            f'持仓者：反弹到昨收({prev_close:.2f})附近是减仓窗口；'
            f'空仓者：不要接下跌中的刀，等止跌信号（放量收阳）')
        plan.append(f'若因利空消息大幅低开 >5%，注意恐慌盘通常在 10:00 前后才出清')
    else:
        plan.append(
            f'中性情形：方向不明时**不预判、只应对**。'
            f'以昨高 {ctx.high:.2f} / 昨低 {ctx.low:.2f} 为界，突破哪边跟哪边')
        plan.append('开盘 15 分钟内的方向往往是噪音，等 9:45 之后再判断')

    # ---- 消息面驱动的特殊预案 ----
    strong_neg = [x for x in senti.verdicts
                  if x.polarity <= -3 and x.item.relevance == 'direct']
    strong_pos = [x for x in senti.verdicts
                  if x.polarity >= 2.5 and x.item.relevance == 'direct']
    if strong_neg:
        plan.append('⚠ 存在重大利空类消息，开盘前先确认是否已出尽，'
                    '利空落地当天常见"低开高走"，但需放量才能确认')
    if strong_pos:
        plan.append('存在重大利好类消息，小心"利好出尽"——'
                    '若开盘冲高后回落跌破开盘价，视为兑现盘出逃')

    # ---- 一字板/涨停次日的特殊处理 ----
    if '涨停' in ctx.limit_event:
        plan.append(f'昨日{ctx.limit_event}：次日溢价概率较高，但炸板风险同样高，'
                    f'盯住是否封住开盘价；一旦开板且量能放大，优先兑现')
    elif '跌停' in ctx.limit_event:
        plan.append(f'昨日{ctx.limit_event}：次日惯性下探概率高，'
                    f'不要抄底，等企稳信号')


def _build_risks(v: PremarketVerdict, snap: GlobalSnapshot,
                 senti: SentimentResult, ctx: StockContext,
                 sector: Optional[SectorContext] = None) -> None:
    """明确说清楚这套分析哪里可能错。不写这节就是耍流氓。"""
    r = v.risks

    if sector is None or not sector.ok:
        r.append(f'板块维度缺失（{(sector.note if sector else "未取数") or "行业归属未知"}），'
                 f'行业政策/景气度变化不会反映在本结论中')
    elif sector.news_raw == 0 and not sector.news:
        r.append(f'所属板块「{sector.industry}」昨夜今晨无相关行业消息，'
                 f'板块分主要来自昨日板块涨跌（{sector.board_pct:+.2f}%），'
                 f'不含增量信息' if sector.board_pct is not None else
                 f'所属板块「{sector.industry}」昨夜今晨无相关行业消息')

    # 同业拆解：区分「板块普跌」和「个股独立利空」——两者应对方式完全不同
    peer = getattr(sector, 'peer', None) if sector is not None else None
    if peer is not None and getattr(peer, 'ok', False) and peer.rel_pct is not None:
        if peer.rel_pct <= -5:
            r.append(f'个股跑输同业 {abs(peer.rel_pct):.1f}pct（同业等权 {peer.ew_pct:+.2f}%、'
                     f'中位 {peer.median_pct:+.2f}%）——板块普跌解释不了这个幅度，'
                     f'存在个股层面的独立利空，不要只按"板块回调"处理')
        elif peer.rel_pct >= 5:
            r.append(f'个股逆势强于同业 {peer.rel_pct:.1f}pct（同业等权 {peer.ew_pct:+.2f}%）——'
                     f'有资金护盘或独立逻辑，但弱势环境里的抗跌常常只是"还没跌"')
        if sector.board_pct is not None and abs(sector.board_pct - peer.ew_pct) >= 1.5:
            r.append(f'板块指数（{sector.board_pct:+.2f}%）与同业等权（{peer.ew_pct:+.2f}%）'
                     f'相差 {sector.board_pct - peer.ew_pct:+.2f}pct：指数成分太杂会稀释板块压力，'
                     f'判断"跑输板块多少"时以同业口径为准')

    if not senti.llm_enabled:
        r.append(f'消息面为纯规则判定（{senti.llm_note or "未启用大模型"}），'
                 f'读不出"业绩预增但低于预期"这类复合语义')

    if senti.no_news or not [x for x in senti.verdicts if x.item.relevance == 'direct']:
        r.append('未抓到该股的直接相关消息——此时"消息面中性"等于"没有信息"，'
                 '不等于"确认无利空"，结论置信度低')

    if ctx.ok and ctx.data_lag_days >= 2:
        r.append(f'本地K线滞后 {ctx.data_lag_days} 天，技术位与昨收价可能不是最新的')

    if ctx.ok and ctx.main_net_flow is None:
        r.append('主力资金流未取到（东财接口不稳定），资金面维度缺失')

    # 环境分与消息面方向相反时，明确提示
    if v.env_score * v.news_score < -200:
        r.append(f'外围环境({v.env_score:+.0f})与个股消息({v.news_score:+.0f})方向相反：'
                 f'大盘与个股背离时，通常个股消息占主导，但开盘缺口会受大盘拖累')

    if ctx.ok and ctx.atr_pct >= 6:
        r.append(f'该股日均波幅高达 {ctx.atr_pct:.1f}%，'
                 f'盘中噪声大，止损位要放宽，仓位要压低')

    if snap.quotes.get('a50') and snap.quotes['a50'].pct is not None:
        r.append('A50 期货是实时价，会一直变动到 A 股开盘，'
                 '盘前结论请在 9:15 集合竞价前再刷新一次')
