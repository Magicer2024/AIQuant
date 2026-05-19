"""
routes/system.py -- system status, sync, scheduler routes
"""
import threading
from flask import jsonify, request

from routes import system_bp
from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING
from scheduler.runner import run_sync_blocking, start_scheduler


@system_bp.route("/sync", methods=["POST"])
def system_sync():
    """Trigger data sync (async background)"""
    if SYNC_STATUS["running"]:
        return jsonify({"error": "同步正在进行中，请稍候"}), 409
    SYNC_STATUS["running"] = True
    SYNC_STATUS["last_result"] = "同步中..."
    t = threading.Thread(target=run_sync_blocking, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "数据同步已启动"})


@system_bp.route("/status", methods=["GET"])
def system_status():
    """Get system status"""
    return jsonify({
        "sync": {
            "running": SYNC_STATUS["running"],
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
        return jsonify({"error": "缺少 enable 参数"}), 400

    if enable:
        if not SCHEDULER_RUNNING["enabled"]:
            SCHEDULER_RUNNING["enabled"] = True
            start_scheduler()
        return jsonify({"status": "enabled", "message": "定时任务已开启"})
    else:
        SCHEDULER_RUNNING["enabled"] = False
        return jsonify({"status": "disabled", "message": "定时任务已关闭"})
