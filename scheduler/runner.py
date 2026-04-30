"""
scheduler/runner.py —— 后台线程任务与定时调度器

支持两种模式：
  1. 传统模式（兼容旧代码）：07:00 同步，09:00 扫描
  2. 多Agent流水线模式：07:30 DataAgent → 09:00 完整流水线

可通过环境变量 AGENT_MODE=pipeline 启用流水线模式
"""

import threading
import schedule
import time
import os
from datetime import datetime

from scheduler.state import (
    SYNC_STATUS, SCAN_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD,
    PIPELINE_STATUS, update_pipeline_status,
)

USE_PIPELINE = os.environ.get("AGENT_MODE", "pipeline") == "pipeline"


# ─────────────────────────────────────────────
# 传统模式任务
# ─────────────────────────────────────────────

def run_sync_blocking():
    """后台线程：执行数据同步"""
    try:
        from core.sync import (sync_one_stock, get_all_stocks, sync_strategy_score,
                               daily_sync, is_trading_day, is_after_market_close,
                               sync_all_indices)
        from core.db import init_db, get_latest_date_all
        from datetime import date

        init_db()
        today = date.today().strftime("%Y-%m-%d")
        latest_in_db = get_latest_date_all()

        # 非交易日，跳过
        if not is_trading_day(today):
            weekday_names = ["一","二","三","四","五","六","日"]
            wday = date.today().weekday()
            msg = "今天是周六" if wday == 5 else "今天是周日" if wday == 6 else f"今天是节假日（{weekday_names[wday]}）"
            SYNC_STATUS["last_result"] = f"{msg}，跳过数据拉取"
            SYNC_STATUS["running"] = False
            return

        # 数据库已是最新
        if latest_in_db and latest_in_db >= today:
            SYNC_STATUS["last_result"] = f"数据库已是最新（{latest_in_db}），无需拉取"
            SYNC_STATUS["running"] = False
            return

        # 收盘后：全量同步当日；盘中：增量补缺
        if is_after_market_close():
            SYNC_STATUS["last_result"] = "收盘同步中（全量当日数据）..."
            stocks = get_all_stocks()
            success_n, fail_n = 0, 0
            for i, row in stocks.iterrows():
                code = row["code"]
                ok = sync_one_stock(code, today, today, verbose=False)
                if ok:
                    success_n += 1
                    try:
                        sync_strategy_score(code, verbose=False)
                    except Exception:
                        pass
                else:
                    fail_n += 1
            SYNC_STATUS["last_result"] = f"成功 {success_n}，失败 {fail_n}，同步指数..."
            index_results = sync_all_indices(start_date=today, end_date=today, verbose=False)
            total_idx = sum(index_results.values()) if index_results else 0
            SYNC_STATUS["last_result"] = f"成功 {success_n}，失败 {fail_n}，指数 {total_idx} 条"
        else:
            SYNC_STATUS["last_result"] = "盘中增量同步中..."
            daily_sync(verbose=False)
            SYNC_STATUS["last_result"] = "增量同步完成"

        SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        SYNC_STATUS["last_result"] = f"错误: {e}"
        import traceback; traceback.print_exc()
    finally:
        SYNC_STATUS["running"] = False


def run_scan_blocking(force_recalc=False, use_v4=True):
    """后台线程：执行策略扫描"""
    try:
        import quant
        SCAN_STATUS["last_result"] = f"{'重算+' if force_recalc else ''}扫描中(use_v4={use_v4})..."
        quant.scan_job(force_recalc=force_recalc, use_v4=use_v4)
        SCAN_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        SCAN_STATUS["last_result"] = "扫描完成"
    except Exception as e:
        SCAN_STATUS["last_result"] = f"错误: {e}"
    finally:
        SCAN_STATUS["running"] = False


# ─────────────────────────────────────────────
# 多Agent流水线模式
# ─────────────────────────────────────────────

def run_pipeline_blocking():
    """后台线程：执行完整多Agent流水线"""
    from agents.orchestrator import get_orchestrator

    orch = get_orchestrator()

    def _on_start(name: str):
        update_pipeline_status(agent_name=name, agent_state="running")

    def _on_finish(name: str, result):
        state = "success" if result.success else "failed"
        update_pipeline_status(agent_name=name, agent_state=state)

    try:
        update_pipeline_status(running=True, pipeline_id=f"sched-{datetime.now().strftime('%Y%m%d')}")
        ctx = orch.run_pipeline(
            on_agent_start=_on_start,
            on_agent_finish=_on_finish,
        )
        update_pipeline_status(
            running=False,
            result=ctx.summary(),
        )
    except Exception as e:
        import traceback
        update_pipeline_status(
            running=False,
            result={"error": str(e), "traceback": traceback.format_exc()},
        )


def run_data_agent_only():
    """仅执行DataAgent（用于07:30预同步）"""
    from agents.orchestrator import get_orchestrator
    from scheduler.state import update_pipeline_status

    orch = get_orchestrator()
    try:
        update_pipeline_status(running=True, agent_name="DataAgent", agent_state="running")
        result = orch.run_single("DataAgent")
        state = "success" if result.success else "failed"
        update_pipeline_status(running=False, agent_name="DataAgent", agent_state=state)
    except Exception as e:
        update_pipeline_status(running=False, agent_name="DataAgent", agent_state=f"error: {e}")


# ─────────────────────────────────────────────
# 调度器启动
# ─────────────────────────────────────────────

def start_scheduler():
    """启动定时调度线程"""
    def _sched_loop():
        while SCHEDULER_RUNNING["enabled"]:
            schedule.run_pending()
            time.sleep(60)

    schedule.clear()

    if USE_PIPELINE:
        # 流水线模式：07:30 预同步数据，09:00 完整流水线
        schedule.every().day.at("07:30").do(_job_data_agent)
        schedule.every().day.at("09:00").do(_job_pipeline)
        print("[Scheduler] 多Agent流水线模式：07:30 数据同步，09:00 完整流水线")
    else:
        # 传统模式：07:00 同步，09:00 扫描
        schedule.every().day.at("07:00").do(_job_sync)
        schedule.every().day.at("09:00").do(_job_scan)
        print("[Scheduler] 传统模式：07:00 同步，09:00 扫描")

    if SCHEDULER_THREAD["t"] is None or not SCHEDULER_THREAD["t"].is_alive():
        SCHEDULER_THREAD["t"] = threading.Thread(target=_sched_loop, daemon=True)
        SCHEDULER_THREAD["t"].start()
        SCHEDULER_RUNNING["enabled"] = True


# ── Job wrappers ────────────────────────────

def _job_sync():
    if not SYNC_STATUS["running"]:
        SYNC_STATUS["running"] = True
        SYNC_STATUS["last_result"] = "定时同步中..."
        t = threading.Thread(target=run_sync_blocking, daemon=True)
        t.start()


def _job_scan():
    if not SCAN_STATUS["running"]:
        SCAN_STATUS["running"] = True
        SCAN_STATUS["last_result"] = "定时扫描中..."
        t = threading.Thread(target=run_scan_blocking, daemon=True)
        t.start()


def _job_data_agent():
    if not PIPELINE_STATUS["running"]:
        t = threading.Thread(target=run_data_agent_only, daemon=True)
        t.start()


def _job_pipeline():
    if not PIPELINE_STATUS["running"]:
        t = threading.Thread(target=run_pipeline_blocking, daemon=True)
        t.start()
