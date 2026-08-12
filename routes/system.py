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
