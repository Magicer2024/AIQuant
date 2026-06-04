"""
tests/test_pagination.py -- ??? API ???
"""
import unittest

from app import app


class TestPaginationAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_daily_scores_returns_pagination(self):
        resp = self.client.get("/api/scoring/daily?page=1&per_page=20")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("pagination", data)
        self.assertIn("page", data["pagination"])
        self.assertIn("per_page", data["pagination"])
        self.assertIn("total", data["pagination"])
        self.assertIn("pages", data["pagination"])

    def test_daily_scores_default_page(self):
        resp = self.client.get("/api/scoring/daily")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["pagination"]["page"], 1)

    def test_daily_scores_per_page_capped(self):
        resp = self.client.get("/api/scoring/daily?per_page=999")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(data["pagination"]["per_page"], 10000)

    def test_daily_scores_page_out_of_range(self):
        resp = self.client.get("/api/scoring/daily?page=99999")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data["data"], list)

    def test_daily_scores_data_limit(self):
        resp = self.client.get("/api/scoring/daily?page=1&per_page=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(len(data["data"]), 5)


if __name__ == "__main__":
    unittest.main()
