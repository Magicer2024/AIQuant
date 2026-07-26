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
    return ok({
        "sync": {
            "running": SYNC_STATUS["running"] or _sync_progress["running"],
            "last_time": SYNC_STATUS.get("last_time"),
            "last_result": SYNC_STATUS.get("last_result", ""),
        },
        "scheduler": {
            "enabled": SCHEDULER_RUNNING["enabled"],
        }
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
