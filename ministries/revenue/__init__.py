"""
ministries/revenue/ -- 户部（资金与持仓管理）

职责：
  1. 资金管理（本金、可用资金、总资产）
  2. 持仓管理（CRUD、盈亏计算）
  3. 收益统计
"""

from ministries.revenue.position_manager import PositionManager, get_position_manager
from ministries.revenue.capital_tracker import CapitalTracker, get_capital_tracker

__all__ = [
    "PositionManager", "get_position_manager",
    "CapitalTracker", "get_capital_tracker",
]
