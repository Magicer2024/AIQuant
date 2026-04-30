"""
routes/strategy.py —— 策略路由（薄层）
"""
import traceback
from flask import jsonify, request, Response, stream_with_context

from routes import strategy_bp
from services.strategy_service import (
    run_all_strategies, screen_stocks_generator,
    multi_strategy_backtest, compare_strategies,
)


@strategy_bp.route("/run", methods=["GET"])
def _run_all_strategies():
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400

    params = {
        "s1_vol_factor": float(request.args.get("s1_vol_factor", 1.5)),
        "s1_rise_3d": float(request.args.get("s1_rise_3d", 0.02)),
        "s1_score_min": int(request.args.get("s1_score_min", 2)),
        "s2_ma_narrow": float(request.args.get("s2_ma_narrow", 0.02)),
        "s4_drop_threshold": float(request.args.get("s4_drop_threshold", -0.05)),
        "s5_vol_ratio": float(request.args.get("s5_vol_ratio", 3.0)),
    }
    try:
        return jsonify(run_all_strategies(symbol, start_date=request.args.get("start_date", ""), params=params))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@strategy_bp.route("/screen", methods=["GET"])
def _screen_stocks():
    symbols_str = request.args.get("symbols", "")
    use_v4 = request.args.get("use_v4", "true").lower() != "false"
    min_score = float(request.args.get("min_score", 0))
    limit = int(request.args.get("limit", 50))

    def generate():
        yield from screen_stocks_generator(symbols_str, use_v4, min_score, limit)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Expose-Headers': 'Content-Type',
        }
    )


@strategy_bp.route("/backtest", methods=["GET"])
def _multi_backtest():
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        return jsonify(multi_strategy_backtest(
            symbol,
            start_date=request.args.get("start_date", "20220101"),
            capital=float(request.args.get("capital", 100000)),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@strategy_bp.route("/compare", methods=["GET"])
def _compare():
    symbols = request.args.get("symbols", "")
    if not symbols:
        return jsonify({"error": "缺少股票代码列表"}), 400
    try:
        return jsonify(compare_strategies(
            symbols,
            start_date=request.args.get("start_date", "20220101"),
            strategy_name=request.args.get("strategy", "fused"),
        ))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
