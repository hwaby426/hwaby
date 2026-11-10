"""盘前分析模块回归测试（离线可跑，不依赖网络）。

覆盖三件最容易出错的事：
1. 利好利空词典的方向与"复合语义求净值"
2. 外围行情三类格式的字段位解析（这里踩过坑）
3. 快讯公司名精确匹配（模糊匹配曾把同名公司全吸进来）
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def recent_ts(hours=2):
    """相对当前时间的发布时间。

    **不要写死日期**：`_time_decay` 是按 age_hours 衰减的，写死 '2026-10-29'
    的测试在两天后就会因为衰减到 0.3 而失败（实测分数从 −40 掉到 −14.9，
    低于 −30 的断言）。这种测试是时间炸弹，过一段时间必红。
    """
    return (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M')


def pd_ts(s):
    import pandas as pd
    return pd.Timestamp(s)

PASS, FAIL = 0, 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}' + (f'  ({detail})' if detail else ''))
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  ({detail})' if detail else ''))


def main():
    print('=' * 72)
    print('盘前分析模块回归测试')
    print('=' * 72)

    from premarket.news import NewsItem, filter_flash_by_company
    from premarket.sentiment import score_with_rules, analyze_news, _time_decay
    from premarket.global_market import _parse_line
    from premarket.stock_context import tech_score, StockContext

    # ------------------------------------------------------------------
    print('\n[1] 利好利空词典 — 方向')
    cases = [
        ('关于2026年前三季度业绩预增的公告', 3.0),
        ('股东拟减持不超过2%股份', -2.5),
        ('关于收到立案调查通知书的公告', -4.0),
        ('公司中标5.2亿元重大项目', 2.5),
        ('前三季度净利润同比下降82.42%', -3.0),
        ('可能被实施退市风险警示的提示性公告', -5.0),
        ('关于回购公司股份方案的公告', 2.5),
        ('关于召开第三次临时股东大会的通知', 0.0),
    ]
    for title, expect in cases:
        v = score_with_rules(NewsItem(title=title, source='announcement'))
        check(f'"{title[:22]}" → {expect:+.1f}',
              abs(v.polarity - expect) < 1e-6, f'实际 {v.polarity:+.1f} / {v.category}')

    # ------------------------------------------------------------------
    print('\n[2] 复合语义 — 正负面命中要"求净值"而不是"取最大绝对值"')
    # "预增但低于预期" 同时命中 +3 和 -3，净值应为 0（中性），不能判成 +3 纯利好
    v = score_with_rules(NewsItem(title='业绩预增30%但低于市场预期', source='announcement'))
    check('"业绩预增但低于预期" 判为中性', abs(v.polarity) < 1e-6, f'实际 {v.polarity:+.1f}')

    v = score_with_rules(NewsItem(title='业绩预增50%，超市场预期', source='announcement'))
    check('"预增且超预期" 仍为明显利好', v.polarity >= 3.0, f'实际 {v.polarity:+.1f}')

    # ------------------------------------------------------------------
    print('\n[3] 定期报告 — 方向未知必须标出来，不能悄悄算 0 分')
    v = score_with_rules(NewsItem(title='2026年三季度报告', source='announcement'))
    check('三季报被标为"方向待定"', '待定' in v.category, v.category)

    # ------------------------------------------------------------------
    print('\n[4] 外围行情字段位解析（美股/港股/外盘期货/外汇 四类格式）')
    # 美股 gb_：名称, 现价, 涨跌幅%, 时间, 涨跌额
    p, pct, t = _parse_line('gb_dji', '道琼斯,52475.1016,0.19,2026-10-29 05:18:59,97.5300')
    check('美股现价/涨跌幅', abs(p - 52475.1016) < 0.01 and abs(pct - 0.19) < 1e-6,
          f'{p} {pct}')

    # 港股 rt_hk：code,名,开,昨收,高,低,现价,涨跌额,涨跌幅
    p, pct, t = _parse_line('rt_hkHSI',
                            'HSI,恒生指数,23088.140,23094.830,23184.090,22928.270,'
                            '22958.650,-136.180,-0.590')
    check('港股现价取第7位', abs(p - 22958.65) < 0.01, f'{p}')
    check('港股涨跌幅取第9位', abs(pct + 0.59) < 1e-6, f'{pct}')

    # 外盘期货 hf_：现价,买,卖,高,低,时间,昨收,开盘  ← 昨收在第8位，不是第7位
    p, pct, t = _parse_line('hf_CHA50CFD',
                            '12909.640,,12908.000,12910.000,12917.000,12887.000,'
                            '18:22:32,12899.000,12894.000')
    check('外盘昨收取第8位（曾误取第7位=时间串）', pct is not None and abs(pct - 0.0824) < 0.01,
          f'{pct}')

    # 外汇 fx_：时间,...,名称,涨跌幅
    p, pct, t = _parse_line('fx_susdcny',
                            '18:22:01,6.6614,6.6616,6.6660,41.0,6.6640,6.6649,'
                            '6.6608,6.6616,在岸人民币,-0.0660,-0.0044')
    check('外汇涨跌幅取第11位', pct is not None and abs(pct + 0.066) < 1e-6, f'{pct}')

    # ------------------------------------------------------------------
    print('\n[5] 快讯公司名精确匹配（模糊匹配会把同名公司吸进来）')
    items = [
        NewsItem(title='【海螺新材：前三季营收37.54亿元 净亏损6129.14万元】海螺新材公告称',
                 source='flash'),
        NewsItem(title='【上纬新材研发投入激增11.8倍 主营业务盈利增加4.91%】上纬新材(688585)',
                 source='flash'),
        NewsItem(title='【康欣新材：前三季营收4.28亿元 净亏损1.98亿元】', source='flash'),
        NewsItem(title='【宏柏新材：前三季度营收12.57亿元 同比增19.94%】', source='flash'),
        NewsItem(title='【贵州茅台：前三季度净利增长15%】', source='flash'),
    ]
    hit = filter_flash_by_company(items, ['海螺新材'])
    check('只命中海螺新材自己', len(hit) == 1 and '海螺新材' in hit[0].title,
          f'命中 {len(hit)} 条')
    hit2 = filter_flash_by_company(items, ['贵州茅台'])
    check('茅台精确命中', len(hit2) == 1, f'命中 {len(hit2)} 条')
    check('空名单返回空', filter_flash_by_company(items, []) == [])

    # ------------------------------------------------------------------
    print('\n[6] 时效衰减：盘后新消息权重最高')
    check('14小时内不衰减', abs(_time_decay(2) - 1.0) < 1e-6)
    check('24小时内衰减到0.7', abs(_time_decay(20) - 0.7) < 1e-6)
    check('一周以上几乎归零', _time_decay(200) <= 0.15, f'{_time_decay(200)}')

    # ------------------------------------------------------------------
    print('\n[7] 综合打分：消息面权重最高，方向要带得动结论')
    # 构造：重大利空消息 + 中性外围 + 中性技术
    items = [NewsItem(title='关于收到立案调查通知书的公告', source='announcement',
                      relevance='direct', published=recent_ts(2))]
    r = analyze_news('sz.000001', items, name='平安银行', use_llm=False)
    check('重大利空 → 消息面明显为负', r.news_score <= -30, f'{r.news_score}')

    items = [NewsItem(title='股东拟减持不超过2%股份', source='announcement',
                      relevance='direct', published=recent_ts(2))]
    r2 = analyze_news('sz.000001', items, name='平安银行', use_llm=False)
    check('减持利空弱于立案调查', r2.news_score > r.news_score,
          f'减持{r2.news_score} vs 立案{r.news_score}')

    # 市场噪音不该进个股分
    noise = [NewsItem(title='收评：双创指数均跌超4%', source='news', relevance='market')]
    r3 = analyze_news('sz.000001', noise, name='平安银行', use_llm=False)
    check('大盘噪音不计入个股分', abs(r3.news_score) < 1e-6, f'{r3.news_score}')

    # ------------------------------------------------------------------
    print('\n[8] 技术分：站上所有均线且高位放量应为正')
    ctx = StockContext(code='sz.000001', ok=True, close=12.0, ma5=11.0, ma20=10.5,
                       ma60=10.0, pos_in_range=90, pct_chg=3.0, volume_ratio=1.5)
    s_hi = tech_score(ctx)
    ctx_lo = StockContext(code='sz.000001', ok=True, close=9.0, ma5=10.0, ma20=10.5,
                          ma60=11.0, pos_in_range=10, pct_chg=-3.0, volume_ratio=1.5)
    s_lo = tech_score(ctx_lo)
    check('强势状态技术分为正', s_hi > 0, f'{s_hi}')
    check('弱势状态技术分为负', s_lo < 0, f'{s_lo}')
    check('两者方向相反且拉开', s_hi - s_lo > 60, f'{s_hi} vs {s_lo}')
    check('技术分落在 ±100 内', abs(s_hi) <= 100 and abs(s_lo) <= 100)

    # ------------------------------------------------------------------
    print('\n[9] 无消息时必须显式标注，不能冒充"确认中性"')
    r4 = analyze_news('sh.600519', [], name='贵州茅台', use_llm=False)
    check('no_news 标记', r4.no_news is True)
    check('判定为无可分析消息', '无' in r4.verdict, r4.verdict)

    # ------------------------------------------------------------------
    print('\n[10] 词典边界：裸"无"误伤 / ST大小写 / 无限售解禁')
    v = score_with_rules(NewsItem(title='无锡子公司中标3.2亿元重大项目', source='announcement'))
    check('地名"无锡"不得触发否定压分', abs(v.polarity - 2.5) < 1e-6, f'{v.polarity:+.1f}')
    v = score_with_rules(NewsItem(title='关于公司股票被实施ST风险警示的公告', source='announcement'))
    check('大写ST命中退市风险', v.polarity <= -4.0, f'{v.polarity:+.1f} / {v.category}')
    v = score_with_rules(NewsItem(title='Q1 results: 1st quarter preview', source='news'))
    check('英文"1st"不得误命中st', abs(v.polarity) < 1e-6, f'{v.polarity:+.1f}')
    v = score_with_rules(NewsItem(title='关于无限售条件流通股上市流通的公告', source='announcement'))
    check('"无限售条件流通股上市流通"判解禁', abs(v.polarity + 2.0) < 1e-6,
          f'{v.polarity:+.1f} / {v.category}')

    # ------------------------------------------------------------------
    print('\n[11] 数据滞后按交易日算：周一早上对上周五 = 1，不报警')
    from premarket.stock_context import trading_day_lag
    # 2026-10-26 是周一，2026-10-23 是周五
    check('周五→周一 滞后1个交易日', trading_day_lag('2026-10-23', pd_ts('2026-10-26')) == 1,
          f'{trading_day_lag("2026-10-23", pd_ts("2026-10-26"))}')
    check('周三→下周一 滞后3个交易日（真滞后）',
          trading_day_lag('2026-10-21', pd_ts('2026-10-26')) == 3)

    # ------------------------------------------------------------------
    print('\n[12] 板块维度：行业识别、关键词、过滤')
    from premarket.sector import (
        SectorContext, filter_sector_news, sector_keywords, pick_industry, _secid,
    )
    kws = sector_keywords('美容护理')
    check('行业词含同义词', '化妆品' in kws and '美容护理' in kws, str(kws))
    # 泛词必须剔除：'品牌' 曾把「东风汽车/宁德时代」「惠发食品」全吸进化妆品板块
    kws_b = sector_keywords('品牌化妆品', ['品牌化妆品', '化妆品', '美容护理'])
    check('泛词"品牌"不进关键词', '品牌' not in kws_b, str(kws_b))
    check('单字关键词不进（"乳"误伤面太大）',
          all(len(k) >= 2 for k in sector_keywords('饮料制造')), str(sector_keywords('饮料制造')))
    # 行业链关键词：宽泛行业名也要能用（"食品饮料"之于白酒）
    kws_chain = sector_keywords('白酒Ⅱ', ['白酒Ⅱ', '白酒Ⅲ', '食品饮料'])
    check('行业链宽泛词纳入', '食品饮料' in kws_chain, str(kws_chain))

    # 东财顺序：行业（细分→宽泛）→ 地区板块 → 概念标签
    bds = [{'code': 'BK1498', 'name': '品牌化妆品', 'pct': -2.04},
           {'code': 'BK1252', 'name': '化妆品', 'pct': -2.86},
           {'code': 'BK1035', 'name': '美容护理', 'pct': -2.63},
           {'code': 'BK0158', 'name': '湖南板块', 'pct': -2.05},
           {'code': 'BK0683', 'name': '婴童概念', 'pct': -1.84}]
    chain = pick_industry(bds)
    check('切出行业链、剔除地区与概念',
          chain == ['品牌化妆品', '化妆品', '美容护理'], str(chain))
    check('无地区板块时用黑名单兜底',
          pick_industry([{'code': 'A', 'name': '软件开发', 'pct': 0},
                         {'code': 'B', 'name': '融资融券', 'pct': 0}]) == ['软件开发'])

    # secid 推导（东财 1=沪 0=深/北）
    check('secid 沪市', _secid('sh.600519') == '1.600519', _secid('sh.600519'))
    check('secid 深市', _secid('sz.300740') == '0.300740', _secid('sz.300740'))
    check('secid 裸代码', _secid('002612') == '0.002612', _secid('002612'))

    # 离线模式不能偷偷联网：否则 --no-network 下板块分会用实时数据，
    # 而且 0.20 的权重被一个"取不到=0分"的维度白占
    from premarket.sector import resolve_sector
    off = resolve_sector('sz.300454', use_network=False)
    check('离线模式不取数（ok=False 退回三档权重）', off.ok is False, str(off.note))
    check('离线模式板块分为0', off.score == 0.0)

    fl = [NewsItem(title='化妆品零售额同比增长12%，美妆行业景气度回升', source='flash'),
          NewsItem(title='【水羊股份：前三季度净利增长20%】', source='flash')]
    hits = {}
    sec = filter_sector_news(fl, kws, exclude_titles={'【水羊股份：前三季度净利增长20%】'},
                            hits_out=hits)
    check('板块消息过滤（排除个股direct）', len(sec) == 1 and '化妆品' in sec[0].title,
          f'{len(sec)}条')
    check('记录命中关键词（报告要展示，便于核对）', hits.get(fl[0].title), str(hits))

    # 板块级 vs 单家公司：单家三季报不能和行业政策等量齐观
    from premarket.sector import classify_sector_news
    cases = [
        ('【中国软件：2026年第三季度净利润亏损2139.7万元】', 'company'),
        ('【福昕软件：前三季度营收7.24亿元 净利同比增3292.39%】', 'company'),
        ('工信部发布软件行业高质量发展规划', 'wide'),
        ('化妆品零售额同比增长12%，美妆行业景气度回升', 'wide'),
        ('【国家药监局批准一款防晒类化妆品新原料】', 'wide'),
    ]
    for t, want in cases:
        got = classify_sector_news(NewsItem(title=t, source='flash'))
        check(f'{want:8s} {t[:22]}', got == want, got)

    # ------------------------------------------------------------------
    print('\n[13] alpha 分：剔除外围，多票比较不被大盘绑架')
    from premarket.analyzer import analyze
    from premarket.global_market import GlobalSnapshot
    from premarket.sentiment import SentimentResult
    snap = GlobalSnapshot(fetched_at='t', env_score=80.0, env_verdict='外围显著偏暖')
    senti0 = SentimentResult(code='x', news_score=0.0, verdict='消息面中性')
    ctx_ok = StockContext(code='x', ok=True, close=10, ma5=10, ma20=10, ma60=10,
                          pos_in_range=50, pct_chg=0, volume_ratio=1.0)
    sec_ok = SectorContext(industry='软件开发', board_pct=0.5, score=10.0, ok=True)
    v = analyze('x', '测试', snap, senti0, ctx_ok, sector=sec_ok)
    check('无消息的票 alpha 分接近 0', abs(v.alpha_score) < 25, f'{v.alpha_score}')
    check('但综合分被外围推高', v.total_score > v.alpha_score,
          f'综合{v.total_score} vs alpha{v.alpha_score}')
    v2 = analyze('x', '测试', snap, senti0, ctx_ok, sector=None)
    check('板块缺失时自动退回三维权重', v2.sector_score == 0.0)

    # ------------------------------------------------------------------
    print('\n[14] 快讯宽松通道：无【】条目按全称/安全核心词收，黑名单核心词不收')
    from premarket.news import filter_flash_loose
    fl2 = [
        NewsItem(title='贵州茅台前三季度净利润同比增长15%', source='flash'),
        NewsItem(title='【上纬新材：营收翻倍】', source='flash'),          # 有【】走精确通道
        NewsItem(title='新材概念股集体走强，多股涨停', source='flash'),    # 黑名单核心词
    ]
    loose = filter_flash_loose(fl2, '贵州茅台', ['茅台'])
    check('无【】+安全核心词 命中', len(loose) == 1 and '茅台' in loose[0].title, f'{len(loose)}')
    loose2 = filter_flash_loose(fl2, '海螺新材', ['新材'])
    check('黑名单核心词"新材"不收', len(loose2) == 0, f'{len(loose2)}')

    print('\n[15] 数据新鲜度闸门：收盘后不允许丢当天K线')
    from premarket.stock_context import allowed_lag, trading_day_lag
    # 2026-10-29 是周四（交易日），10-31 是周六
    check('交易日盘中(11:00) 允许滞后1', allowed_lag(pd_ts('2026-10-29 11:00')) == 1)
    check('交易日盘前(08:30) 允许滞后1', allowed_lag(pd_ts('2026-10-29 08:30')) == 1)
    check('交易日收盘后(20:00) 允许滞后0', allowed_lag(pd_ts('2026-10-29 20:00')) == 0)
    check('周六 允许滞后1（周五数据即最新）', allowed_lag(pd_ts('2026-10-31 10:00')) == 1)
    # 事故复现：10-29 盘后，库里只有 10-28 的数据 -> lag=1，旧逻辑(>=2)漏报
    lag = trading_day_lag('2026-10-28', pd_ts('2026-10-29'))
    check('复现：10-29 盘后丢失当天数据 lag=1', lag == 1, f'lag={lag}')
    check('旧阈值 >=2 会漏报', not (lag >= 2))
    check('新阈值 0 能抓住', lag > allowed_lag(pd_ts('2026-10-29 20:00')))

    # ==================== 16. 邮件正文 HTML 化 ====================
    # 邮件客户端不认 CSS 变量 / display:grid / body 背景色，这三条都会导致
    # 「白底 + 浅灰字」的几乎不可读，或者卡片竖排成一列。
    print('\n[16] 邮件正文 HTML 化')
    from types import SimpleNamespace
    from premarket.global_market import GlobalSnapshot
    from premarket.report import render_email_html, render_html, _env_bar
    from utils.email_sender import plain_to_html, send_email

    rep = SimpleNamespace(
        generated_at='2026-10-29 20:00', avg_score=12.3, stocks=[],
        snap=GlobalSnapshot(quotes={}, bullets=[], sector_hints=[],
                            env_score=12.3, env_verdict='中性',
                            fetched_at='2026-10-29 20:00'))

    mail = render_email_html(rep)
    check('邮件 HTML：无 var(--) 残留', 'var(--' not in mail, f"{mail.count('var(--')} 处未展开")
    check('邮件 HTML：外层套 bgcolor（客户端会丢弃 body 背景）', 'bgcolor="#0d1117"' in mail)
    check('邮件 HTML：grid 降级为 inline-block', 'display:inline-block' in mail)
    check('邮件 HTML：仍带“不是投资建议”声明', '不是投资建议' in mail)
    check('邮件 HTML：div 闭合平衡', mail.count('<div') == mail.count('</div>'),
          f"{mail.count('<div')}/{mail.count('</div>')}")
    check('邮件 HTML：table 闭合平衡', mail.count('<table') == mail.count('</table>'))

    web = render_html(rep)
    check('网页版不受影响（仍用 CSS 变量）',
          'var(--bg)' in web and 'bgcolor="#0d1117"' not in web)

    bar = _env_bar(45.0, email=True)
    check('邮件条形图不用绝对定位（客户端不支持）',
          'position:absolute' not in bar and '<td' in bar)
    check('邮件条形图 td 显式清零 padding（否则被全局 td 规则撑形）', 'padding:0' in bar)

    plain = plain_to_html('code  price\n600000  9.99', '标题')
    check('纯文本转 HTML 保留等宽对齐', 'white-space:pre-wrap' in plain and 'monospace' in plain)
    injected = plain_to_html('<script>alert(1)</script>')
    check('纯文本转 HTML 转义标签（防注入）',
          '&lt;script&gt;' in injected and '<script>' not in injected)
    check('无凭证时不发信直接返回 False', send_email('a@b.c', 's', 'x') is False)

    # 踩过的坑：字体名写双引号会把 style="..." 属性提前闭合，整条 font-family
    # 声明被浏览器静默丢弃 —— 页面不报错，只是字体变回默认、表格全错位。
    from utils.email_sender import _FONT, _MONO
    from premarket.report import _EMAIL_FONT
    check('字体名不含双引号（会截断 style 属性）',
          '"' not in _FONT and '"' not in _MONO and '"' not in _EMAIL_FONT,
          '含双引号')
    check('字符表格字体以 NSimSun 打头（中文严格 2 倍西文宽）',
          _MONO.split(',')[0] == 'NSimSun', _MONO.split(',')[0])

    # ==================== 17. 核心题材板块 ====================
    # 行业分类是基本面归属，个股在盘面上跟的常是另一条题材线：
    # 深信服行业归"软件开发"，但实际跟的是"网络安全/VPN/信创"。
    print('\n[17] 核心题材板块识别')
    from premarket.sector import pick_themes, sector_keywords

    _boards = [
        {'code': 'BK0737', 'name': '软件开发', 'pct': -4.21},
        {'code': 'BK1448', 'name': '横向通用软件', 'pct': -5.79},
        {'code': 'BK0153', 'name': '广东板块', 'pct': -3.51},
        {'code': 'BK0655', 'name': '网络安全', 'pct': -3.91},
        {'code': 'BK1104', 'name': '信创', 'pct': -3.72},
        {'code': 'BK0885', 'name': 'VPN', 'pct': -4.88},
        {'code': 'BK0579', 'name': '云计算', 'pct': -3.73},
        {'code': 'BK0596', 'name': '融资融券', 'pct': -2.75},
    ]
    th = pick_themes(_boards, ['软件开发', '计算机', '横向通用软件'])
    check('题材按映射表优先级取（不按板块返回顺序）',
          [t['name'] for t in th] == ['网络安全', '信创', 'VPN'], str([t['name'] for t in th]))
    check('题材不含地区板块', all('板块' not in t['name'] for t in th))
    check('题材拍到涨跌幅', th[0]['pct'] == -3.91)
    check('映射表外的行业返回空（安全降级）',
          pick_themes(_boards, ['未知行业']) == [])
    check('板块列表里没有该题材时跳过',
          [t['name'] for t in pick_themes(_boards, ['横向通用软件'])]
          == ['网络安全', '信创', '云计算'])

    kws = sector_keywords('软件开发', ['软件开发'], themes=['网络安全', '数据安全'])
    check('题材名进入板块关键词', '网络安全' in kws and '数据安全' in kws, str(kws))
    check('题材名不被泛词黑名单误删', '数据安全' in kws and '数据' not in kws)

    print('\n[18] 同业等权口径 + 板块分波动率归一化')
    from premarket import peers as P
    from premarket.sector import SectorContext, build_sector_context, _perf_denom

    # 匹配优先级：题材比行业具体。同为「软件开发」，深信服有网安题材 → 用网安篮子；
    # 用友网络没有 → 返回 None 而不是硬塞进网安篮子。
    check('题材优先于行业匹配',
          P.basket_for('软件开发', ['软件开发'], [{'name': '网络安全'}]) == '网络安全')
    check('软件开发无题材时不硬塞进网安篮子',
          P.basket_for('软件开发', ['软件开发'], []) is None)
    check('行业别名能落到篮子', P.basket_for('白酒Ⅲ', ['白酒Ⅱ', '白酒Ⅲ']) == '白酒')
    check('映射表外行业返回 None', P.basket_for('安防设备', ['计算机设备']) is None)

    snap = P.peer_snapshot('sz.300454', '软件开发', ['软件开发'],
                           [{'name': '网络安全'}])
    if snap is not None and snap.ok:
        check('同业快照取到', True, f'{snap.basket} n={len(snap.refs)} @{snap.as_of}')
        check('自身不在同业样本里（否则是自证）', 'sz.300454' not in snap.refs)
        check('σ 在合理区间', 0.5 <= snap.sigma <= 15, f'{snap.sigma}%')
        check('分母 = 2σ 且夹在 2~6', 2.0 <= snap.denom <= 6.0, f'{snap.denom}%')
        check('相对强弱 = 个股 − 同业中位',
              abs((snap.stock_pct - snap.median_pct) - snap.rel_pct) < 0.011,
              f'{snap.stock_pct} − {snap.median_pct} = {snap.rel_pct}')
        check('相对强弱有定性标签', bool(snap.rel_label()), snap.rel_label())
    else:
        check('同业快照取到', False, (snap.note if snap else 'None'))

    bank = P.peer_snapshot('sh.600036', '银行Ⅱ', ['银行', '股份制银行Ⅱ'])
    semi = P.peer_snapshot('sh.688981', '半导体', ['半导体'])
    if bank and bank.ok and semi and semi.ok:
        check('低波动板块分母更小（波动率归一化生效）',
              bank.denom < semi.denom, f'银行 {bank.denom}% < 半导体 {semi.denom}%')
    else:
        check('低波动板块分母更小（波动率归一化生效）', False, '快照缺失')

    check('同业不可用时退回 2×池化 σ = 4.0%', _perf_denom(None) == 4.0,
          str(_perf_denom(None)))

    # 同样跌 3%：银行（σ≈1.0）应触顶，半导体（σ≈3.0）不该触顶 → 分数不同
    a = build_sector_context('sh.600036', flash=[],
                             resolved=SectorContext(industry='银行', board_pct=-3.0, ok=True),
                             use_network=False)
    b = build_sector_context('sh.688981', flash=[],
                             resolved=SectorContext(industry='半导体', board_pct=-3.0, ok=True),
                             use_network=False)
    check('同一跌幅在不同波动板块得分不同', a.score < b.score,
          f'银行 {a.score:+.1f} vs 半导体 {b.score:+.1f}')
    check('评分口径可追溯（记录来源与分母）',
          a.perf_src == '板块指数' and a.perf_denom and a.perf_pct == -3.0,
          f'{a.perf_src} {a.perf_pct}% ÷ {a.perf_denom}%')

    # 板块指数取不到时用同业等权顶上（东财限流/离线场景）
    c = build_sector_context('sh.600036', flash=[],
                             resolved=SectorContext(industry='银行', ok=True),
                             use_network=False)
    check('板块指数缺失时用同业等权顶上',
          c.perf_src == '同业等权' and c.perf_pct is not None,
          f'{c.perf_src} {c.perf_pct}')

    from premarket.report import sector_peer_lines
    lines = sector_peer_lines(a)
    check('报告能展示同业与评分口径',
          any('同业等权' in x for x in lines) and any('评分口径' in x for x in lines),
          str(len(lines)) + ' 行')

    print('\n' + '=' * 72)
    print(f'结果: {PASS} passed, {FAIL} failed')
    print('=' * 72)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
