"""tests/test_scheduler_lock.py —— 同步入口原子化（2026-10 优化计划 T2）

背景（A1）：`_job_sync` 与 `_schedule_retry` 对 SYNC_STATUS["running"] 是
「先检查后置位」非原子操作，且与手动触发入口互不设防——调度循环、手动触发、
退避重试之间可能叠加跑多个同步任务并发写库。

修复口径：scheduler/state.py 提供 try_acquire_sync()/release_sync()，
以 threading.Lock 包裹的 compare-and-set 统一三入口（定时 _job_sync /
手动 start_sync·start_fast_sync / 重试 _schedule_retry）的抢占与释放。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scheduler import runner
from scheduler.state import SYNC_STATUS, release_sync, try_acquire_sync


@pytest.fixture(autouse=True)
def _clean_state():
    """全局同步状态用例间隔离"""
    SYNC_STATUS["running"] = False
    yield
    SYNC_STATUS["running"] = False


# ─────────────────────────────────────────────
# 1. 抢占原语：compare-and-set
# ─────────────────────────────────────────────
def test_acquire_marks_running_and_rejects_repeat_trigger():
    """抢占成功置 running=True；重复触发（已在运行）必须被拒绝"""
    assert try_acquire_sync() is True
    assert SYNC_STATUS["running"] is True
    assert try_acquire_sync() is False, "重复触发应返回『已在运行』"
    release_sync()
    assert SYNC_STATUS["running"] is False
    assert try_acquire_sync() is True, "释放后应可再次抢占"
    release_sync()


def test_try_acquire_sync_atomic_under_concurrency():
    """20 个线程同时抢占，有且仅有一个成功"""
    results = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()          # 对齐出发，放大并发窗口
        results.append(try_acquire_sync())

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)

    assert len(results) == 20
    assert results.count(True) == 1, f"抢占成功 {results.count(True)} 次，必须恰好 1 次"


# ─────────────────────────────────────────────
# 2. 定时入口 _job_sync 的 check-then-set 窗口
# ─────────────────────────────────────────────
def test_job_sync_concurrent_triggers_start_only_one(monkeypatch):
    """两个 _job_sync 并发触发（任务表提交慢、放大窗口），只允许一个进入同步"""
    started = []
    gate = threading.Event()

    def fake_submit(source):
        gate.wait(2)            # 模拟任务表提交慢：停留检查与置位之间的窗口
        return None, None

    def fake_run(task_id=None, owner_token=None):
        started.append(1)
        release_sync()          # 模拟 run_sync_blocking finally 里的释放

    monkeypatch.setattr(runner, "_submit_daily_sync_task", fake_submit)
    monkeypatch.setattr(runner, "run_sync_blocking", fake_run)

    threads = [threading.Thread(target=runner._job_sync) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.05)            # 让两个线程都到达提交窗口
    gate.set()
    for t in threads:
        t.join(3)

    assert len(started) == 1, f"并发触发启动了 {len(started)} 个同步任务，必须恰好 1 个"


def test_job_sync_skips_when_already_running(monkeypatch):
    """已有同步在跑时，定时触发应直接跳过（返回『已在运行』语义）"""
    started = []
    monkeypatch.setattr(runner, "run_sync_blocking",
                        lambda task_id=None, owner_token=None: started.append(1))

    assert try_acquire_sync()   # 模拟手动同步进行中
    try:
        runner._job_sync()
        assert started == [], "已在运行时不得再启动同步"
    finally:
        release_sync()


# ─────────────────────────────────────────────
# 3. 重试入口 _schedule_retry
# ─────────────────────────────────────────────
def test_retry_skips_when_sync_already_running(monkeypatch):
    """退避醒来时若已有同步在跑，不得叠加第二个任务"""
    calls = []
    monkeypatch.setattr(runner, "run_sync_blocking",
                        lambda task_id=None, owner_token=None: calls.append(1))

    assert try_acquire_sync()   # 模拟 sleep 期间手动/定时同步已启动
    try:
        runner._schedule_retry(0)
        assert calls == []
    finally:
        release_sync()


def test_retry_runs_once_and_hands_release_to_runner(monkeypatch):
    """空闲时重试正常跑一次；释放由 run_sync_blocking 的 finally 承担"""
    calls = []

    def fake_run(task_id=None, owner_token=None):
        calls.append(1)
        release_sync()          # 与 run_sync_blocking 的 finally 同契约

    monkeypatch.setattr(runner, "run_sync_blocking", fake_run)
    runner._schedule_retry(0)

    assert calls == [1]
    assert SYNC_STATUS["running"] is False
