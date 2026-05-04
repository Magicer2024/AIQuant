"""
routes/strategy_config.py —— 策略配置管理API

路由：
  GET  /api/strategy/config         → 策略配置
  POST /api/strategy/config/reload  → 热加载配置
  GET  /api/strategy/list           → 策略列表
  GET  /api/strategy/weights        → 融合权重
"""

from flask import Blueprint, jsonify

from strategy.config import strategy_config

strategy_config_bp = Blueprint("strategy_config", __name__, url_prefix="/api/strategy")


@strategy_config_bp.route("/config", methods=["GET"])
def get_strategy_config():
    """获取当前策略配置"""
    return jsonify({
        "success": True,
        "config": strategy_config.to_dict(),
    })


@strategy_config_bp.route("/config/reload", methods=["POST"])
def reload_strategy_config():
    """热加载策略配置"""
    try:
        strategy_config.reload()
        return jsonify({
            "success": True,
            "message": "策略配置已热加载",
            "config": strategy_config.to_dict(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_config_bp.route("/list", methods=["GET"])
def list_strategies():
    """获取策略列表"""
    strategies = strategy_config.all_strategies()
    result = []
    for key, cfg in strategies.items():
        result.append({
            "id": key,
            "name": cfg.get("name", key),
            "description": cfg.get("description", ""),
            "enabled": cfg.get("enabled", True),
            "params": cfg.get("params", {}),
        })
    return jsonify({
        "success": True,
        "count": len(result),
        "strategies": result,
    })


@strategy_config_bp.route("/weights", methods=["GET"])
def get_fusion_weights():
    """获取融合权重配置"""
    presets = ["default", "pure_bottom", "conservative", "aggressive"]
    weights = {}
    for p in presets:
        weights[p] = strategy_config.get_fusion_weights(p)
    return jsonify({
        "success": True,
        "weights": weights,
    })
