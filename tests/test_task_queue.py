"""
tests/test_task_queue.py — 异步任务队列
"""
import unittest
import time


class TestTaskQueue(unittest.TestCase):
    def test_submit_task_returns_task_id(self):
        from core.task_queue import submit_task
        task_id = submit_task(lambda: 42)
        self.assertIsInstance(task_id, str)
        self.assertGreater(len(task_id), 0)

    def test_get_task_status_exists(self):
        from core.task_queue import submit_task, get_task_status
        task_id = submit_task(lambda: None)
        status = get_task_status(task_id)
        self.assertIsNotNone(status)
        self.assertIn(status["status"], ("pending", "running", "success"))

    def test_task_completes_successfully(self):
        from core.task_queue import submit_task, get_task_status
        def slow_ok():
            time.sleep(0.1)
            return "done"
        task_id = submit_task(slow_ok)
        time.sleep(0.5)
        status = get_task_status(task_id)
        self.assertEqual(status["status"], "success")

    def test_task_error_status(self):
        from core.task_queue import submit_task, get_task_status
        def bad_task():
            raise ValueError("test error")
        task_id = submit_task(bad_task)
        time.sleep(0.3)
        status = get_task_status(task_id)
        self.assertEqual(status["status"], "error")
        self.assertIn("test error", status["message"])

    def test_nonexistent_task(self):
        from core.task_queue import get_task_status
        self.assertIsNone(get_task_status("nonexistent"))

    def test_cleanup_removes_old_tasks(self):
        from core.task_queue import submit_task, get_task_status, cleanup_old_tasks
        task_id = submit_task(lambda: "ok")
        time.sleep(0.3)
        cleanup_old_tasks(max_age_seconds=0)
        status = get_task_status(task_id)
        self.assertIsNone(status)

    def test_is_any_running(self):
        from core.task_queue import submit_task, is_any_running
        def slow():
            time.sleep(0.2)
            return "ok"
        task_id = submit_task(slow)
        self.assertTrue(is_any_running())
        time.sleep(0.5)
        self.assertFalse(is_any_running())


if __name__ == "__main__":
    unittest.main()
