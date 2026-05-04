"""
governance/secretariat/execution_engine.py —— 尚书省执行引擎

职责：
  1. 接收交易信号
  2. 风控检查（调用门下省）
  3. 生成交易订单
  4. 调度兵部执行
  5. 记录执行结果
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from risk.guard import check_risk, build_account_state
from risk.models import RiskLevel


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"          # 待提交
    SUBMITTED = "submitted"      # 已提交
    PARTIAL = "partial"          # 部分成交
    FILLED = "filled"            # 全部成交
    CANCELLED = "cancelled"      # 已撤销
    REJECTED = "rejected"        # 已拒绝


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
    reason: str = ""
    note: str = ""


class ExecutionEngine:
    """尚书省执行引擎"""

    def __init__(self):
        self.pending_orders: list[Order] = []
        self.executed_orders: list[Order] = []

    def submit_signal(self, signal: dict, account_state: dict) -> dict:
        """
        提交交易信号，经过风控检查后生成订单

        :param signal: {"code": "", "name": "", "price": 0, "shares": 0}
        :param account_state: 账户状态
        :return: {"success": bool, "order": Order, "risk_check": dict}
        """
        # 1. 风控检查
        risk_result = check_risk(account_state)
        if risk_result.get("blocked"):
            return {
                "success": False,
                "blocked": True,
                "reason": risk_result.get("block_reason", "风控拦截"),
                "risk_check": risk_result,
            }

        # 2. 生成订单
        order = Order(
            order_id=f"ORD-{uuid.uuid4().hex[:8].upper()}",
            code=signal["code"],
            name=signal.get("name", ""),
            direction=OrderDirection.BUY,
            price=signal["price"],
            shares=signal.get("shares", 0),
            created_at=datetime.now().isoformat(),
            reason=signal.get("reason", "策略信号"),
        )

        self.pending_orders.append(order)

        return {
            "success": True,
            "blocked": False,
            "order": order,
            "risk_check": risk_result,
        }

    def execute_order(self, order_id: str, broker: str = "sim") -> dict:
        """
        执行订单

        :param order_id: 订单ID
        :param broker: 券商通道 sim=模拟 trade=实盘
        :return: 执行结果
        """
        order = self._find_order(order_id)
        if not order:
            return {"success": False, "error": "订单不存在"}

        if broker == "sim":
            return self._execute_simulated(order)
        else:
            return self._execute_real(order)

    def _execute_simulated(self, order: Order) -> dict:
        """模拟执行（立即全部成交）"""
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now().isoformat()
        order.filled_price = order.price
        order.filled_shares = order.shares

        self.pending_orders = [o for o in self.pending_orders if o.order_id != order.order_id]
        self.executed_orders.append(order)

        return {
            "success": True,
            "order_id": order.order_id,
            "filled_price": order.filled_price,
            "filled_shares": order.filled_shares,
            "status": order.status.value,
        }

    def _execute_real(self, order: Order) -> dict:
        """实盘执行（预留接口）"""
        # TODO: 接入券商API
        return {"success": False, "error": "实盘交易暂未接入"}

    def cancel_order(self, order_id: str) -> dict:
        """撤销订单"""
        order = self._find_order(order_id)
        if not order:
            return {"success": False, "error": "订单不存在"}

        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            return {"success": False, "error": f"订单已{order.status.value}，无法撤销"}

        order.status = OrderStatus.CANCELLED
        self.pending_orders = [o for o in self.pending_orders if o.order_id != order.order_id]

        return {"success": True, "order_id": order_id, "status": "cancelled"}

    def get_pending_orders(self) -> list[Order]:
        """获取待执行订单"""
        return self.pending_orders

    def get_executed_orders(self, limit: int = 50) -> list[Order]:
        """获取已执行订单"""
        return self.executed_orders[-limit:]

    def _find_order(self, order_id: str) -> Optional[Order]:
        """查找订单"""
        for o in self.pending_orders + self.executed_orders:
            if o.order_id == order_id:
                return o
        return None


# 全局单例
_execution_engine: ExecutionEngine | None = None


def get_execution_engine() -> ExecutionEngine:
    """获取执行引擎单例"""
    global _execution_engine
    if _execution_engine is None:
        _execution_engine = ExecutionEngine()
    return _execution_engine
