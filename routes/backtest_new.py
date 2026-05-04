"""
routes/backtest_new.py —— 回测API（Backtrader集成）

路由：
  POST /api/backtest/run          → 执行回测
  POST /api/backtest/optimize     → 参数优化
  GET  /api/backtest/results      → 回测结果列表
"""

from flask import Blueprint, jsonify, request

from backtest.engine import get_backtest_engine

backtest_new_bp = Blueprint("backtest_new", __name__, url_prefix="/api/backtest")


@backtest_new_bp.route("/run", methods=["POST"])
def run_backtest():
    """执行回测"""
    data = request.get_json() or {}
    code = data.get("code", "")
    start_date = data.get("start_date", "20230101")
    end_date = data.get("end_date", "20241231")
    params = data.get("params", {})
    initial_cash = data.get("initial_cash", 100000.0)

    if not code:
        return jsonify({"success": False, "error": "code required"}), 400

    try:
        # 获取数据
        from core.db import get_daily_price
        df = get_daily_price(code, start_date, end_date)

        if df.empty:
            return jsonify({"success": False, "error": "无数据"}), 404

        # 执行回测
        engine = get_backtest_engine()
        result = engine.run_backtest(df, params, initial_cash)

        return jsonify({
            "success": True,
            "result": {
                "strategy_name": result.strategy_name,
                "start_date": result.start_date,
                "end_date": result.end_date,
                "initial_capital": result.initial_capital,
                "final_value": round(result.final_value, 2),
                "total_return": round(result.total_return, 2),
                "annual_return": round(result.annual_return, 2),
                "sharpe_ratio": round(result.sharpe_ratio, 3),
                "max_drawdown": round(result.max_drawdown, 2),
                "max_drawdown_duration": result.max_drawdown_duration,
                "total_trades": result.total_trades,
                "win_rate": round(result.win_rate, 2),
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@backtest_new_bp.route("/optimize", methods=["POST"])
def optimize_backtest():
    """参数优化"""
    data = request.get_json() or {}
    code = data.get("code", "")
    param_grid = data.get("param_grid", [])

    if not code or not param_grid:
        return jsonify({"success": False, "error": "code and param_grid required"}), 400

    try:
        from core.db import get_daily_price
        df = get_daily_price(code, "20230101", "20241231")

        if df.empty:
            return jsonify({"success": False, "error": "无数据"}), 404

        engine = get_backtest_engine()
        results = engine.optimize(df, param_grid)

        return jsonify({
            "success": True,
            "count": len(results),
            "results": [
                {
                    "strategy_name": r.strategy_name,
                    "total_return": round(r.total_return, 2),
                    "sharpe_ratio": round(r.sharpe_ratio, 3),
                    "max_drawdown": round(r.max_drawdown, 2),
                    "total_trades": r.total_trades,
                    "win_rate": round(r.win_rate, 2),
                }
                for r in results
            ],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
