"""
scheduler/runner.py -- background sync task and scheduler

调度策略：
  - 18:00  盘后同步（全市场行情 + 策略分重算）
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
    """启动定时同步调度器（每日 18:00 盘后同步 + 策略分重算）"""
    def _sched_loop():
        while SCHEDULER_RUNNING["enabled"]:
            schedule.run_pending()
            time.sleep(60)

    schedule.clear()
    schedule.every().day.at("18:00").do(_job_sync)
    print("[Scheduler] 每日 18:00 盘后自动数据同步")

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
