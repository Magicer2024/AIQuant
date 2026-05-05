"""
ministries/personnel/ -- 吏部（策略管理与账户管理）

职责：
  1. 选股策略注册、启用/禁用、权重管理
  2. 账户 CRUD
  3. 权限验证
  4. 账户快照查询
"""

from ministries.personnel.account_manager import AccountManager, get_account_manager
from ministries.personnel.strategy_registry import (
    StrategyDef,
    StrategyRegistry,
    get_strategy_registry,
)

__all__ = [
    "AccountManager",
    "get_account_manager",
    "StrategyDef",
    "StrategyRegistry",
    "get_strategy_registry",
]
