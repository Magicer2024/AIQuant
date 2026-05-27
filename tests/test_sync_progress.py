"""
tests/test_sync_progress.py —— 数据同步进度显示
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestSyncProgressAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        from routes.sync import _sync_progress
        _sync_progress.update({"running": False, "current": 0, "total": 0,
                                "success": 0, "failed": 0, "message": "", "last_error": None})

    def test_get_progress_returns_dict(self):
        resp = self.client.get("/api/sync/progress")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("running", data["data"])
        self.assertIn("current", data["data"])
        self.assertIn("total", data["data"])
        self.assertIn("success", data["data"])
        self.assertIn("failed", data["data"])
        self.assertIn("last_error", data["data"])

    def test_concurrent_sync_returns_409(self):
        from routes.sync import _sync_progress
        _sync_progress["running"] = True
        resp = self.client.post("/api/sync")
        self.assertEqual(resp.status_code, 409)
        _sync_progress["running"] = False

    def test_progress_callback_updates_state(self):
        from routes.sync import _sync_progress, _progress_callback
        _progress_callback(50, 100, 48, 2)
        self.assertEqual(_sync_progress["current"], 50)
        self.assertEqual(_sync_progress["total"], 100)
        self.assertEqual(_sync_progress["success"], 48)
        self.assertEqual(_sync_progress["failed"], 2)


class TestProgressCallbackIntegration(unittest.TestCase):
    def test_callback_callable(self):
        from routes.sync import _progress_callback
        self.assertTrue(callable(_progress_callback))


if __name__ == "__main__":
    unittest.main()
