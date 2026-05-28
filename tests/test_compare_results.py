"""
tests/test_compare_results.py —— 回测结果对比
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestCompareAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_compare_returns_list(self):
        resp = self.client.get("/api/backtest/compare?ids=1,2")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["data"], list)

    def test_compare_empty_ids_returns_empty(self):
        resp = self.client.get("/api/backtest/compare?ids=")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["data"], [])

    def test_compare_invalid_ids_skipped(self):
        resp = self.client.get("/api/backtest/compare?ids=abc,xyz")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data["data"], list)

    def test_compare_result_has_result_id(self):
        resp = self.client.get("/api/backtest/compare?ids=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        if data["data"]:
            self.assertIn("result_id", data["data"][0])
            self.assertIn("rule_name", data["data"][0])


if __name__ == "__main__":
    unittest.main()
