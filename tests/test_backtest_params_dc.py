"""
tests/test_backtest_params_dc.py — BacktestParams dataclass
"""
import unittest


class TestBacktestParamsDataclass(unittest.TestCase):
    def test_params_defaults(self):
        from backtest.engine import BacktestParams
        p = BacktestParams(
            rule_name="test", rule_id="1",
            conditions_json="{}", start_date="2024-01-01", end_date="2024-12-31"
        )
        self.assertEqual(p.holding_max, 20)
        self.assertEqual(p.stop_loss, -0.08)
        self.assertEqual(p.take_profit, 0.20)
        self.assertTrue(p.save)

    def test_params_custom(self):
        from backtest.engine import BacktestParams
        p = BacktestParams(
            rule_name="test", rule_id="2",
            conditions_json="{}", start_date="2024-01-01", end_date="2024-12-31",
            holding_max=10, stop_loss=-0.05, take_profit=0.15, save=False,
            sell_conditions_json='[]'
        )
        self.assertEqual(p.holding_max, 10)
        self.assertEqual(p.stop_loss, -0.05)
        self.assertEqual(p.take_profit, 0.15)
        self.assertFalse(p.save)
        self.assertEqual(p.sell_conditions_json, '[]')

    def test_run_backtest_accepts_params(self):
        import inspect
        from backtest.engine import run_backtest
        sig = inspect.signature(run_backtest)
        self.assertIn("params", sig.parameters)


if __name__ == "__main__":
    unittest.main()
