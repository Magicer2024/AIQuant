"""
routes/sync.py —— 数据同步相关接口
"""
import threading
from datetime import datetime

from routes import sync_bp
from utils.api import ok, fail
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
    return ok(dict(_sync_progress))


@sync_bp.route("/stock/<code>", methods=["POST"])
def sync_single_stock(code: str):
    """
    单只股同步（自选场景）：补拉该股历史行情 + 重算策略分
    同步阻塞，3-5 秒返回
    body: {} 或 {"force": true} 强制重拉（即使已有数据也再拉一次）
    """
    from flask import request
    from core.sync import sync_single_stock_to_watchlist

    body = request.get_json(silent=True) or {}
    force = bool(body.get("force", False))

    if not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        return fail("code 格式非法（需 6 位数字）")

    # 强制重拉：先清空 daily_price 中该 code 的所有数据
    if force:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM daily_price WHERE code = ?", (code,))
        # 重置 has_watchlist_data 缓存效果：sync_single_stock_to_watchlist 会重新拉取

    result = sync_single_stock_to_watchlist(code, verbose=False)
    if result.get("ok"):
        return ok(result)
    return fail(result.get("error", "同步失败"))


@sync_bp.route("/auto-status", methods=["GET"])
def auto_sync_status():
    """
    判断是否需要自动同步（供前端首页调用）。
    规则：
      - 如果当前正在同步，running=True
      - last_time 以 sync_log 表为准（持久化真相：手动/定时/命令行同步都会落库），
        避免进程内 SYNC_STATUS 在重启或非调度器路径同步时丢失导致误报「从未同步」
      - 如果 last_time 为空（从未同步过），needs_sync=True
      - 如果 last_time 的日期 ≠ 今天，needs_sync=True
      - 其他情况 needs_sync=False
    """
    running = bool(_sync_progress["running"] or SYNC_STATUS.get("running"))
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
        # 兜底：进程内状态（老逻辑，可能为 None）
        last_time = SYNC_STATUS.get("last_time")
    needs_sync = True
    if last_time:
        try:
            last_date = str(last_time).split(" ")[0]
            today = datetime.now().strftime("%Y-%m-%d")
            needs_sync = (last_date != today)
        except Exception:
            needs_sync = True
    return ok({
        "running": running,
        "last_time": last_time,
        "last_result": SYNC_STATUS.get("last_result", ""),
        "needs_sync": needs_sync,
    })


@sync_bp.route("", methods=["POST"])
def start_sync():
    """Trigger data sync (async background) with progress tracking
    body: {"target": "all" | "watchlist"}  默认 "all"（向后兼容）
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")
    if target not in ("all", "watchlist"):
        target = "all"

    if _sync_progress["running"]:
        return fail("同步正在进行中，请稍候", 409)
    _sync_progress.update({"running": True, "current": 0, "total": 0,
                            "success": 0, "failed": 0, "message": "",
                            "last_error": None, "target": target})

    def _run():
        try:
            from core.sync import daily_sync, _sync_stats
            # daily_sync 自洽完成：拉行情 + 拉龙虎榜 + 重算打分（recalc=True 默认）
            result = daily_sync(verbose=False, progress_callback=_progress_callback,
                                target=target)
            recalc = (result or {}).get("recalc") or {}
            sig_txt = f"，重算打分命中 {recalc.get('signals')} 条" if recalc else ""
            _sync_progress["message"] = (
                f"同步完成 [target={target}]: 成功{_sync_stats['success']} 失败{_sync_stats['failed']} "
                f"跳过(ST:{_sync_stats['skipped_st']} 北交所:{_sync_stats['skipped_bse']}){sig_txt}"
            )
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"同步失败: {e}"
        finally:
            _sync_progress["running"] = False
            # 回写进程内状态，保持与 sync_log 一致（scheduler/state 供定时任务与文档沿用）
            SYNC_STATUS["running"] = False
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["last_result"] = _sync_progress["message"] or SYNC_STATUS.get("last_result", "")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return ok({"status": "started", "message": f"数据同步已启动（target={target}）", "target": target})


@sync_bp.route("/fast", methods=["POST"])
def start_fast_sync():
    """
    按日批量同步（东财 push2his 直连，1 次 HTTP 拉全 A）
    推荐盘前/盘后场景使用：1 个交易日 ≈ 3 秒
    body: {"target": "all" | "watchlist", "trade_dates": ["20260624"]}
    """
    from flask import request
    if _sync_progress["running"]:
        return fail("同步正在进行中，请稍候", 409)

    body = request.get_json(silent=True) or {}
    # 默认拉最近 1 个交易日；可指定 trade_dates: ["20260624", "20260625"]
    trade_dates = body.get("trade_dates")
    target = body.get("target", "all")
    if target not in ("all", "watchlist"):
        target = "all"

    _sync_progress.update({"running": True, "current": 0, "total": 0,
                            "success": 0, "failed": 0, "message": "",
                            "last_error": None, "target": target})

    def _run():
        try:
            from core.sync import daily_sync_by_date
            result = daily_sync_by_date(trade_dates=trade_dates, verbose=False, target=target)
            _sync_progress["message"] = (
                f"按日批量同步完成 [target={target}]: {result['rows']} 行, 耗时 {result['elapsed_s']}s"
            )
            # 同步完成后对当前持仓跑移动止盈出场诊断（与每日 18:00 定时同步同口径）
            try:
                from scheduler.runner import _evaluate_holdings
                _evaluate_holdings()
            except Exception:
                import traceback
                traceback.print_exc()
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"按日同步失败: {e}"
        finally:
            _sync_progress["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return ok({"status": "started", "message": f"按日批量同步已启动（target={target}）", "target": target})


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
        return fail("codes 不能为空")
    max_workers = int(body.get("max_workers", 8))

    def _run():
        from core.sync import fetch_recommend_pool_parallel
        result = fetch_recommend_pool_parallel(codes, max_workers=max_workers, verbose=False)
        return result

    task_id = submit_task(_run)
    return ok({"task_id": task_id, "total": len(codes)})


@sync_bp.route("/realtime", methods=["GET"])
def get_realtime():
    """
    一次 HTTP 拉全 A 最新行情（盘前/盘后推荐专用，带护栏）

    护栏行为：
      - TTL=120s 内直接返回缓存，0 次网络请求
      - 超过频次/配额：返回 stale 缓存，0 次网络请求
      - 返回 data 里包含 source 字段（fresh/network/stale/empty）
    """
    from core.em_realtime import fetch_realtime_all
    from core.em_guard import cached_fetch, GUARD_CONFIG

    cfg = GUARD_CONFIG.get("realtime_all", {})
    ttl = cfg.get("default_ttl", 120)

    def _do_fetch():
        return fetch_realtime_all()  # 实际网络调用

    # 再包一层（fetch_realtime_all 内部已经走护栏了，这里双保险）
    df, source = cached_fetch("realtime_all", {"endpoint": "/realtime"}, _do_fetch, ttl=ttl)
    if df is None or df.empty:
        return fail("东财接口无数据，可能被反爬封禁", 502, source=source)

    return ok(df.to_dict("records"), source=source, count=len(df))


@sync_bp.route("/guard-status", methods=["GET"])
def guard_status():
    """
    查询东财接口护栏状态（供前端监控面板）
    返回每个接口的：今日调用次数、剩余配额、最后调用时间、缓存条数
    """
    from core.em_guard import guard_status as _gs
    status = _gs()
    return ok(status)


@sync_bp.route("/guard-clear", methods=["POST"])
def guard_clear():
    """
    手动清空护栏缓存（运维用）
    请求体: {"name": "realtime_all"}  不传 name 则清空全部
    """
    from flask import request
    from core.em_guard import force_clear_cache
    body = request.get_json(silent=True) or {}
    n = force_clear_cache(name=body.get("name"))
    return ok({"cleared": n})


@sync_bp.route("/recalc_all_scores", methods=["GET"])
def recalc_all_scores():
    """
    对数据库中所有股票的历史评分进行全量补算（同步版本，保留向后兼容），
    同时将每日融合分高于阈值的日期写入 stock_signal 表。
    query: ?target=all|watchlist  默认 "all"
    """
    from flask import request
    target = request.args.get("target", "all")
    if target not in ("all", "watchlist"):
        target = "all"
    result = run_recalc_all_scores(target=target)
    return ok(result)


@sync_bp.route("/scan_rules", methods=["POST"])
def scan_rules():
    """手动触发常驻规则扫描（消费启用规则，命中写入 strategy_signals）。
    body/query: {"date": "YYYY-MM-DD", "max_rules": 50, "lookback_rows": 300}
      - date          可选，缺省取最新交易日
      - max_rules      可选，只扫 fitness 最高的前 N 条（缺省用模块默认值）
      - lookback_rows  可选，因子计算保留的最近行数（缺省用模块默认值）
    """
    from flask import request
    from strategy.rule_scanner import (
        scan_active_rules, DEFAULT_MAX_RULES, DEFAULT_LOOKBACK_ROWS,
    )
    body = request.get_json(silent=True) or {}

    def _pick_int(key, default):
        raw = body.get(key, request.args.get(key))
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    date = (body.get("date") or request.args.get("date") or "").strip() or None
    max_rules = _pick_int("max_rules", DEFAULT_MAX_RULES)
    lookback_rows = _pick_int("lookback_rows", DEFAULT_LOOKBACK_ROWS)
    result = scan_active_rules(trade_date=date, max_rules=max_rules, lookback_rows=lookback_rows)
    return ok(result)


@sync_bp.route("/recalc", methods=["POST"])
def start_recalc():
    """重算打分（异步）
    body: {"target": "all" | "watchlist"}  默认 "all"
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")
    if target not in ("all", "watchlist"):
        target = "all"

    # 同步进行中：daily_sync 完成后会自动重算，无需重复触发
    if _sync_progress["running"]:
        return fail("正在同步行情（完成后会自动重算打分），请等待同步结束", 409)
    # 只与同类重算任务互斥，避免加自选补拉/推荐池拉取等轻量任务误伤
    if is_any_running(kind="recalc"):
        return fail("已有一个重算任务在进行中，请稍后再试", 409)

    # progress_callback=True 启用 task_queue 的进度桥接（任务内回调 → /sync/status/<id> 可读）
    task_id = submit_task(run_recalc_all_scores, target=target,
                          progress_callback=True, kind="recalc")
    return ok({"task_id": task_id, "target": target,
              "message": f"重算任务已启动（target={target}）"})


@sync_bp.route("/recalc_incremental", methods=["POST"])
def start_recalc_incremental():
    """
    增量重算打分（异步）：只重写最新交易日的 stock_signal，历史日保留。
    替代分钟级全量 /recalc——前端兜底/盘后补刷用，秒级~分钟级完成。
    body: {"target": "all" | "watchlist"}  默认 "all"
    """
    from flask import request
    from core.db import get_conn
    from core.sync import recalc_incremental_signals as run_recalc_incremental

    body = request.get_json(silent=True) or {}
    target = body.get("target", "all")
    if target not in ("all", "watchlist"):
        target = "all"

    if _sync_progress["running"]:
        return fail("正在同步行情（完成后会自动重算打分），请等待同步结束", 409)
    if is_any_running(kind="recalc"):
        return fail("已有一个重算任务在进行中，请稍后再试", 409)

    # 信号评估日 = 最新交易日（daily_price 中最大的 trade_date）
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
    latest = row["d"] if row else None
    if not latest:
        return fail("daily_price 无行情数据，请先同步行情", 400)

    # 分数列新鲜性检查：增量复用 daily_price 分数列（reuse_scores），
    # 若最新交易日 fusion_score 覆盖率过低，说明行情刚写入但策略分未重算，先拒绝并提示
    with get_conn() as conn:
        cov = conn.execute(
            "SELECT 1.0 * SUM(CASE WHEN fusion_score IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*) AS cov "
            "FROM daily_price WHERE trade_date=?", (latest,)).fetchone()["cov"]
    if cov is None or cov < 0.5:
        return fail(f"最新交易日({latest})策略分未重算（覆盖率 {cov:.0%}），请先执行同步或全量重算", 409)

    task_id = submit_task(run_recalc_incremental, [latest.replace("-", "")],
                          verbose=False, progress_callback=True, kind="recalc",
                          target=target)
    return ok({"task_id": task_id, "target": target, "scan_date": latest,
              "message": f"增量重算任务已启动（scan_date={latest}，target={target}）"})


@sync_bp.route("/status/<task_id>", methods=["GET"])
def task_status(task_id):
    """查询任务状态"""
    status = get_task_status(task_id)
    if not status:
        return fail("任务不存在或已过期", 404)
    return ok(status)
