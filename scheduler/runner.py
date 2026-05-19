"""
scheduler/runner.py -- background sync task and scheduler
"""
import threading
import schedule
import time
from datetime import datetime

from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD


def run_sync_blocking():
    """Background thread: run data sync"""
    try:
        from core.sync import (
            sync_one_stock, get_all_stocks, sync_strategy_score,
            daily_sync, is_trading_day, is_after_market_close,
            sync_all_indices,
        )
        from core.db import init_db, get_latest_date_all
        from datetime import date

        init_db()
        today = date.today().strftime("%Y-%m-%d")
        latest_in_db = get_latest_date_all()

        if not is_trading_day(today):
            SYNC_STATUS["last_result"] = "非交易日，跳过数据拉取"
            SYNC_STATUS["running"] = False
            return

        if latest_in_db and latest_in_db >= today:
            SYNC_STATUS["last_result"] = f"数据库已是最新（{latest_in_db}），无需拉取"
            SYNC_STATUS["running"] = False
            return

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
