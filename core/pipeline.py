"""pipeline.py —— 唯一流水线编排（方案 D2）。

把「确认交易日 → 行情 → 质量快照 → 信号计算 → 横截面完成 → 推荐发布 →
结果推进 → 持仓建议 → 深度扫描跟踪 → 诊断寻优」组织为可单独调用的阶段，
外层入口（手动 / 定时 / CLI）只决定阶段组合，不再各自重复编排。

引擎不变式：
  - 一件事一个 background_task；同一 resource_group 内串行（由 task_repo 租约保证）。
  - 每阶段记录 pending/running/success/skipped/error、耗时、输入输出数量、数据日期、失败原因。
  - 关键阶段（critical）失败→任务 error；非关键阶段（如影子评估、深度扫描）失败但关键阶段
    已成功→partial_success，前端可展示具体失败阶段，不笼统显示全部完成。
  - 协作式取消在阶段边界检查；失去租约的旧执行器不再写库（由 recover_interrupted 标 interrupted）。
  - 重试仅执行失败阶段及受影响下游；输入哈希改变时相关旧阶段失效（plan_retry 计算复用集合）。
  - 阶段可抛 StopPipeline 优雅停止（如非交易日），其后阶段登记 skipped，任务不算失败。
"""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.repository import task_repo
from core.repository.task_repo import RESOURCE_PIPELINE_WRITE

# 文档化的每日流水线阶段顺序（唯一事实来源；具体入口按需取子集）。
DAILY_STAGE_KEYS = (
    "confirm_trade_date", "fetch_market_data", "quality_snapshot",
    "compute_signals", "cross_section_complete", "publish_recommendations",
    "advance_outcomes", "position_advice", "deep_scan_tracking", "diagnose_optimize",
)


class StopPipeline(Exception):
    """阶段主动请求优雅停止（如非交易日、必要数据整体缺失）。其后阶段登记 skipped。"""


@dataclass
class Stage:
    key: str
    fn: Callable[["PipelineContext"], Optional[dict]]
    critical: bool = True
    depends_on: tuple = ()


@dataclass
class PipelineContext:
    task_id: str
    owner_token: str
    data_date: Optional[str] = None
    state: dict = field(default_factory=dict)     # 阶段间共享的执行上下文
    results: dict = field(default_factory=dict)   # stage_key -> detail（成功阶段产出）


def _summarize(task_id):
    stages = task_repo.get_stages(task_id)
    return {
        "task_id": task_id,
        "stages": [{"key": s["stage_key"], "status": s["status"],
                    "elapsed_ms": s["elapsed_ms"], "error": s["error"],
                    "output_count": s["output_count"]} for s in stages],
        "failed": [s["stage_key"] for s in stages if s["status"] == "error"],
        "skipped": [s["stage_key"] for s in stages if s["status"] == "skipped"],
    }


def run_pipeline(task_id, owner_token, stages, *, context=None, reuse_stages=None):
    """执行阶段序列并逐阶段落库状态。返回任务终态摘要 dict。

    reuse_stages：命中集合的阶段登记 skipped(reason='reused') 而不执行（重试复用旧成功结果，
    不重建名单）。关键阶段失败→error；仅非关键失败→partial_success；StopPipeline→success。
    """
    reuse = set(reuse_stages or ())
    ctx = context or PipelineContext(task_id=task_id, owner_token=owner_token)
    ctx.task_id, ctx.owner_token = task_id, owner_token
    task_repo.init_stages(task_id, [s.key for s in stages])

    unavailable = set()   # 失败或因上游失败被跳过、无产出的阶段
    non_critical_failed = False
    critical_failed = False
    stopped = False
    lease_lost = False

    for stage in stages:
        if stopped:
            task_repo.mark_stage_skipped(task_id, stage.key, reason="pipeline_stopped")
            continue
        # 阶段边界：协作式取消检查。
        task_row = task_repo.get_task(task_id)
        if task_row is None:
            break
        if task_row.get("cancel_requested") or task_row["status"] == "cancel_requested":
            task_repo.mark_stage_skipped(task_id, stage.key, reason="cancelled")
            task_repo.set_status(task_id, "cancelled", owner_token=owner_token,
                                 result=_summarize(task_id))
            return _finalize(task_id, "cancelled")
        # 心跳续期；失败说明已失去租约，旧执行器不得继续写库。
        if not task_repo.renew_lease(task_id, owner_token):
            lease_lost = True
            task_repo.mark_stage_skipped(task_id, stage.key, reason="lease_lost")
            break
        # 上游依赖失败 → 本阶段跳过并计入 unavailable，触发下游连锁跳过。
        if any(dep in unavailable for dep in stage.depends_on):
            task_repo.mark_stage_skipped(task_id, stage.key, reason="upstream_failed")
            unavailable.add(stage.key)
            if stage.critical:
                critical_failed = True
            continue
        if stage.key in reuse:
            task_repo.mark_stage_skipped(task_id, stage.key, reason="reused")
            continue

        task_repo.start_stage(task_id, stage.key, data_date=ctx.data_date)
        try:
            detail = stage.fn(ctx) or {}
            if not isinstance(detail, dict):
                detail = {"value": detail}
            out_count = detail.get("output_count")
            data_date = detail.get("data_date", ctx.data_date)
            task_repo.finish_stage(task_id, stage.key, "success",
                                   output_count=out_count, detail=detail)
            ctx.results[stage.key] = detail
            if data_date:
                ctx.data_date = data_date
        except StopPipeline as stop:
            task_repo.finish_stage(task_id, stage.key, "skipped", detail={"reason": str(stop)})
            stopped = True
        except Exception as exc:  # noqa: BLE001 —— 阶段失败须落库并决定终态
            task_repo.finish_stage(task_id, stage.key, "error", error=str(exc))
            unavailable.add(stage.key)
            if stage.critical:
                critical_failed = True
            else:
                non_critical_failed = True

    if lease_lost:
        # 不写终态：失去租约的任务由 recover_interrupted 标记 interrupted。
        return _finalize(task_id, "interrupted")
    if critical_failed:
        status = "error"
    elif non_critical_failed:
        status = "partial_success"
    else:
        status = "success"
    task_repo.set_status(task_id, status, owner_token=owner_token, result=_summarize(task_id))
    return _finalize(task_id, status)


def _finalize(task_id, status):
    summary = _summarize(task_id)
    summary["status"] = status
    return summary


def plan_retry(old_task_id, stages, *, input_hashes=None):
    """计算重试复用集合：旧任务中 success 且输入哈希未变的阶段可复用（不重跑）。

    失效传播：任何需重跑阶段的所有下游（依赖它的）也必须重跑。
    返回 (reuse_keys, rerun_keys)。reuse_keys 交给 run_pipeline(reuse_stages=...)。
    """
    old = {s["stage_key"]: s for s in task_repo.get_stages(old_task_id)}
    hashes = input_hashes or {}
    rerun = set()
    for s in stages:
        prev = old.get(s.key)
        if prev is None or prev["status"] != "success":
            rerun.add(s.key)
            continue
        new_hash = hashes.get(s.key)
        if new_hash is not None and prev.get("input_hash") not in (None, new_hash):
            rerun.add(s.key)   # 输入哈希改变 → 旧阶段失效
    changed = True
    while changed:
        changed = False
        for s in stages:
            if s.key in rerun:
                continue
            if any(dep in rerun for dep in s.depends_on):
                rerun.add(s.key)
                changed = True
    reuse = [s.key for s in stages if s.key not in rerun]
    return reuse, sorted(rerun)


# ─────────────────────────── 端到端执行器 ───────────────────────────

class _Heartbeat:
    """后台心跳线程：每 interval 秒续租，直到 stop()。失去租约时记录，供主循环感知。"""

    def __init__(self, task_id, owner_token, interval=task_repo.HEARTBEAT_INTERVAL_SECONDS):
        self._task_id = task_id
        self._token = owner_token
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.lost = False

    def __enter__(self):
        def _loop():
            while not self._stop.wait(self._interval):
                if not task_repo.renew_lease(self._task_id, self._token):
                    self.lost = True
                    return
        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1)
        return False


def execute_pipeline_task(*, stages, task_type, idempotency_key=None,
                          resource_group=RESOURCE_PIPELINE_WRITE, input=None,
                          input_version=None, owner_token=None, context=None,
                          reuse_stages=None, submit=True):
    """端到端：幂等提交 → 认领租约 → 心跳续期 → run_pipeline → 释放租约。

    返回 {submit, task_id, claimed, summary, status}。
      - submit.conflict=True：已有同资源组写任务在跑，未新建（调用方可 409 或复用）。
      - submit.reused=True：命中同 idempotency_key 的已有任务，不重复执行。
      - claimed=False：本执行器未取到租约（资源被占），不运行阶段。
    """
    token = owner_token or uuid.uuid4().hex
    sub = task_repo.submit_task(
        task_type=task_type, idempotency_key=idempotency_key, resource_group=resource_group,
        input=input, input_version=input_version) if submit else {"task_id": None}
    if sub.get("reused") or sub.get("conflict"):
        return {"submit": sub, "task_id": sub["task_id"], "claimed": False,
                "summary": None, "status": sub.get("status")}
    task_id = sub["task_id"]
    claim = task_repo.claim_task(task_id, token)
    if not claim.get("claimed"):
        return {"submit": sub, "task_id": task_id, "claimed": False,
                "summary": None, "status": claim.get("reason")}
    try:
        with _Heartbeat(task_id, token):
            summary = run_pipeline(task_id, token, stages, context=context,
                                   reuse_stages=reuse_stages)
        return {"submit": sub, "task_id": task_id, "claimed": True,
                "summary": summary, "status": summary.get("status")}
    finally:
        task_repo.release_lease(task_id, token)


def retry_task(old_task_id, *, stages, task_type=None, input_hashes=None,
               resource_group=RESOURCE_PIPELINE_WRITE, owner_token=None):
    """重试：新建 retry_of 关联任务，仅重跑失败阶段及下游，复用旧成功阶段（不重建名单）。"""
    old = task_repo.get_task(old_task_id)
    if old is None:
        raise ValueError(f"任务不存在：{old_task_id}")
    reuse, rerun = plan_retry(old_task_id, stages, input_hashes=input_hashes)
    token = owner_token or uuid.uuid4().hex
    sub = task_repo.submit_task(
        task_type=task_type or old["task_type"], resource_group=resource_group,
        input=old.get("input"), input_version=old.get("input_version"), retry_of=old_task_id)
    if sub.get("conflict"):
        return {"submit": sub, "task_id": sub["task_id"], "claimed": False,
                "reuse": reuse, "rerun": rerun}
    task_id = sub["task_id"]
    claim = task_repo.claim_task(task_id, token)
    if not claim.get("claimed"):
        return {"submit": sub, "task_id": task_id, "claimed": False,
                "reuse": reuse, "rerun": rerun}
    try:
        with _Heartbeat(task_id, token):
            summary = run_pipeline(task_id, token, stages, reuse_stages=reuse)
        return {"submit": sub, "task_id": task_id, "claimed": True,
                "summary": summary, "reuse": reuse, "rerun": rerun}
    finally:
        task_repo.release_lease(task_id, token)
