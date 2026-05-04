"""
routes/agents.py —— 三省六部制流水线API

路由：
  GET  /api/agents/status      → 当前流水线状态
  POST /api/agents/run         → 手动触发完整流水线
  POST /api/agents/run/<name>  → 手动触发单个Agent
  GET  /api/agents/history     → 最近执行历史
  GET  /api/agents/report      → 查看最新报告
  GET  /api/agents/governance  → 三省六部制架构信息
"""

from flask import Blueprint, jsonify, request
from agents.orchestrator import get_orchestrator
from agents.governance_mapping import get_pipeline_flow, get_governance_role, format_agent_display
from scheduler.state import PIPELINE_STATUS
import os

agents_bp = Blueprint("agents", __name__, url_prefix="/api/agents")


@agents_bp.route("/status", methods=["GET"])
def get_status():
    """获取当前流水线状态"""
    orch = get_orchestrator()
    status = {
        "pipeline_running": orch.is_running(),
        "pipeline_status": PIPELINE_STATUS,
        "current": orch.get_current_status(),
    }
    return jsonify(status)


@agents_bp.route("/run", methods=["POST"])
def run_pipeline():
    """手动触发完整流水线"""
    orch = get_orchestrator()
    if orch.is_running():
        return jsonify({"success": False, "error": "流水线已在运行中"}), 409

    def _run():
        orch.run_pipeline()

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"success": True, "message": "流水线已启动"})


@agents_bp.route("/run/<agent_name>", methods=["POST"])
def run_single_agent(agent_name: str):
    """手动触发单个Agent"""
    orch = get_orchestrator()
    if orch.is_running():
        return jsonify({"success": False, "error": "流水线已在运行中，请等待"}), 409

    valid = ["DataAgent", "SignalAgent", "BacktestAgent", "RiskAgent", "ReportAgent"]
    if agent_name not in valid:
        return jsonify({"success": False, "error": f"未知Agent: {agent_name}，可选: {valid}"}), 400

    def _run():
        orch.run_single(agent_name)

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"success": True, "message": f"Agent {agent_name} 已启动"})


@agents_bp.route("/history", methods=["GET"])
def get_history():
    """获取最近执行历史"""
    limit = request.args.get("limit", 10, type=int)
    orch = get_orchestrator()
    return jsonify({"success": True, "history": orch.get_history(limit=limit)})


@agents_bp.route("/report", methods=["GET"])
def get_latest_report():
    """查看最新生成的报告"""
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")

    # 尝试今天的报告
    html_path = os.path.join(reports_dir, f"daily_report_{today}.html")
    json_path = os.path.join(reports_dir, f"daily_report_{today}.json")

    if os.path.exists(json_path):
        import json
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"success": True, "date": today, "data": data})

    return jsonify({"success": False, "error": "今日报告尚未生成", "date": today})


@agents_bp.route("/governance", methods=["GET"])
def get_governance_info():
    """获取三省六部制架构信息"""
    flow = get_pipeline_flow()
    
    # 获取各Agent的三省六部角色
    agents_info = []
    for agent_name in ["DataAgent", "SignalAgent", "BacktestAgent", "RiskAgent", "ReportAgent"]:
        role = get_governance_role(agent_name)
        if role:
            agents_info.append({
                "agent_name": agent_name,
                "province": role.province,
                "ministry": role.ministry,
                "role": role.role,
                "display_name": f"{role.province}·{role.role}",
                "description": role.description,
            })

    return jsonify({
        "success": True,
        "governance": {
            "flow": flow,
            "agents": agents_info,
            "provinces": ["太子院", "中书省", "门下省", "尚书省"],
            "ministries": ["吏部", "户部", "礼部", "兵部", "刑部", "工部"],
        }
    })
