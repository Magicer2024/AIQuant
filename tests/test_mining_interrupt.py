"""
tests/test_mining_interrupt.py —— 策略挖掘中断机制
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMiningStateModule(unittest.TestCase):
    def setUp(self):
        from strategy.mining_state import _reset_for_test
        _reset_for_test()

    def test_initial_state_not_running(self):
        from strategy.mining_state import get_mining_status
        status = get_mining_status()
        self.assertFalse(status["running"])
        self.assertFalse(status["stop_requested"])

    def test_request_stop_sets_flag(self):
        from strategy.mining_state import request_stop, get_mining_status
        request_stop()
        self.assertTrue(get_mining_status()["stop_requested"])

    def test_request_stop_idempotent(self):
        from strategy.mining_state import request_stop, get_mining_status
        request_stop()
        request_stop()
        self.assertTrue(get_mining_status()["stop_requested"])

    def test_reset_clears_stop_flag(self):
        from strategy.mining_state import request_stop, get_mining_status, _reset_for_test
        request_stop()
        _reset_for_test()
        self.assertFalse(get_mining_status()["stop_requested"])


class TestMiningStopAPI(unittest.TestCase):
    def setUp(self):
        from app import app
        app.config["TESTING"] = True
        self.client = app.test_client()
        from strategy.mining_state import _reset_for_test
        _reset_for_test()

    def test_stop_when_not_running(self):
        resp = self.client.post("/api/strategy/mine/stop")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])

    def test_stop_when_running(self):
        from strategy.mining_state import get_mining_status
        get_mining_status()["running"] = True
        resp = self.client.post("/api/strategy/mine/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(get_mining_status()["stop_requested"])

    def test_reset_after_stop(self):
        from strategy.mining_state import get_mining_status
        get_mining_status()["running"] = True
        self.client.post("/api/strategy/mine/stop")
        resp = self.client.post("/api/strategy/mine/reset")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(get_mining_status()["running"])
        self.assertFalse(get_mining_status()["stop_requested"])

    def test_stop_idempotent_multiple_calls(self):
        resp1 = self.client.post("/api/strategy/mine/stop")
        resp2 = self.client.post("/api/strategy/mine/stop")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)


if __name__ == "__main__":
    unittest.main()
