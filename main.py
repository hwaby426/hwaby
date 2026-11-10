"""hwaby 主入口 —— 只保留 4 项功能

1. 每日收盘后更新全市场 K 线 / 估值 / 快照 / 特征  → daily-update, build-features
2. 交易时段 1 分钟 MACD 盯盘 + 邮件信号 + 分时落库  → monitor, scheduler
3. 手动跑预测（分数 / 行业 / 消息面 / 技术位 / 公告）→ premarket, announce
4. 每交易日定时盘前分析配置股票池                    → scheduler

其余研究/回测/因子/ML/复盘模块已移除。精简前的完整代码可从
git 快照 backup/pre-trim-20261110 恢复。
"""
import sys
import subprocess
from pathlib import Path

import click
from loguru import logger

from config.settings import settings

BASE_DIR = Path(__file__).resolve().parent

logger.remove()
logger.add(sys.stderr,
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
           level="INFO")
logger.add(settings.LOG_DIR / "stock_{time:YYYYMMDD}.log",
           rotation="00:00", retention="30 days", encoding="utf-8")


@click.group()
def cli():
    """A股盯盘 / 日更 / 盘前分析"""


@cli.command(name='init-db')
def init_db_cmd():
    """初始化数据库表结构（首次部署跑一次）"""
    from db.init_db import init_db
    init_db()


@cli.command(name='daily-update')
@click.option('--date', default=None, help='目标交易日 YYYY-MM-DD，默认今天')
@click.option('--check', is_flag=True, help='只体检，不写库')
@click.option('--skip-features', is_flag=True, help='跳过特征构建')
def daily_update(date, check, skip_features):
    """腾讯通道一键日更：日线 / 估值 / 快照 / 特征（收盘后跑）

    \b
    正式入口与 WorkBuddy 16:30 自动化用的是同一份脚本：
      python tools/update_daily_tencent.py
    """
    cmd = [sys.executable, 'tools/update_daily_tencent.py']
    if date:
        cmd += ['--date', date]
    if check:
        cmd += ['--check']
    if skip_features:
        cmd += ['--skip-features']
    logger.info('日更：' + ' '.join(cmd[1:]))
    raise SystemExit(subprocess.run(cmd, cwd=str(BASE_DIR)).returncode)


@cli.command(name='build-features')
@click.option('--start', default='2025-06-27',
              help='输出区间起点，默认 2025-06-27（全市场横截面完整的那天）')
@click.option('--end', default=None, help='输出区间终点，默认到库内最新')
@click.option('--codes', default='', help='逗号分隔，只构建这些票（调试用）')
@click.option('--min-bars', default=25, type=int, help='少于这么多根 K 线的票跳过')
@click.option('--truncate', is_flag=True,
              help='先清空 stock_features 再写。改区间/修数据后必须加，否则旧行残留')
@click.option('--no-db', is_flag=True, help='只写 parquet，不落库')
@click.option('--no-parquet', is_flag=True,
              help='不写 parquet，只落库。日更时用——否则会把全量 parquet 覆盖成当天 1 天')
@click.option('--append-parquet', is_flag=True,
              help='新行与原 parquet 合并（按 code+trade_date 去重后保留新值），日更时用')
def build_features(start, end, codes, min_bars, truncate, no_db,
                   no_parquet, append_parquet):
    """构建全市场特征表（供日更链路 subprocess 调用）

    \b
    ⚠ 日更脚本固定调用 `main.py build-features --start D --end D --append-parquet`，
    命令名与这 4 个选项不能改，否则日更会静默丢特征。
    """
    from features.builder import build_all, save_parquet, save_to_db, create_table
    import pandas as pd

    code_list = [c.strip() for c in codes.split(',') if c.strip()] or None
    click.echo(f'构建特征 {start} ~ {end or "最新"}'
               f'{"（仅 " + str(len(code_list)) + " 只）" if code_list else "（全市场）"} ...')
    feat = build_all(start=start, end=end, codes=code_list, min_bars=min_bars)
    if feat.empty:
        click.echo('没有数据，检查 daily_kline 是否已更新')
        return

    if no_parquet:
        click.echo('已跳过 parquet（--no-parquet）')
    else:
        # ⚠ 用 to_save 而不是覆盖 feat：feat 还要拿去落库（161 万行全表重写要几小时）。
        to_save = feat
        if append_parquet:
            from features.builder import load_features
            try:
                old = load_features()
            except Exception as e:
                click.echo(f'旧 parquet 读不到（{e}），本次只写新行')
                old = None
            if old is not None and not old.empty:
                to_save = pd.concat([old, feat], ignore_index=True)
                to_save = to_save.drop_duplicates(subset=['code', 'trade_date'],
                                                  keep='last').reset_index(drop=True)
                click.echo(f'parquet 与旧文件合并后 {len(to_save):,} 行'
                           f'（本次落库仍只有 {len(feat):,} 行）')
        path = save_parquet(to_save)
        click.echo(f'parquet: {path}  {len(to_save):,} 行')

    if no_db:
        click.echo('已跳过落库（--no-db）')
        return

    if truncate:
        from db.database import engine
        from sqlalchemy import text
        create_table()
        with engine.begin() as conn:
            conn.execute(text('TRUNCATE TABLE stock_features'))
        click.echo('stock_features 已清空')

    n = save_to_db(feat)
    click.echo(f'落库完成 {n:,} 行')


@cli.command(name='premarket')
@click.option('--codes', default=None, help='股票代码，逗号分隔，如 sh.600519,sz.000001')
@click.option('--pool', is_flag=True, default=False, help='使用 .env 里配置的股票池')
@click.option('--max-stocks', default=20, type=int, help='用股票池时最多取多少只，默认20')
@click.option('--hours', default=16.0, type=float, help='消息时间窗（小时），默认16')
@click.option('--news-file', default=None, help='本地消息文件，一行一条，可写 [代码] 前缀')
@click.option('--no-llm', is_flag=True, default=False, help='关闭大模型精判，只用规则词典')
@click.option('--no-network', is_flag=True, default=False, help='离线模式：不抓外围与消息')
@click.option('--no-html', is_flag=True, default=False, help='不生成 HTML 报告')
@click.option('--no-email', is_flag=True, default=False, help='不发送邮件')
@click.option('--no-save', is_flag=True, default=False, help='不把评分写入 premarket_scores')
@click.option('--trade-date', default=None,
              help='评分指向的交易日，默认自动推断；预测明天请显式指定')
@click.option('--out', default=None, help='HTML 输出路径')
def premarket(codes, pool, max_stocks, hours, news_file, no_llm, no_network,
              no_html, no_email, no_save, trade_date, out):
    """盘前分析：外围 + 消息面 + 板块 + 技术位 → 综合分与开盘应对预案

    \b
    例：
      python main.py premarket --codes sz.300454
      python main.py premarket --codes sz.300454 --trade-date 2026-11-11
      python main.py premarket --pool --max-stocks 10

    \b
    评分默认落 premarket_scores，供事后回填涨跌算准确率；
    --trade-date 指定的是「预测指向的交易日」。
    大模型精判可选：.env 配 PREMARKET_LLM_* ，没配自动退回规则判定。
    """
    from datetime import datetime
    from data_fetcher.baostock_fetcher import normalize_code
    from premarket.service import run_premarket, save_scores, send_report_email
    from premarket.report import render_terminal, save_html

    if codes:
        code_list = [normalize_code(c.strip()) for c in codes.split(',') if c.strip()]
    elif pool:
        code_list = [normalize_code(c) for c in settings.get_stock_pool()][:max_stocks]
    else:
        logger.error('请指定 --codes 或 --pool')
        return

    if not code_list:
        logger.error('股票列表为空')
        return

    logger.info(f'盘前分析 {len(code_list)} 只标的，消息时间窗 {hours} 小时')
    t_run = datetime.now()
    rep = run_premarket(
        code_list,
        use_network=not no_network,
        use_llm=not no_llm,
        news_hours=hours,
        local_news=news_file,
    )

    print()
    print(render_terminal(rep))

    if not no_save:
        n = save_scores(rep, trade_date=trade_date, use_llm=not no_llm, run_at=t_run)
        if n:
            print(f'\n评分已落库 {n} 条（premarket_scores），可事后回填涨跌算准确率')

    html_path = ''
    if not no_html:
        try:
            html_path = save_html(rep, out)
            print(f'\nHTML 报告已生成: {html_path}')
        except Exception as e:
            logger.warning(f'HTML 生成失败: {e}')

    if not no_email:
        send_report_email(rep, html_path)


@cli.command(name='announce')
@click.option('--code', '-c', multiple=True, required=True, help='6 位代码，可重复传多个')
@click.option('--date', default=None, help='单日 YYYY-MM-DD')
@click.option('--start', default=None, help='起始日期')
@click.option('--end', default=None, help='结束日期，默认今天')
@click.option('--days', default=None, type=int, help='最近 N 自然日（含今天）')
@click.option('--save', is_flag=True, help='写入 announcements 表')
def announce_cmd(code, date, start, end, days, save):
    """个股公告查询 —— 异动归因用，不是预测

    \b
      python main.py announce -c 300454 --date 2026-11-04
      python main.py announce -c 300454 --days 10

    \b
    深市走深交所官方接口，沪市走巨潮；单票查询不会触发东财 IP 限流。
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td
    from tools.announce_lookup import lookup, render, save as _save

    if date:
        s = e = date
    else:
        e = end or _date.today().isoformat()
        if start:
            s = start
        elif days:
            s = (_dt.strptime(e, '%Y-%m-%d').date() - _td(days=days - 1)).isoformat()
        else:
            s = (_dt.strptime(e, '%Y-%m-%d').date() - _td(days=30)).isoformat()

    click.echo(f'\n区间 {s} ~ {e}')
    df = lookup(list(code), s, e)
    render(df)
    if save and not df.empty:
        click.echo(f'入库 {_save(df)} 条（重复自动跳过）')


@cli.command()
@click.option('--once', is_flag=True, help='只轮询一次（调试/收盘后补数据）')
@click.option('--no-mail', is_flag=True, help='不发邮件，只落库与打印')
def monitor(once, no_mail):
    """启动 1 分钟 MACD 盯盘（配置股票池、邮件信号、分时落库）

    \b
    交易时段常驻，按 1min K 线判定：
      卖出：DIF>0 且 DEA>0 且 DIF 处滚动高位 → 死叉
      买入：DIF<0 且 DEA<0 且 DEA 处滚动低位 → 金叉
    """
    import signal
    from scheduler.macd_monitor import MacdMonitor

    m = MacdMonitor(send_mail=not no_mail)
    if once:
        m.poll_once()
        return

    def _stop(signum, frame):
        logger.info('收到停止信号，正在退出...')
        m.stop()

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass

    try:
        m.run()
    except KeyboardInterrupt:
        m.stop()
    logger.info('盯盘进程已退出')


@cli.command()
def scheduler():
    """启动固定进程：定时盘前 + 定时日更 + 交易时段启停盯盘"""
    from scheduler.scheduler_service import run_scheduler
    run_scheduler()


if __name__ == '__main__':
    cli()
