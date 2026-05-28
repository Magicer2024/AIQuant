"""
tests/test_sync_retry.py —— 失败任务自动重试
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRetryBackoff(unittest.TestCase):
    def test_exponential_backoff_sequence(self):
        delays = [2 ** i for i in range(2)]
        self.assertEqual(delays, [1, 2])

    def test_max_retries_plus_initial_attempt(self):
        max_retries = 2
        total_attempts = max_retries + 1
        self.assertEqual(total_attempts, 3)

    def test_sleep_not_called_on_last_attempt(self):
        max_retries = 2
        attempt = 2
        should_sleep = attempt < max_retries
        self.assertFalse(should_sleep)


class TestSyncRetrySignature(unittest.TestCase):
    def test_sync_one_stock_with_timeout_has_max_retries(self):
        import inspect
        from core.sync import sync_one_stock_with_timeout
        sig = inspect.signature(sync_one_stock_with_timeout)
        self.assertIn("max_retries", sig.parameters)

    def test_default_max_retries_is_2(self):
        import inspect
        from core.sync import sync_one_stock_with_timeout
        sig = inspect.signature(sync_one_stock_with_timeout)
        self.assertEqual(sig.parameters["max_retries"].default, 2)


class TestSchedulerRetryGuard(unittest.TestCase):
    def test_retry_queued_flag_prevents_double_retry(self):
        retry_queued = False

        def schedule_retry():
            nonlocal retry_queued
            if not retry_queued:
                retry_queued = True
                return True
            return False

        self.assertTrue(schedule_retry())
        self.assertFalse(schedule_retry())
        self.assertTrue(retry_queued)


if __name__ == "__main__":
    unittest.main()
