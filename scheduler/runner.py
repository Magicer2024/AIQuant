"""
scheduler/runner.py -- background sync task and scheduler
"""
import threading
import schedule
import time
from datetime import datetime, date

from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING, SCHEDULER_THREAD

_retry_queued = False
RETRY_DELAY_SECONDS = 30 * 60


def run_sync_blocking():
    """Background thread: 每日 07:00 自选股同步

    流程：
      1) update_stock_list() —— 拉全 A 列表写入 stock_info（约 5~10 秒）
         保证后续自选股能匹配到 stock_info 记录
      2) 检查自选股列表
         - 空：直接跳过（日志记录"自选为空"）
         - 非空：daily_sync_by_date(target="watchlist")（东财 push2his，
           1 个交易日 ≈ 3 秒，5~10 个交易日约 15~30 秒）
    """
    global _retry_queued
    try:
        from core.sync import daily_sync_by_date, is_trading_day, update_stock_list
        from core.db import init_db, count_watchlist

        init_db()
        today = date.today().strftime("%Y-%m-%d")

        if not is_trading_day(today):
            SYNC_STATUS["last_result"] = "非交易日，跳过数据拉取"
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["running"] = False
            return

        # 0) 先同步股票列表全集（用户要求）
        SYNC_STATUS["last_result"] = "更新股票列表..."
        try:
            update_stock_list()
        except Exception as e:
            print(f"[Scheduler] update_stock_list 失败（继续执行自选同步）: {e}")

        # 1) 自选股空校验
        if count_watchlist() == 0:
            SYNC_STATUS["last_result"] = "自选列表为空，跳过数据同步（请先添加自选股）"
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["running"] = False
            _retry_queued = False
            return

        # 2) 自选股按日同步（东财 push2his 快速路径）
        SYNC_STATUS["last_result"] = "自选股按日同步中..."
        result = daily_sync_by_date(
            trade_dates=None,  # 默认最近 1 个交易日
            verbose=False,
            target="watchlist",
        )

        if result.get("skipped") == "empty_watchlist":
            SYNC_STATUS["last_result"] = "自选列表为空，跳过"
        else:
            SYNC_STATUS["last_result"] = (
                f"自选同步完成 [watchlist]: {result['rows']} 行, 耗时 {result['elapsed_s']}s"
            )
        SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _retry_queued = False
    except Exception as e:
        SYNC_STATUS["last_result"] = f"错误: {e}"
        if not _retry_queued:
            _retry_queued = True
            threading.Thread(target=_schedule_retry, daemon=True).start()
        import traceback
        traceback.print_exc()
    finally:
        SYNC_STATUS["running"] = False


def _schedule_retry():
    """30分钟后重试一次"""
    import time as _time
    global _retry_queued
    _time.sleep(RETRY_DELAY_SECONDS)
    if SYNC_STATUS["running"]:
        return
    SYNC_STATUS["running"] = True
    run_sync_blocking()
    _retry_queued = False


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
