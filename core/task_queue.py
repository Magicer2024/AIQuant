"""
core/task_queue.py — 简单异步任务队列（ThreadPoolExecutor + UUID，不引入 Celery）

D6（2026-10 可测试性/背压改造）：
  - configure(executor=...) 支持运行期注入执行器，单测可用同步/假执行器零线程隔离；
  - 待处理（pending，尚未被 worker 领取）任务数受 max_pending 约束，超限抛
    QueueFullError，避免高峰任务无界堆积；running 任务已被领取，不占排队槽位。
"""
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_PENDING = 32

_UNSET = object()  # 区分「未传 max_pending」与「显式传 None（不限）」


class QueueFullError(RuntimeError):
    """待处理任务数已达上限（worker 正在执行的 running 任务不计入）。"""


_executor = ThreadPoolExecutor(max_workers=DEFAULT_MAX_WORKERS)
_owns_executor = True   # 当前执行器是否由本模块创建（决定替换时能否代为 shutdown）
_max_pending = DEFAULT_MAX_PENDING
_lock = Lock()
_tasks = {}


def configure(*, executor=None, max_workers=None, max_pending=_UNSET):
    """运行期配置（应用启动定制 / 测试注入）。

    - executor：直接替换为外部执行器；其生命周期归外部所有，本模块不代为 shutdown。
    - max_workers：未传 executor 时由本模块新建线程池；旧池仅在「本模块自建」时优雅关闭。
    - max_pending：待处理任务上限；显式传 None 表示不限制；不传则保持原值。
    """
    global _executor, _owns_executor, _max_pending
    with _lock:
        if executor is not None:
            _executor = executor
            _owns_executor = False
        elif max_workers is not None:
            old_executor, old_owned = _executor, _owns_executor
            _executor = ThreadPoolExecutor(max_workers=max_workers)
            _owns_executor = True
            if old_owned:
                # 外来注入池不归本模块管，避免提前关掉调用方仍在使用的执行器
                old_executor.shutdown(wait=False)
        if max_pending is not _UNSET:
            _max_pending = max_pending


def shutdown(wait=False):
    """应用退出时关闭当前执行器；外来注入池同样转发，由其自身实现决定行为。"""
    with _lock:
        _executor.shutdown(wait=wait)


def submit_task(fn, *args, **kwargs):
    """提交异步任务，返回 8 字符任务 ID。kind 用于按类型做互斥判定（如 "recalc"）。

    排队中（pending）任务达到 max_pending 时抛 QueueFullError：任务不登记、不提交，
    由 Flask errorhandler 统一转 503（见 app.py）。
    """
    task_id = str(uuid.uuid4())[:8]
    progress_callback = kwargs.pop("progress_callback", None)
    kind = kwargs.pop("kind", "misc")

    with _lock:
        # 背压闸门：只数排队中的任务。running 已被 worker 领取，不算队列堆积；
        # 任务一旦在 _wrap 中转 running / success / error，槽位即自动释放。
        pending_count = sum(1 for t in _tasks.values() if t["status"] == "pending")
        if _max_pending is not None and pending_count >= _max_pending:
            raise QueueFullError(
                f"任务队列繁忙（待处理 {pending_count}/{_max_pending}），请稍后重试")
        _tasks[task_id] = {
            "status": "pending", "progress": 0, "message": "",
            "kind": kind, "started_at": time.time()
        }
        current_executor = _executor

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

    # 锁外提交：executor.submit 在池满（线程+内部队列满）时可能阻塞，不得持锁
    current_executor.submit(_wrap)
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
