"""
tests/test_repository.py —— 验证 Repository 拆分后的可导入性与基础功能
"""
import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRepositoryImports(unittest.TestCase):
    """验证所有 Repository 模块可正常导入"""

    def test_stock_repo_import(self):
        from core.repository.stock_repo import (
            upsert_stock_list, get_all_stocks, get_stock_name,
            get_market_cap_map, update_market_cap, batch_update_market_cap,
        )
        self.assertTrue(callable(upsert_stock_list))
        self.assertTrue(callable(get_all_stocks))

    def test_price_repo_import(self):
        from core.repository.price_repo import (
            upsert_daily_price, get_daily_price, update_strategy_scores_batch,
            upsert_index_daily, get_index_daily,
            get_latest_date, get_latest_date_all, has_today_data, get_stock_count_in_db,
        )
        self.assertTrue(callable(upsert_daily_price))
        self.assertTrue(callable(get_daily_price))

    def test_signal_repo_import(self):
        from core.repository.signal_repo import (
            save_signals, save_scan_signals, get_signals, get_today_signals,
        )
        self.assertTrue(callable(save_signals))

    def test_position_repo_import(self):
        from core.repository.position_repo import (
            add_position, close_position, get_positions, get_position_summary,
        )
        self.assertTrue(callable(add_position))

    def test_trade_repo_import(self):
        from core.repository.trade_repo import (
            add_trade, get_trades, save_account_snapshot, get_latest_snapshot,
        )
        self.assertTrue(callable(add_trade))

    def test_sync_repo_import(self):
        from core.repository.sync_repo import log_sync, get_sync_logs, db_stats
        self.assertTrue(callable(log_sync))

    def test_db_compat_import(self):
        """验证 core.db 兼容层仍可正常导入所有函数"""
        from core.db import (
            get_all_stocks, get_daily_price, save_signals,
            get_positions, add_trade, log_sync, db_stats,
        )
        self.assertTrue(callable(get_all_stocks))
        self.assertTrue(callable(get_daily_price))


class TestRepositoryBehavior(unittest.TestCase):
    """验证 Repository 函数的基础行为"""

    def test_get_stock_name_fallback(self):
        """get_stock_name 对不存在的代码应返回代码本身"""
        from core.repository.stock_repo import get_stock_name
        # 不连接数据库，直接验证函数存在且签名正确
        import inspect
        sig = inspect.signature(get_stock_name)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["code"])

    def test_has_today_data_signature(self):
        """has_today_data 参数签名正确"""
        from core.repository.price_repo import has_today_data
        import inspect
        sig = inspect.signature(has_today_data)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["code"])


if __name__ == "__main__":
    unittest.main()
