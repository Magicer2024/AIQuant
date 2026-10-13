"""tests/test_task_queue_limits.py —— D6：执行器可注入 + 待处理队列上限

core/task_queue.py 原为模块级固定 ThreadPoolExecutor(4) 且提交无界：
单测只能起真线程 sleep 等待，高峰任务可无限堆积。本文件用注入式假执行器
（零线程、零 sleep）锁定改造后的行为：

  - configure(executor=...) 注入：同步执行器立即跑完 / 挂起执行器保持 pending
  - max_pending：只约束排队中（pending）任务，running 不占槽位、完成即释放
  - 外来执行器生命周期不归本模块（替换时不 shutdown）
  - QueueFullError 经 Flask errorhandler 统一转 503，提交点无需各自 try/except
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import task_queue
from core.task_queue import (
    submit_task, get_task_status, configure, shutdown,
    QueueFullError, DEFAULT_MAX_PENDING, DEFAULT_MAX_WORKERS,
)


# ─────────────────────────────────────────────
# 测试用假执行器
# ─────────────────────────────────────────────
class _SyncExecutor:
    """submit 在调用线程立即执行：任务在 submit 返回前已跑完"""

    def __init__(self):
        self.submit_count = 0
        self.shutdown_calls = []

    def submit(self, fn, *args, **kwargs):
        self.submit_count += 1
        fn(*args, **kwargs)
        return object()  # 调用方不使用 future

    def shutdown(self, wait=False):
        self.shutdown_calls.append(wait)


class _HoldingExecutor:
    """只登记不执行：任务永久停留 pending，模拟 worker 全忙时的排队堆积"""

    def __init__(self):
        self.jobs = []
        self.shutdown_calls = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append(fn)
        return object()

    def shutdown(self, wait=False):
        self.shutdown_calls.append(wait)


@pytest.fixture(autouse=True)
def _isolated_queue():
    """每条用例隔离执行器与任务表；用例后只关闭替换上来的池，默认池保留给其他测试"""
    saved_executor = task_queue._executor
    saved_max = task_queue._max_pending
    with task_queue._lock:
        task_queue._tasks.clear()
    yield
    current = task_queue._executor
    if current is not saved_executor:
        current.shutdown(wait=False)  # 假池 shutdown 无害；真线程池防泄漏
    task_queue._executor = saved_executor
    task_queue._max_pending = saved_max
    with task_queue._lock:
        task_queue._tasks.clear()


# ─────────────────────────────────────────────
# 1. 默认值与执行器注入
# ─────────────────────────────────────────────
def test_defaults_pinned():
    """默认 4 worker / 32 待处理上限 / 启动即自带线程池"""
    assert DEFAULT_MAX_WORKERS == 4
    assert DEFAULT_MAX_PENDING == 32
    assert isinstance(task_queue._executor, ThreadPoolExecutor)
    assert task_queue._max_pending == 32
    assert task_queue._owns_executor is True


def test_injected_sync_executor_runs_inline():
    """注入同步执行器：submit 返回时任务已 success，无需线程与 sleep"""
    sync_exec = _SyncExecutor()
    configure(executor=sync_exec, max_pending=32)

    task_id = submit_task(lambda: 42)

    status = get_task_status(task_id)
    assert status["status"] == "success"
    assert status["result"] == "42"
    assert sync_exec.submit_count == 1
    assert task_queue._owns_executor is False


def test_injected_holding_executor_keeps_pending():
    """注入挂起执行器：任务只排队不执行，状态保持 pending"""
    holding = _HoldingExecutor()
    configure(executor=holding)

    task_id = submit_task(lambda: None, kind="recalc")

    assert get_task_status(task_id)["status"] == "pending"
    assert len(holding.jobs) == 1


# ─────────────────────────────────────────────
# 2. 队列上限
# ─────────────────────────────────────────────
def test_queue_rejects_after_pending_limit():
    """pending 达上限：再提交抛 QueueFullError 且任务不登记、不提交执行器"""
    holding = _HoldingExecutor()
    configure(executor=holding, max_pending=2)

    first = submit_task(lambda: None)
    second = submit_task(lambda: None)
    with pytest.raises(QueueFullError, match="2/2"):
        submit_task(lambda: None)

    # 只有前两条登记并提交；被拒任务不留痕
    assert len(holding.jobs) == 2
    with task_queue._lock:
        assert set(task_queue._tasks) == {first, second}


def test_running_tasks_do_not_consume_pending_slots():
    """running（worker 已领取）不占排队槽位：4 个重任务在跑时仍允许新任务排队"""
    configure(executor=_HoldingExecutor(), max_pending=2)
    with task_queue._lock:
        for i in range(4):  # 手工模拟 4 个 worker 正在执行
            task_queue._tasks[f"run{i}"] = {
                "status": "running", "progress": 0, "message": "",
                "kind": "misc", "started_at": 0}

    first = submit_task(lambda: None)
    second = submit_task(lambda: None)
    with pytest.raises(QueueFullError):
        submit_task(lambda: None)

    assert get_task_status(first)["status"] == "pending"
    assert get_task_status(second)["status"] == "pending"


def test_finished_tasks_release_pending_slots():
    """同步执行器下每个任务提交即完成：上限 1 也能连续提交，槽位即时释放"""
    configure(executor=_SyncExecutor(), max_pending=1)

    for _ in range(5):
        task_id = submit_task(lambda: "ok")
        assert get_task_status(task_id)["status"] == "success"


def test_max_pending_none_means_unlimited():
    """显式 None：关闭上限（挂起执行器下连续提交大量任务也不拒）"""
    holding = _HoldingExecutor()
    configure(executor=holding, max_pending=None)

    for _ in range(50):
        submit_task(lambda: None)
    assert len(holding.jobs) == 50


# ─────────────────────────────────────────────
# 3. configure 生命周期
# ─────────────────────────────────────────────
def test_configure_max_workers_replaces_self_owned_pool():
    """自建池替换：旧池被 shutdown，新池 worker 数生效"""
    old_pool = ThreadPoolExecutor(max_workers=4)
    task_queue._executor = old_pool
    task_queue._owns_executor = True

    configure(max_workers=8)

    assert task_queue._executor._max_workers == 8
    # ThreadPoolExecutor.shutdown 后不再接受新任务（提交抛 RuntimeError）
    with pytest.raises(RuntimeError):
        old_pool.submit(lambda: None)
    assert task_queue._owns_executor is True


def test_injected_executor_not_shutdown_on_replace():
    """外来执行器不归本模块：改用自建池时不得 shutdown 它"""
    injected = _HoldingExecutor()
    configure(executor=injected)

    configure(max_workers=2)

    assert injected.shutdown_calls == []
    assert isinstance(task_queue._executor, ThreadPoolExecutor)
    assert task_queue._owns_executor is True


def test_configure_without_args_keeps_max_pending():
    """不传 max_pending 时保持原值（哨兵语义，None 与「未传」可区分）"""
    configure(executor=_SyncExecutor(), max_pending=None)
    configure(max_workers=2)  # 只动执行器
    assert task_queue._max_pending is None


def test_shutdown_forwards_to_executor():
    """shutdown 透传 wait 参数到当前执行器"""
    holding = _HoldingExecutor()
    configure(executor=holding)

    shutdown(wait=False)
    shutdown(wait=True)
    assert holding.shutdown_calls == [False, True]


# ─────────────────────────────────────────────
# 4. Flask 统一 503
# ─────────────────────────────────────────────
def test_queue_full_error_is_runtime_error():
    """QueueFullError 是 RuntimeError 子类（调用方可用宽泛异常兜底）"""
    assert issubclass(QueueFullError, RuntimeError)


def test_flask_errorhandler_maps_queue_full_to_503():
    """create_app 注册 QueueFullError 处理器：所有提交点超限统一 503，无需各自捕获"""
    from app import create_app
    app = create_app()
    # error_handler_spec 三层结构：{蓝图名: {HTTP 码或 None: {异常类: 处理函数}}}
    handler = app.error_handler_spec[None][None][QueueFullError]

    with app.app_context():  # jsonify 需要应用上下文；无请求上下文时 request_id 自动为 None
        response, status = handler(QueueFullError("任务队列繁忙（待处理 2/2）"))

    assert status == 503
    body = response.get_json()
    assert body["success"] is False
    assert "任务队列繁忙" in body["message"]
