"""1 分钟 MACD 盯盘链路回归测试（离线：不联网、不连库）。

锁住 5 个最容易悄悄坏掉的点：
1. 代码格式：写 min_kline 一律带点，否则同表两份数据、回测查不到
2. 滚动分位无未来函数：改尾部价格不能影响前面的信号
3. 信号判据：双正高位死叉卖 / 双负低位金叉买，预热期不出信号
4. 归档幂等键与 code/period 口径
5. 盯盘进程：首次只建基线不补发历史信号、只有新增 bar 才触发、同向冷却
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals import macd_intraday as mm
import scheduler.macd_monitor as mon
from scheduler.macd_monitor import MacdMonitor


def make_df(n: int = 400) -> pd.DataFrame:
    """确定性序列：实测能同时产生高位死叉与低位金叉。"""
    x = np.arange(n)
    close = 100 + 8 * np.sin(x / 14.0) + 3 * np.sin(x / 3.0)
    return pd.DataFrame({
        'dt': pd.date_range('2026-11-05 09:31', periods=n, freq='min'),
        'date': pd.date_range('2026-11-05', periods=n, freq='min').date,
        'open': close, 'high': close + 0.2, 'low': close - 0.2,
        'close': close, 'volume': 1000.0, 'amount': close * 1000,
    })


class TestCodeFormat(unittest.TestCase):
    def test_db_code_always_dotted(self):
        for raw in ('sz300454', 'sz.300454', '300454', 'SZ300454'):
            self.assertEqual(mm.db_code(raw), 'sz.300454', raw)
        self.assertEqual(mm.db_code('600519'), 'sh.600519')
        self.assertEqual(mm.norm_code('sz.300454'), 'sz300454')


class TestMacd(unittest.TestCase):
    def test_matches_pandas_ewm(self):
        c = make_df()['close']
        dif, dea, hist = mm.macd(c)
        exp_dif = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        exp_dea = exp_dif.ewm(span=9, adjust=False).mean()
        pd.testing.assert_series_equal(dif, exp_dif)
        pd.testing.assert_series_equal(dea, exp_dea)
        pd.testing.assert_series_equal(hist, (exp_dif - exp_dea) * 2)

    def test_prefix_unchanged_when_tail_edited(self):
        """滚动分位必须只依赖过去：把尾段价格整体改掉，前 250 根信号一根不变。"""
        df = make_df()
        df2 = df.copy()
        df2.loc[df2.index[250:], 'close'] *= 1.5
        a = mm.compute_signals(df)
        b = mm.compute_signals(df2)
        for col in ('dif', 'dea', 'hist', 'pct_dif', 'pct_dea', 'sell', 'buy'):
            pd.testing.assert_series_equal(
                a[col].iloc[:250].reset_index(drop=True),
                b[col].iloc[:250].reset_index(drop=True), check_names=False)

    def test_same_as_prefix_recompute(self):
        """另测一种：只喂前 250 根，结果应与全量前 250 根逐项一致。"""
        df = make_df()
        full = mm.compute_signals(df)
        head = mm.compute_signals(df.iloc[:250].reset_index(drop=True))
        for col in ('dif', 'dea', 'hist', 'pct_dif', 'pct_dea', 'sell', 'buy'):
            pd.testing.assert_series_equal(
                full[col].iloc[:250].reset_index(drop=True),
                head[col].reset_index(drop=True), check_names=False)

    def test_signal_conditions(self):
        s = mm.compute_signals(make_df())
        self.assertGreater(int(s.sell.sum()), 0, '构造序列应产生卖出信号')
        self.assertGreater(int(s.buy.sum()), 0, '构造序列应产生买入信号')
        for r in s[s.sell].itertuples(index=False):
            self.assertGreater(r.dif, 0)
            self.assertGreater(r.dea, 0)
            self.assertGreaterEqual(r.pct_dif, mm.HI_Q)
            self.assertLess(r.hist, 0, '死叉：柱体应为负')
        for r in s[s.buy].itertuples(index=False):
            self.assertLess(r.dif, 0)
            self.assertLess(r.dea, 0)
            self.assertLessEqual(r.pct_dea, mm.LO_Q)
            self.assertGreater(r.hist, 0, '金叉：柱体应为正')
        self.assertFalse(s.iloc[:mm.WARMUP][['sell', 'buy']].to_numpy().any(),
                         'EMA 预热期不得出信号')

    def test_pct_is_one_based_rank_in_window(self):
        s = mm.compute_signals(make_df())
        self.assertTrue(s['pct_dif'].iloc[:19].isna().all(), 'min_periods=20 前应为 NaN')
        v = s['pct_dif'].dropna()
        self.assertGreater(v.min(), 0)
        self.assertLessEqual(v.max(), 1)


class TestArchive(unittest.TestCase):
    def test_rows_use_dotted_code_and_unique_key(self):
        rows = mm.archive_rows(make_df(5), 'sz300454', 1)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r['code'] == 'sz.300454' for r in rows))
        self.assertTrue(all(r['period'] == '1min' for r in rows))
        keys = [(r['code'], r['period'], r['t']) for r in rows]
        self.assertEqual(len(set(keys)), len(keys), '唯一键必须不重复')
        self.assertEqual(mm.archive_rows(make_df(5).iloc[:0], 'sz300454'), [])


class TestMonitor(unittest.TestCase):
    def _fake_sig(self, df, last_sell=False):
        n = len(df)
        return pd.DataFrame({
            'dt': df['dt'].to_numpy(), 'close': df['close'].to_numpy(),
            'dif': np.zeros(n), 'dea': np.zeros(n), 'hist': np.zeros(n),
            'pct_dif': np.full(n, 0.9), 'pct_dea': np.full(n, 0.1),
            'sell': np.array([False] * (n - 1) + [last_sell]),
            'buy': np.zeros(n, bool),
        })

    def test_baseline_does_not_replay_history(self):
        df = make_df(120)
        m = MacdMonitor(stock_pool=['sz300454'], send_mail=False, cooldown_min=0)
        m._emit = MagicMock()
        sig = self._fake_sig(df, last_sell=True)   # 最后一根就有信号
        with patch.object(mm, 'load', return_value=(df.copy(), 'test')), \
             patch.object(mm, 'compute_signals', return_value=sig), \
             patch.object(mm, 'archive', return_value=0), \
             patch.object(mon, 'get_last_min_kline_time', return_value=None):
            m.poll_once()
        m._emit.assert_not_called()
        self.assertEqual(m._last_bar['sz.300454'], pd.Timestamp(df['dt'].iloc[-1]))

    def test_only_new_bar_emits(self):
        df = make_df(120)
        df2 = pd.concat([df, make_df(121).iloc[[120]]], ignore_index=True)
        df2['dt'] = pd.date_range('2026-11-05 09:31', periods=121, freq='min')
        m = MacdMonitor(stock_pool=['sz300454'], send_mail=False, cooldown_min=0)
        m._emit = MagicMock()
        s1 = self._fake_sig(df, last_sell=False)
        s2 = self._fake_sig(df2, last_sell=True)
        with patch.object(mm, 'load', side_effect=[(df.copy(), 'test'), (df2.copy(), 'test')]), \
             patch.object(mm, 'compute_signals', side_effect=[s1, s2]), \
             patch.object(mm, 'archive', return_value=0), \
             patch.object(mon, 'get_last_min_kline_time', return_value=None):
            m.poll_once()          # 建基线
            n_new = m.poll_once()  # 新增 1 根
        self.assertEqual(n_new, 1)
        self.assertEqual(m._emit.call_count, 1)
        self.assertEqual(m._emit.call_args[0][1], 'sell')

    def test_cooldown_suppresses_duplicate_mail(self):
        row = SimpleNamespace(dt=pd.Timestamp('2026-11-10 10:45'), close=123.45,
                              dif=0.4, dea=0.5, hist=-0.2,
                              pct_dif=0.92, pct_dea=0.08)
        with patch.object(mon.settings, 'SEND_EMAIL', True), \
             patch.object(mon.settings, 'EMAIL_TO', 'test@example.com'), \
             patch.object(mon.settings, 'EMAIL_FROM', 'test@example.com'), \
             patch.object(mon.settings, 'EMAIL_PASS', 'pw'), \
             patch('utils.email_sender.send_email') as send:
            m = MacdMonitor(stock_pool=['sz300454'], send_mail=True, cooldown_min=5)
            m._emit('sz.300454', 'sell', row)
            m._emit('sz.300454', 'sell', row)
            self.assertEqual(send.call_count, 1)
            self.assertIn('不构成投资建议', send.call_args.kwargs['content'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
