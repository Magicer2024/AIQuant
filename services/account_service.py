"""
services/account_service.py —— 账户快照服务
"""
import traceback
from typing import Dict, Any, List

import core.db as db


def get_snapshots(days: int = 30) -> List[Dict[str, Any]]:
    """获取账户快照历史"""
    return db.get_account_snapshots(days)


def get_latest_snapshot() -> Dict[str, Any]:
    """获取最新账户快照"""
    return db.get_latest_snapshot() or {}


def save_snapshot(data: Dict[str, Any]) -> None:
    """保存账户快照"""
    db.save_account_snapshot(
        total_assets=float(data.get("total_assets", 0)),
        cash=float(data.get("cash", 0)),
        position_value=float(data.get("position_value", 0)),
        positions_count=int(data.get("positions_count", 0)),
        total_cost=float(data.get("total_cost", 0)),
        total_pnl=float(data.get("total_pnl", 0)),
        pnl_pct=float(data.get("pnl_pct", 0)),
    )
