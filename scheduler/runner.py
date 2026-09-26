"""
scheduler/runner.py -- background sync task and scheduler

调度策略：
  - 19:00  盘后同步（全市场行情 + 龙虎榜 + 策略分重算）
    （19 点而非 18 点：东财龙虎榜通常 18:30 后才发布齐全，强势突破
     硬过滤与隔日动量都依赖当日龙虎榜，宁晚勿缺）
  - 失败后指数退避重试（30min → 60min → 120min，最多 3 次）
"""
import threading
import schedule
import time
import uuid
from datetime import datetime, date

from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD

# 独立 Scheduler 实例：不使用全局 schedule 模块，避免 start_scheduler 的 schedule.clear()
# 清掉 routes/sync.py 等其他模块注册的定时任务（方案 D2）。清空只作用于本实例。
_SCHEDULER = schedule.Scheduler()

_retry_count = 0
_MAX_RETRIES = 3
_BASE_RETRY_DELAY = 30 * 60  # 30 分钟
_DAILY_SYNC_TIME = "19:00"
_DAILY_SYNC_JOB_ID = "daily_sync_1900"


def _evaluate_holdings():
    """同步完成后对当前持仓跑移动止盈出场诊断，结果写入 SYNC_STATUS 供前端展示。

    与持仓页 _diagnose_position 同口径（evaluate_exit + TUNABLE_PARAMS 短线参数）：
    硬止损 / 浮盈达启动线后移动止盈（回撤清仓）/ 破MA5 / 超期兜底。
    """
    try:
        from strategy.exit_advisor import (evaluate_exit, get_max_hold,
                                           atr_dynamic_stop_pct, LIVE_MID_TRAILING_PCT)
        from config.strategy_params import get_param
        from core.db import get_conn
        import pandas as pd

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT code, name, cost_price, opened_at, COALESCE(horizon, 'short') AS horizon "
                "FROM personal_position WHERE status='holding' AND opened_at IS NOT NULL").fetchall()
        if not rows:
            SYNC_STATUS["holdings_advice"] = {
                "count": 0, "summary": "无持仓", "items": []}
            return

        items = []
        for r in rows:
            try:
                with get_conn() as conn:
                    price_rows = conn.execute(
                        "SELECT trade_date, close, high, low FROM daily_price "
                        "WHERE code = ? ORDER BY trade_date ASC", (r["code"],)).fetchall()
                if not price_rows:
                    continue
                df = pd.DataFrame([dict(x) for x in price_rows]).set_index("trade_date")
                kwargs = {}
                hz = r["horizon"]
                stop_mode, atr_pct = "fixed", None
                if hz == "short":
                    # 短线：止损 = ATR 自适应，每天用最新交易日 ATR14 重算（2026-09-14 落地），
                    # 与持仓页 _diagnose_position 同口径；ATR 不可用时回退固定 short_stop_loss。
                    # 移动止盈参数与推荐出场同口径（TUNABLE_PARAMS，DB 可覆盖）
                    atr_pct = atr_dynamic_stop_pct(df)
                    if atr_pct is not None:
                        stop_mode = "atr"
                    kwargs = dict(
                        stop_loss_pct=(atr_pct if atr_pct is not None
                                       else get_param("short_stop_loss")),
                        # 动态止损 → 硬止损只按当前收盘判定（否则波动率下行时会假阳性）
                        stop_is_dynamic=(atr_pct is not None),
                        partial_tp=get_param("short_take_profit"),
                        trailing_pct=get_param("short_trailing_pct"),
                        max_hold_days=int(get_param("short_max_hold_days")),
                    )
                else:
                    # 中/长线：各自移动止盈启动线/回撤 + 各自周期上限（mid=60 / long=不限）。
                    # 止损用 evaluate_exit 默认 -6%（与 short_stop_loss 同值，无独立参数）
                    kwargs = dict(
                        partial_tp=get_param("mid_partial_tp") if hz == "mid" else get_param("long_partial_tp"),
                        # ⚠ 中线用 LIVE_MID_TRAILING_PCT(0.10) 而非 mid_trailing_pct(0.05)：
                        # 本链路走 evaluate_exit（移动止盈无启动线），语义与推荐跟踪不同，
                        # 详见 strategy/exit_advisor.py::LIVE_MID_TRAILING_PCT 注释。
                        trailing_pct=(LIVE_MID_TRAILING_PCT if hz == "mid"
                                      else get_param("long_trailing_pct")),
                        max_hold_days=get_max_hold(hz),
                    )
                adv = evaluate_exit(
                    entry_price=float(r["cost_price"]),
                    entry_date=str(r["opened_at"])[:10],
                    df=df,
                    **kwargs,
                )
                if isinstance(adv.get("detail"), dict):
                    adv["detail"]["stop_mode"] = stop_mode
                    adv["detail"]["stop_loss_pct"] = (
                        atr_pct if atr_pct is not None else kwargs.get("stop_loss_pct"))
            except Exception:
                continue
            items.append({
                "code": r["code"],
                "name": r["name"],
                "status": adv["status"],   # hold / reduce / clear
                "reason": adv["reason"],
                "detail": adv.get("detail") or {},
            })

        clears = [i for i in items if i["status"] == "clear"]
        reduces = [i for i in items if i["status"] == "reduce"]
        holds = [i for i in items if i["status"] == "hold"]
        SYNC_STATUS["holdings_advice"] = {
            "count": len(items),
            "summary": f"持仓 {len(items)} 只：清仓 {len(clears)} · 减仓 {len(reduces)} · 持有 {len(holds)}",
            "items": items,
        }
    except Exception as e:
        SYNC_STATUS["holdings_advice"] = {
            "count": 0, "summary": f"持仓诊断失败: {e}", "items": []}


def _task_set_status(task_id, owner_token, status, *, result=None, error=None):
    """把同步结果回写统一任务表（best-effort，失败不影响同步本身）。"""
    if not task_id:
        return
    try:
        from core.repository import task_repo
        task_repo.set_status(task_id, status, owner_token=owner_token, result=result, error=error)
    except Exception:
        import traceback
        traceback.print_exc()


def _task_release(task_id, owner_token):
    if not (task_id and owner_token):
        return
    try:
        from core.repository import task_repo
        task_repo.release_lease(task_id, owner_token)
    except Exception:
        pass


def run_sync_blocking(task_id=None, owner_token=None):
    """Background thread: 盘后全市场同步

    流程：
      1) update_stock_list() —— 拉全 A 列表写入 stock_info
      2) daily_sync_by_date(target="all") —— 东财一次 HTTP 拉全 A 当日行情
         写入 daily_price 并触发策略分重算 + latest_price 刷新
      3) 失败时指数退避重试

    task_id/owner_token 给定时（定时/手动/CLI 经 task_repo 提交），把执行结果回写统一
    任务表并在结束时释放租约；不给定时保持旧行为（仅更新进程内 SYNC_STATUS）。
    """
    global _retry_count
    try:
        from core.sync import daily_sync_by_date, is_trading_day, update_stock_list
        from core.db import init_db, log_sync
        from core.repository.sync_repo import db_stats

        init_db()
        today = date.today().strftime("%Y-%m-%d")

        if not is_trading_day(today):
            SYNC_STATUS["last_result"] = "非交易日，跳过数据拉取"
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["running"] = False
            _task_set_status(task_id, owner_token, "success", result={"skipped": "non_trading_day"})
            return

        # 0) 先同步股票列表全集
        SYNC_STATUS["last_result"] = "更新股票列表..."
        try:
            update_stock_list()
        except Exception as e:
            print(f"[Scheduler] update_stock_list 失败（继续执行全市场同步）: {e}")

        # 1) 全市场按日同步（东财一次 HTTP 拉全 A 当日行情）
        SYNC_STATUS["last_result"] = "全市场同步中..."
        t0 = time.time()
        result = daily_sync_by_date(
            trade_dates=None,
            verbose=False,
            target="all",
        )
        elapsed = round(time.time() - t0, 1)

        rows = result.get('rows', 0)
        SYNC_STATUS["last_result"] = (
            f"全市场同步完成 [all]: {rows} 行, 耗时 {elapsed}s"
        )
        # 写入 sync_log 表（持久化同步记录）
        try:
            log_sync(
                sync_type="scheduler_all",
                total=db_stats()["股票列表数"],
                success=rows,
                failed=0,
                duration_s=elapsed,
                note=f"target=all, dates={result.get('dates', [])}",
            )
        except Exception:
            pass

        SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _retry_count = 0  # 成功后重置重试计数

        # 同步完成后对当前持仓跑移动止盈出场诊断（每日推荐持仓操作）
        try:
            _evaluate_holdings()
        except Exception:
            import traceback
            traceback.print_exc()

        # 盘后：同步 + 打分重算完成后，再跑一遍全市场深析扫描，结果落库 stock_deep_signal
        # （供「明日候选」读取；best-effort，失败不阻断）
        try:
            from strategy.stock_deep import run_full_market_scan
            from core.db import get_conn as _gc
            SYNC_STATUS["last_result"] = "全市场深析扫描中..."
            ss = time.time()
            with _gc() as conn:
                scan_res = run_full_market_scan(conn)
            print(f"[Scheduler] 全市场深析扫描完成: 扫描 {scan_res['scanned']} 只, "
                  f"候选 {scan_res['candidates']} 只, {scan_res['elapsed_s']}s, "
                  f"共 {round(time.time()-ss,1)}s")
            SYNC_STATUS["last_result"] = (
                f"全市场深析扫描完成: 候选 {scan_res['candidates']} 只（{scan_res['elapsed_s']}s）"
            )
        except Exception:
            import traceback
            traceback.print_exc()

        # 盘后最后一步：把刚扫描出的候选转成跟踪单，再按买点建仓 / 卖点出场推进全部未了结单
        # 并结算每笔持仓天数与收益（个股深度板块「跟踪列表」的数据源）。
        # 必须在扫描之后：先有当日候选，才有东西可跟踪。best-effort，失败不阻断。
        try:
            from strategy.deep_tracker import refresh as _track_refresh
            from core.db import get_conn as _gc2
            SYNC_STATUS["last_result"] = "个股深度跟踪推进中..."
            with _gc2() as conn:
                tr = _track_refresh(conn)
            if tr.get("enabled") is False:
                print("[Scheduler] 个股深度跟踪已停用（DEEP_TRACK.enabled=False）")
            else:
                s, u = tr.get("sync", {}), tr.get("update", {})
                print(f"[Scheduler] 个股深度跟踪: 新增候选 {s.get('added', 0)} 只, "
                      f"建仓 {u.get('filled', 0)} 笔, 出场 {u.get('closed', 0)} 笔, "
                      f"失效 {u.get('expired', 0)} 笔；当前持仓 {u.get('holding', 0)} / "
                      f"待回踩 {u.get('watching', 0)}")
                SYNC_STATUS["last_result"] += (
                    f" → 跟踪: 建仓 {u.get('filled', 0)} · 出场 {u.get('closed', 0)} · "
                    f"持仓 {u.get('holding', 0)}"
                )
        except Exception:
            import traceback
            traceback.print_exc()
        # 主同步 + 持仓诊断 + 深析扫描 + 跟踪全部完成（各 best-effort 阶段自吞异常）。
        _task_set_status(task_id, owner_token, "success",
                         result={"last_result": SYNC_STATUS.get("last_result", "")})
    except Exception as e:
        SYNC_STATUS["last_result"] = f"错误: {e}"
        _task_set_status(task_id, owner_token, "error", error=str(e))
        # 指数退避重试
        if _retry_count < _MAX_RETRIES:
            delay = _BASE_RETRY_DELAY * (2 ** _retry_count)
            _retry_count += 1
            print(f"[Scheduler] 同步失败，{delay//60}分钟后第 {_retry_count} 次重试")
            threading.Thread(target=_schedule_retry, args=(delay,), daemon=True).start()
        else:
            print(f"[Scheduler] 同步失败，已达最大重试次数({_MAX_RETRIES})，放弃")
            _retry_count = 0
        import traceback
        traceback.print_exc()
    finally:
        SYNC_STATUS["running"] = False
        _task_release(task_id, owner_token)


def _schedule_retry(delay_seconds: int):
    """指数退避重试"""
    time.sleep(delay_seconds)
    if SYNC_STATUS["running"]:
        return
    SYNC_STATUS["running"] = True
    run_sync_blocking()


def _recover_and_persist():
    """启动引导（纯 DB，可测试）：回收上次进程中断遗留的 running 任务，持久化每日 19:00 安排。

    回收依据租约是否失效（无法验证存活即标 interrupted），不自动重复历史修正/迁移/参数采纳。
    """
    from core.repository import task_repo
    recovered = task_repo.recover_interrupted()
    task_repo.upsert_scheduled_job(job_type="daily_sync", schedule_kind="daily",
                                   at_time=_DAILY_SYNC_TIME, input={"target": "all"},
                                   job_id=_DAILY_SYNC_JOB_ID)
    return recovered


def start_scheduler():
    """启动定时同步调度器（每日 19:00 盘后同步 + 龙虎榜 + 策略分重算）。

    使用独立 Scheduler 实例（不碰全局 schedule，避免清掉其他模块任务）；
    启动即回收中断任务并持久化每日安排。
    """
    from config.settings import SCHEDULER_ENABLED
    if not SCHEDULER_ENABLED:
        return
    SCHEDULER_RUNNING["enabled"] = True

    try:
        recovered = _recover_and_persist()
        if recovered:
            print(f"[Scheduler] 回收中断任务 {len(recovered)} 个（标记 interrupted）")
    except Exception:
        import traceback
        traceback.print_exc()

    def _sched_loop():
        while SCHEDULER_RUNNING["enabled"]:
            _SCHEDULER.run_pending()
            time.sleep(60)

    # 只清空并注册本模块的独立实例，不影响 routes/sync.py 等其他模块的定时任务。
    _SCHEDULER.clear()
    _SCHEDULER.every().day.at(_DAILY_SYNC_TIME).do(_job_sync)
    print(f"[Scheduler] 每日 {_DAILY_SYNC_TIME} 盘后自动数据同步（含龙虎榜）")

    if SCHEDULER_THREAD["t"] is None or not SCHEDULER_THREAD["t"].is_alive():
        SCHEDULER_THREAD["t"] = threading.Thread(target=_sched_loop, daemon=True)
        SCHEDULER_THREAD["t"].start()
        SCHEDULER_RUNNING["enabled"] = True


def _submit_daily_sync_task(source):
    """经 task_repo 幂等提交并认领每日同步写任务（手动/定时/CLI 共用，保证单一有效写任务）。

    返回 (task_id, owner_token)：
      - owner_token 非空：认领成功，调用方执行同步并持令牌回写状态。
      - task_id 非空但 owner_token 为空：已有同日/同资源组任务在处理，调用方跳过重复触发。
    任务表不可用时抛异常，由调用方回退旧的无跟踪同步。
    """
    from core.repository import task_repo
    today = date.today().strftime("%Y-%m-%d")
    sub = task_repo.submit_task(task_type="daily_sync", idempotency_key=f"daily_sync:{today}",
                                resource_group=task_repo.RESOURCE_PIPELINE_WRITE,
                                input={"target": "all", "source": source})
    if sub.get("reused") or sub.get("conflict"):
        return sub.get("task_id"), None
    token = uuid.uuid4().hex
    if not task_repo.claim_task(sub["task_id"], token).get("claimed"):
        return sub["task_id"], None
    return sub["task_id"], token


def _job_sync():
    if SYNC_STATUS["running"]:
        return
    task_id = owner_token = None
    try:
        task_id, owner_token = _submit_daily_sync_task("scheduler")
        if task_id and owner_token is None:
            # 已有同日写任务在处理（手动/CLI/上次定时）→ 不重复触发。
            print("[Scheduler] 已有同步任务在处理，跳过本次定时触发")
            try:
                from core.repository import task_repo
                task_repo.record_job_run(_DAILY_SYNC_JOB_ID, task_id=task_id,
                                         status="skipped_duplicate")
            except Exception:
                pass
            return
    except Exception:
        import traceback
        traceback.print_exc()
        task_id = owner_token = None   # 回退：任务表不可用也照常同步
    SYNC_STATUS["running"] = True
    SYNC_STATUS["last_result"] = "定时同步中..."
    threading.Thread(target=run_sync_blocking, args=(task_id, owner_token), daemon=True).start()
    if task_id:
        try:
            from core.repository import task_repo
            task_repo.record_job_run(_DAILY_SYNC_JOB_ID, task_id=task_id, status="started")
        except Exception:
            pass
