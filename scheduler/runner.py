"""
scheduler/runner.py -- background sync task and scheduler
"""
import threading
import schedule
import time
from datetime import datetime, date

from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD


def run_sync_blocking():
    """Background thread: run incremental data sync via daily_sync"""
    try:
        from core.sync import daily_sync, is_trading_day
        from core.db import init_db

        init_db()
        today = date.today().strftime("%Y-%m-%d")

        if not is_trading_day(today):
            SYNC_STATUS["last_result"] = "非交易日，跳过数据拉取"
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["running"] = False
            return

        SYNC_STATUS["last_result"] = "增量同步中..."
        result = daily_sync(verbose=False)
        SYNC_STATUS["last_result"] = (
            f"成功 {result['success']}，失败 {result['failed']}"
        )
        SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        SYNC_STATUS["last_result"] = f"错误: {e}"
        import traceback
        traceback.print_exc()
    finally:
        SYNC_STATUS["running"] = False


def start_scheduler():
    """Start timed sync scheduler (daily at 07:00)"""
    def _sched_loop():
        while SCHEDULER_RUNNING["enabled"]:
            schedule.run_pending()
            time.sleep(60)

    schedule.clear()
    schedule.every().day.at("07:00").do(_job_sync)
    print("[Scheduler] 每日 07:00 自动数据同步")

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
