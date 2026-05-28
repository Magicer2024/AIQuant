"""
routes/backtest.py —— 回测 API
"""
import json
import math
import threading
from datetime import date
from flask import Blueprint, request, jsonify
from backtest.reporter import build_result_list, build_result_detail
from backtest.trade_store import get_result, get_trades, delete_result
from strategy.rules_store import get_rule

backtest_bp = Blueprint("backtest", __name__, url_prefix="/api/backtest")


def _sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if hasattr(obj, "item"):  # numpy scalar → Python native
        return _sanitize(obj.item())
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


_backtest_status = {"running": False, "result_id": None}
_backtest_lock = threading.Lock()


@backtest_bp.route("/run", methods=["POST"])
def start_backtest():
    """触发回测 POST /api/backtest/run {rule_id, start_date, end_date}"""
    global _backtest_status
    with _backtest_lock:
        if _backtest_status["running"]:
            return jsonify({"success": False, "data": None, "error": "Backtest already in progress"}), 409
        _backtest_status = {"running": True, "result_id": None, "progress": 0, "total": 0}

    body = request.get_json(silent=True) or {}
    rule_id = body.get("rule_id")
    start_date = body.get("start_date", "2024-01-01")
    end_date = body.get("end_date", date.today().strftime("%Y-%m-%d"))
    stop_loss = max(min(body.get("stop_loss", -0.08), 0), -0.50)
    take_profit = max(min(body.get("take_profit", 0.20), 1.00), 0)
    holding_max = max(min(body.get("holding_max", 20), 100), 1)

    rule = get_rule(int(rule_id)) if rule_id else None
    if not rule:
        with _backtest_lock:
            _backtest_status = {"running": False, "result_id": None}
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404

    def _progress(processed, total):
        with _backtest_lock:
            _backtest_status["progress"] = processed
            _backtest_status["total"] = total

    def _run():
        global _backtest_status
        try:
            from backtest.engine import run_backtest
            sell_conds = rule.get("sell_conditions")
            if isinstance(sell_conds, str):
                sell_conds = sell_conds if sell_conds.strip() and sell_conds.strip() != "[]" else None
            elif isinstance(sell_conds, list):
                sell_conds = json.dumps(sell_conds) if sell_conds else None

            result = run_backtest(
                rule_name=rule["rule_name"],
                rule_id=str(rule["id"]),
                conditions_json=rule["conditions"] or "{}",
                start_date=start_date,
                end_date=end_date,
                sell_conditions_json=sell_conds,
                holding_max=holding_max,
                stop_loss=stop_loss,
                take_profit=take_profit,
                save=True,
                progress_callback=_progress,
            )
            with _backtest_lock:
                _backtest_status = {"running": False, "result_id": result.get("result_id")}
        except Exception as e:
            with _backtest_lock:
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


@backtest_bp.route("/results/<int:result_id>", methods=["DELETE"])
def delete_backtest_result(result_id: int):
    """删除回测结果 DELETE /api/backtest/results/1"""
    delete_result(result_id)
    return jsonify({"success": True, "data": None, "error": None})


@backtest_bp.route("/results/<int:result_id>/trades", methods=["GET"])
def result_trades(result_id: int):
    """回测逐笔交易 GET /api/backtest/results/1/trades"""
    trades = get_trades(result_id)
    return jsonify({"success": True, "data": _sanitize(trades), "error": None})
