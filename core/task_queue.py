"""
core/task_queue.py —— 轻量级任务队列

职责：
  1. 异步任务提交与执行
  2. 任务状态跟踪（pending/running/success/failed）
  3. 结果存储与查询
  4. 线程池执行器

无需外部中间件，基于内存队列 + threading 实现。
适合单机异步任务场景：回测、批量评分、数据同步等。
"""

import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """任务对象"""
    id: str
    name: str
    fn: Callable
    args: tuple
    kwargs: dict
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    progress: float = 0.0


class TaskQueue:
    """任务队列"""

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._running = True
        self._callback_handlers: list[Callable] = []

    # ── 任务提交 ────────────────────────────────

    def submit(self, name: str, fn: Callable, *args, **kwargs) -> str:
        """提交异步任务"""
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        task = Task(
            id=task_id,
            name=name,
            fn=fn,
            args=args,
            kwargs=kwargs,
            status=TaskStatus.PENDING,
            created_at=datetime.now().isoformat(),
        )

        with self._lock:
            self._tasks[task_id] = task

        # 提交到线程池
        future = self._executor.submit(self._run_task, task)
        return task_id

    def _run_task(self, task: Task):
        """执行任务"""
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now().isoformat()

        try:
            result = task.fn(*task.args, **task.kwargs)
            task.result = result
            task.status = TaskStatus.SUCCESS
        except Exception as e:
            task.error = str(e)
            task.status = TaskStatus.FAILED
        finally:
            task.finished_at = datetime.now().isoformat()
            self._notify_callbacks(task)

    # ── 任务查询 ────────────────────────────────

    def get_task(self, task_id: str) -> Task | None:
        """获取任务"""
        with self._lock:
            return self._tasks.get(task_id)

    def get_task_dict(self, task_id: str) -> dict | None:
        """获取任务字典形式"""
        task = self.get_task(task_id)
        if not task:
            return None
        return self._task_to_dict(task)

    def list_tasks(self, status: str = None, limit: int = 100) -> list[dict]:
        """列出任务"""
        with self._lock:
            tasks = list(self._tasks.values())

        if status:
            tasks = [t for t in tasks if t.status.value == status]

        tasks = sorted(tasks, key=lambda x: x.created_at, reverse=True)[:limit]
        return [self._task_to_dict(t) for t in tasks]

    def get_stats(self) -> dict:
        """获取队列统计"""
        with self._lock:
            tasks = list(self._tasks.values())

        stats = {
            "total": len(tasks),
            "pending": sum(1 for t in tasks if t.status == TaskStatus.PENDING),
            "running": sum(1 for t in tasks if t.status == TaskStatus.RUNNING),
            "success": sum(1 for t in tasks if t.status == TaskStatus.SUCCESS),
            "failed": sum(1 for t in tasks if t.status == TaskStatus.FAILED),
            "max_workers": self.max_workers,
        }
        return stats

    # ── 任务控制 ────────────────────────────────

    def cancel(self, task_id: str) -> bool:
        """取消任务（仅对pending状态有效）"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                return True
        return False

    def clear_completed(self):
        """清理已完成的任务"""
        with self._lock:
            to_remove = [
                tid for tid, t in self._tasks.items()
                if t.status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
            ]
            for tid in to_remove:
                del self._tasks[tid]
            return len(to_remove)

    def shutdown(self, wait: bool = True):
        """关闭队列"""
        self._running = False
        self._executor.shutdown(wait=wait)

    # ── 回调 ────────────────────────────────────

    def add_callback(self, handler: Callable):
        """添加任务完成回调"""
        self._callback_handlers.append(handler)

    def _notify_callbacks(self, task: Task):
        """通知回调"""
        for handler in self._callback_handlers:
            try:
                handler(task)
            except Exception as e:
                print(f"[TaskQueue] 回调错误: {e}")

    # ── 辅助 ────────────────────────────────────

    @staticmethod
    def _task_to_dict(task: Task) -> dict:
        return {
            "id": task.id,
            "name": task.name,
            "status": task.status.value,
            "result": task.result if task.status == TaskStatus.SUCCESS else None,
            "error": task.error if task.status == TaskStatus.FAILED else "",
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "duration_ms": _calc_duration(task.started_at, task.finished_at),
        }


# ── 辅助函数 ────────────────────────────────────

def _calc_duration(start: str, end: str) -> int:
    """计算耗时（毫秒）"""
    if not start or not end:
        return 0
    try:
        t1 = datetime.fromisoformat(start)
        t2 = datetime.fromisoformat(end)
        return int((t2 - t1).total_seconds() * 1000)
    except Exception:
        return 0


# 全局单例
_task_queue: TaskQueue | None = None


def get_task_queue() -> TaskQueue:
    """获取任务队列单例"""
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueue(max_workers=4)
    return _task_queue
