"""
mining_state.py —— 策略挖掘共享状态（消除 routes/strategy.py 与 strategy/miner.py 的循环导入）
"""

_mining_status = {
    "running": False,
    "progress": None,
    "result": None,
    "stop_requested": False,
}


def get_mining_status() -> dict:
    return _mining_status


def request_stop():
    _mining_status["stop_requested"] = True


def _reset_for_test():
    """仅测试用：重置状态"""
    _mining_status.update({
        "running": False,
        "progress": None,
        "result": None,
        "stop_requested": False,
    })
