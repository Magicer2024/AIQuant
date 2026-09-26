"""
routes/system.py -- system status, sync, scheduler routes
"""
import threading
from flask import request

from routes import system_bp
from utils.api import ok, fail
from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING
from scheduler.runner import start_scheduler


@system_bp.route("/status", methods=["GET"])
def system_status():
    """Get system status"""
    from routes.sync import _sync_progress
    # last_time 以 sync_log 表为准（持久化真相），兜底进程内状态
    last_time = None
    try:
        from core.db import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(sync_time) AS t FROM sync_log "
                "WHERE sync_type IN ('daily_sync', 'scheduler_all')"
            ).fetchone()
            last_time = row["t"] if row and row["t"] else None
    except Exception:
        last_time = None
    if not last_time:
        last_time = SYNC_STATUS.get("last_time")
    return ok({
        "sync": {
            "running": SYNC_STATUS["running"] or _sync_progress["running"],
            "last_time": last_time,
            "last_result": SYNC_STATUS.get("last_result", ""),
        },
        "scheduler": {
            "enabled": SCHEDULER_RUNNING["enabled"],
        },
        # 最近一次同步后对持仓的移动止盈出场诊断（每日 18:00 定时 / 手动同步后生成）
        "holdings_advice": SYNC_STATUS.get("holdings_advice"),
    })


@system_bp.route("/scheduler", methods=["POST"])
def system_scheduler():
    """Enable/disable scheduled tasks"""
    data = request.get_json() or {}
    enable = data.get("enable")

    if enable is None:
        return fail("缺少 enable 参数")

    if enable:
        if not SCHEDULER_RUNNING["enabled"]:
            SCHEDULER_RUNNING["enabled"] = True
            start_scheduler()
        return ok({"status": "enabled", "message": "定时任务已开启"})
    else:
        SCHEDULER_RUNNING["enabled"] = False
        return ok({"status": "disabled", "message": "定时任务已关闭"})


# ─────────────────────────────────────────────
# 统一任务 / 阶段 / 定时安排（方案 D）——只读查询 + 协作式取消
# ─────────────────────────────────────────────

@system_bp.route("/tasks", methods=["GET"])
def list_background_tasks():
    """列出后台任务（可按状态/类型/资源组/仅活跃过滤）。"""
    from core.repository import task_repo
    status = request.args.get("status")
    task_type = request.args.get("task_type")
    resource_group = request.args.get("resource_group")
    active_only = request.args.get("active_only") in ("1", "true", "yes")
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50
    tasks = task_repo.list_tasks(status=status, task_type=task_type,
                                 resource_group=resource_group, active_only=active_only,
                                 limit=limit)
    return ok({"tasks": tasks, "count": len(tasks)})


@system_bp.route("/tasks/<task_id>", methods=["GET"])
def get_background_task(task_id):
    """任务详情 + 阶段明细（前端展示具体失败阶段，不笼统显示全部完成）。"""
    from core.repository import task_repo
    task = task_repo.get_task(task_id)
    if task is None:
        return fail("任务不存在或已过期", 404)
    return ok({"task": task, "stages": task_repo.get_stages(task_id)})


@system_bp.route("/tasks/<task_id>/cancel", methods=["POST"])
def cancel_background_task(task_id):
    """协作式取消：置取消请求，执行器在阶段/批次边界停止（不强杀写库线程）。"""
    from core.repository import task_repo
    if task_repo.get_task(task_id) is None:
        return fail("任务不存在或已过期", 404)
    changed = task_repo.request_cancel(task_id)
    if not changed:
        return fail("任务已结束，无法取消", 409)
    return ok({"task_id": task_id, "status": "cancel_requested", "message": "已请求取消"})


@system_bp.route("/scheduler/jobs", methods=["GET"])
def list_scheduled_jobs():
    """列出持久化的定时安排（每日同步 / 用户历史修正）。"""
    from core.repository import task_repo
    enabled_only = request.args.get("enabled_only") not in ("0", "false", "no")
    jobs = task_repo.list_scheduled_jobs(enabled_only=enabled_only)
    return ok({"jobs": jobs, "count": len(jobs)})
