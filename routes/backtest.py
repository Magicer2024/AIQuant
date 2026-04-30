"""
routes/backtest.py —— 批量回测路由（薄层）
"""
import traceback
from datetime import date
from flask import jsonify, request

from routes import backtest_bp
from services.backtest_service import run_batch_backtest


@backtest_bp.route("/batch", methods=["GET"])
def batch_backtest():
    weights_str = request.args.get("weights", "")
    weights = None
    if weights_str:
        try:
            weights = [float(w.strip()) for w in weights_str.split(",")]
            if len(weights) != 5:
                return jsonify({"error": "weights 必须包含 5 个数值"}), 400
        except ValueError:
            return jsonify({"error": "weights 格式错误，需如 0.30,0.15,0.20,0.20,0.15"}), 400

    try:
        result = run_batch_backtest(
            start_date=request.args.get("start_date", "2025-04-03"),
            end_date=request.args.get("end_date", date.today().strftime("%Y-%m-%d")),
            min_score=float(request.args.get("min_score", 15.0)),
            capital=float(request.args.get("capital", 100000)),
            max_positions=int(request.args.get("max_positions", 3)),
            max_position_size=float(request.args.get("max_position_size", 400000)),
            stop_loss=float(request.args.get("stop_loss", -0.06)),
            take_profit=float(request.args.get("take_profit", 0.20)),
            use_market_timing=request.args.get("use_market_timing", "true").lower() == "true",
            use_dynamic_position=request.args.get("use_dynamic_position", "false").lower() == "true",
            weights=weights,
            use_v4=True,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
