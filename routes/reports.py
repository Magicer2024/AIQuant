"""
routes/reports.py —— 报告生成API

路由：
  GET  /api/reports/position         → 持仓报告（HTML）
  GET  /api/reports/trade            → 交易报告（HTML）
  GET  /api/reports/risk             → 风控报告（HTML）
  GET  /api/reports/daily            → 综合日报（HTML）
  GET  /api/reports/download/<type>  → 下载报告文件
"""

import os
from datetime import datetime

from flask import Blueprint, jsonify, request, Response

from ministries.rites.report_generator import get_report_generator
from ministries.war.order_manager import get_order_manager, OrderStatus
from ministries.rites.data_source_manager import get_data_source_manager

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")
_gen = get_report_generator()
_om = get_order_manager()

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


@reports_bp.route("/position", methods=["GET"])
def position_report():
    """持仓报告"""
    # 获取账户数据
    positions = _om.get_positions(status="holding")

    # 更新价格
    ds = get_data_source_manager()
    for pos in positions:
        try:
            data = ds.get_daily_price(pos.code)
            if data and len(data) > 0:
                _om.update_position_price(pos.code, data[-1].get("close", pos.current_price))
        except Exception:
            pass

    positions = _om.get_positions(status="holding")

    # 计算账户概览
    total_cost = sum(p.entry_price * p.shares for p in positions)
    total_mv = sum(p.market_value for p in positions)
    total_pnl = sum(p.unrealized_pnl for p in positions)
    initial = 1_000_000.0
    commissions = sum(o.commission for o in _om.get_orders(limit=1000) if o.status == OrderStatus.FILLED)
    cash = initial - total_cost - commissions

    account = {
        "initial_capital": initial,
        "cash": cash,
        "market_value": total_mv,
        "total_assets": cash + total_mv,
        "total_return": ((cash + total_mv) / initial - 1) * 100,
    }

    html = _gen.generate_position_report(account, [_position_to_dict(p) for p in positions])
    return _html_response(html, "持仓报告")


@reports_bp.route("/trade", methods=["GET"])
def trade_report():
    """交易报告"""
    orders = _om.get_orders(limit=500)
    html = _gen.generate_trade_report([_order_to_dict(o) for o in orders])
    return _html_response(html, "交易报告")


@reports_bp.route("/risk", methods=["GET"])
def risk_report():
    """风控报告"""
    # 简化的风控状态（实际应从 risk 模块获取）
    risk_status = {
        "overall_level": "NORMAL",
        "block_count": 0,
        "restrict_count": 0,
        "warning_count": 0,
    }
    events = []
    html = _gen.generate_risk_report(risk_status, events)
    return _html_response(html, "风控报告")


@reports_bp.route("/daily", methods=["GET"])
def daily_report():
    """综合日报"""
    positions = _om.get_positions(status="holding")
    orders = _om.get_orders(limit=50)

    ds = get_data_source_manager()
    for pos in positions:
        try:
            data = ds.get_daily_price(pos.code)
            if data and len(data) > 0:
                _om.update_position_price(pos.code, data[-1].get("close", pos.current_price))
        except Exception:
            pass

    positions = _om.get_positions(status="holding")
    total_cost = sum(p.entry_price * p.shares for p in positions)
    commissions = sum(o.commission for o in _om.get_orders(limit=1000) if o.status == OrderStatus.FILLED)
    cash = 1_000_000.0 - total_cost - commissions

    data = {
        "account": {
            "total_assets": cash + sum(p.market_value for p in positions),
            "total_return": ((cash + sum(p.market_value for p in positions)) / 1_000_000.0 - 1) * 100,
        },
        "positions": [_position_to_dict(p) for p in positions],
        "orders": [_order_to_dict(o) for o in orders],
        "risk": {"overall_level": "NORMAL"},
        "data_source": ds.primary.value if hasattr(ds, "primary") else "local",
        "strategy_mode": "default",
    }

    html = _gen.generate_daily_report(data)
    return _html_response(html, "每日报告")


@reports_bp.route("/save/<report_type>", methods=["POST"])
def save_report(report_type: str):
    """保存报告到文件"""
    if report_type == "position":
        html = _gen.generate_position_report({}, [])
    elif report_type == "trade":
        html = _gen.generate_trade_report([])
    elif report_type == "risk":
        html = _gen.generate_risk_report({}, [])
    elif report_type == "daily":
        html = _gen.generate_daily_report({})
    else:
        return jsonify({"success": False, "error": "未知报告类型"}), 400

    filename = f"{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    filepath = os.path.join(REPORTS_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return jsonify({
        "success": True,
        "filename": filename,
        "url": f"/reports/{filename}",
    })


# ── 辅助函数 ──────────────────────────────────────

def _html_response(html: str, title: str) -> Response:
    """返回HTML响应"""
    return Response(html, mimetype="text/html")


def _order_to_dict(o) -> dict:
    return {
        "order_id": o.order_id,
        "code": o.code,
        "name": o.name,
        "direction": o.direction.value,
        "price": o.price,
        "shares": o.shares,
        "status": o.status.value,
        "filled_price": o.filled_price,
        "filled_shares": o.filled_shares,
        "commission": o.commission,
        "reason": o.reason,
        "created_at": o.created_at,
    }


def _position_to_dict(p) -> dict:
    return {
        "code": p.code,
        "name": p.name,
        "shares": p.shares,
        "entry_price": p.entry_price,
        "current_price": p.current_price,
        "market_value": p.market_value,
        "unrealized_pnl": p.unrealized_pnl,
        "unrealized_pnl_pct": p.unrealized_pnl_pct,
    }
