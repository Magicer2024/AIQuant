"""
routes/market_ws.py —— 实时行情WebSocket路由

路由：
  WS  /ws/market          → 行情WebSocket连接
  GET /api/market/status  → 监控状态
  GET /api/market/clients → 客户端列表
"""

import uuid

from flask import Blueprint, jsonify
from flask_sock import Sock

from ministries.rites.market_monitor import get_market_monitor

market_ws_bp = Blueprint("market_ws", __name__, url_prefix="/api/market")

# WebSocket 实例（在 app.py 中初始化后注入）
ws_sock: Sock | None = None

# 行情监控器单例
_monitor = get_market_monitor()


@market_ws_bp.route("/status", methods=["GET"])
def get_status():
    """获取行情监控状态"""
    return jsonify({
        "success": True,
        "status": _monitor.get_status(),
    })


@market_ws_bp.route("/clients", methods=["GET"])
def get_clients():
    """获取当前连接的客户端列表"""
    status = _monitor.get_status()
    return jsonify({
        "success": True,
        "clients": status.get("clients", []),
    })


@market_ws_bp.route("/start", methods=["POST"])
def start_monitor():
    """手动启动行情推送"""
    _monitor.start()
    return jsonify({
        "success": True,
        "message": "行情推送已启动",
        "status": _monitor.get_status(),
    })


@market_ws_bp.route("/stop", methods=["POST"])
def stop_monitor():
    """手动停止行情推送"""
    _monitor.stop()
    return jsonify({
        "success": True,
        "message": "行情推送已停止",
        "status": _monitor.get_status(),
    })


def register_ws_routes(sock: Sock):
    """注册WebSocket路由"""
    global ws_sock
    ws_sock = sock

    @sock.route("/ws/market")
    def market_websocket(ws):
        """行情WebSocket连接处理"""
        ws_id = str(uuid.uuid4())[:8]

        # 注册客户端
        _monitor.register(ws_id, ws)
        ws.send(__build_message("connected", {
            "ws_id": ws_id,
            "message": "已连接到行情服务",
        }))

        try:
            while True:
                # 接收客户端消息
                raw = ws.receive()
                if raw is None:
                    break

                try:
                    msg = __parse_message(raw)
                    action = msg.get("action")
                    payload = msg.get("payload", {})

                    if action == "subscribe":
                        codes = payload.get("codes", [])
                        result = _monitor.subscribe(ws_id, codes)
                        ws.send(__build_message("subscribed", result))

                    elif action == "unsubscribe":
                        codes = payload.get("codes", [])
                        result = _monitor.unsubscribe(ws_id, codes)
                        ws.send(__build_message("unsubscribed", result))

                    elif action == "ping":
                        ws.send(__build_message("pong", {"time": payload.get("time")}))

                    elif action == "status":
                        info = _monitor.get_client_info(ws_id)
                        ws.send(__build_message("status", info or {}))

                    else:
                        ws.send(__build_message("error", {"message": f"未知动作: {action}"}))

                except Exception as e:
                    ws.send(__build_message("error", {"message": str(e)}))

        except Exception as e:
            print(f"[WebSocket] {ws_id} 连接异常: {e}")
        finally:
            _monitor.unregister(ws_id)


def __parse_message(raw: str | bytes) -> dict:
    """解析客户端消息"""
    import json
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def __build_message(msg_type: str, data: dict) -> str:
    """构建JSON消息"""
    import json
    from datetime import datetime
    return json.dumps({
        "type": msg_type,
        "data": data,
        "timestamp": datetime.now().isoformat(),
    })
