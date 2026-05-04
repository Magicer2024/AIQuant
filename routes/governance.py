"""
routes/governance.py —— 三省六部架构API

路由：
  GET  /api/governance/status       → 三省状态总览
  GET  /api/governance/decision     → 太子院决策状态
  POST /api/governance/decision     → 设置策略意图
  GET  /api/governance/strategy     → 中书省策略状态
  GET  /api/governance/execution    → 尚书省执行状态
  GET  /api/governance/ministries   → 六部状态
"""

from flask import Blueprint, jsonify, request

from governance.crown_prince.decision_engine import get_decision_engine
from governance.chancellery.strategy_engine import get_strategy_engine
from governance.secretariat.execution_engine import get_execution_engine
from ministries.rites.data_source_manager import get_data_source_manager
from ministries.war.order_manager import get_order_manager
from ministries.works.monitor import get_system_monitor

governance_bp = Blueprint("governance", __name__, url_prefix="/api/governance")


@governance_bp.route("/status", methods=["GET"])
def get_governance_status():
    """获取三省六部整体状态"""
    decision = get_decision_engine()
    strategy = get_strategy_engine()
    execution = get_execution_engine()
    monitor = get_system_monitor()

    return jsonify({
        "success": True,
        "governance": {
            "crown_prince": decision.get_current_status(),
            "chancellery": {
                "plan_loaded": decision.current_plan is not None,
                "params": strategy.get_params(),
            },
            "secretariat": {
                "pending_orders": len(execution.get_pending_orders()),
                "executed_orders": len(execution.get_executed_orders()),
            },
        },
        "ministries": {
            "rites": {
                "sources": [s.name for s in get_data_source_manager().sources.values()],
                "primary": get_data_source_manager().primary.value,
            },
            "war": {
                "pending_orders": len(get_order_manager().get_orders("pending")),
                "holding_positions": len(get_order_manager().get_positions("holding")),
            },
            "works": monitor.get_status(),
        },
    })


@governance_bp.route("/decision", methods=["GET"])
def get_decision():
    """获取太子院决策状态"""
    engine = get_decision_engine()
    return jsonify({
        "success": True,
        "decision": engine.get_current_status(),
    })


@governance_bp.route("/decision", methods=["POST"])
def set_decision():
    """设置策略意图"""
    data = request.get_json() or {}
    stance = data.get("stance", "neutral")
    risk = data.get("risk_appetite", "moderate")
    notes = data.get("notes", "")

    engine = get_decision_engine()
    intent = engine.set_intent(stance=stance, risk=risk, notes=notes)
    plan = engine.generate_plan(intent)

    # 同步到中书省
    strategy = get_strategy_engine()
    strategy.load_plan(plan.__dict__)

    return jsonify({
        "success": True,
        "intent": {
            "intent_id": intent.intent_id,
            "stance": intent.stance.value,
            "risk_appetite": intent.risk_appetite.value,
        },
        "plan": {
            "plan_id": plan.plan_id,
            "fusion_weights": plan.fusion_weights,
            "score_threshold": plan.score_threshold,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
        },
    })


@governance_bp.route("/strategy", methods=["GET"])
def get_strategy_status():
    """获取中书省策略状态"""
    engine = get_strategy_engine()
    return jsonify({
        "success": True,
        "strategy": {
            "params": engine.get_params(),
        },
    })


@governance_bp.route("/execution", methods=["GET"])
def get_execution_status():
    """获取尚书省执行状态"""
    engine = get_execution_engine()
    return jsonify({
        "success": True,
        "execution": {
            "pending_orders_count": len(engine.get_pending_orders()),
            "executed_orders_count": len(engine.get_executed_orders()),
        },
    })


@governance_bp.route("/ministries", methods=["GET"])
def get_ministries_status():
    """获取六部状态"""
    ds_manager = get_data_source_manager()
    order_mgr = get_order_manager()
    monitor = get_system_monitor()

    return jsonify({
        "success": True,
        "ministries": {
            "rites": {
                "data_sources": [
                    {"name": s.get_name(), "type": st.value}
                    for st, s in ds_manager.sources.items()
                ],
            },
            "war": {
                "pending_orders": len(order_mgr.get_orders("pending")),
                "holding_positions": len(order_mgr.get_positions("holding")),
            },
            "works": monitor.get_status(),
        },
    })
