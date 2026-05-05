"""
routes/chart.py —— K线图表API

路由：
  GET /api/chart/kline/<code> → 返回股票OHLCV数据（JSON）
"""

from flask import Blueprint, jsonify, request
from ministries.rites.data_source_manager import get_data_source_manager
from datetime import date, timedelta

chart_bp = Blueprint("chart", __name__, url_prefix="/api/chart")


@chart_bp.route("/kline/<code>", methods=["GET"])
def get_kline_data(code: str):
    """返回股票K线数据（最近一年）"""
    days = request.args.get("days", 250, type=int)
    end_date = date.today().strftime("%Y-%m-%d")
    start_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        df = get_data_source_manager().get_daily_price_df(code, start_date, end_date)
        if df.empty:
            return jsonify({"success": False, "error": f"未找到股票 {code} 的行情数据"}), 404

        # 构建 OHLCV 数组，ECharts 格式：[[date, open, close, low, high, volume], ...]
        data = []
        for idx, row in df.iterrows():
            trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
            data.append([
                trade_date,
                round(float(row.get("open", 0)), 2),
                round(float(row.get("close", 0)), 2),
                round(float(row.get("low", 0)), 2),
                round(float(row.get("high", 0)), 2),
                int(row.get("volume", 0)),
            ])

        # 获取股票名称
        name = code
        try:
            from core.db import get_stock_name
            n = get_stock_name(code)
            if n:
                name = n
        except Exception:
            pass

        return jsonify({
            "success": True,
            "code": code,
            "name": name,
            "count": len(data),
            "data": data,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
