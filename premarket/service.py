"""盘前分析编排 —— 取数 → 分析 → 渲染 → 发送。

这一层只负责把各模块串起来，不放业务逻辑。
任一环节失败都降级而不是中断：盘前时间紧，简报出得来比出得完美更重要，
缺失的维度会在报告里显式标注，不会假装没发生过。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger

from premarket.analyzer import PremarketVerdict, analyze
from premarket.global_market import fetch_global_snapshot
from premarket.news import fetch_flash_news, gather_news
from premarket.report import (
    PremarketReport, StockReport, render_email_html, render_terminal, save_html,
)
from premarket.sector import (
    SectorContext, build_sector_context, resolve_sector,
)
from premarket.sentiment import analyze_news
from premarket.stock_context import StockContext, load_context


def resolve_name(code: str, fallback: str = '') -> str:
    """从 stock_info 取股票简称。取不到就用代码兜底。

    坑：`stock_info.code` 存的是 **sh600000（无点）**，而本项目其他地方
    （daily_kline、normalize_code）用的是 **sh.600000（带点）**。
    只按带点格式查会永远查不到，简称缺失会连锁导致快讯关键词过滤失效
    （快讯里写的是"海螺新材"而不是"000619"），所以这里要试多种写法。
    """
    variants = [code, code.replace('.', ''), code.split('.')[-1]]
    try:
        from db.database import session_scope
        from db.models import StockInfo
        with session_scope() as s:
            for v in variants:
                if not v:
                    continue
                row = (s.query(StockInfo)
                       .filter((StockInfo.code == v) | (StockInfo.symbol == v))
                       .first())
                if row and row.name:
                    return row.name
    except Exception:                    # noqa: BLE001
        pass
    return fallback or code


def name_core(name: str) -> str:
    """从全称里取一个更短的核心词，用于快讯模糊匹配。

    "贵州茅台" → "茅台"，"海螺新材" → "新材"。
    快讯标题里常写简称而非全称，只用全称会漏掉不少命中。
    短名（<=3字）不再截短，否则噪声太大。
    """
    n = (name or '').strip()
    if len(n) <= 3:
        return ''
    return n[-2:]


def run_premarket(
    codes: List[str],
    use_network: bool = True,
    use_llm: bool = True,
    news_hours: float = 16.0,
    local_news: Optional[str] = None,
    with_flow: bool = True,
    names: Optional[Dict[str, str]] = None,
) -> PremarketReport:
    """跑一次完整的盘前分析。"""
    names = names or {}
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rep = PremarketReport(generated_at=generated_at, snap=fetch_global_snapshot(use_network))

    # ---- 公共数据：一次抓取，全体复用 ----
    # 快讯要翻页（16h 窗约 3~5 页），20 只票各抓一遍既慢又容易被限流
    flash = fetch_flash_news(hours=news_hours) if use_network else []

    for code in codes:
        name = names.get(code) or resolve_name(code)
        logger.info(f'[盘前] 分析 {code} {name}')

        # 1) 消息面
        try:
            core = name_core(name)
            items = gather_news(code, name=name, hours=news_hours,
                                local_path=local_news, use_network=use_network,
                                extra_keywords=[core] if core else None,
                                flash=flash)
            senti = analyze_news(code, items, name=name, use_llm=use_llm)
        except Exception as e:           # noqa: BLE001
            logger.warning(f'[盘前] {code} 消息分析失败: {e}')
            from premarket.sentiment import SentimentResult
            senti = SentimentResult(code=code, name=name, verdict='分析失败')
            items = []

        # 2) 板块维度（每票 1 次请求：行业归属 + 板块涨跌幅一起回来）
        try:
            direct_titles = {x.title for x in items if x.relevance == 'direct'}
            sector = build_sector_context(code, flash=flash if use_network else None,
                                          exclude_titles=direct_titles,
                                          use_network=use_network)
        except Exception as e:           # noqa: BLE001
            logger.warning(f'[盘前] {code} 板块分析失败: {e}')
            sector = SectorContext(note=f'板块分析失败: {e}')

        # 3) 技术位
        try:
            ctx = load_context(code, name=name, with_flow=with_flow and use_network)
        except Exception as e:           # noqa: BLE001
            logger.warning(f'[盘前] {code} 技术状态失败: {e}')
            ctx = StockContext(code=code, name=name, error=str(e))

        # 4) 综合研判
        try:
            verdict = analyze(code, name, rep.snap, senti, ctx, sector=sector)
        except Exception as e:           # noqa: BLE001
            logger.warning(f'[盘前] {code} 综合研判失败: {e}')
            verdict = PremarketVerdict(code=code, name=name, ok=False, error=str(e))

        direct = [x for x in items if x.relevance == 'direct']
        market = [x for x in items if x.relevance == 'market']
        rep.stocks.append(StockReport(code=code, name=name, ctx=ctx, senti=senti,
                                      verdict=verdict, sector=sector,
                                      direct_items=direct,
                                      market_items=market))

    return rep


def guess_trade_date(run_at: Optional[datetime] = None,
                     override: Optional[str] = None) -> date:
    """推断这份评分是针对哪一个交易日的。

    为什么不能用「库里有没有今天的 K 线」来判断：盘前跑的时候当天还没
    收盘，K 线本来就不该有；而周末跑时库里也不会有。两种情形在数据上
    长得一样，区分不出来。

    这里改用时刻推断：
      · 15:00 之前跑 → 预测**今天**（盘前预案，开盘就要用）
      · 15:00 之后跑 → 今天已收盘，评分实际指向**下一个交易日**
      · 落在周六/周日 → 顺延到下周一

    已知不精确的地方：法定节假日会推错（如 10-01 是周四，休市）。这种
    推测没法靠工作日判断，但代价可控——事后回填时按 trade_date 去
    daily_kline 找不到 K 线，那条样本直接标记无效，不会把准确率算歪。
    要精确就显式传 override。
    """
    if override:
        try:
            return datetime.strptime(str(override)[:10], '%Y-%m-%d').date()
        except ValueError:
            logger.warning(f'--trade-date 格式应为 YYYY-MM-DD，收到 {override}，改用推断值')

    t = run_at or datetime.now()
    d = t.date()
    if t.hour >= 15:                     # 收盘后跑的，指向下一个交易日
        d += timedelta(days=1)
    while d.weekday() >= 5:              # 周六/周日 顺延到周一
        d += timedelta(days=1)
    return d


def save_scores(rep: PremarketReport, trade_date: Optional[str] = None,
                use_llm: bool = True, run_at: Optional[datetime] = None) -> int:
    """把本次评分的分项落库，返回写入行数。

    幂等：同一个 (code, trade_date) 先删后插，当天重跑几次都只留最后一份。
    这是必要的——盘前那半小时里结论会随 A50 跳动而变，我们只想留下
    「真正拿去决策的那一版」，不是每次刷新的流水账。

    ok=False 的票也记一行（total_score 记 0 并置 ok=0）：盘前某个维度
    取数失败是常态，覆盖率本身要能查得到，统计时用 WHERE ok=1 过滤即可。
    """
    if not rep.stocks:
        return 0

    from db.database import session_scope
    from db.models import PremarketScore

    td = guess_trade_date(run_at=run_at, override=trade_date)
    ra = run_at or datetime.now()
    rows = 0
    try:
        with session_scope() as s:
            for st in rep.stocks:
                v = st.verdict
                s.query(PremarketScore).filter(
                    PremarketScore.code == v.code,
                    PremarketScore.trade_date == td,
                ).delete(synchronize_session=False)

                s.add(PremarketScore(
                    code=v.code,
                    symbol=v.code.split('.')[-1],
                    name=(v.name or st.name or '')[:32],
                    trade_date=td,
                    run_at=ra,
                    env_score=v.env_score,
                    news_score=v.news_score,
                    sector_score=v.sector_score,
                    tech_score=v.tech_score,
                    total_score=v.total_score,
                    alpha_score=v.alpha_score,
                    conclusion=(v.conclusion or '')[:16],
                    strength=(v.strength or '')[:4],
                    industry=(v.industry or '')[:32],
                    direct_news=len(st.direct_items or []),
                    market_news=len(st.market_items or []),
                    use_llm=1 if use_llm else 0,
                    ok=1 if v.ok else 0,
                    error=(v.error or '')[:255] if not v.ok else None,
                ))
                rows += 1
    except Exception as e:               # noqa: BLE001
        logger.warning(f'[盘前] 评分落库失败（不影响简报）: {e}')
        return 0

    logger.info(f'[盘前] 评分已落库 {rows} 条 · 交易日 {td}')
    return rows


def send_report_email(rep: PremarketReport, html_path: str = '') -> bool:
    """把简报发到 .env 里配置的邮箱（HTML 正文 + 纯文本备选）。"""
    import os
    if os.getenv('SEND_EMAIL', 'false').strip().lower() not in ('true', '1', 'yes'):
        logger.info('[盘前] SEND_EMAIL 未开启，跳过邮件')
        return False
    try:
        from utils.email_sender import send_email
        subject = (f'[盘前简报] {rep.generated_at[:10]} '
                   f'{len(rep.stocks)}只标的 均分{rep.avg_score:+.1f}')

        # 正文用 HTML：比等宽文本好读得多，也才能显示涨红跌绿的配色
        html_body = render_email_html(rep)
        text_body = render_terminal(rep)          # 备选正文（纯文本客户端/手机降级）
        if html_path:
            text_body += f'\n\nHTML 版：{html_path}\n'

        return send_email(
            to_email=os.getenv('EMAIL_TO', ''),
            subject=subject,
            content=html_body,
            is_html=True,
            text_fallback=text_body,
            smtp_server=os.getenv('EMAIL_SMTP_SERVER', 'smtp.qq.com'),
            smtp_port=int(os.getenv('EMAIL_SMTP_PORT', '587')),
            sender_email=os.getenv('EMAIL_FROM', ''),
            sender_password=os.getenv('EMAIL_PASS', ''),
        )
    except Exception as e:               # noqa: BLE001
        logger.warning(f'[盘前] 邮件发送失败: {e}')
        return False
