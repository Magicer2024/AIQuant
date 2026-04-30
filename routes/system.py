"""
routes/system.py —— 系统状态、同步、扫描、调度器路由
"""
import threading
from flask import jsonify, request

from routes import system_bp
from scheduler.state import SYNC_STATUS, SCAN_STATUS, SCHEDULER_RUNNING
from scheduler.runner import run_sync_blocking, run_scan_blocking, start_scheduler


@system_bp.route("/sync", methods=["POST"])
def system_sync():
    """触发数据同步（异步后台执行）"""
    if SYNC_STATUS["running"]:
        return jsonify({"error": "同步正在进行中，请稍候"}), 409
    SYNC_STATUS["running"] = True
    SYNC_STATUS["last_result"] = "同步中..."
    t = threading.Thread(target=run_sync_blocking, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "数据同步已启动"})


@system_bp.route("/scan", methods=["POST"])
def system_scan():
    """触发策略扫描（异步后台执行）"""
    if SCAN_STATUS["running"]:
        return jsonify({"error": "扫描正在进行中，请稍候"}), 409
    data = request.get_json() or {}
    force_recalc = bool(data.get("force_recalc", False))
    use_v4 = bool(data.get("use_v4", True))
    SCAN_STATUS["running"] = True
    SCAN_STATUS["last_result"] = "扫描中..."
    t = threading.Thread(target=run_scan_blocking,
                        args=(force_recalc, use_v4),
                        daemon=True)
    t.start()
    mode = "v4" if use_v4 else "5策略融合"
    recalc_note = "（重算分数+扫描，约10分钟）" if force_recalc else ""
    return jsonify({"status": "started",
                     "message": f"策略扫描已启动 {mode}{recalc_note}",
                     "force_recalc": force_recalc,
                     "use_v4": use_v4})


@system_bp.route("/status", methods=["GET"])
def system_status():
    """获取系统状态"""
    return jsonify({
        "sync": {
            "running": SYNC_STATUS["running"],
            "last_time": SYNC_STATUS.get("last_time"),
            "last_result": SYNC_STATUS.get("last_result", ""),
        },
        "scan": {
            "running": SCAN_STATUS["running"],
            "last_time": SCAN_STATUS.get("last_time"),
            "last_result": SCAN_STATUS.get("last_result", ""),
        },
        "scheduler": {
            "enabled": SCHEDULER_RUNNING["enabled"],
        }
    })


@system_bp.route("/scheduler", methods=["POST"])
def system_scheduler():
    """开启/关闭定时任务调度"""
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


@system_bp.route("/auto_check", methods=["GET"])
def system_auto_check():
    """页面加载时自动检查是否需要执行策略扫描"""
    from datetime import date
    from core.db import get_latest_date_all, get_conn

    today_str = date.today().strftime("%Y-%m-%d")
    latest_db_date = get_latest_date_all()

    if not latest_db_date:
        return jsonify({
            "should_scan": False,
            "reason": "数据库无行情数据，请先同步",
            "latest_date": None,
            "check_date": None,
            "scan_done": False,
            "scan_running": SCAN_STATUS["running"],
        })

    check_date = latest_db_date
    is_data_stale = check_date < today_str

    if is_data_stale:
        return jsonify({
            "should_scan": False,
            "reason": f"行情数据停留在 {check_date}（已过时），跳过扫描",
            "latest_date": latest_db_date,
            "check_date": check_date,
            "scan_done": False,
            "scan_running": SCAN_STATUS["running"],
        })

    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_signal WHERE scan_date=?",
            (check_date,),
        ).fetchone()
        scan_done = (row["cnt"] > 0) if row else False

    if scan_done:
        return jsonify({
            "should_scan": False,
            "reason": f"{check_date} 已完成扫描，跳过",
            "latest_date": latest_db_date,
            "check_date": check_date,
            "scan_done": True,
            "scan_running": SCAN_STATUS["running"],
        })

    if SCAN_STATUS["running"]:
        return jsonify({
            "should_scan": False,
            "reason": "扫描正在进行中",
            "latest_date": latest_db_date,
            "check_date": check_date,
            "scan_done": False,
            "scan_running": True,
        })

    return jsonify({
        "should_scan": True,
        "reason": f"行情最新（{check_date}）且未扫描，开始自动扫描",
        "latest_date": latest_db_date,
        "check_date": check_date,
        "scan_done": False,
        "scan_running": False,
    })
