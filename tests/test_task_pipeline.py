"""D 任务持久化、租约与流水线编排验收。

覆盖计划验收：手动/定时/CLI 同时提交只产生一个有效写任务；非交易日不反复触发；
进程中断不留永久 running；影子失败可单阶段恢复且不重建名单。
"""
import pytest

from core import db
from core import pipeline
from core.repository import task_repo


# ─────────────────────────── schema ───────────────────────────

def test_schema_tables_exist():
    with db.get_conn(readonly=True) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"background_task", "scheduled_job", "task_stage"} <= names


# ─────────────────────────── 提交幂等 + 资源互斥 ───────────────────────────

def test_submit_idempotent_by_key():
    """同一幂等键（手动/定时/CLI 同请求）只产生一个任务。"""
    a = task_repo.submit_task(task_type="sync", idempotency_key="daily:2026-01-05",
                              resource_group="pipeline_write")
    b = task_repo.submit_task(task_type="sync", idempotency_key="daily:2026-01-05",
                              resource_group="pipeline_write")
    assert a["conflict"] is False and b["reused"] is True
    assert a["task_id"] == b["task_id"]
    assert len(task_repo.list_tasks(task_type="sync")) == 1


def test_concurrent_submit_one_write_task():
    """不同入口、不同幂等键但同资源组 → 冲突，返回已有任务，不新建第二个写任务。"""
    first = task_repo.submit_task(task_type="sync", idempotency_key="manual",
                                  resource_group="pipeline_write")
    second = task_repo.submit_task(task_type="recalc", idempotency_key="sched",
                                   resource_group="pipeline_write")
    assert first["conflict"] is False
    assert second["conflict"] is True and second["holder_id"] == first["task_id"]
    assert len(task_repo.list_tasks(resource_group="pipeline_write")) == 1


def test_claim_only_pending_and_sets_lease():
    tid = task_repo.submit_task(task_type="sync", resource_group="pipeline_write")["task_id"]
    assert task_repo.claim_task(tid, "tok")["claimed"] is True
    assert task_repo.get_task(tid)["status"] == "running"
    # 已 running 的任务不可再次认领（不接管，交由 recover 判中断）。
    assert task_repo.claim_task(tid, "tok2")["claimed"] is False


# ─────────────────────────── 租约 / 心跳 / 所有权令牌 ───────────────────────────

def test_heartbeat_and_ownership_token():
    tid = task_repo.submit_task(task_type="sync")["task_id"]
    task_repo.claim_task(tid, "tok", ttl=60)
    assert task_repo.renew_lease(tid, "tok") is True
    assert task_repo.owns_lease(tid, "tok") is True
    # 失去租约的旧执行器（错误令牌）不能续租、不能写终态。
    assert task_repo.renew_lease(tid, "wrong") is False
    assert task_repo.set_status(tid, "success", owner_token="wrong") is False
    assert task_repo.set_status(tid, "success", owner_token="tok") is True


def test_recover_interrupted_on_lease_expiry():
    """进程中断（租约失效）后不留永久 running，回收为 interrupted。"""
    tid = task_repo.submit_task(task_type="sync", resource_group="pipeline_write")["task_id"]
    task_repo.claim_task(tid, "tok", ttl=1)
    future = task_repo._plus_ttl(120)   # 模拟时间推进远超 TTL
    ids = task_repo.recover_interrupted(now_iso=future)
    assert tid in ids
    assert task_repo.get_task(tid)["status"] == "interrupted"


def test_zombie_reclaimed_on_new_submit():
    """僵尸任务（租约失效的 running）阻塞时，新提交自愈回收后再建任务。"""
    t1 = task_repo.submit_task(task_type="sync", resource_group="pipeline_write")["task_id"]
    task_repo.claim_task(t1, "dead", ttl=1)
    future = task_repo._plus_ttl(120)
    # 用带 now 的提交路径：先手动回收，再提交应成功建第二个任务。
    task_repo.recover_interrupted(now_iso=future)
    t2 = task_repo.submit_task(task_type="sync", idempotency_key="next",
                               resource_group="pipeline_write", now_iso=future)
    assert t2["conflict"] is False and t2["task_id"] != t1


def test_finished_task_cannot_resurrect():
    tid = task_repo.submit_task(task_type="sync")["task_id"]
    task_repo.claim_task(tid, "tok")
    task_repo.set_status(tid, "success", owner_token="tok")
    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute("UPDATE background_task SET status='running' WHERE id=?", (tid,))


# ─────────────────────────── 流水线阶段编排 ───────────────────────────

def _claimed(task_type="pipeline"):
    tid = task_repo.submit_task(task_type=task_type)["task_id"]
    task_repo.claim_task(tid, "tok")
    return tid


def _stage_status(tid):
    return {s["stage_key"]: s["status"] for s in task_repo.get_stages(tid)}


def test_pipeline_success_records_stages():
    tid = _claimed()
    calls = []

    def s1(ctx):
        calls.append("s1")
        return {"output_count": 3, "data_date": "2026-01-05"}

    def s2(ctx):
        calls.append("s2")
        assert ctx.data_date == "2026-01-05"   # 上游产出的数据日期在上下文传递
        return {"output_count": 1}

    stages = [pipeline.Stage("a", s1), pipeline.Stage("b", s2, depends_on=("a",))]
    summary = pipeline.run_pipeline(tid, "tok", stages)
    assert summary["status"] == "success" and calls == ["s1", "s2"]
    assert _stage_status(tid) == {"a": "success", "b": "success"}
    st = {s["stage_key"]: s for s in task_repo.get_stages(tid)}
    assert st["a"]["output_count"] == 3 and st["a"]["elapsed_ms"] is not None


def test_critical_failure_marks_error_and_skips_downstream():
    tid = _claimed()

    def boom(ctx):
        raise RuntimeError("行情缺失")

    stages = [pipeline.Stage("fetch", boom),
              pipeline.Stage("publish", lambda ctx: {}, depends_on=("fetch",))]
    summary = pipeline.run_pipeline(tid, "tok", stages)
    assert summary["status"] == "error" and summary["failed"] == ["fetch"]
    assert _stage_status(tid) == {"fetch": "error", "publish": "skipped"}


def test_shadow_failure_is_partial_success():
    """主推荐成功、影子评估失败 → partial_success（不笼统显示全部完成）。"""
    tid = _claimed()
    published = {}

    def publish(ctx):
        published["done"] = True
        return {"output_count": 2}

    def shadow(ctx):
        raise RuntimeError("影子评估失败")

    stages = [pipeline.Stage("publish", publish),
              pipeline.Stage("shadow_eval", shadow, critical=False, depends_on=("publish",))]
    summary = pipeline.run_pipeline(tid, "tok", stages)
    assert summary["status"] == "partial_success" and published["done"] is True
    assert summary["failed"] == ["shadow_eval"]
    assert _stage_status(tid)["publish"] == "success"


def test_retry_only_reruns_failed_stage_without_rebuilding():
    """影子失败可单阶段恢复：重试复用已成功的发布阶段（不重建名单），只重跑失败阶段。"""
    old = _claimed()
    publish_calls = []

    def publish(ctx):
        publish_calls.append(1)
        return {"output_count": 2}

    def shadow_fail(ctx):
        raise RuntimeError("boom")

    stages_v1 = [pipeline.Stage("publish", publish),
                 pipeline.Stage("shadow_eval", shadow_fail, critical=False, depends_on=("publish",))]
    pipeline.run_pipeline(old, "tok", stages_v1)
    assert len(publish_calls) == 1

    stages_v2 = [pipeline.Stage("publish", publish),
                 pipeline.Stage("shadow_eval", lambda ctx: {"output_count": 2},
                                critical=False, depends_on=("publish",))]
    result = pipeline.retry_task(old, stages=stages_v2, task_type="pipeline")
    assert result["claimed"] is True
    assert "publish" in result["reuse"] and "shadow_eval" in result["rerun"]
    # 发布阶段被复用（skipped reused），未再次执行 → 名单未被重建。
    assert len(publish_calls) == 1
    st = _stage_status(result["task_id"])
    assert st["publish"] == "skipped" and st["shadow_eval"] == "success"
    assert result["summary"]["status"] == "success"


def test_retry_input_hash_change_invalidates_stage():
    """输入哈希改变 → 该阶段及下游失效重跑，即便旧记录曾成功。"""
    old = _claimed()

    def ok(ctx):
        return {"output_count": 1}

    stages = [pipeline.Stage("signals", ok), pipeline.Stage("publish", ok, depends_on=("signals",))]
    pipeline.run_pipeline(old, "tok", stages)
    # 手动登记旧阶段的 input_hash，模拟上游输入版本变化。
    with db.get_conn() as conn:
        conn.execute("UPDATE task_stage SET input_hash='v1' WHERE task_id=? AND stage_key='signals'", (old,))
    reuse, rerun = pipeline.plan_retry(old, stages, input_hashes={"signals": "v2"})
    # signals 输入变了 → 重跑；publish 依赖它 → 连锁重跑。
    assert reuse == [] and set(rerun) == {"signals", "publish"}


def test_non_trading_day_stops_pipeline_and_is_idempotent():
    """非交易日优雅停止（下游不执行、不算失败），同幂等键再次提交复用不重复触发。"""
    key = "daily:2026-01-04"
    tid = task_repo.submit_task(task_type="pipeline", idempotency_key=key)["task_id"]
    task_repo.claim_task(tid, "tok")
    ran = []

    def confirm(ctx):
        raise pipeline.StopPipeline("non_trading_day")

    stages = [pipeline.Stage("confirm_trade_date", confirm),
              pipeline.Stage("fetch", lambda ctx: ran.append("fetch"),
                             depends_on=("confirm_trade_date",))]
    summary = pipeline.run_pipeline(tid, "tok", stages)
    assert summary["status"] == "success" and ran == []
    assert _stage_status(tid) == {"confirm_trade_date": "skipped", "fetch": "skipped"}
    again = task_repo.submit_task(task_type="pipeline", idempotency_key=key)
    assert again["reused"] is True and again["task_id"] == tid


def test_cooperative_cancel_at_stage_boundary():
    tid = _claimed()

    def s1(ctx):
        task_repo.request_cancel(tid)   # 阶段内请求取消
        return {}

    def s2(ctx):
        raise AssertionError("取消后不应执行下游阶段")

    stages = [pipeline.Stage("a", s1), pipeline.Stage("b", s2, depends_on=("a",))]
    summary = pipeline.run_pipeline(tid, "tok", stages)
    assert summary["status"] == "cancelled"
    assert _stage_status(tid) == {"a": "success", "b": "skipped"}


# ─────────────────────────── 端到端执行器 ───────────────────────────

def test_execute_pipeline_task_end_to_end():
    stages = [pipeline.Stage("only", lambda ctx: {"output_count": 1})]
    r = pipeline.execute_pipeline_task(stages=stages, task_type="pipeline", idempotency_key="e2e:1")
    assert r["claimed"] is True and r["status"] == "success"
    assert task_repo.get_task(r["task_id"])["status"] == "success"
    # 同幂等键再次执行 → 复用，不重跑。
    r2 = pipeline.execute_pipeline_task(stages=stages, task_type="pipeline", idempotency_key="e2e:1")
    assert r2["submit"]["reused"] is True and r2["claimed"] is False


def test_execute_conflict_when_resource_busy():
    holder = task_repo.submit_task(task_type="sync", resource_group="pipeline_write")["task_id"]
    task_repo.claim_task(holder, "other", ttl=600)   # 有效租约占住写资源
    r = pipeline.execute_pipeline_task(
        stages=[pipeline.Stage("x", lambda ctx: {})], task_type="sync",
        resource_group="pipeline_write", idempotency_key="busy:1")
    assert r["submit"]["conflict"] is True and r["claimed"] is False
    assert r["task_id"] == holder


# ─────────────────────────── 定时任务持久化 ───────────────────────────

def test_scheduled_job_persistence():
    jid = task_repo.upsert_scheduled_job(job_type="daily_sync", schedule_kind="daily",
                                         at_time="19:00", input={"target": "all"})
    job = task_repo.get_scheduled_job(jid)
    assert job["job_type"] == "daily_sync" and job["at_time"] == "19:00" and job["enabled"] == 1
    # 重复登记同 (type,time) 不新增，仅更新参数。
    task_repo.upsert_scheduled_job(job_type="daily_sync", schedule_kind="daily",
                                   at_time="19:00", input={"target": "watchlist"})
    jobs = task_repo.list_scheduled_jobs()
    assert len(jobs) == 1 and jobs[0]["input"] == {"target": "watchlist"}
    task_repo.set_job_enabled(jid, False)
    assert task_repo.list_scheduled_jobs(enabled_only=True) == []


def test_record_job_run():
    jid = task_repo.upsert_scheduled_job(job_type="daily_sync", schedule_kind="daily", at_time="19:00")
    tid = task_repo.submit_task(task_type="sync")["task_id"]
    task_repo.record_job_run(jid, task_id=tid, status="success")
    job = task_repo.get_scheduled_job(jid)
    assert job["last_task_id"] == tid and job["last_status"] == "success" and job["last_run_at"]
