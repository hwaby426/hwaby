import time
import threading
from typing import List, Dict
from loguru import logger

from data_fetcher.sina_fetcher import get_realtime_quotes, is_trading_time, is_market_open_day, _convert_sina_code
from data_fetcher.min_kline_builder import MinKlineBuilder
from data_fetcher.min_kline_service import save_min_klines, get_min_kline_df
from data_fetcher.sina_fetcher import get_minute_kline
from signals.signal_service import save_signals
from config.settings import settings


class RealtimeMonitor:
    def __init__(self, stock_pool: List[str] = None):
        self.stock_pool = stock_pool or settings.get_stock_pool()
        self.builders: Dict[str, MinKlineBuilder] = {}
        self.periods = ['5min', '15min']
        self.running = False
        self._stop_event = threading.Event()

    def init_builders(self):
        logger.info(f"开始初始化实时监控，股票池 ({len(self.stock_pool)} 只):")
        for code in self.stock_pool:
            logger.info(f"  - {code}")
        for code in self.stock_pool:
            builder = MinKlineBuilder(code, self.periods)
            history_dfs = {}
            for period in self.periods:
                scale = int(period.replace('min', ''))
                df_sina = get_minute_kline(code, scale=scale, datalen=200)
                if not df_sina.empty:
                    history_dfs[period] = df_sina
            builder.init_from_history(history_dfs)
            self.builders[code] = builder
        logger.info(f"实时监控初始化完成，共 {len(self.builders)} 只股票")

    def poll_once(self):
        if not self.stock_pool:
            return
        quotes_df = get_realtime_quotes(self.stock_pool)
        if quotes_df.empty:
            return

        for _, row in quotes_df.iterrows():
            code = _convert_sina_code(row['code'])  # 转换格式：sz.002624 -> sz002624
            builder = self.builders.get(code)
            if builder is None:
                continue
            tick = {
                'price': row['price'],
                'volume': row['volume'],
                'amount': row['amount'],
                'time': row['time'],
            }
            newly_closed = builder.on_tick(tick)

            for period, bars in newly_closed.items():
                save_min_klines(code, period, bars)
                for bar in bars:
                    logger.info(
                        f"[分钟K线] {code} {period} 时间={bar.kline_time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"开={bar.open} 高={bar.high} 低={bar.low} 收={bar.close} 量={bar.volume}"
                    )

    def run(self, poll_interval: int = None):
        if poll_interval is None:
            poll_interval = settings.REALTIME_POLL_INTERVAL
        self.running = True
        self._stop_event.clear()
        self.init_builders()
        logger.info(f"开始实时监控，股票池: {len(self.stock_pool)} 只，轮询间隔: {poll_interval}s")

        while self.running and not self._stop_event.is_set():
            try:
                if is_market_open_day() and is_trading_time():
                    self.poll_once()
                else:
                    self._stop_event.wait(10)
                    continue
            except Exception as e:
                logger.error(f"实时轮询异常: {e}")
            self._stop_event.wait(poll_interval)

        logger.info("实时监控已停止")
        self._close_all_bars()

    def _close_all_bars(self):
        for code, builder in self.builders.items():
            closed = builder.close_current_bars()
            for period, bar in closed.items():
                save_min_klines(code, period, [bar])
        logger.info("所有未闭合K线已保存")

    def stop(self):
        self.running = False
        self._stop_event.set()
