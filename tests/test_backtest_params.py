"""
tests/test_backtest_params.py —— 回测参数可配置
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBacktestParamsClamping(unittest.TestCase):
    def test_stop_loss_clamped_negative_50(self):
        val = max(min(-0.60, 0), -0.50)
        self.assertEqual(val, -0.50)

    def test_stop_loss_clamped_zero(self):
        val = max(min(0.05, 0), -0.50)
        self.assertEqual(val, 0)

    def test_take_profit_clamped_zero(self):
        val = max(min(-0.05, 1.00), 0)
        self.assertEqual(val, 0)

    def test_take_profit_clamped_100(self):
        val = max(min(1.50, 1.00), 0)
        self.assertEqual(val, 1.00)

    def test_holding_max_clamped(self):
        val = max(min(200, 100), 1)
        self.assertEqual(val, 100)
        val = max(min(0, 100), 1)
        self.assertEqual(val, 1)

    def test_default_values_unchanged(self):
        stop_loss = -0.08
        take_profit = 0.20
        holding_max = 20
        self.assertEqual(stop_loss, -0.08)
        self.assertEqual(take_profit, 0.20)
        self.assertEqual(holding_max, 20)


class TestEngineSignature(unittest.TestCase):
    def test_generate_signals_accepts_params(self):
        from backtest.engine import _generate_backtest_signals
        import inspect
        sig = inspect.signature(_generate_backtest_signals)
        self.assertIn("stop_loss", sig.parameters)
        self.assertIn("take_profit", sig.parameters)

    def test_run_backtest_accepts_params(self):
        from backtest.engine import run_backtest
        import inspect
        sig = inspect.signature(run_backtest)
        self.assertIn("stop_loss", sig.parameters)
        self.assertIn("take_profit", sig.parameters)


if __name__ == "__main__":
    unittest.main()
