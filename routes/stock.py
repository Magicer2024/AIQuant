"""
routes/stock.py —— 股票数据路由（薄层，只做 HTTP 协议转换）
"""
import traceback
from flask import jsonify, request

from routes import stock_bp

from core.data_fetcher import search_stocks, get_hot_stocks, get_stock_info
from services.stock_service import get_stock_list, get_kline_data, analyze_stock, run_single_backtest


@stock_bp.route("/search", methods=["GET"])
def search():
    keyword = request.args.get("q", "")
    if not keyword:
        return jsonify({"error": "缺少搜索关键字"}), 400
    return jsonify({"results": search_stocks(keyword)})


@stock_bp.route("/hot", methods=["GET"])
def hot_stocks():
    """保留旧接口"""
    return jsonify({"stocks": get_hot_stocks(20)})


@stock_bp.route("/list", methods=["GET"])
def stock_list():
    with_score = request.args.get("with_score", "true").lower() != "false"
    try:
        return jsonify(get_stock_list(with_score=with_score))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@stock_bp.route("/kline", methods=["GET"])
def stock_kline():
    code = request.args.get("code", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    if not code:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        return jsonify(get_kline_data(code, start_date=start_date, end_date=end_date))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@stock_bp.route("/analyze", methods=["GET"])
def analyze():
    symbol = request.args.get("symbol", "")
    start_date = request.args.get("start_date", "")
    strategy_name = request.args.get("strategy", "composite")
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        return jsonify(analyze_stock(symbol, start_date=start_date, strategy_name=strategy_name))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@stock_bp.route("/info", methods=["GET"])
def stock_info():
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    return jsonify({"info": get_stock_info(symbol)})


@stock_bp.route("/backtest", methods=["GET"])
def backtest():
    symbol = request.args.get("symbol", "")
    start_date = request.args.get("start_date", "20220101")
    strategy_name = request.args.get("strategy", "composite")
    capital = float(request.args.get("capital", 100000))
    if not symbol:
        return jsonify({"error": "缺少股票代码"}), 400
    try:
        return jsonify(run_single_backtest(symbol, start_date, strategy_name, capital))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
