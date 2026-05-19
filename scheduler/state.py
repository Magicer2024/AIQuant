"""
scheduler/state.py -- in-process scheduler state
"""

SYNC_STATUS = {"running": False, "last_time": None, "last_result": ""}
SCHEDULER_RUNNING = {"enabled": False}
SCHEDULER_THREAD = {"t": None}
