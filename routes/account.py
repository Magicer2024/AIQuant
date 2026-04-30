"""
routes/account.py —— 账户快照路由（薄层）
"""
import traceback
from flask import jsonify, request

from routes import account_bp
from services.account_service import get_snapshots, get_latest_snapshot, save_snapshot


@account_bp.route("/snapshots", methods=["GET"])
def account_snapshots():
    days = int(request.args.get("days", 30))
    try:
        return jsonify({"snapshots": get_snapshots(days)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@account_bp.route("/snapshot/latest", methods=["GET"])
def latest_snapshot():
    try:
        return jsonify(get_latest_snapshot())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@account_bp.route("/snapshot/save", methods=["POST"])
def _save_snapshot():
    try:
        save_snapshot(request.get_json() or {})
        return jsonify({"success": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
