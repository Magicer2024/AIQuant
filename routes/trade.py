"""
routes/trade.py —— 交易记录路由（薄层）
"""
import traceback
from datetime import date
from flask import jsonify, request

from routes import trade_bp
import core.db as db


@trade_bp.route("", methods=["GET"])
def list_trades():
    code = request.args.get("code")
    limit = int(request.args.get("limit", 100))
    try:
        return jsonify({"trades": db.get_trades(code, limit)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@trade_bp.route("/add", methods=["POST"])
def add_trade():
    data = request.get_json() or {}
    try:
        trade_id = db.add_trade(
            position_id=data.get("position_id"),
            code=data.get("code", ""),
            name=data.get("name", ""),
            trade_date=data.get("trade_date", date.today().strftime("%Y-%m-%d")),
            direction=data.get("direction", "buy"),
            price=float(data.get("price", 0)),
            shares=int(data.get("shares", 0)),
            commission=float(data.get("commission", 0)),
            reason=data.get("reason", ""),
            note=data.get("note", ""),
        )
        return jsonify({"success": True, "trade_id": trade_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
