"""
routes/backtest.py —— 回测 API
"""
import math
import threading
from flask import Blueprint, request, jsonify
from backtest.reporter import build_result_list, build_result_detail
from backtest.trade_store import get_result, get_trades
from strategy.rules_store import get_rule

backtest_bp = Blueprint("backtest", __name__, url_prefix="/api/backtest")


def _sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


_backtest_status = {"running": False, "result_id": None}


@backtest_bp.route("/run", methods=["POST"])
def start_backtest():
    """触发回测 POST /api/backtest/run {rule_id, start_date, end_date}"""
    global _backtest_status
    if _backtest_status["running"]:
        return jsonify({"success": False, "data": None, "error": "Backtest already in progress"}), 409

    body = request.get_json(silent=True) or {}
    rule_id = body.get("rule_id")
    start_date = body.get("start_date", "2024-01-01")
    end_date = body.get("end_date", "2025-12-31")

    rule = get_rule(int(rule_id)) if rule_id else None
    if not rule:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404

    _backtest_status = {"running": True, "result_id": None}

    def _run():
        global _backtest_status
        try:
            from backtest.engine import run_backtest
            result = run_backtest(
                rule_name=rule["rule_name"],
                rule_id=str(rule["id"]),
                conditions_json=rule["conditions"] or "{}",
                start_date=start_date,
                end_date=end_date,
                save=True,
            )
            _backtest_status = {"running": False, "result_id": result.get("result_id")}
        except Exception as e:
            _backtest_status = {"running": False, "result_id": None, "error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "data": {"status": "started"}, "error": None}), 202


@backtest_bp.route("/status", methods=["GET"])
def backtest_status():
    """回测进度查询 GET /api/backtest/status"""
    return jsonify({"success": True, "data": _backtest_status, "error": None})


@backtest_bp.route("/results", methods=["GET"])
def results_list():
    """回测结果列表 GET /api/backtest/results"""
    results = build_result_list()
    return jsonify({"success": True, "data": _sanitize(results), "error": None})


@backtest_bp.route("/results/<int:result_id>", methods=["GET"])
def result_detail(result_id: int):
    """回测汇总 GET /api/backtest/results/1"""
    detail = build_result_detail(result_id)
    if not detail:
        return jsonify({"success": False, "data": None, "error": "Result not found"}), 404
    return jsonify({"success": True, "data": _sanitize(detail["summary"]), "error": None})


@backtest_bp.route("/results/<int:result_id>/trades", methods=["GET"])
def result_trades(result_id: int):
    """回测逐笔交易 GET /api/backtest/results/1/trades"""
    trades = get_trades(result_id)
    return jsonify({"success": True, "data": _sanitize(trades), "error": None})
