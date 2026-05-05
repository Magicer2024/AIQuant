"""
risk/ -- 向后兼容重导出 shim

风控模块已移至 ministries/justice/。
此文件保持旧导入路径可用，避免破坏现有代码。
"""

from ministries.justice.risk_models import RiskLevel, RiskCategory, RiskCheckResult, RiskStatus
from ministries.justice.risk_engine import RiskEngine
from ministries.justice.risk_rules import DEFAULT_RULES

__all__ = [
    "RiskLevel",
    "RiskCategory",
    "RiskCheckResult",
    "RiskStatus",
    "RiskEngine",
    "DEFAULT_RULES",
]
