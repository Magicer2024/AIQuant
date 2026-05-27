"""
tests/test_sync_stats.py —— 北交所/ST 跳过统计
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestShouldSkipStats(unittest.TestCase):
    def setUp(self):
        from core.sync import _sync_stats
        _sync_stats.update({"total": 0, "success": 0, "failed": 0,
                            "skipped_bse": 0, "skipped_st": 0})

    def test_skip_bse_increments_stat(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("430001", "测试股票")
        self.assertTrue(result)
        self.assertEqual(_sync_stats["skipped_bse"], 1)
        self.assertEqual(_sync_stats["skipped_st"], 0)

    def test_skip_st_increments_stat(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("000001", "*ST测试")
        self.assertTrue(result)
        self.assertEqual(_sync_stats["skipped_st"], 1)

    def test_skip_delisted_increments_stat(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("000002", "退市股票")
        self.assertTrue(result)
        self.assertEqual(_sync_stats["skipped_st"], 1)

    def test_no_skip_does_not_increment(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("000001", "平安银行")
        self.assertFalse(result)
        self.assertEqual(_sync_stats["skipped_bse"], 0)
        self.assertEqual(_sync_stats["skipped_st"], 0)

    def test_skip_empty_code_or_name(self):
        from core.sync import _should_skip
        self.assertTrue(_should_skip("", "测试"))
        self.assertTrue(_should_skip("000001", ""))

    def test_sync_stats_reset(self):
        from core.sync import _sync_stats
        _sync_stats.update({"total": 0, "success": 0, "failed": 0,
                            "skipped_bse": 0, "skipped_st": 0})
        self.assertEqual(_sync_stats["skipped_bse"], 0)
        self.assertEqual(_sync_stats["skipped_st"], 0)


if __name__ == "__main__":
    unittest.main()
