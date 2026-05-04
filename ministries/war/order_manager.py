"""
ministries/war/order_manager.py —— 兵部订单管理器

职责：
  1. 订单全生命周期管理
  2. 持仓管理
  3. 交易记录
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderDirection(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    """交易订单"""
    order_id: str
    code: str
    name: str
    direction: OrderDirection
    price: float
    shares: int
    status: OrderStatus = OrderStatus.PENDING
    created_at: str = ""
    submitted_at: str = ""
    filled_at: str = ""
    filled_price: float = 0.0
    filled_shares: int = 0
    commission: float = 0.0
    reason: str = ""


@dataclass
class Position:
    """持仓"""
    position_id: str
    code: str
    name: str
    entry_price: float
    shares: int
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    status: str = "holding"
    entry_date: str = ""
    stop_loss: float = 0.0
    take_profit: float = 0.0


class OrderManager:
    """订单管理器"""

    def __init__(self):
        self.orders: list[Order] = []
        self.positions: list[Position] = []

    def create_order(self, code: str, name: str, direction: OrderDirection,
                     price: float, shares: int, reason: str = "") -> Order:
        """创建订单"""
        import uuid
        order = Order(
            order_id=f"ORD-{uuid.uuid4().hex[:8].upper()}",
            code=code,
            name=name,
            direction=direction,
            price=price,
            shares=shares,
            created_at=datetime.now().isoformat(),
            reason=reason,
        )
        self.orders.append(order)
        return order

    def submit_order(self, order_id: str) -> bool:
        """提交订单"""
        order = self._find_order(order_id)
        if not order or order.status != OrderStatus.PENDING:
            return False
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = datetime.now().isoformat()
        return True

    def fill_order(self, order_id: str, filled_price: float, filled_shares: int,
                   commission: float = 0.0) -> bool:
        """订单成交"""
        order = self._find_order(order_id)
        if not order or order.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL):
            return False

        order.filled_price = filled_price
        order.filled_shares = filled_shares
        order.commission = commission
        order.filled_at = datetime.now().isoformat()

        if filled_shares >= order.shares:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIAL

        # 更新持仓
        if order.direction == OrderDirection.BUY:
            self._add_position(order)
        else:
            self._reduce_position(order)

        return True

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单"""
        order = self._find_order(order_id)
        if not order or order.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            return False
        order.status = OrderStatus.CANCELLED
        return True

    def get_orders(self, status: str = None, limit: int = 100) -> list[Order]:
        """获取订单列表"""
        result = self.orders
        if status:
            result = [o for o in result if o.status.value == status]
        return result[-limit:]

    def get_positions(self, status: str = "holding") -> list[Position]:
        """获取持仓列表"""
        return [p for p in self.positions if p.status == status]

    def update_position_price(self, code: str, current_price: float):
        """更新持仓价格"""
        for pos in self.positions:
            if pos.code == code and pos.status == "holding":
                pos.current_price = current_price
                pos.market_value = current_price * pos.shares
                pos.unrealized_pnl = (current_price - pos.entry_price) * pos.shares
                pos.unrealized_pnl_pct = (current_price / pos.entry_price - 1) * 100

    def _find_order(self, order_id: str) -> Optional[Order]:
        """查找订单"""
        for o in self.orders:
            if o.order_id == order_id:
                return o
        return None

    def _add_position(self, order: Order):
        """增加持仓"""
        # 检查是否已有持仓
        for pos in self.positions:
            if pos.code == order.code and pos.status == "holding":
                # 加仓，更新成本
                total_cost = pos.entry_price * pos.shares + order.filled_price * order.filled_shares
                total_shares = pos.shares + order.filled_shares
                pos.entry_price = total_cost / total_shares
                pos.shares = total_shares
                pos.market_value = pos.current_price * pos.shares
                return

        # 新建持仓
        pos = Position(
            position_id=f"POS-{order.order_id}",
            code=order.code,
            name=order.name,
            entry_price=order.filled_price,
            shares=order.filled_shares,
            current_price=order.filled_price,
            market_value=order.filled_price * order.filled_shares,
            status="holding",
            entry_date=datetime.now().strftime("%Y-%m-%d"),
        )
        self.positions.append(pos)

    def _reduce_position(self, order: Order):
        """减少持仓"""
        for pos in self.positions:
            if pos.code == order.code and pos.status == "holding":
                pos.shares -= order.filled_shares
                if pos.shares <= 0:
                    pos.status = "closed"
                    pos.shares = 0
                pos.market_value = pos.current_price * pos.shares
                return


# 全局单例
_order_manager: OrderManager | None = None


def get_order_manager() -> OrderManager:
    """获取订单管理器单例"""
    global _order_manager
    if _order_manager is None:
        _order_manager = OrderManager()
    return _order_manager
