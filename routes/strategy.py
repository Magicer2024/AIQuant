"""
routes/strategy.py —— 策略管理 API
"""
import math
import threading
from flask import Blueprint, request, jsonify
from strategy.rules_store import list_rules, get_rule, toggle_active, delete_rule
from backtest.trade_store import get_rule_trades
from strategy.mining_state import get_mining_status, request_stop

strategy_bp = Blueprint("strategy", __name__, url_prefix="/api/strategy")


def _sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    # numpy int/float → Python native (prevents BLOB storage in SQLite)
    if hasattr(obj, "item"):  # numpy scalar
        return _sanitize(obj.item())
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@strategy_bp.route("/rules", methods=["GET"])
def get_rules():
    """获取所有策略规则 GET /api/strategy/rules"""
    active_only = request.args.get("active_only", "0") == "1"
    rules = list_rules(active_only=active_only)
    return jsonify({"success": True, "data": _sanitize(rules), "error": None})


@strategy_bp.route("/rules/<int:rule_id>", methods=["GET"])
def get_rule_detail(rule_id: int):
    """获取单条规则详情"""
    rule = get_rule(rule_id)
    if not rule:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404
    return jsonify({"success": True, "data": _sanitize(dict(rule)), "error": None})


@strategy_bp.route("/rules/<int:rule_id>/toggle", methods=["PUT"])
def toggle_rule(rule_id: int):
    """启用/禁用策略 PUT /api/strategy/rules/1/toggle"""
    result = toggle_active(rule_id)
    if result is None:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404
    return jsonify({"success": True, "data": result, "error": None})


@strategy_bp.route("/rules/<int:rule_id>", methods=["DELETE"])
def remove_rule(rule_id: int):
    """删除策略 DELETE /api/strategy/rules/1"""
    delete_rule(rule_id)
    return jsonify({"success": True, "data": None, "error": None})


@strategy_bp.route("/rules/<int:rule_id>/trades", methods=["GET"])
def rule_trades(rule_id: int):
    """获取某策略的历史交易明细"""
    rule = get_rule(rule_id)
    if not rule:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404
    trades = get_rule_trades(str(rule_id))
    return jsonify({"success": True, "data": _sanitize(trades), "error": None})


@strategy_bp.route("/mine", methods=["POST"])
def start_mining():
    """触发策略挖掘 POST /api/strategy/mine"""
    status = get_mining_status()
    if status["running"]:
        return jsonify({"success": False, "data": None, "error": "Mining already in progress"}), 409

    body = request.get_json(silent=True) or {}
    trade_date = body.get("trade_date") or _get_latest_trade_date()
    if not trade_date:
        return jsonify({"success": False, "data": None, "error": "No trade_date available"}), 400

    status.update({"running": True, "progress": None, "result": None, "stop_requested": False})

    def _run():
        try:
            from strategy.miner import mine_strategies
            saved = mine_strategies(trade_date)
            status.update({"running": False, "progress": "done", "result": saved, "stop_requested": False})
        except Exception as e:
            status.update({"running": False, "progress": "error", "result": str(e), "stop_requested": False})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "data": {"status": "started", "trade_date": trade_date}, "error": None}), 202


@strategy_bp.route("/mine/status", methods=["GET"])
def mining_status():
    """查询挖掘进度 GET /api/strategy/mine/status"""
    return jsonify({"success": True, "data": dict(get_mining_status()), "error": None})


@strategy_bp.route("/mine/stop", methods=["POST"])
def stop_mining():
    """请求停止策略挖掘（幂等） POST /api/strategy/mine/stop"""
    status = get_mining_status()
    if not status["running"]:
        return jsonify({"success": True, "message": "No mining in progress"})
    request_stop()
    return jsonify({"success": True, "message": "Stop requested"})


@strategy_bp.route("/mine/reset", methods=["POST"])
def reset_mining():
    """重置挖掘状态 POST /api/strategy/mine/reset"""
    status = get_mining_status()
    status.update({"running": False, "progress": None, "result": None, "stop_requested": False})
    return jsonify({"success": True, "message": "Mining status reset"})


def _get_latest_trade_date() -> str:
    from core.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM daily_price").fetchone()
        return row["d"] if row else ""
