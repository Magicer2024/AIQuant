"""
scheduler/state.py -- in-process scheduler state
"""
import threading

SYNC_STATUS = {"running": False, "last_time": None, "last_result": ""}
SCHEDULER_RUNNING = {"enabled": False}
SCHEDULER_THREAD = {"t": None}

# 同步执行权的原子抢占锁：定时(_job_sync)/手动(start_sync·start_fast_sync)/
# 重试(_schedule_retry)三入口共用，杜绝「先检查后置位」窗口内的并发重复触发。
_SYNC_GUARD = threading.Lock()


def try_acquire_sync() -> bool:
    """原子抢占同步执行权：成功置 running=True 返回 True，已在运行返回 False。"""
    with _SYNC_GUARD:
        if SYNC_STATUS["running"]:
            return False
        SYNC_STATUS["running"] = True
        return True


def release_sync():
    """释放同步执行权（幂等，供 run_sync_blocking 的 finally 与失败回退路径调用）。"""
    with _SYNC_GUARD:
        SYNC_STATUS["running"] = False
