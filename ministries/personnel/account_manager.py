"""
ministries/personnel/account_manager.py -- 吏部·账户管理器

提供账户快照查询，供 RiskAgent 获取真实账户状态。
"""

from datetime import date
from typing import Any

from services.account_service import get_snapshots, get_latest_snapshot, save_snapshot


class AccountManager:
    """账户管理器（吏部）

    当前委托给 services/account_service.py，后续可直接操作 DB。
    """

    def get_snapshots(self, days: int = 30) -> list[dict[str, Any]]:
        return get_snapshots(days)

    def get_latest_snapshot(self) -> dict[str, Any]:
        return get_latest_snapshot()

    def save_snapshot(self, data: dict[str, Any]) -> None:
        save_snapshot(data)

    def get_current_account(self) -> dict[str, Any]:
        """获取当前账户概览，供风控使用"""
        snap = get_latest_snapshot()
        if not snap:
            return {
                "total_value": 100000.0,
                "available_cash": 100000.0,
                "total_exposure": 0.0,
                "position_count": 0,
            }
        return {
            "total_value": float(snap.get("total_assets", 100000)),
            "available_cash": float(snap.get("cash", 100000)),
            "total_exposure": float(snap.get("position_value", 0)),
            "position_count": int(snap.get("positions_count", 0)),
        }


_account_manager: AccountManager | None = None


def get_account_manager() -> AccountManager:
    global _account_manager
    if _account_manager is None:
        _account_manager = AccountManager()
    return _account_manager
