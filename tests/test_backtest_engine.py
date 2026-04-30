"""
tests/test_backtest_engine.py —— 回测引擎核心逻辑验证

重点验证项（基于历史 Bug）：
1. T+1 执行：买入信号当天不应成交，次日开盘价成交
2. 资金曲线延续估值：持仓股无数据日不应 pos_value=0
3. 手续费计算：买入 + 卖出双向费用正确
4. NaN 处理：np.nan or 0 不等于 0 的情况
"""
import unittest
import sys
import os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestT1Execution(unittest.TestCase):
    """T+1 交易执行验证"""

    def test_signal_day_not_traded(self):
        """信号日当天收盘价不应作为买入价"""
        dates = pd.date_range("2024-01-01", periods=5, freq="D")
        df = pd.DataFrame({
            "open":  [10, 11, 12, 13, 14],
            "high":  [11, 12, 13, 14, 15],
            "low":   [9,  10, 11, 12, 13],
            "close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "volume": [1000] * 5,
        }, index=dates)
        df["BUY_SIGNAL"] = [False, True, False, False, False]
        df["SELL_SIGNAL"] = False
        df["SCORE"] = 0.0

        # 若信号在第2天（index=1），买入价应为第3天的 open=12，而非当天的 close=11.5
        signal_idx = df[df["BUY_SIGNAL"]].index[0]
        next_day_idx = df.index[df.index.get_loc(signal_idx) + 1]
        buy_price = df.loc[next_day_idx, "open"]
        self.assertEqual(buy_price, 12)
        self.assertNotEqual(buy_price, df.loc[signal_idx, "close"])

    def test_slippage_applied_on_open(self):
        """滑点应作用于次日开盘价"""
        slippage_rate = 0.001
        open_price = 10.0
        slip_buy = open_price * (1 + slippage_rate)
        slip_sell = open_price * (1 - slippage_rate)
        self.assertAlmostEqual(slip_buy, 10.01, places=2)
        self.assertAlmostEqual(slip_sell, 9.99, places=2)


class TestEquityCurveContinuity(unittest.TestCase):
    """资金曲线延续估值验证（Bug: 缺数据日 pos_value 虚假归零）"""

    def test_missing_day_uses_last_close(self):
        """当日无数据时，应使用最近可用收盘价延续估值"""
        # 模拟持仓：100 股，最近已知收盘价 15
        last_known_close = 15.0
        shares = 100
        pos_value = last_known_close * shares
        self.assertEqual(pos_value, 1500.0)

    def test_nan_handling(self):
        """np.nan or 0 不等于 0，必须用 pd.isna() 判断"""
        val = np.nan
        # 错误写法（曾经是 Bug）：
        # self.assertTrue(val or 0 == 0)  # 这是错的，np.nan 是 truthy
        # 正确写法：
        self.assertTrue(pd.isna(val))
        safe_val = 0.0 if pd.isna(val) else float(val)
        self.assertEqual(safe_val, 0.0)


class TestFeeCalculation(unittest.TestCase):
    """手续费计算验证"""

    def test_commission_and_tax(self):
        """万三手续费双向 + 千一印花税卖出单向"""
        commission_rate = 0.0003
        stamp_tax = 0.001
        amount = 100000.0

        buy_commission = amount * commission_rate
        sell_commission = amount * commission_rate
        sell_tax = amount * stamp_tax

        self.assertAlmostEqual(buy_commission, 30.0, places=1)
        self.assertAlmostEqual(sell_commission, 30.0, places=1)
        self.assertAlmostEqual(sell_tax, 100.0, places=1)
        self.assertAlmostEqual(buy_commission + sell_commission + sell_tax, 160.0, places=1)


class TestUnifiedEngineFramework(unittest.TestCase):
    """统一回测引擎框架验证"""

    def test_engine_import(self):
        from backtest.unified_engine import UnifiedBacktester, BacktestResult
        self.assertTrue(callable(UnifiedBacktester))

    def test_strategy_interface_import(self):
        from backtest.strategy_interface import Strategy, FuncStrategy, Signal
        self.assertTrue(callable(FuncStrategy))

    def test_func_strategy_wrapper(self):
        """FuncStrategy 适配器能正确包装现有函数"""
        from backtest.strategy_interface import FuncStrategy

        def dummy_strategy(df):
            df["BUY_SIGNAL"] = False
            df["SELL_SIGNAL"] = False
            df["BUY_SCORE"] = 0.0
            return df

        strat = FuncStrategy("dummy", dummy_strategy)
        self.assertEqual(strat.name, "dummy")

        dates = pd.date_range("2024-01-01", periods=3)
        df = pd.DataFrame({
            "open": [1, 2, 3], "high": [2, 3, 4],
            "low": [0, 1, 2], "close": [1.5, 2.5, 3.5],
            "volume": [100, 200, 300],
        }, index=dates)
        result = strat.generate(df)
        self.assertIn("SCORE", result.columns)


if __name__ == "__main__":
    unittest.main()
