"""
routes/sync.py —— 数据同步相关接口
"""
import threading
from datetime import datetime, timedelta
import schedule
import time

from routes import sync_bp
from utils.api import ok, fail
from core.sync import recalc_all_scores as run_recalc_all_scores
from core.task_queue import submit_task, get_task_status, is_any_running
from scheduler.state import SYNC_STATUS

# ─────────────────────────────────────────────
# 定时任务管理（全量历史修正）
# ─────────────────────────────────────────────
_scheduled_jobs = {}  # job_id -> job info
_schedule_thread = None
_schedule_running = False
# 独立 Scheduler 实例：不使用全局 schedule，避免与 scheduler/runner.py 互相 clear（方案 D2）。
_FIX_SCHEDULER = schedule.Scheduler()

def _run_scheduler_loop():
    """后台调度器循环，每分钟检查一次（仅本模块独立实例）"""
    global _schedule_running
    _schedule_running = True
    while _schedule_running:
        _FIX_SCHEDULER.run_pending()
        time.sleep(60)

def _reload_persisted_jobs():
    """从 scheduled_job 表恢复用户历史修正安排（重启后不丢失），注册到本模块实例。"""
    try:
        from core.repository import task_repo
        for job in task_repo.list_scheduled_jobs(enabled_only=True):
            if job["job_type"] != "fix_history" or not job.get("at_time"):
                continue
            inp = job.get("input") or {}
            job_id = job["id"]
            if job_id in _scheduled_jobs:
                continue
            sched_job = _FIX_SCHEDULER.every().day.at(job["at_time"]).do(
                _job_fix_history, job_id=job_id, source=inp.get("source", "tx"),
                threads=int(inp.get("threads", 2)), rate_limit=float(inp.get("rate_limit", 1.0)))
            _scheduled_jobs[job_id] = {
                "id": job_id, "time": job["at_time"], "source": inp.get("source", "tx"),
                "threads": inp.get("threads", 2), "rate_limit": inp.get("rate_limit", 1.0),
                "schedule_job": sched_job, "created_at": job.get("created_at"),
                "last_run": job.get("last_run_at"), "last_status": job.get("last_status"),
                "last_report": None, "last_error": None}
    except Exception:
        import traceback
        traceback.print_exc()

def _start_scheduler_if_needed():
    """启动调度器线程（如果未启动），并恢复持久化的历史修正安排"""
    global _schedule_thread
    from config.settings import SCHEDULER_ENABLED
    if not SCHEDULER_ENABLED:
        raise RuntimeError("当前配置禁止启动调度器")
    if _schedule_thread is None or not _schedule_thread.is_alive():
        _reload_persisted_jobs()
        _schedule_thread = threading.Thread(target=_run_scheduler_loop, daemon=True)
        _schedule_thread.start()

def _job_fix_history(job_id: str, source: str, threads: int, rate_limit: float):
    """执行历史修正：经统一流水线执行器提交（pipeline_write 资源组互斥 + 任务跟踪）。

    与同步/重算/深度扫描共享 pipeline_write 租约，保证对主数据的并发修改串行化；
    已有写任务在跑时本次触发被去重跳过，不并行改主数据。
    """
    print(f"[Schedule] 开始执行定时任务 {job_id}: source={source}, threads={threads}")
    status = "error"
    result_detail = None
    try:
        from core import pipeline
        from core.repository import task_repo

        def _run(ctx):
            from tools.fix_history import run_fix_history
            report = run_fix_history(codes=None, source=source, threads=threads,
                                     rate_limit=rate_limit, dry_run=False, verbose=False)
            return {"report": str(report)[:500]}

        today = datetime.now().strftime("%Y-%m-%d")
        r = pipeline.execute_pipeline_task(
            stages=[pipeline.Stage("fix_history", _run)], task_type="fix_history",
            idempotency_key=f"fix_history:{job_id}:{today}",
            resource_group=task_repo.RESOURCE_PIPELINE_WRITE,
            input={"source": source, "threads": threads, "rate_limit": rate_limit})
        status = r.get("status") or "error"
        if r.get("submit", {}).get("conflict") or r.get("submit", {}).get("reused"):
            print(f"[Schedule] 任务 {job_id} 跳过：已有主数据写任务在处理（{status}）")
            status = "skipped_duplicate"
        else:
            result_detail = (r.get("summary") or {})
            print(f"[Schedule] 任务 {job_id} 执行完成（{status}）")
    except Exception as e:
        print(f"[Schedule] 任务 {job_id} 执行失败: {e}")
        status = "failed"
        result_detail = {"error": str(e)}
    if job_id in _scheduled_jobs:
        _scheduled_jobs[job_id]["last_run"] = datetime.now().isoformat()
        _scheduled_jobs[job_id]["last_status"] = status
        if isinstance(result_detail, dict) and result_detail.get("error"):
            _scheduled_jobs[job_id]["last_error"] = result_detail["error"]
        else:
            _scheduled_jobs[job_id]["last_report"] = result_detail
    try:
        from core.repository import task_repo
        task_repo.record_job_run(job_id, status=status)
    except Exception:
        pass

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


def _acquire_write_task(task_type, *, idempotency_key=None, input=None):
    """手动写入口获取 pipeline_write 租约，纳入统一任务互斥（手动/定时/CLI 单一有效写任务）。

    返回 (task_id, owner_token, conflict)：
      - conflict=True：已有活跃写任务（返回其 task_id），调用方应回 409。
      - task_id/owner_token 均为 None：任务表不可用，调用方回退旧的无跟踪执行。
    """
    try:
        import uuid
        from core.repository import task_repo
        sub = task_repo.submit_task(task_type=task_type, idempotency_key=idempotency_key,
                                    resource_group=task_repo.RESOURCE_PIPELINE_WRITE, input=input)
        if sub.get("reused") or sub.get("conflict"):
            return sub.get("task_id"), None, True
        token = uuid.uuid4().hex
        if not task_repo.claim_task(sub["task_id"], token).get("claimed"):
            return sub["task_id"], None, True
        return sub["task_id"], token, False
    except Exception:
        import traceback
        traceback.print_exc()
        return None, None, False


def _finish_write_task(task_id, owner_token, status, *, result=None, error=None):
    """回写手动任务终态并释放租约（best-effort，失败不影响接口）。"""
    if not (task_id and owner_token):
        return
    try:
        from core.repository import task_repo
        task_repo.set_status(task_id, status, owner_token=owner_token, result=result, error=error)
        task_repo.release_lease(task_id, owner_token)
    except Exception:
        pass


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
    # 纳入统一写任务互斥：与定时/CLI 共享 pipeline_write 租约，只产生一个有效写任务。
    task_id, owner_token, conflict = _acquire_write_task(
        "daily_sync", input={"target": target, "source": "manual"})
    if conflict:
        return fail("已有主数据写任务在处理，请稍候", 409)
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
            _finish_write_task(task_id, owner_token, "success",
                               result={"message": _sync_progress["message"]})
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"同步失败: {e}"
            _finish_write_task(task_id, owner_token, "error", error=str(e))
        finally:
            _sync_progress["running"] = False
            # 回写进程内状态，保持与 sync_log 一致（scheduler/state 供定时任务与文档沿用）
            SYNC_STATUS["running"] = False
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["last_result"] = _sync_progress["message"] or SYNC_STATUS.get("last_result", "")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return ok({"status": "started", "message": f"数据同步已启动（target={target}）",
               "target": target, "task_id": task_id})


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

    # 纳入统一写任务互斥（与定时/CLI 共享 pipeline_write 租约）。
    task_id, owner_token, conflict = _acquire_write_task(
        "fast_sync", input={"target": target, "trade_dates": trade_dates, "source": "manual"})
    if conflict:
        return fail("已有主数据写任务在处理，请稍候", 409)
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
            _finish_write_task(task_id, owner_token, "success",
                               result={"message": _sync_progress["message"]})
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"按日同步失败: {e}"
            _finish_write_task(task_id, owner_token, "error", error=str(e))
        finally:
            _sync_progress["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return ok({"status": "started", "message": f"按日批量同步已启动（target={target}）",
               "target": target, "task_id": task_id})


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

    # 纳入统一写任务互斥（重算属 pipeline_write 组）。租约只在入口获取，
    # recalc_incremental_signals 内部不再申请，避免与同步流水线自锁。
    repo_task_id, owner_token, conflict = _acquire_write_task(
        "recalc", input={"target": target, "scan_date": latest, "source": "manual"})
    if conflict:
        return fail("已有主数据写任务在处理，请稍候", 409)

    def _recalc_with_lease(scan_dates, **kw):
        try:
            res = run_recalc_incremental(scan_dates, **kw)
            _finish_write_task(repo_task_id, owner_token, "success",
                               result={"signals": (res or {}).get("signals")})
            return res
        except Exception as e:
            _finish_write_task(repo_task_id, owner_token, "error", error=str(e))
            raise

    task_id = submit_task(_recalc_with_lease, [latest.replace("-", "")],
                          verbose=False, progress_callback=True, kind="recalc",
                          target=target)
    return ok({"task_id": task_id, "pipeline_task_id": repo_task_id, "target": target,
               "scan_date": latest,
               "message": f"增量重算任务已启动（scan_date={latest}，target={target}）"})


@sync_bp.route("/status/<task_id>", methods=["GET"])
def task_status(task_id):
    """查询任务状态"""
    status = get_task_status(task_id)
    if not status:
        return fail("任务不存在或已过期", 404)
    return ok(status)


# ─────────────────────────────────────────────
# 全量历史修正确时任务 API
# ─────────────────────────────────────────────

@sync_bp.route("/fix-history/schedule", methods=["POST"])
def schedule_fix_history():
    """
    添加全量历史修正确时任务
    body: {
        "time": "10:00",       # 每天执行时间 (HH:MM)
        "source": "tx",        # 数据源 (em/tx/both)
        "threads": 2,          # 并发数
        "rate_limit": 1.0,     # 限速 (秒/股)
    }
    """
    from flask import request
    
    body = request.get_json(silent=True) or {}
    run_time = body.get("time", "10:00")
    source = body.get("source", "tx")
    threads = int(body.get("threads", 2))
    rate_limit = float(body.get("rate_limit", 1.0))
    
    # 验证时间格式
    try:
        hour, minute = map(int, run_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return fail("时间格式错误，应为 HH:MM (00:00-23:59)", 400)
    except Exception:
        return fail("时间格式错误，应为 HH:MM", 400)
    
    # 验证参数
    if source not in ("em", "tx", "both"):
        return fail("source 必须是 em/tx/both", 400)
    if threads < 1 or threads > 8:
        return fail("threads 必须在 1-8 之间", 400)
    if rate_limit < 0.1 or rate_limit > 10:
        return fail("rate_limit 必须在 0.1-10 之间", 400)
    
    # 启动调度器（如果需要）
    _start_scheduler_if_needed()
    
    # 创建任务 ID
    job_id = f"fix_history_{run_time.replace(':', '')}"
    
    # 检查是否已存在相同时间的任务
    if job_id in _scheduled_jobs:
        # 更新现有任务
        _FIX_SCHEDULER.cancel_job(_scheduled_jobs[job_id]["schedule_job"])
    
    # 创建 schedule job（本模块独立实例）
    job = _FIX_SCHEDULER.every().day.at(run_time).do(
        _job_fix_history, 
        job_id=job_id,
        source=source,
        threads=threads,
        rate_limit=rate_limit
    )

    # 持久化安排（重启后可恢复），失败不阻断本次注册
    try:
        from core.repository import task_repo
        task_repo.upsert_scheduled_job(
            job_type="fix_history", schedule_kind="daily", at_time=run_time,
            input={"source": source, "threads": threads, "rate_limit": rate_limit},
            enabled=True, job_id=job_id)
    except Exception:
        import traceback
        traceback.print_exc()
    
    # 保存任务信息
    _scheduled_jobs[job_id] = {
        "id": job_id,
        "time": run_time,
        "source": source,
        "threads": threads,
        "rate_limit": rate_limit,
        "schedule_job": job,
        "created_at": datetime.now().isoformat(),
        "last_run": None,
        "last_status": None,
        "last_report": None,
        "last_error": None,
    }
    
    return ok({
        "job_id": job_id,
        "time": run_time,
        "source": source,
        "threads": threads,
        "rate_limit": rate_limit,
        "message": f"定时任务已添加：每天 {run_time} 执行全量历史修正"
    })


@sync_bp.route("/fix-history/schedule", methods=["GET"])
def list_fix_history_jobs():
    """获取所有历史修正确时任务"""
    jobs = []
    for job_id, info in _scheduled_jobs.items():
        jobs.append({
            "id": info["id"],
            "time": info["time"],
            "source": info["source"],
            "threads": info["threads"],
            "rate_limit": info["rate_limit"],
            "created_at": info["created_at"],
            "last_run": info["last_run"],
            "last_status": info["last_status"],
            "last_error": info["last_error"],
        })
    return ok({"jobs": jobs, "count": len(jobs)})


@sync_bp.route("/fix-history/schedule/<job_id>", methods=["DELETE"])
def cancel_fix_history_job(job_id: str):
    """取消指定的历史修正确时任务"""
    if job_id not in _scheduled_jobs:
        return fail("任务不存在", 404)
    
    # 取消 schedule job（本模块独立实例）
    _FIX_SCHEDULER.cancel_job(_scheduled_jobs[job_id]["schedule_job"])
    
    # 删除任务信息
    del _scheduled_jobs[job_id]

    # 同步停用持久化安排（重启后不再恢复）
    try:
        from core.repository import task_repo
        task_repo.set_job_enabled(job_id, False)
    except Exception:
        pass
    
    return ok({"job_id": job_id, "message": "定时任务已取消"})


@sync_bp.route("/fix-history/run", methods=["POST"])
def run_fix_history_now():
    """
    立即执行一次全量历史修正（不定时）
    body: {
        "source": "tx",        # 数据源 (em/tx/both)
        "threads": 2,          # 并发数
        "rate_limit": 1.0,     # 限速 (秒/股)
        "dry_run": false,      # 是否试运行
    }
    """
    from flask import request
    
    body = request.get_json(silent=True) or {}
    source = body.get("source", "tx")
    threads = int(body.get("threads", 2))
    rate_limit = float(body.get("rate_limit", 1.0))
    dry_run = bool(body.get("dry_run", False))
    
    # 验证参数
    if source not in ("em", "tx", "both"):
        return fail("source 必须是 em/tx/both", 400)
    if threads < 1 or threads > 8:
        return fail("threads 必须在 1-8 之间", 400)
    if rate_limit < 0.1 or rate_limit > 10:
        return fail("rate_limit 必须在 0.1-10 之间", 400)
    
    # 检查是否有任务正在运行
    if is_any_running(kind="fix_history"):
        return fail("已有历史修正任务在运行中", 409)
    
    def run_task():
        import sys
        sys.path.insert(0, '.')
        from tools.fix_history import run_fix_history
        
        report = run_fix_history(
            codes=None,
            source=source,
            threads=threads,
            rate_limit=rate_limit,
            dry_run=dry_run,
            verbose=False,
        )
        return report
    
    task_id = submit_task(run_task, [], kind="fix_history")
    
    return ok({
        "task_id": task_id,
        "message": f"全量历史修正任务已启动 ({'试运行' if dry_run else '实际运行'})",
        "params": {
            "source": source,
            "threads": threads,
            "rate_limit": rate_limit,
            "dry_run": dry_run,
        }
    })
