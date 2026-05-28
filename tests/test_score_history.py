"""
tests/test_score_history.py —— 打分历史趋势 API
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestScoreHistoryAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_history_returns_list(self):
        resp = self.client.get("/api/scoring/stock/000001/history?days=7")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["data"], list)

    def test_history_default_days(self):
        resp = self.client.get("/api/scoring/stock/000001/history")
        self.assertEqual(resp.status_code, 200)

    def test_history_invalid_days_defaults_to_30(self):
        resp = self.client.get("/api/scoring/stock/000001/history?days=999")
        self.assertEqual(resp.status_code, 200)

    def test_history_whitelist_days_values(self):
        for days in ("7", "30", "60", "90"):
            resp = self.client.get(f"/api/scoring/stock/000001/history?days={days}")
            self.assertEqual(resp.status_code, 200)

    def test_history_returns_rule_name_field(self):
        resp = self.client.get("/api/scoring/stock/000001/history?days=30")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        if data["data"]:
            self.assertIn("rule_name", data["data"][0])
            self.assertIn("score", data["data"][0])
            self.assertIn("trade_date", data["data"][0])


if __name__ == "__main__":
    unittest.main()
