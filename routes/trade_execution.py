"""
routes/trade_execution.py —— 交易执行API

路由：
  POST /api/trade/order              → 创建订单
  GET  /api/trade/orders             → 订单列表
  POST /api/trade/order/<id>/cancel  → 撤单
  GET  /api/trade/positions          → 持仓列表
  POST /api/trade/position/update    → 更新持仓价格
  GET  /api/trade/account            → 账户概览
  POST /api/trade/simulate/fill      → 模拟成交（测试用）
"""

from flask import Blueprint, jsonify, request

from ministries.war.order_manager import get_order_manager, OrderDirection, OrderStatus
from ministries.rites.data_source_manager import get_data_source_manager

trade_exec_bp = Blueprint("trade_execution", __name__, url_prefix="/api/trade")
_om = get_order_manager()


@trade_exec_bp.route("/order", methods=["POST"])
def create_order():
    """创建交易订单"""
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    name = data.get("name", code)
    direction = data.get("direction", "").lower()
    price = data.get("price", 0.0)
    shares = data.get("shares", 0)
    reason = data.get("reason", "")
    simulate = data.get("simulate", True)  # 默认模拟交易

    if not code or not direction or price <= 0 or shares <= 0:
        return jsonify({
            "success": False,
            "error": "参数错误：需要 code, direction(buy/sell), price, shares",
        }), 400

    if direction not in ("buy", "sell"):
        return jsonify({"success": False, "error": "direction 必须是 buy 或 sell"}), 400

    dir_enum = OrderDirection.BUY if direction == "buy" else OrderDirection.SELL
    order = _om.create_order(code, name, dir_enum, price, shares, reason)

    # 模拟交易：自动提交并撮合
    if simulate:
        _om.submit_order(order.order_id)
        # 模拟以当前价成交
        _om.fill_order(order.order_id, price, shares, commission=price * shares * 0.0003)
        order.status = OrderStatus.FILLED

    return jsonify({
        "success": True,
        "order": _order_to_dict(order),
        "mode": "simulate" if simulate else "live",
    })


@trade_exec_bp.route("/orders", methods=["GET"])
def list_orders():
    """获取订单列表"""
    status = request.args.get("status")
    limit = request.args.get("limit", 100, type=int)
    orders = _om.get_orders(status=status, limit=limit)
    return jsonify({
        "success": True,
        "count": len(orders),
        "orders": [_order_to_dict(o) for o in orders],
    })


@trade_exec_bp.route("/order/<order_id>/cancel", methods=["POST"])
def cancel_order(order_id: str):
    """撤销订单"""
    ok = _om.cancel_order(order_id)
    return jsonify({
        "success": ok,
        "message": "已撤单" if ok else "撤单失败（订单不存在或已成交）",
    })


@trade_exec_bp.route("/positions", methods=["GET"])
def list_positions():
    """获取持仓列表"""
    positions = _om.get_positions(status="holding")

    # 更新当前价格
    ds = get_data_source_manager()
    for pos in positions:
        try:
            data = ds.get_daily_price(pos.code)
            if data and len(data) > 0:
                latest = data[-1]
                current = latest.get("close", pos.current_price)
                _om.update_position_price(pos.code, current)
        except Exception:
            pass

    # 重新获取更新后的持仓
    positions = _om.get_positions(status="holding")

    return jsonify({
        "success": True,
        "count": len(positions),
        "positions": [_position_to_dict(p) for p in positions],
    })


@trade_exec_bp.route("/position/update", methods=["POST"])
def update_positions():
    """批量更新持仓价格"""
    data = request.get_json() or {}
    prices = data.get("prices", {})  # {code: price}

    updated = []
    for code, price in prices.items():
        _om.update_position_price(code.upper(), float(price))
        updated.append(code)

    return jsonify({
        "success": True,
        "updated": updated,
    })


@trade_exec_bp.route("/account", methods=["GET"])
def get_account():
    """获取账户概览"""
    positions = _om.get_positions(status="holding")
    orders = _om.get_orders(limit=1000)

    total_market_value = sum(p.market_value for p in positions)
    total_unrealized_pnl = sum(p.unrealized_pnl for p in positions)
    total_cost = sum(p.entry_price * p.shares for p in positions)

    # 已实现盈亏（从已关闭持仓估算，简化处理）
    filled_orders = [o for o in orders if o.status == OrderStatus.FILLED]
    total_commission = sum(o.commission for o in filled_orders)

    # 模拟初始资金 100万
    initial_capital = 1_000_000.0
    cash = initial_capital - total_cost - total_commission
    total_assets = cash + total_market_value

    return jsonify({
        "success": True,
        "account": {
            "initial_capital": initial_capital,
            "cash": round(cash, 2),
            "market_value": round(total_market_value, 2),
            "total_assets": round(total_assets, 2),
            "total_return": round((total_assets / initial_capital - 1) * 100, 2),
            "unrealized_pnl": round(total_unrealized_pnl, 2),
            "total_commission": round(total_commission, 2),
            "position_count": len(positions),
            "order_count": len(orders),
        },
    })


@trade_exec_bp.route("/simulate/fill", methods=["POST"])
def simulate_fill():
    """手动模拟成交（测试用）"""
    data = request.get_json() or {}
    order_id = data.get("order_id", "")
    filled_price = data.get("filled_price", 0.0)
    filled_shares = data.get("filled_shares", 0)

    if not order_id or filled_price <= 0 or filled_shares <= 0:
        return jsonify({"success": False, "error": "参数错误"}), 400

    ok = _om.fill_order(order_id, filled_price, filled_shares)
    return jsonify({
        "success": ok,
        "message": "已成交" if ok else "成交失败",
    })


# ── 辅助函数 ──────────────────────────────────────

def _order_to_dict(order) -> dict:
    return {
        "order_id": order.order_id,
        "code": order.code,
        "name": order.name,
        "direction": order.direction.value,
        "price": order.price,
        "shares": order.shares,
        "status": order.status.value,
        "filled_price": order.filled_price,
        "filled_shares": order.filled_shares,
        "commission": order.commission,
        "reason": order.reason,
        "created_at": order.created_at,
        "filled_at": order.filled_at,
    }


def _position_to_dict(pos) -> dict:
    return {
        "position_id": pos.position_id,
        "code": pos.code,
        "name": pos.name,
        "entry_price": round(pos.entry_price, 2),
        "shares": pos.shares,
        "current_price": round(pos.current_price, 2),
        "market_value": round(pos.market_value, 2),
        "unrealized_pnl": round(pos.unrealized_pnl, 2),
        "unrealized_pnl_pct": round(pos.unrealized_pnl_pct, 2),
        "status": pos.status,
        "entry_date": pos.entry_date,
        "stop_loss": pos.stop_loss,
        "take_profit": pos.take_profit,
    }
