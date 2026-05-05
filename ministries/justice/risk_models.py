"""
ministries/justice/risk_models.py —— 风控数据模型
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class RiskLevel(Enum):
    PASS = "pass"
    WARNING = "warning"
    RESTRICT = "restrict"
    BLOCK = "block"


class RiskCategory(Enum):
    DRAWDOWN = "drawdown"
    CONCENTRATION = "concentration"
    VOLATILITY = "volatility"
    MARKET_TIMING = "market_timing"
    POSITION_LIMIT = "position_limit"
    COOLDOWN = "cooldown"
    BLACKLIST = "blacklist"


@dataclass
class RiskCheckResult:
    """单条风控规则检查结果"""
    level: RiskLevel
    category: RiskCategory
    rule_name: str
    message: str
    metric_value: float
    threshold: float
    timestamp: str
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "category": self.category.value,
            "rule_name": self.rule_name,
            "message": self.message,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
            "timestamp": self.timestamp,
            "suggestion": self.suggestion,
        }


@dataclass
class RiskStatus:
    """账户实时风控状态快照"""
    account_id: str
    overall_level: RiskLevel
    active_rules: list[str]
    current_drawdown: float
    current_positions: int
    total_exposure: float
    available_capital: float
    last_check: str
    block_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "overall_level": self.overall_level.value,
            "active_rules": self.active_rules,
            "current_drawdown": self.current_drawdown,
            "current_positions": self.current_positions,
            "total_exposure": self.total_exposure,
            "available_capital": self.available_capital,
            "last_check": self.last_check,
            "block_reason": self.block_reason,
        }
