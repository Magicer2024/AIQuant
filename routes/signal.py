"""
routes/signal.py —— 信号历史路由（薄层）
"""
import traceback
from datetime import date, timedelta
from flask import jsonify, request

from routes import signal_bp
from services.signal_service import get_signal_history, get_signal_history_v4


@signal_bp.route("/history", methods=["GET"])
def _signal_history():
    today = date.today()
    start_str = request.args.get("start_date", "")
    end_str = request.args.get("end_date", "")
    min_score = float(request.args.get("min_score", 15))
    limit = int(request.args.get("limit", 100))

    end_date = date.fromisoformat(end_str) if end_str else today
    start_date = date.fromisoformat(start_str) if start_str else end_date - timedelta(days=30)

    try:
        return jsonify(get_signal_history(start_date, end_date, min_score, limit))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@signal_bp.route("/history_v4", methods=["GET"])
def _signal_history_v4():
    today = date.today()
    start_str = request.args.get("start_date", "")
    end_str = request.args.get("end_date", "")
    min_score = float(request.args.get("min_score", 15))
    limit = int(request.args.get("limit", 50))

    end_date = date.fromisoformat(end_str) if end_str else today
    start_date = date.fromisoformat(start_str) if start_str else end_date - timedelta(days=30)

    try:
        return jsonify(get_signal_history_v4(start_date, end_date, min_score, limit))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
