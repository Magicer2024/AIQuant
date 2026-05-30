"""
routes/sync.py —— 数据同步相关接口
"""
import threading
from flask import jsonify

from routes import sync_bp
from core.sync import recalc_all_scores as run_recalc_all_scores
from core.task_queue import submit_task, get_task_status, is_any_running

# ─────────────────────────────────────────────
# 同步进度状态（模块级全局变量）
# ─────────────────────────────────────────────
_sync_progress = {
    "running": False,
    "current": 0,
    "total": 0,
    "success": 0,
    "failed": 0,
    "message": "",
    "last_error": None,
}


def _progress_callback(current, total, success, failed):
    """Update sync progress state from within daily_sync()"""
    _sync_progress.update({
        "current": current,
        "total": total,
        "success": success,
        "failed": failed,
    })


@sync_bp.route("/progress", methods=["GET"])
def sync_progress():
    """Get sync progress"""
    return jsonify({"success": True, "data": dict(_sync_progress)})


@sync_bp.route("", methods=["POST"])
def start_sync():
    """Trigger data sync (async background) with progress tracking"""
    if _sync_progress["running"]:
        return jsonify({"error": "同步正在进行中，请稍候"}), 409
    _sync_progress.update({"running": True, "current": 0, "total": 0,
                            "success": 0, "failed": 0, "message": "", "last_error": None})

    def _run():
        try:
            from core.sync import daily_sync, _sync_stats
            daily_sync(verbose=False, progress_callback=_progress_callback)
            _sync_progress["message"] = (
                f"同步完成: 成功{_sync_stats['success']} 失败{_sync_stats['failed']} "
                f"跳过(ST:{_sync_stats['skipped_st']} 北交所:{_sync_stats['skipped_bse']})"
            )
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"同步失败: {e}"
        finally:
            _sync_progress["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "数据同步已启动"})


@sync_bp.route("/recalc_all_scores", methods=["GET"])
def recalc_all_scores():
    """
    对数据库中所有股票的历史评分进行全量补算（同步版本，保留向后兼容），
    同时将每日融合分高于阈值的日期写入 stock_signal 表。
    """
    result = run_recalc_all_scores()
    return jsonify(result)


@sync_bp.route("/recalc", methods=["POST"])
def start_recalc():
    """重算全市场打分（异步）"""
    if is_any_running():
        return jsonify({"success": False, "error": "有任务正在进行中，请稍后再试"}), 409

    task_id = submit_task(run_recalc_all_scores)
    return jsonify({"success": True, "task_id": task_id, "message": "重算任务已启动"})


@sync_bp.route("/status/<task_id>", methods=["GET"])
def task_status(task_id):
    """查询任务状态"""
    status = get_task_status(task_id)
    if not status:
        return jsonify({"success": False, "error": "任务不存在或已过期"}), 404
    return jsonify({"success": True, "data": status})
