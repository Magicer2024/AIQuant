"""
routes/deployment.py —— 策略实盘部署路由

端点：
  POST /api/deployment/register   —— 注册策略
  POST /api/deployment/deploy     —— 部署启动
  POST /api/deployment/stop       —— 停止
  POST /api/deployment/pause      —— 暂停
  POST /api/deployment/resume     —— 恢复
  POST /api/deployment/undeploy   —— 注销
  GET  /api/deployment/status     —— 查询状态（全部或单个）
  POST /api/deployment/config     —— 热更新配置
"""

from flask import Blueprint, request, jsonify

from deployment.manager import get_deployment_manager

deployment_bp = Blueprint("deployment", __name__, url_prefix="/api/deployment")


@deployment_bp.route("/register", methods=["POST"])
def register():
    """注册策略"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    strategy_name = data.get("strategy_name", "")
    interval = data.get("interval", 60)
    config = data.get("config", {})
    strategy_content = data.get("strategy_content", "")

    if not strategy_id or not strategy_name:
        return jsonify({"success": False, "error": "strategy_id 和 strategy_name 必填"}), 400

    manager = get_deployment_manager()
    result = manager.register_strategy(
        strategy_id, strategy_name,
        config=config, interval=interval,
        strategy_content=strategy_content
    )
    return jsonify(result)


@deployment_bp.route("/deploy", methods=["POST"])
def deploy():
    """部署启动策略"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.deploy(strategy_id))


@deployment_bp.route("/stop", methods=["POST"])
def stop():
    """停止策略"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.stop(strategy_id))


@deployment_bp.route("/pause", methods=["POST"])
def pause():
    """暂停策略"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.pause(strategy_id))


@deployment_bp.route("/resume", methods=["POST"])
def resume():
    """恢复策略"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.resume(strategy_id))


@deployment_bp.route("/undeploy", methods=["POST"])
def undeploy():
    """注销策略"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.undeploy(strategy_id))


@deployment_bp.route("/status", methods=["GET"])
def status():
    """查询策略状态"""
    strategy_id = request.args.get("strategy_id")
    manager = get_deployment_manager()
    return jsonify(manager.get_status(strategy_id))


@deployment_bp.route("/config", methods=["POST"])
def update_config():
    """热更新策略配置"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    config = data.get("config", {})
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.update_config(strategy_id, config))


@deployment_bp.route("/content", methods=["GET"])
def get_content():
    """获取策略内容"""
    strategy_id = request.args.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.get_content(strategy_id))


@deployment_bp.route("/content", methods=["POST"])
def update_content():
    """更新策略内容"""
    data = request.get_json() or {}
    strategy_id = data.get("strategy_id", "")
    content = data.get("content", "")
    if not strategy_id:
        return jsonify({"success": False, "error": "strategy_id 必填"}), 400

    manager = get_deployment_manager()
    return jsonify(manager.update_content(strategy_id, content))
