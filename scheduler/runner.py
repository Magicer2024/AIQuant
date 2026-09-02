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
from datetime import datetime, date

from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD

_retry_count = 0
_MAX_RETRIES = 3
_BASE_RETRY_DELAY = 30 * 60  # 30 分钟


def _evaluate_holdings():
    """同步完成后对当前持仓跑移动止盈出场诊断，结果写入 SYNC_STATUS 供前端展示。

    与持仓页 _diagnose_position 同口径（evaluate_exit + TUNABLE_PARAMS 短线参数）：
    硬止损 / 浮盈达启动线后移动止盈（回撤清仓）/ 破MA5 / 超期兜底。
    """
    try:
        from strategy.exit_advisor import evaluate_exit, get_max_hold
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
                if hz == "short":
                    # 短线：移动止盈参数与推荐出场同口径（TUNABLE_PARAMS，DB 可覆盖）
                    kwargs = dict(
                        stop_loss_pct=get_param("short_stop_loss"),
                        partial_tp=get_param("short_take_profit"),
                        trailing_pct=get_param("short_trailing_pct"),
                        max_hold_days=int(get_param("short_max_hold_days")),
                    )
                else:
                    # 中/长线：各自移动止盈启动线/回撤 + 各自周期上限（mid=60 / long=不限）。
                    # 止损用 evaluate_exit 默认 -6%（与 short_stop_loss 同值，无独立参数）
                    kwargs = dict(
                        partial_tp=get_param("mid_partial_tp") if hz == "mid" else get_param("long_partial_tp"),
                        trailing_pct=get_param("mid_trailing_pct") if hz == "mid" else get_param("long_trailing_pct"),
                        max_hold_days=get_max_hold(hz),
                    )
                adv = evaluate_exit(
                    entry_price=float(r["cost_price"]),
                    entry_date=str(r["opened_at"])[:10],
                    df=df,
                    **kwargs,
                )
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


def run_sync_blocking():
    """Background thread: 盘后全市场同步

    流程：
      1) update_stock_list() —— 拉全 A 列表写入 stock_info
      2) daily_sync_by_date(target="all") —— 东财一次 HTTP 拉全 A 当日行情
         写入 daily_price 并触发策略分重算 + latest_price 刷新
      3) 失败时指数退避重试
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
    except Exception as e:
        SYNC_STATUS["last_result"] = f"错误: {e}"
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


def _schedule_retry(delay_seconds: int):
    """指数退避重试"""
    time.sleep(delay_seconds)
    if SYNC_STATUS["running"]:
        return
    SYNC_STATUS["running"] = True
    run_sync_blocking()


def start_scheduler():
    """启动定时同步调度器（每日 19:00 盘后同步 + 龙虎榜 + 策略分重算）"""
    def _sched_loop():
        while SCHEDULER_RUNNING["enabled"]:
            schedule.run_pending()
            time.sleep(60)

    schedule.clear()
    schedule.every().day.at("19:00").do(_job_sync)
    print("[Scheduler] 每日 19:00 盘后自动数据同步（含龙虎榜）")

    if SCHEDULER_THREAD["t"] is None or not SCHEDULER_THREAD["t"].is_alive():
        SCHEDULER_THREAD["t"] = threading.Thread(target=_sched_loop, daemon=True)
        SCHEDULER_THREAD["t"].start()
        SCHEDULER_RUNNING["enabled"] = True


def _job_sync():
    if not SYNC_STATUS["running"]:
        SYNC_STATUS["running"] = True
        SYNC_STATUS["last_result"] = "定时同步中..."
        t = threading.Thread(target=run_sync_blocking, daemon=True)
        t.start()
