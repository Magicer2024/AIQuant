"""
routes/strategy_lab.py —— 策略实验室 API

路由：
  GET  /api/strategy-lab/overview             → 系统概览
  GET  /api/strategy-lab/factors              → 因子列表 (?category=X)
  GET  /api/strategy-lab/factors/categories   → 因子分类列表
  GET  /api/strategy-lab/rules                → 规则列表 (?status=active|degraded|all)
  POST /api/strategy-lab/rules/<id>/activate  → 激活规则
  POST /api/strategy-lab/rules/<id>/degrade   → 降级规则
  GET  /api/strategy-lab/active-strategies    → 当前活跃策略
  GET  /api/strategy-lab/ic-analysis          → IC 分析 (?factor=name)
"""

from flask import Blueprint, jsonify, request

from services.strategy_lab_service import (
    get_overview,
    get_factors,
    get_factor_categories,
    get_ic_analysis,
    get_rules,
    get_active_strategies_detail,
    activate_rule,
    degrade_rule_action,
)

strategy_lab_bp = Blueprint("strategy_lab", __name__, url_prefix="/api/strategy-lab")


@strategy_lab_bp.route("/overview", methods=["GET"])
def overview():
    try:
        data = get_overview()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/factors", methods=["GET"])
def factors():
    try:
        category = request.args.get("category")
        data = get_factors(category=category)
        return jsonify({"success": True, "data": data, "count": len(data)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/factors/categories", methods=["GET"])
def factor_categories():
    try:
        cats = get_factor_categories()
        return jsonify({"success": True, "data": cats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/ic-analysis", methods=["GET"])
def ic_analysis():
    try:
        factor_name = request.args.get("factor")
        data = get_ic_analysis(factor_name=factor_name)
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/rules", methods=["GET"])
def rules():
    try:
        status = request.args.get("status", "all")
        data = get_rules(status_filter=status if status != "all" else None)
        return jsonify({"success": True, "data": data, "count": len(data)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/rules/<int:rule_id>/activate", methods=["POST"])
def activate_rule_route(rule_id: int):
    try:
        result = activate_rule(rule_id)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/rules/<int:rule_id>/degrade", methods=["POST"])
def degrade_rule_route(rule_id: int):
    try:
        result = degrade_rule_action(rule_id)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@strategy_lab_bp.route("/active-strategies", methods=["GET"])
def active_strategies():
    try:
        data = get_active_strategies_detail()
        return jsonify({"success": True, "data": data, "count": len(data)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
