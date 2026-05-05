"""
routes/optimizer.py —— 参数优化路由（薄层）
"""
import traceback
from flask import jsonify, request

from routes import optimizer_bp
from services.optimizer_service import run_optimizer, run_walkforward


@optimizer_bp.route("/run", methods=["GET"])
def _run_optimizer():
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        return jsonify(run_optimizer(
            symbol,
            start_date=request.args.get("start_date", "20220101"),
            strategy_name=request.args.get("strategy", "volume_breakout"),
            method=request.args.get("method", "grid"),
            metric=request.args.get("metric", "sharpe_ratio"),
        ))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@optimizer_bp.route("/walkforward", methods=["GET"])
def _run_walkforward():
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        return jsonify(run_walkforward(
            symbol,
            start_date=request.args.get("start_date", "20220101"),
            strategy_name=request.args.get("strategy", "volume_breakout"),
            n_folds=int(request.args.get("n_folds", 3)),
        ))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
