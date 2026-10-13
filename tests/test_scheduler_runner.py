"""tests/test_scheduler_runner.py —— 调度器主流程护栏（2026-10 优化计划 T13）

T2（test_scheduler_lock.py）已锁定三入口的原子抢占与并发窗口；本文件补齐其余
无回归保障的调度逻辑，全部以 monkeypatch 隔离 IO：

  1. run_sync_blocking：非交易日跳过 / 成功路径状态流转 / 失败指数退避 / 达到上限放弃
  2. _submit_daily_sync_task：reused / conflict / 认领成功 / 认领失败 四分支
  3. _job_sync：同日任务重复跳过（skipped_duplicate）/ 任务表异常回退照常同步
  4. start_scheduler：开关关闭不启动 / 打开注册 19:00 job
  5. _recover_and_persist：回收 + 每日安排持久化参数

约束：task_repo 一律 mock，绝不让 daily_sync:<today> 幂等键写进真实任务表。
"""
import contextlib
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as settings
from core import db as core_db
from core.repository import sync_repo, task_repo
from core import sync as core_sync
import strategy.stock_deep as stock_deep
import strategy.deep_tracker as deep_tracker
from scheduler import runner
from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD, release_sync


# ─────────────────────────────────────────────
# 共用夹具：隔离 run_sync_blocking 的全部外部 IO
# ─────────────────────────────────────────────
class _SyncIO:
    """记录 run_sync_blocking 各副作用调用，供断言；具体返回可在用例内覆写"""

    def __init__(self):
        self.status_calls = []        # (task_id, status, kwargs)
        self.released = []            # 释放的租约 (task_id, token)
        self.job_runs = []            # record_job_run 调用
        self.sync_logs = []           # log_sync 调用（含 success 行数）
        self.sync_kwargs = None       # daily_sync_by_date 入参
        self.sync_result = {"rows": 123, "dates": ["2026-10-11"]}
        self.holdings_evaluated = 0
        self.market_scan_called = 0
        self.track_refresh_called = 0
        self.db_stats_called = 0


@pytest.fixture
def sync_io(monkeypatch):
    io = _SyncIO()

    monkeypatch.setattr(core_db, "init_db", lambda: None)
    monkeypatch.setattr(core_db, "get_conn",
                        lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(core_sync, "is_trading_day", lambda d: True)
    monkeypatch.setattr(core_sync, "update_stock_list", lambda: None)

    def _fake_sync(**kwargs):
        io.sync_kwargs = kwargs
        return io.sync_result
    monkeypatch.setattr(core_sync, "daily_sync_by_date", _fake_sync)

    monkeypatch.setattr(sync_repo, "log_sync",
                        lambda **kw: io.sync_logs.append(kw))
    monkeypatch.setattr(sync_repo, "db_stats", lambda: {"股票列表数": 4000})

    monkeypatch.setattr(runner, "_evaluate_holdings", lambda: io.__dict__.update(
        holdings_evaluated=io.holdings_evaluated + 1))
    monkeypatch.setattr(stock_deep, "run_full_market_scan",
                        lambda conn: io.__dict__.update(
                            market_scan_called=io.market_scan_called + 1)
                        or {"scanned": 10, "candidates": 2, "elapsed_s": 1.0})
    monkeypatch.setattr(deep_tracker, "refresh",
                        lambda conn: io.__dict__.update(
                            track_refresh_called=io.track_refresh_called + 1)
                        or {"enabled": True, "sync": {"added": 1},
                            "update": {"filled": 0, "closed": 0, "holding": 1, "watching": 0}})

    monkeypatch.setattr(task_repo, "set_status",
                        lambda tid, status, **kw: io.status_calls.append((tid, status, kw)) or True)
    monkeypatch.setattr(task_repo, "release_lease",
                        lambda tid, tok: io.released.append((tid, tok)))
    monkeypatch.setattr(task_repo, "record_job_run",
                        lambda jid, **kw: io.job_runs.append((jid, kw)))

    # 失败重试不得真起线程（否则会重入 run_sync_blocking）
    spawned = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self.target, self.args = target, args
            spawned.append(self)

        def start(self):
            pass  # 只记录、不执行：重试调度由独立用例验证

    monkeypatch.setattr(runner.threading, "Thread", _FakeThread)

    yield io, spawned

    # 全局状态复位（锁必须在每条用例后释放，避免污染后续用例）
    release_sync()
    runner._retry_count = 0


# ─────────────────────────────────────────────
# 1. run_sync_blocking 主流程
# ─────────────────────────────────────────────
def test_non_trading_day_skips_pull(sync_io, monkeypatch):
    """非交易日：不拉行情、直接 success(non_trading_day)、释放锁与租约"""
    io, spawned = sync_io
    # 夹具默认交易日，此用例改为非交易日（不触发任何行情/扫描链路）
    monkeypatch.setattr(core_sync, "is_trading_day", lambda d: False)

    runner.run_sync_blocking("task-nt", "tok-nt")

    assert SYNC_STATUS["running"] is False
    assert "非交易日" in SYNC_STATUS["last_result"]
    assert io.sync_kwargs is None, "非交易日不得调用 daily_sync_by_date"
    assert io.holdings_evaluated == 0
    assert io.status_calls == [
        ("task-nt", "success", {"owner_token": "tok-nt",
                                "result": {"skipped": "non_trading_day"}, "error": None})]
    assert io.released == [("task-nt", "tok-nt")]
    assert spawned == []


def test_success_path_runs_all_stages_and_resets_retry(sync_io):
    """交易日成功：全市场同步→log_sync→持仓诊断→深析→跟踪，重试计数清零，回写 success"""
    io, _ = sync_io
    runner._retry_count = 2   # 历史失败计数：成功一次必须归零

    runner.run_sync_blocking("task-ok", "tok-ok")

    assert io.sync_kwargs == {"trade_dates": None, "verbose": False, "target": "all"}
    # 同步行数经 log_sync 落库（last_result 随后会被深析/跟踪阶段覆盖，不在此断言）
    assert len(io.sync_logs) == 1
    assert io.sync_logs[0]["success"] == 123
    assert io.sync_logs[0]["sync_type"] == "scheduler_all"
    # 末态状态串收尾于跟踪阶段摘要
    assert "持仓 1" in SYNC_STATUS["last_result"]
    assert io.holdings_evaluated == 1
    assert io.market_scan_called == 1
    assert io.track_refresh_called == 1
    assert runner._retry_count == 0
    tid, status, kw = io.status_calls[-1]
    assert (tid, status) == ("task-ok", "success")
    assert "last_result" in kw["result"]
    assert io.released == [("task-ok", "tok-ok")]


def test_stage_failures_do_not_block_pipeline(sync_io):
    """深析/跟踪阶段 best-effort：抛异常被吞，主同步仍记 success"""
    io, _ = sync_io

    def _boom(conn):
        raise RuntimeError("scan down")
    stock_deep.run_full_market_scan = _boom
    deep_tracker.refresh = _boom

    runner.run_sync_blocking("task-partial", "tok-p")

    assert io.status_calls[-1][1] == "success", "best-effort 阶段失败不影响主任务成功"
    assert io.released == [("task-partial", "tok-p")]


def test_failure_schedules_exponential_backoff_and_reports_error(sync_io):
    """主同步抛错：回写 error、首次退避 30min、计数 +1、起一个重试线程、释放锁"""
    io, spawned = sync_io

    def _fail(**kwargs):
        raise ConnectionError("em reset")
    core_sync.daily_sync_by_date = _fail

    runner.run_sync_blocking("task-fail", "tok-fail")

    assert SYNC_STATUS["last_result"].startswith("错误:")
    assert io.status_calls[-1][1] == "error"
    assert runner._retry_count == 1
    assert len(spawned) == 1
    # 首次退避 = 30min × 2^0 = 1800s
    assert spawned[0].target is runner._schedule_retry
    assert spawned[0].args == (1800,)
    assert SYNC_STATUS["running"] is False
    assert io.released == [("task-fail", "tok-fail")]


def test_failure_after_max_retries_gives_up_without_new_thread(sync_io):
    """已达 3 次上限：不再排程重试，计数归零"""
    io, spawned = sync_io
    runner._retry_count = runner._MAX_RETRIES

    def _fail(**kwargs):
        raise RuntimeError("persistent")
    core_sync.daily_sync_by_date = _fail

    runner.run_sync_blocking("task-giveup", "tok-g")

    assert runner._retry_count == 0, "放弃后计数应归零，等次日全新窗口"
    assert spawned == []
    assert io.status_calls[-1][1] == "error"


def test_no_task_ids_keeps_legacy_behavior(sync_io):
    """无 task_id/token（旧调用方式）：不触碰任务表，只更新进程内状态"""
    io, _ = sync_io
    runner.run_sync_blocking()
    assert io.status_calls == []
    assert io.released == []
    assert SYNC_STATUS["running"] is False


# ─────────────────────────────────────────────
# 2. _submit_daily_sync_task 四分支
# ─────────────────────────────────────────────
def test_submit_reused_returns_no_token(monkeypatch):
    """命中同日幂等键：返回 task_id 但无 owner_token，调用方跳过"""
    captured = {}
    monkeypatch.setattr(task_repo, "submit_task",
                        lambda **kw: captured.update(kw) or
                        {"task_id": "t-reused", "reused": True, "conflict": False})
    claim_calls = []
    monkeypatch.setattr(task_repo, "claim_task",
                        lambda tid, tok: claim_calls.append((tid, tok)) or {"claimed": True})

    tid, token = runner._submit_daily_sync_task("manual")

    assert (tid, token) == ("t-reused", None)
    assert claim_calls == [], "复用已有任务时不得再认领"
    assert captured["idempotency_key"] == f"daily_sync:{date.today():%Y-%m-%d}"
    assert captured["resource_group"] == task_repo.RESOURCE_PIPELINE_WRITE
    assert captured["input"] == {"target": "all", "source": "manual"}


def test_submit_conflict_returns_holder_without_token(monkeypatch):
    """同资源组已有活跃任务：返回持有者 id、无 token"""
    monkeypatch.setattr(task_repo, "submit_task",
                        lambda **kw: {"task_id": "t-holder", "reused": False,
                                      "conflict": True, "holder_id": "t-holder"})
    monkeypatch.setattr(task_repo, "claim_task",
                        lambda tid, tok: pytest.fail("冲突分支不得认领"))
    assert runner._submit_daily_sync_task("scheduler") == ("t-holder", None)


def test_submit_new_task_claimed_returns_token(monkeypatch):
    """新建任务且认领成功：返回 (task_id, 非空 token)"""
    monkeypatch.setattr(task_repo, "submit_task",
                        lambda **kw: {"task_id": "t-new", "reused": False, "conflict": False})
    monkeypatch.setattr(task_repo, "claim_task",
                        lambda tid, tok: {"claimed": True})
    tid, token = runner._submit_daily_sync_task("cli")
    assert tid == "t-new"
    assert isinstance(token, str) and len(token) == 32  # uuid4().hex


def test_submit_claim_lost_returns_no_token(monkeypatch):
    """新建成功但认领被抢（claimed=False）：无 token，调用方不得执行"""
    monkeypatch.setattr(task_repo, "submit_task",
                        lambda **kw: {"task_id": "t-lost", "reused": False, "conflict": False})
    monkeypatch.setattr(task_repo, "claim_task",
                        lambda tid, tok: {"claimed": False})
    assert runner._submit_daily_sync_task("cli") == ("t-lost", None)


# ─────────────────────────────────────────────
# 3. _job_sync 定时入口
# ─────────────────────────────────────────────
def test_job_sync_duplicate_skips_and_records(sync_io, monkeypatch):
    """同日已有写任务：不起同步线程，记录 skipped_duplicate，释放抢占锁"""
    io, spawned = sync_io
    monkeypatch.setattr(runner, "_submit_daily_sync_task",
                        lambda source: ("t-dup", None))

    runner._job_sync()

    assert spawned == []
    assert io.job_runs == [("daily_sync_1900", {"task_id": "t-dup",
                                                "status": "skipped_duplicate"})]
    assert SYNC_STATUS["running"] is False


def test_job_sync_task_table_error_falls_back_to_sync(sync_io, monkeypatch):
    """任务表不可用：回退旧路径照常起同步（task_id=None），不记 job_run"""
    io, spawned = sync_io

    def _raise(source):
        raise RuntimeError("task table unavailable")
    monkeypatch.setattr(runner, "_submit_daily_sync_task", _raise)

    runner._job_sync()

    assert len(spawned) == 1
    assert spawned[0].target is runner.run_sync_blocking
    assert spawned[0].args == (None, None)
    assert io.job_runs == []
    release_sync()   # 假线程未执行，锁手动复位


def test_job_sync_starts_thread_and_records_started(sync_io, monkeypatch):
    """认领成功：起同步线程并记录 started"""
    io, spawned = sync_io
    monkeypatch.setattr(runner, "_submit_daily_sync_task",
                        lambda source: ("t-start", "tok123"))

    runner._job_sync()

    assert len(spawned) == 1
    assert spawned[0].args == ("t-start", "tok123")
    assert io.job_runs == [("daily_sync_1900", {"task_id": "t-start",
                                                "status": "started"})]
    release_sync()


# ─────────────────────────────────────────────
# 4. start_scheduler 开关与注册
# ─────────────────────────────────────────────
def test_start_scheduler_disabled_does_nothing(monkeypatch):
    """SCHEDULER_ENABLED=False：不回收、不注册、不起线程"""
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)

    def _forbidden():
        pytest.fail("关闭时不得执行回收/持久化")
    monkeypatch.setattr(runner, "_recover_and_persist", _forbidden)

    assert runner.start_scheduler() is None
    assert SCHEDULER_RUNNING["enabled"] is False


def test_start_scheduler_registers_daily_job_and_thread(monkeypatch):
    """开启：注册唯一一个每日 19:00 job，标记 enabled 并起调度线程"""
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(runner, "_recover_and_persist", lambda: ["t-zombie"])

    runner._SCHEDULER.clear()
    runner.start_scheduler()

    try:
        jobs = runner._SCHEDULER.get_jobs()
        assert len(jobs) == 1
        assert (jobs[0].at_time.hour, jobs[0].at_time.minute) == (19, 0)
        assert SCHEDULER_RUNNING["enabled"] is True
        t = SCHEDULER_THREAD["t"]
        assert t is not None and t.is_alive() and t.daemon is True
    finally:
        # 停循环：daemon 线程在当前 60s sleep 结束后自行退出，不阻塞测试进程
        SCHEDULER_RUNNING["enabled"] = False
        runner._SCHEDULER.clear()
        if SCHEDULER_THREAD["t"] is not None:
            SCHEDULER_THREAD["t"].join(timeout=0.2)
        SCHEDULER_THREAD["t"] = None


# ─────────────────────────────────────────────
# 5. _recover_and_persist 引导
# ─────────────────────────────────────────────
def test_recover_and_persist_args(monkeypatch):
    """启动引导：回收中断任务并以固定 job_id/19:00/daily 持久化每日安排，回收结果透传"""
    calls = {}
    monkeypatch.setattr(task_repo, "recover_interrupted",
                        lambda: calls.setdefault("recovered", ["t1", "t2"]) or ["t1", "t2"])

    def _upsert(**kwargs):
        calls["upsert"] = kwargs
    monkeypatch.setattr(task_repo, "upsert_scheduled_job", _upsert)

    recovered = runner._recover_and_persist()

    assert recovered == ["t1", "t2"]
    assert calls["upsert"] == {
        "job_type": "daily_sync", "schedule_kind": "daily",
        "at_time": "19:00", "input": {"target": "all"},
        "job_id": "daily_sync_1900",
    }
