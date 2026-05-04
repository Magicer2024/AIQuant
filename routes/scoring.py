"""
routes/scoring.py —— 多因子评分API

路由：
  POST /api/scoring/evaluate       → 单股评分
  POST /api/scoring/batch          → 批量评分
  GET  /api/scoring/factors        → 可用因子列表
  POST /api/scoring/weights        → 设置权重
  GET  /api/scoring/weights        → 获取当前权重
"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from governance.chancellery.scoring_engine import get_scoring_engine, MultiFactorScoringEngine
from ministries.rites.data_source_manager import get_data_source_manager

scoring_bp = Blueprint("scoring", __name__, url_prefix="/api/scoring")
_engine = get_scoring_engine()


def _get_date_range(days: int) -> tuple[str, str]:
    """根据天数计算日期范围"""
    end = datetime.now()
    start = end - timedelta(days=days + 30)  # 多取一些数据用于计算均线
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


@scoring_bp.route("/evaluate", methods=["POST"])
def evaluate_stock():
    """对单只股票进行多因子评分"""
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    name = data.get("name", code)
    days = data.get("days", 120)

    if not code:
        return jsonify({"success": False, "error": "缺少股票代码"}), 400

    # 获取数据
    ds = get_data_source_manager()
    start_date, end_date = _get_date_range(days)
    raw_data = ds.get_daily_price(code, start_date=start_date, end_date=end_date)

    if not raw_data or len(raw_data) < 20:
        return jsonify({
            "success": False,
            "error": f"股票 {code} 数据不足（仅 {len(raw_data) if raw_data else 0} 条）",
        }), 400

    import pandas as pd
    df = pd.DataFrame(raw_data)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")

    # 评分
    result = _engine.score(code, name, df)

    return jsonify({
        "success": True,
        "code": result.code,
        "name": result.name,
        "total_score": result.total_score,
        "grade": result.grade,
        "recommendation": result.recommendation,
        "signals": result.signals,
        "factors": [
            {
                "name": f.name,
                "score": f.score,
                "weight": f.weight,
                "raw_value": f.raw_value,
                "description": f.description,
            }
            for f in result.factors
        ],
    })


@scoring_bp.route("/batch", methods=["POST"])
def batch_evaluate():
    """批量评分"""
    data = request.get_json() or {}
    stocks = data.get("stocks", [])
    days = data.get("days", 120)
    top_n = data.get("top_n", 20)

    if not stocks:
        return jsonify({"success": False, "error": "缺少股票列表"}), 400

    ds = get_data_source_manager()
    results = []

    for stock in stocks:
        code = stock.get("code", "").strip()
        name = stock.get("name", code)
        if not code:
            continue

        try:
            start_date, end_date = _get_date_range(days)
            raw_data = ds.get_daily_price(code, start_date=start_date, end_date=end_date)
            if not raw_data or len(raw_data) < 20:
                continue

            import pandas as pd
            df = pd.DataFrame(raw_data)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date")

            result = _engine.score(code, name, df)
            results.append({
                "code": result.code,
                "name": result.name,
                "total_score": result.total_score,
                "grade": result.grade,
                "signals": result.signals,
            })
        except Exception as e:
            print(f"[Scoring] {code} 评分失败: {e}")
            continue

    # 排序并截取
    results.sort(key=lambda x: x["total_score"], reverse=True)
    top_results = results[:top_n]

    return jsonify({
        "success": True,
        "evaluated": len(results),
        "top_n": top_n,
        "results": top_results,
    })


@scoring_bp.route("/factors", methods=["GET"])
def list_factors():
    """获取可用因子列表"""
    return jsonify({
        "success": True,
        "factors": _engine.get_available_factors(),
    })


@scoring_bp.route("/weights", methods=["GET"])
def get_weights():
    """获取当前权重配置"""
    return jsonify({
        "success": True,
        "weights": _engine.weights,
    })


@scoring_bp.route("/weights", methods=["POST"])
def set_weights():
    """设置因子权重"""
    data = request.get_json() or {}
    weights = data.get("weights", {})

    if not weights:
        return jsonify({"success": False, "error": "缺少权重配置"}), 400

    # 验证权重
    total = sum(weights.values())
    if abs(total - 1.0) > 0.01:
        return jsonify({
            "success": False,
            "error": f"权重总和必须为1.0，当前为 {total:.2f}",
        }), 400

    _engine.set_weights(weights)
    return jsonify({
        "success": True,
        "message": "权重已更新",
        "weights": _engine.weights,
    })
