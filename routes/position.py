"""
routes/position.py —— 持仓路由（薄层）
"""
import traceback
from flask import jsonify, request

from routes import position_bp
from services.position_service import (
    get_positions, add_position, close_position,
    partial_close_position, update_position_price,
    get_position_summary, delete_position, refresh_position_prices,
)


@position_bp.route("/list", methods=["GET"])
def list_positions():
    status = request.args.get("status", "holding")
    try:
        return jsonify({"positions": get_positions(status)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/add", methods=["POST"])
def _add_position():
    try:
        position_id = add_position(request.get_json() or {})
        return jsonify({"success": True, "position_id": position_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/close", methods=["POST"])
def _close_position():
    data = request.get_json() or {}
    position_id = data.get("position_id")
    exit_price = data.get("exit_price", 0)
    reason = data.get("reason", "manual")
    if not position_id or not exit_price:
        return jsonify({"error": "缺少必要参数"}), 400
    try:
        result = close_position(position_id, exit_price, reason=reason)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/partial_close", methods=["POST"])
def _partial_close():
    data = request.get_json() or {}
    position_id = data.get("position_id")
    exit_price = data.get("exit_price", 0)
    shares = data.get("shares", 0)
    reason = data.get("reason", "manual")
    if not position_id or not exit_price or not shares:
        return jsonify({"error": "缺少必要参数"}), 400
    if shares <= 0:
        return jsonify({"error": "卖出股数必须大于0"}), 400
    try:
        result = partial_close_position(position_id, exit_price, shares, reason=reason)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/update_price", methods=["POST"])
def _update_price():
    data = request.get_json() or {}
    position_id = data.get("position_id")
    current_price = data.get("current_price")
    if not position_id or not current_price:
        return jsonify({"error": "缺少必要参数"}), 400
    try:
        update_position_price(position_id, current_price)
        return jsonify({"success": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/summary", methods=["GET"])
def _summary():
    try:
        return jsonify(get_position_summary())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/delete", methods=["POST"])
def _delete():
    data = request.get_json() or {}
    position_id = data.get("position_id")
    if not position_id:
        return jsonify({"error": "缺少持仓ID"}), 400
    try:
        delete_position(position_id)
        return jsonify({"success": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@position_bp.route("/refresh_prices", methods=["POST"])
def _refresh_prices():
    try:
        result = refresh_position_prices()
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
