"""
routes/sync.py —— 数据同步相关接口
"""
import threading
from datetime import datetime
from flask import jsonify

from routes import sync_bp
from core.sync import recalc_all_scores as run_recalc_all_scores
from core.task_queue import submit_task, get_task_status, is_any_running
from scheduler.state import SYNC_STATUS

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


@sync_bp.route("/auto-status", methods=["GET"])
def auto_sync_status():
    """
    判断是否需要自动同步（供前端首页调用）。
    规则：
      - 如果当前正在同步，running=True
      - 如果 last_time 为空（从未同步过），needs_sync=True
      - 如果 last_time 的日期 ≠ 今天，needs_sync=True
      - 其他情况 needs_sync=False
    """
    running = bool(_sync_progress["running"] or SYNC_STATUS.get("running"))
    last_time = SYNC_STATUS.get("last_time")
    needs_sync = True
    if last_time:
        try:
            last_date = str(last_time).split(" ")[0]
            today = datetime.now().strftime("%Y-%m-%d")
            needs_sync = (last_date != today)
        except Exception:
            needs_sync = True
    return jsonify({
        "success": True,
        "data": {
            "running": running,
            "last_time": last_time,
            "last_result": SYNC_STATUS.get("last_result", ""),
            "needs_sync": needs_sync,
        }
    })


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


@sync_bp.route("/fast", methods=["POST"])
def start_fast_sync():
    """
    按日批量同步（东财 push2his 直连，1 次 HTTP 拉全 A）
    推荐盘前/盘后场景使用：1 个交易日 ≈ 3 秒
    """
    from flask import request
    if _sync_progress["running"]:
        return jsonify({"error": "同步正在进行中，请稍候"}), 409

    body = request.get_json(silent=True) or {}
    # 默认拉最近 1 个交易日；可指定 trade_dates: ["20260624", "20260625"]
    trade_dates = body.get("trade_dates")

    _sync_progress.update({"running": True, "current": 0, "total": 0,
                            "success": 0, "failed": 0, "message": "", "last_error": None})

    def _run():
        try:
            from core.sync import daily_sync_by_date
            result = daily_sync_by_date(trade_dates=trade_dates, verbose=False)
            _sync_progress["message"] = (
                f"按日批量同步完成: {result['rows']} 行, 耗时 {result['elapsed_s']}s"
            )
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"按日同步失败: {e}"
        finally:
            _sync_progress["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "按日批量同步已启动（东财直连）"})


@sync_bp.route("/pool", methods=["POST"])
def fetch_recommend_pool():
    """
    并行拉取推荐池 K 线（盘前/盘后推荐用）
    请求体: {"codes": ["000001", "600519", ...], "max_workers": 8}
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    codes = body.get("codes", [])
    if not codes:
        return jsonify({"error": "codes 不能为空"}), 400
    max_workers = int(body.get("max_workers", 8))

    def _run():
        from core.sync import fetch_recommend_pool_parallel
        result = fetch_recommend_pool_parallel(codes, max_workers=max_workers, verbose=False)
        return result

    task_id = submit_task(_run)
    return jsonify({"success": True, "task_id": task_id, "total": len(codes)})


@sync_bp.route("/realtime", methods=["GET"])
def get_realtime():
    """
    一次 HTTP 拉全 A 最新行情（盘前/盘后推荐专用）
    3 秒内返回 ~5000 只
    """
    from core.em_realtime import fetch_realtime_all
    df = fetch_realtime_all()
    if df.empty:
        return jsonify({"success": False, "error": "东财接口无数据"}), 502
    return jsonify({
        "success": True,
        "count": len(df),
        "data": df.to_dict("records"),
    })


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
