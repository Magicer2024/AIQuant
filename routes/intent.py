"""
routes/intent.py —— 策略意图解析API

路由：
  POST /api/intent/parse          → 解析策略意图
  GET  /api/intent/examples       → 示例策略描述
  GET  /api/intent/types          → 支持的策略类型
"""

from flask import Blueprint, jsonify, request

from strategy.intent.parser import get_intent_parser

intent_bp = Blueprint("intent", __name__, url_prefix="/api/intent")
_parser = get_intent_parser()


@intent_bp.route("/parse", methods=["POST"])
def parse_intent():
    """解析策略意图"""
    data = request.get_json() or {}
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"success": False, "error": "缺少策略描述文本"}), 400

    result = _parser.parse(text)

    return jsonify({
        "success": True,
        "original": result.original_text,
        "intent_type": result.intent_type,
        "confidence": round(result.confidence, 2),
        "explanation": result.explanation,
        "conditions": result.conditions,
        "params": result.params,
    })


@intent_bp.route("/examples", methods=["GET"])
def get_examples():
    """获取示例策略描述"""
    examples = [
        {
            "text": "找出近20日跌幅超过15%但近3日出现放量的股票",
            "type": "oversold_rebound",
            "description": "超跌反弹：先大幅下跌，随后放量企稳",
        },
        {
            "text": "MA5上穿MA20的放量突破股",
            "type": "volume_breakout",
            "description": "均线金叉 + 成交量放大",
        },
        {
            "text": "RSI低于30的超跌反弹股",
            "type": "oversold_rebound",
            "description": "RSI超卖区域，反弹概率高",
        },
        {
            "text": "连续5天上涨且成交量放大的强势股",
            "type": "trend_following",
            "description": "趋势确认 + 量能配合",
        },
        {
            "text": "近10日回调不超过5%的趋势股",
            "type": "mean_reversion",
            "description": "健康回调，趋势未破",
        },
        {
            "text": "近3日涨幅超过10%的动量股",
            "type": "momentum",
            "description": "短期强势， momentum 延续",
        },
    ]
    return jsonify({
        "success": True,
        "examples": examples,
    })


@intent_bp.route("/types", methods=["GET"])
def get_types():
    """获取支持的策略类型"""
    types = [
        {
            "id": "oversold_rebound",
            "name": "超跌反弹",
            "description": "寻找大幅下跌后企稳反弹的股票",
            "params": ["days", "drop_pct", "rsi_threshold"],
        },
        {
            "id": "volume_breakout",
            "name": "放量突破",
            "description": "均线金叉配合成交量放大",
            "params": ["ma_short", "ma_long", "volume_ratio"],
        },
        {
            "id": "trend_following",
            "name": "趋势跟踪",
            "description": "跟随已确认的上涨趋势",
            "params": ["consecutive_days", "days"],
        },
        {
            "id": "mean_reversion",
            "name": "均值回归",
            "description": "价格偏离均值后的回归交易",
            "params": ["days", "drop_pct", "ma_long"],
        },
        {
            "id": "momentum",
            "name": "动量策略",
            "description": "追逐短期强势股票",
            "params": ["days", "rise_pct"],
        },
    ]
    return jsonify({
        "success": True,
        "types": types,
    })
