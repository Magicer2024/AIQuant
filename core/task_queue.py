"""
core/task_queue.py — 简单异步任务队列（ThreadPoolExecutor + UUID，不引入 Celery）
"""
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

_executor = ThreadPoolExecutor(max_workers=4)
_lock = Lock()
_tasks = {}


def submit_task(fn, *args, **kwargs):
    """提交异步任务，返回 8 字符任务 ID。kind 用于按类型做互斥判定（如 "recalc"）"""
    task_id = str(uuid.uuid4())[:8]
    progress_callback = kwargs.pop("progress_callback", None)
    kind = kwargs.pop("kind", "misc")

    with _lock:
        _tasks[task_id] = {
            "status": "pending", "progress": 0, "message": "",
            "kind": kind, "started_at": time.time()
        }

    def _wrap():
        with _lock:
            _tasks[task_id]["status"] = "running"
        try:
            call_kwargs = dict(kwargs)
            if progress_callback is not None:
                def _pcb(p, m):
                    with _lock:
                        if task_id in _tasks:
                            _tasks[task_id]["progress"] = p
                            _tasks[task_id]["message"] = m
                call_kwargs["progress_callback"] = _pcb
            result = fn(*args, **call_kwargs)
            with _lock:
                if task_id in _tasks:
                    _tasks[task_id]["status"] = "success"
                    _tasks[task_id]["result"] = str(result) if result else ""
        except Exception as e:
            with _lock:
                if task_id in _tasks:
                    _tasks[task_id]["status"] = "error"
                    _tasks[task_id]["message"] = str(e)

    _executor.submit(_wrap)
    return task_id


def get_task_status(task_id):
    """查询任务状态，不存在返回 None"""
    return _tasks.get(task_id)


def cleanup_old_tasks(max_age_seconds=3600):
    """清理超过 max_age_seconds 的已完成任务"""
    now = time.time()
    with _lock:
        expired = [tid for tid, t in _tasks.items()
                   if t["status"] in ("success", "error")
                   and now - t.get("started_at", 0) > max_age_seconds]
        for tid in expired:
            del _tasks[tid]


def is_any_running(kind=None):
    """检查是否有正在运行的任务。kind=None 时不区分类型（兼容旧调用）；
    指定 kind 时只统计同类任务，避免「加自选补拉/推荐池拉取」等轻量任务误伤重算。"""
    with _lock:
        return any(t["status"] in ("pending", "running")
                   and (kind is None or t.get("kind") == kind)
                   for t in _tasks.values())


def get_running_task(kind=None):
    """取当前正在运行（pending/running）的任务信息，含 progress/message。

    指定 kind 时只匹配该类任务；返回 None 表示无运行中任务。供前端实时显示进度条。
    """
    with _lock:
        for tid, t in _tasks.items():
            if t["status"] in ("pending", "running") and (kind is None or t.get("kind") == kind):
                return {"task_id": tid, "progress": t.get("progress", 0),
                        "message": t.get("message", "")}
        return None
