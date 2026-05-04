"""
routes/live.py —— 实盘监控API

路由：
  GET  /api/live/status        → 监控状态
  POST /api/live/start         → 启动监控
  POST /api/live/stop          → 停止监控
  GET  /api/live/alerts        → 告警列表
  POST /api/live/alerts/<id>/resolve → 解决告警
  POST /api/live/alerts/clear  → 清空告警
  POST /api/live/config        → 更新配置
"""

from flask import Blueprint, jsonify, request

from live.monitor import get_live_monitor, AlertLevel, webhook_alert_handler

live_bp = Blueprint("live", __name__, url_prefix="/api/live")
_monitor = get_live_monitor()


@live_bp.route("/status", methods=["GET"])
def get_status():
    """获取监控状态"""
    return jsonify({
        "success": True,
        "status": _monitor.get_status(),
    })


@live_bp.route("/start", methods=["POST"])
def start_monitor():
    """启动实盘监控"""
    _monitor.start()
    return jsonify({
        "success": True,
        "message": "实盘监控已启动",
        "status": _monitor.get_status(),
    })


@live_bp.route("/stop", methods=["POST"])
def stop_monitor():
    """停止实盘监控"""
    _monitor.stop()
    return jsonify({
        "success": True,
        "message": "实盘监控已停止",
        "status": _monitor.get_status(),
    })


@live_bp.route("/alerts", methods=["GET"])
def list_alerts():
    """获取告警列表"""
    code = request.args.get("code")
    level = request.args.get("level")
    active_only = request.args.get("active_only", "false").lower() == "true"
    limit = request.args.get("limit", 100, type=int)

    alerts = _monitor.get_alerts(
        code=code,
        level=level,
        active_only=active_only,
        limit=limit,
    )
    return jsonify({
        "success": True,
        "count": len(alerts),
        "alerts": alerts,
    })


@live_bp.route("/alerts/<alert_id>/resolve", methods=["POST"])
def resolve_alert(alert_id: str):
    """解决告警"""
    ok = _monitor.resolve_alert(alert_id)
    return jsonify({
        "success": ok,
        "message": "已解决" if ok else "告警不存在",
    })


@live_bp.route("/alerts/clear", methods=["POST"])
def clear_alerts():
    """清空所有告警"""
    _monitor.clear_alerts()
    return jsonify({
        "success": True,
        "message": "告警已清空",
    })


@live_bp.route("/config", methods=["POST"])
def update_config():
    """更新监控配置"""
    data = request.get_json() or {}

    if "stop_loss_pct" in data:
        _monitor.config.stop_loss_pct = float(data["stop_loss_pct"])
    if "take_profit_pct" in data:
        _monitor.config.take_profit_pct = float(data["take_profit_pct"])
    if "price_spike_pct" in data:
        _monitor.config.price_spike_pct = float(data["price_spike_pct"])
    if "check_interval" in data:
        _monitor.config.check_interval = int(data["check_interval"])
    if "webhook_url" in data:
        url = data["webhook_url"]
        _monitor.config.webhook_url = url
        # 注册 Webhook 处理器
        if url:
            handler = webhook_alert_handler(url)
            _monitor.add_alert_handler(handler)

    return jsonify({
        "success": True,
        "message": "配置已更新",
        "config": {
            "stop_loss_pct": _monitor.config.stop_loss_pct,
            "take_profit_pct": _monitor.config.take_profit_pct,
            "price_spike_pct": _monitor.config.price_spike_pct,
            "check_interval": _monitor.config.check_interval,
            "webhook_url": _monitor.config.webhook_url,
        },
    })
