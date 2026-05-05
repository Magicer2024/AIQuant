"""
ministries/revenue/position_manager.py -- 户部·持仓管理器

封装持仓 CRUD，当前委托给 services/position_service.py。
"""

from typing import Any


class PositionManager:
    """持仓管理器（户部）"""

    def get_positions(self, status: str = "holding") -> list[dict[str, Any]]:
        from services.position_service import get_positions
        return get_positions(status)

    def get_position_summary(self) -> dict[str, Any]:
        from services.position_service import get_position_summary
        return get_position_summary()

    def add_position(self, data: dict[str, Any]) -> int:
        from services.position_service import add_position
        return add_position(data)

    def close_position(self, position_id: Any, exit_price: float, reason: str = "manual") -> dict[str, Any]:
        from services.position_service import close_position
        return close_position(position_id, exit_price, reason=reason)

    def update_price(self, position_id: Any, current_price: float) -> None:
        from services.position_service import update_position_price
        update_position_price(position_id, current_price)

    def refresh_prices(self) -> dict[str, Any]:
        from services.position_service import refresh_position_prices
        return refresh_position_prices()


_position_manager: PositionManager | None = None


def get_position_manager() -> PositionManager:
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager()
    return _position_manager
