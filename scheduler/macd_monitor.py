"""1 分钟 MACD 盯盘进程：交易时段常驻、分时落库、邮件信号。

设计要点：
  · 每轮重新拉全量 1min 线并重算 EMA（因果计算，历史值不变），
    避免进程内维护 EMA 状态——重启/漏拉都不会算歪。
  · 只有「新增的 bar」才参与信号判定与落库，靠 bar 时间戳去重，
    20 秒轮询也不会对同一根重复发邮件。
  · 首次启动只建立基线并归档，不补发历史信号，否则会把过去几天的
    交叉一次性刷进邮箱。
  · 同一标的同一方向有冷却期，默认 5 分钟。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

from config.settings import settings
from data_fetcher.min_kline_service import get_last_min_kline_time
from data_fetcher.sina_fetcher import is_market_open_day, is_trading_time
from signals import macd_intraday as mm

RISK_NOTE = ('提示：该信号只是 1 分钟 MACD 状态变化。历史检验未显示稳定超额收益，'
             '分钟级交易成本约 0.15% 名义金额，不构成投资建议。')


class MacdMonitor:
    def __init__(self, stock_pool=None, send_mail: bool = True,
                 poll_interval: int | None = None, cooldown_min: int | None = None):
        raw = stock_pool or settings.get_stock_pool()
        self.codes = [mm.db_code(c) for c in raw if str(c).strip()]
        # 尊重 .env 的 SEND_EMAIL 总开关；关掉时仍打印信号、只是不推邮件
        self.send_mail = bool(send_mail and settings.SEND_EMAIL and settings.EMAIL_TO)
        self.poll_interval = int(poll_interval or os.getenv('MACD_POLL_INTERVAL', '20'))
        self.cooldown_min = int(cooldown_min if cooldown_min is not None
                                else os.getenv('MACD_COOLDOWN_MIN', '5'))
        self._last_bar: dict[str, pd.Timestamp] = {}
        self._last_mail: dict[tuple[str, str], datetime] = {}
        self._stop = threading.Event()
        self.running = False

    # ────────────────────────── 单轮 ──────────────────────────

    def poll_once(self, emit_latest: bool = False) -> int:
        """轮询一轮，返回本轮新增的 bar 总数。"""
        total = 0
        for code in self.codes:
            try:
                total += self._poll_code(code, emit_latest)
            except Exception as e:
                logger.error(f'{code} 盯盘轮询失败：{type(e).__name__}: {e}')
        return total

    def _poll_code(self, code: str, emit_latest: bool = False) -> int:
        df, src = mm.load(code, 1)
        if df.empty or len(df) < mm.WARMUP + 1:
            return 0
        sig = mm.compute_signals(df)
        last_dt = pd.Timestamp(sig['dt'].iloc[-1])

        # 分时数据留存：只写库里没有的新 bar，UPSERT 保证幂等
        self._archive_new(df, code)

        prev = self._last_bar.get(code)
        self._last_bar[code] = last_dt
        if prev is None:
            r = sig.iloc[-1]
            logger.info(f'{code} 基线建立（{src}，最后一根 {last_dt:%m-%d %H:%M} '
                        f'收 {r.close:.2f} DIF {r.dif:+.4f} DEA {r.dea:+.4f}），'
                        f'不补发历史信号')
            if emit_latest:
                self._emit(code, 'sell' if r.sell else ('buy' if r.buy else None), r)
            return 0

        new = sig[sig['dt'] > prev]
        for r in new.itertuples(index=False):
            side = 'sell' if r.sell else ('buy' if r.buy else None)
            if side:
                self._emit(code, side, r)
        return len(new)

    def _archive_new(self, df: pd.DataFrame, code: str) -> None:
        try:
            last_db = get_last_min_kline_time(code, '1min')
        except Exception as e:
            logger.warning(f'{code} 读 min_kline 最后时间失败（{e}），本次跳过归档')
            return
        sub = df[df['dt'] > pd.Timestamp(last_db)] if last_db else df
        if sub.empty:
            return
        # 盘中最后一根可能还在形成中，也照写（下一轮同时间戳会 UPSERT 修正）
        try:
            n = mm.archive(sub, code, 1)
            logger.debug(f'{code} 分时归档 {n} 根 1min bar')
        except Exception as e:
            logger.error(f'{code} 分时归档失败：{type(e).__name__}: {e}')

    def _emit(self, code: str, side: str | None, r) -> None:
        if side is None:
            return
        now = datetime.now()
        key = (code, side)
        if self.cooldown_min > 0:
            prev = self._last_mail.get(key)
            if prev and now - prev < timedelta(minutes=self.cooldown_min):
                logger.debug(f'{code} {side} 信号在冷却期内，跳过')
                return
        self._last_mail[key] = now

        label = '卖出' if side == 'sell' else '买入'
        t = pd.Timestamp(r.dt)
        pct = r.pct_dif if side == 'sell' else r.pct_dea
        body = (
            f'标的：{code}\n'
            f'时间：{t:%Y-%m-%d %H:%M}（该分钟 K 线收盘）\n'
            f'价格：{r.close:.2f}\n'
            f'信号：{label}\n\n'
            f'判据：DIF 与 DEA 双{"正" if side == "sell" else "负"}，'
            f'DEA/DIF 处于{"高" if side == "sell" else "低"}位'
            f'（截至当根滚动 {mm.LOOKBACK} 根分位 {pct:.2f}）'
            f'，{"死叉" if side == "sell" else "金叉"}\n'
            f'DIF {r.dif:+.4f} / DEA {r.dea:+.4f} / 柱 {r.hist:+.4f}\n\n'
            f'{RISK_NOTE}\n'
        )
        logger.info(f'[{label}信号] {code} {t:%H:%M} @ {r.close:.2f} '
                    f'DIF {r.dif:+.4f} DEA {r.dea:+.4f}')
        if not self.send_mail:
            return
        try:
            from utils.email_sender import send_email
            send_email(
                to_email=settings.EMAIL_TO,
                subject=f'[MACD {label}信号] {code} {t:%m-%d %H:%M} @ {r.close:.2f}',
                content=body,
                smtp_server=settings.EMAIL_SMTP_SERVER,
                smtp_port=settings.EMAIL_SMTP_PORT,
                sender_email=settings.EMAIL_FROM or None,
                sender_password=settings.EMAIL_PASS or None,
            )
            logger.info(f'{code} {label}信号邮件已发送 → {settings.EMAIL_TO}')
        except Exception as e:
            logger.error(f'{code} 信号邮件发送失败：{type(e).__name__}: {e}')

    # ────────────────────────── 常驻 ──────────────────────────

    def run(self) -> None:
        self.running = True
        self._stop.clear()
        logger.info(f'1min MACD 盯盘启动：{len(self.codes)} 只 '
                    f'{", ".join(self.codes)}，轮询 {self.poll_interval}s，'
                    f'冷却 {self.cooldown_min} 分钟，邮件 {"开" if self.send_mail else "关"}')
        while not self._stop.is_set():
            if is_market_open_day() and is_trading_time():
                self.poll_once()
            self._stop.wait(self.poll_interval)
        self.running = False
        logger.info('1min MACD 盯盘已停止')

    def stop(self) -> None:
        self.running = False
        self._stop.set()
