"""
routes/broker.py —— 交易接口管理API

路由：
  GET  /api/broker/status         → 交易接口状态
  POST /api/broker/connect        → 连接交易终端
  POST /api/broker/disconnect     → 断开连接
  POST /api/broker/switch         → 切换交易模式
  POST /api/broker/order          → 下单
  POST /api/broker/order/<id>/cancel → 撤单
  GET  /api/broker/orders         → 查询订单
  GET  /api/broker/positions      → 查询持仓
  GET  /api/broker/account        → 查询账户
"""

from flask import Blueprint, jsonify, request

from ministries.war.broker_adapter import (
    get_broker_manager, BrokerType, OrderSide, OrderType
)

broker_bp = Blueprint("broker", __name__, url_prefix="/api/broker")
_mgr = get_broker_manager()


@broker_bp.route("/status", methods=["GET"])
def get_status():
    """获取交易接口状态"""
    broker = _mgr.get_broker()
    account = broker.get_account() if broker else None
    return jsonify({
        "success": True,
        "mode": _mgr.get_current_type(),
        "connected": broker.is_connected() if broker else False,
        "account": {
            "total_assets": account.total_assets if account else 0,
            "cash": account.cash if account else 0,
            "market_value": account.market_value if account else 0,
        } if account else None,
    })


@broker_bp.route("/connect", methods=["POST"])
def connect():
    """连接交易终端"""
    broker = _mgr.get_broker()
    ok = broker.connect() if broker else False
    return jsonify({
        "success": ok,
        "message": "已连接" if ok else "连接失败",
        "mode": _mgr.get_current_type(),
    })


@broker_bp.route("/disconnect", methods=["POST"])
def disconnect():
    """断开交易终端"""
    broker = _mgr.get_broker()
    if broker:
        broker.disconnect()
    return jsonify({
        "success": True,
        "message": "已断开",
    })


@broker_bp.route("/switch", methods=["POST"])
def switch_mode():
    """切换交易模式"""
    data = request.get_json() or {}
    mode = data.get("mode", "simulation")

    type_map = {
        "simulation": BrokerType.SIMULATION,
        "easytrader": BrokerType.EASYTRADER,
        "api": BrokerType.API,
    }

    bt = type_map.get(mode)
    if not bt:
        return jsonify({"success": False, "error": f"未知模式: {mode}"}), 400

    ok = _mgr.switch(bt)
    return jsonify({
        "success": ok,
        "message": f"已切换为 {mode}" if ok else f"未注册 {mode} 接口",
        "mode": mode,
    })


@broker_bp.route("/order", methods=["POST"])
def place_order():
    """下单"""
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    name = data.get("name", code)
    side_str = data.get("side", "").lower()
    price = data.get("price", 0.0)
    shares = data.get("shares", 0)
    order_type_str = data.get("order_type", "limit").lower()

    if not code or not side_str or price <= 0 or shares <= 0:
        return jsonify({
            "success": False,
            "error": "参数错误：需要 code, side(buy/sell), price, shares",
        }), 400

    side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
    order_type = OrderType.MARKET if order_type_str == "market" else OrderType.LIMIT

    broker = _mgr.get_broker()
    if not broker:
        return jsonify({"success": False, "error": "交易接口未初始化"}), 500

    if not broker.is_connected():
        broker.connect()

    order = broker.place_order(code, side, price, shares, order_type, name)

    return jsonify({
        "success": order.status not in ("rejected",),
        "order": _order_to_dict(order),
    })


@broker_bp.route("/order/<order_id>/cancel", methods=["POST"])
def cancel_order(order_id: str):
    """撤单"""
    broker = _mgr.get_broker()
    if not broker:
        return jsonify({"success": False, "error": "交易接口未初始化"}), 500

    ok = broker.cancel_order(order_id)
    return jsonify({
        "success": ok,
        "message": "已撤单" if ok else "撤单失败",
    })


@broker_bp.route("/orders", methods=["GET"])
def get_orders():
    """查询订单"""
    status = request.args.get("status")
    broker = _mgr.get_broker()
    if not broker:
        return jsonify({"success": False, "error": "交易接口未初始化"}), 500

    orders = broker.get_orders(status=status)
    return jsonify({
        "success": True,
        "count": len(orders),
        "orders": [_order_to_dict(o) for o in orders],
    })


@broker_bp.route("/positions", methods=["GET"])
def get_positions():
    """查询持仓"""
    broker = _mgr.get_broker()
    if not broker:
        return jsonify({"success": False, "error": "交易接口未初始化"}), 500

    positions = broker.get_positions()
    return jsonify({
        "success": True,
        "count": len(positions),
        "positions": [_position_to_dict(p) for p in positions],
    })


@broker_bp.route("/account", methods=["GET"])
def get_account():
    """查询账户"""
    broker = _mgr.get_broker()
    if not broker:
        return jsonify({"success": False, "error": "交易接口未初始化"}), 500

    account = broker.get_account()
    return jsonify({
        "success": True,
        "account": {
            "account_id": account.account_id,
            "total_assets": account.total_assets,
            "cash": account.cash,
            "market_value": account.market_value,
            "frozen_cash": account.frozen_cash,
            "available_cash": account.available_cash,
        },
    })


# ── 辅助函数 ──────────────────────────────────────

def _order_to_dict(o) -> dict:
    return {
        "order_id": o.order_id,
        "code": o.code,
        "name": o.name,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "price": o.price,
        "shares": o.shares,
        "filled_shares": o.filled_shares,
        "filled_price": o.filled_price,
        "status": o.status,
        "message": o.message,
        "created_at": o.created_at,
    }


def _position_to_dict(p) -> dict:
    return {
        "code": p.code,
        "name": p.name,
        "shares": p.shares,
        "available_shares": p.available_shares,
        "avg_cost": p.avg_cost,
        "current_price": p.current_price,
        "market_value": p.market_value,
        "unrealized_pnl": p.unrealized_pnl,
        "unrealized_pnl_pct": p.unrealized_pnl_pct,
    }
