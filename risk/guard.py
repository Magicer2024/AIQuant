"""
risk/guard.py —— 运行时风控守卫

使用方式：
    1. 装饰器: @risk_guard(action="scan")
    2. 直接调用: check_risk(account_state)
"""

import functools
from typing import Callable
from .engine import RiskEngine
from .models import RiskLevel


def build_account_state(**kwargs) -> dict:
    """
    从各模块收集账户状态，构建风控检查所需的 account_state
    """
    state = {
        "account_id": kwargs.get("account_id", "default"),
        "current_drawdown": kwargs.get("current_drawdown", 0.0),
        "positions": kwargs.get("positions", []),
        "total_value": kwargs.get("total_value", 0.0),
        "total_exposure": kwargs.get("total_exposure", 0.0),
        "available_capital": kwargs.get("available_capital", 0.0),
        "consecutive_losses": kwargs.get("consecutive_losses", 0),
        "daily_open_count": kwargs.get("daily_open_count", 0),
        "index_ma5": kwargs.get("index_ma5", None),
        "index_ma20": kwargs.get("index_ma20", None),
        "trade_target": kwargs.get("trade_target", ""),
        "upcoming_holiday_days": kwargs.get("upcoming_holiday_days", 0),
    }
    return state


def risk_guard(action: str = "generic"):
    """
    风控装饰器
    被装饰函数如果返回 dict，会自动注入 risk_check 字段
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 构建账户状态（简化版，实际应从数据库/参数获取）
            account_state = kwargs.get("account_state", build_account_state())

            engine = RiskEngine()
            overall, results = engine.check(account_state)

            # 如果是禁止级，直接拦截
            if overall == RiskLevel.BLOCK:
                block_reasons = [r.message for r in results if r.level == RiskLevel.BLOCK]
                return {
                    "success": False,
                    "blocked": True,
                    "risk_level": overall.value,
                    "reason": "; ".join(block_reasons),
                    "risk_details": [r.to_dict() for r in results],
                }

            # 执行原函数
            result = func(*args, **kwargs)

            # 注入风控结果
            if isinstance(result, dict):
                result["risk_check"] = {
                    "level": overall.value,
                    "passed": overall in (RiskLevel.PASS, RiskLevel.WARNING),
                    "restricted": overall == RiskLevel.RESTRICT,
                    "details": [r.to_dict() for r in results],
                }
            return result
        return wrapper
    return decorator


def check_risk(account_state: dict, pipeline_id: str = "") -> dict:
    """
    直接执行风控检查，返回结果字典
    """
    engine = RiskEngine()
    status = engine.check_and_record(account_state, pipeline_id)

    return {
        "success": True,
        "overall_level": status.overall_level.value,
        "blocked": status.overall_level == RiskLevel.BLOCK,
        "active_rules": status.active_rules,
        "current_drawdown": status.current_drawdown,
        "block_reason": status.block_reason,
    }
