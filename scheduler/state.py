"""
scheduler/state.py —— 进程内调度状态（线程安全由调用方保证）
"""

import threading
from datetime import datetime

# 传统同步/扫描状态（兼容旧代码）
SYNC_STATUS = {"running": False, "last_time": None, "last_result": ""}
SCAN_STATUS = {"running": False, "last_time": None, "last_result": ""}

# 调度器状态
SCHEDULER_RUNNING = {"enabled": False}
SCHEDULER_THREAD = {"t": None}

# ── 新增：多Agent流水线状态 ──────────────────────

PIPELINE_STATUS = {
    "running": False,
    "last_pipeline_id": None,
    "last_time": None,
    "last_result": None,
    "agent_status": {},  # 各Agent实时状态
    "logs": [],          # 流水线执行日志（透传到前端）
}

_log_lock = threading.Lock()


def append_log(level: str, message: str, agent: str | None = None):
    """追加一条流水线日志（线程安全），供前端实时展示"""
    with _log_lock:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "agent": agent,
        }
        PIPELINE_STATUS["logs"].append(entry)
        # 保留最近 500 条，防止内存无限增长
        if len(PIPELINE_STATUS["logs"]) > 500:
            PIPELINE_STATUS["logs"] = PIPELINE_STATUS["logs"][-500:]


def get_logs(since_index: int = 0) -> list[dict]:
    """获取从指定索引开始的增量日志"""
    with _log_lock:
        return PIPELINE_STATUS["logs"][since_index:]


def update_pipeline_status(
    running: bool | None = None,
    pipeline_id: str | None = None,
    result: dict | None = None,
    agent_name: str | None = None,
    agent_state: str | None = None,
):
    """更新流水线状态（线程安全由调用方lock保证）"""
    if running is not None:
        PIPELINE_STATUS["running"] = running
    if pipeline_id is not None:
        PIPELINE_STATUS["last_pipeline_id"] = pipeline_id
    if result is not None:
        PIPELINE_STATUS["last_result"] = result
        from datetime import datetime
        PIPELINE_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if agent_name and agent_state:
        PIPELINE_STATUS["agent_status"][agent_name] = {
            "state": agent_state,
            "time": __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
