"""
ministries/war/broker_adapter.py —— 交易接口抽象层

职责：
  1. 统一交易接口（下单、查单、撤单、查持仓、查资金）
  2. 支持模拟/实盘切换
  3. 预留 EasyTrader 等实盘接口适配器

模式：
  - simulation: 模拟交易（默认）
  - easytrader: 同花顺/东方财富自动化（需安装 easytrader）
  - api: 券商直接API（预留）
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class BrokerType(Enum):
    """券商类型"""
    SIMULATION = "simulation"
    EASYTRADER = "easytrader"
    API = "api"


class OrderSide(Enum):
    """买卖方向"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """订单类型"""
    LIMIT = "limit"       # 限价
    MARKET = "market"     # 市价


@dataclass
class BrokerOrder:
    """统一订单结构"""
    order_id: str
    code: str
    name: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    shares: int = 0
    filled_shares: int = 0
    filled_price: float = 0.0
    status: str = "pending"    # pending/submitted/partial/filled/cancelled/rejected
    message: str = ""          # 错误信息
    created_at: str = ""
    updated_at: str = ""


@dataclass
class BrokerPosition:
    """统一持仓结构"""
    code: str
    name: str = ""
    shares: int = 0
    available_shares: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0


@dataclass
class BrokerAccount:
    """统一账户结构"""
    account_id: str = ""
    total_assets: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    frozen_cash: float = 0.0
    available_cash: float = 0.0


class BaseBroker(ABC):
    """交易接口抽象基类"""

    @property
    @abstractmethod
    def broker_type(self) -> BrokerType:
        pass

    @abstractmethod
    def connect(self) -> bool:
        """连接交易终端"""
        pass

    @abstractmethod
    def disconnect(self):
        """断开连接"""
        pass

    @abstractmethod
    def place_order(self, code: str, side: OrderSide, price: float, shares: int,
                    order_type: OrderType = OrderType.LIMIT, name: str = "") -> BrokerOrder:
        """下单"""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        pass

    @abstractmethod
    def get_orders(self, status: str = None) -> list[BrokerOrder]:
        """查询订单"""
        pass

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        """查询持仓"""
        pass

    @abstractmethod
    def get_account(self) -> BrokerAccount:
        """查询账户资金"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接"""
        pass


class SimulationBroker(BaseBroker):
    """模拟交易接口"""

    def __init__(self, initial_cash: float = 1_000_000.0):
        self._connected = False
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self._orders: list[BrokerOrder] = []
        self._positions: dict[str, BrokerPosition] = {}
        self._order_counter = 0

    @property
    def broker_type(self) -> BrokerType:
        return BrokerType.SIMULATION

    def connect(self) -> bool:
        self._connected = True
        print("[SimulationBroker] 模拟交易已连接")
        return True

    def disconnect(self):
        self._connected = False
        print("[SimulationBroker] 模拟交易已断开")

    def place_order(self, code: str, side: OrderSide, price: float, shares: int,
                    order_type: OrderType = OrderType.LIMIT, name: str = "") -> BrokerOrder:
        self._order_counter += 1
        order_id = f"SIM-{datetime.now().strftime('%Y%m%d')}-{self._order_counter:04d}"

        order = BrokerOrder(
            order_id=order_id,
            code=code,
            name=name or code,
            side=side,
            order_type=order_type,
            price=price,
            shares=shares,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

        # 模拟资金检查
        if side == OrderSide.BUY:
            cost = price * shares * 1.0003  # 含手续费
            if cost > self.cash:
                order.status = "rejected"
                order.message = "资金不足"
                self._orders.append(order)
                return order
            self.cash -= cost

        # 模拟立即成交
        order.status = "filled"
        order.filled_shares = shares
        order.filled_price = price
        order.updated_at = datetime.now().isoformat()

        # 更新持仓
        self._update_position(code, name or code, side, price, shares)
        self._orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        for o in self._orders:
            if o.order_id == order_id and o.status in ("pending", "submitted"):
                o.status = "cancelled"
                o.updated_at = datetime.now().isoformat()
                # 退回资金
                if o.side == OrderSide.BUY:
                    self.cash += o.price * o.shares * 1.0003
                return True
        return False

    def get_orders(self, status: str = None) -> list[BrokerOrder]:
        orders = self._orders
        if status:
            orders = [o for o in orders if o.status == status]
        return sorted(orders, key=lambda x: x.created_at, reverse=True)

    def get_positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    def get_account(self) -> BrokerAccount:
        market_value = sum(p.market_value for p in self._positions.values())
        return BrokerAccount(
            account_id="SIM001",
            total_assets=self.cash + market_value,
            cash=self.cash,
            market_value=market_value,
            available_cash=self.cash,
        )

    def is_connected(self) -> bool:
        return self._connected

    def _update_position(self, code: str, name: str, side: OrderSide, price: float, shares: int):
        """更新持仓"""
        pos = self._positions.get(code)
        if pos is None:
            pos = BrokerPosition(code=code, name=name)
            self._positions[code] = pos

        if side == OrderSide.BUY:
            total_cost = pos.avg_cost * pos.shares + price * shares
            pos.shares += shares
            pos.avg_cost = total_cost / pos.shares if pos.shares > 0 else 0
        else:
            pos.shares -= shares
            if pos.shares <= 0:
                del self._positions[code]
                return

        pos.current_price = price
        pos.market_value = pos.current_price * pos.shares
        pos.unrealized_pnl = (pos.current_price - pos.avg_cost) * pos.shares
        pos.unrealized_pnl_pct = (pos.current_price / pos.avg_cost - 1) * 100 if pos.avg_cost > 0 else 0


class EasyTraderBroker(BaseBroker):
    """EasyTrader 实盘接口适配器（预留）"""

    def __init__(self, client_type: str = "ths"):
        self.client_type = client_type
        self._connected = False
        self._client = None

    @property
    def broker_type(self) -> BrokerType:
        return BrokerType.EASYTRADER

    def connect(self) -> bool:
        """连接 EasyTrader 客户端"""
        try:
            import easytrader
            self._client = easytrader.use(self.client_type)
            self._client.connect()
            self._connected = True
            print(f"[EasyTraderBroker] 已连接: {self.client_type}")
            return True
        except ImportError:
            print("[EasyTraderBroker] 未安装 easytrader，请先 pip install easytrader")
            return False
        except Exception as e:
            print(f"[EasyTraderBroker] 连接失败: {e}")
            return False

    def disconnect(self):
        self._connected = False
        self._client = None

    def place_order(self, code: str, side: OrderSide, price: float, shares: int,
                    order_type: OrderType = OrderType.LIMIT, name: str = "") -> BrokerOrder:
        if not self._client:
            raise RuntimeError("未连接交易终端")

        et_side = "buy" if side == OrderSide.BUY else "sell"
        try:
            result = self._client.et_fn(et_side, code, price=price, amount=shares)
            # 解析返回值
            order_id = str(result.get("entrust_no", ""))
            return BrokerOrder(
                order_id=order_id,
                code=code,
                name=name,
                side=side,
                price=price,
                shares=shares,
                status="submitted",
                created_at=datetime.now().isoformat(),
            )
        except Exception as e:
            return BrokerOrder(
                order_id="",
                code=code,
                status="rejected",
                message=str(e),
            )

    def cancel_order(self, order_id: str) -> bool:
        if not self._client:
            return False
        try:
            self._client.cancel_entrust(order_id)
            return True
        except Exception:
            return False

    def get_orders(self, status: str = None) -> list[BrokerOrder]:
        if not self._client:
            return []
        try:
            records = self._client.today_entrusts
            return [_convert_et_order(r) for r in records]
        except Exception:
            return []

    def get_positions(self) -> list[BrokerPosition]:
        if not self._client:
            return []
        try:
            records = self._client.today_trades
            # 简化处理
            return []
        except Exception:
            return []

    def get_account(self) -> BrokerAccount:
        if not self._client:
            return BrokerAccount()
        try:
            info = self._client.balance
            return BrokerAccount(
                total_assets=info.get("总资产", 0),
                cash=info.get("可用金额", 0),
                market_value=info.get("股票市值", 0),
            )
        except Exception:
            return BrokerAccount()

    def is_connected(self) -> bool:
        return self._connected


def _convert_et_order(record: dict) -> BrokerOrder:
    """转换 EasyTrader 订单记录"""
    return BrokerOrder(
        order_id=str(record.get("合同编号", "")),
        code=record.get("证券代码", ""),
        name=record.get("证券名称", ""),
        side=OrderSide.BUY if "买入" in str(record.get("操作", "")) else OrderSide.SELL,
        price=float(record.get("委托价格", 0)),
        shares=int(record.get("委托数量", 0)),
        filled_shares=int(record.get("成交数量", 0)),
        status=str(record.get("备注", "")),
    )


class BrokerManager:
    """交易接口管理器"""

    def __init__(self):
        self._brokers: dict[BrokerType, BaseBroker] = {}
        self._current: BrokerType = BrokerType.SIMULATION
        self._register_brokers()

    def _register_brokers(self):
        """注册默认接口"""
        self.register(BrokerType.SIMULATION, SimulationBroker())

    def register(self, broker_type: BrokerType, broker: BaseBroker):
        """注册交易接口"""
        self._brokers[broker_type] = broker

    def get_broker(self, broker_type: BrokerType = None) -> BaseBroker:
        """获取交易接口"""
        bt = broker_type or self._current
        return self._brokers.get(bt)

    def switch(self, broker_type: BrokerType) -> bool:
        """切换交易接口"""
        if broker_type not in self._brokers:
            return False
        self._current = broker_type
        return True

    def get_current_type(self) -> str:
        return self._current.value


# 全局单例
_broker_manager: BrokerManager | None = None


def get_broker_manager() -> BrokerManager:
    """获取交易接口管理器"""
    global _broker_manager
    if _broker_manager is None:
        _broker_manager = BrokerManager()
    return _broker_manager
