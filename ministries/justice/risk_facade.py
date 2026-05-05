"""
ministries/justice/risk_facade.py -- 刑部·风控门面

从 risk/ 模块重导出所有风控符号。
Agent 通过此 facade 访问风控功能，不再直接导入 risk/。
"""

from risk.engine import RiskEngine
from risk.models import RiskLevel, RiskCategory, RiskCheckResult, RiskStatus
from risk.rules import DEFAULT_RULES
from risk.config_loader import risk_config
from risk.guard import build_account_state, risk_guard, check_risk

_risk_engine: RiskEngine | None = None


def get_risk_engine() -> RiskEngine:
    """获取风控引擎单例"""
    global _risk_engine
    if _risk_engine is None:
        _risk_engine = RiskEngine()
    return _risk_engine


__all__ = [
    "RiskEngine", "RiskLevel", "RiskCategory", "RiskCheckResult", "RiskStatus",
    "DEFAULT_RULES", "risk_config",
    "build_account_state", "risk_guard", "check_risk",
    "get_risk_engine",
]
