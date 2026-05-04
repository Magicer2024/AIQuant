"""
ministries/rites/market_monitor.py —— 礼部实时行情监控

职责：
  1. 管理WebSocket客户端连接
  2. 维护股票订阅列表
  3. 定时拉取行情并推送给订阅者
  4. 连接生命周期管理
"""

import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable

from ministries.rites.data_source_manager import get_data_source_manager


class MarketMonitor:
    """实时行情监控器"""

    def __init__(self, interval: int = 5):
        """
        :param interval: 行情拉取间隔（秒）
        """
        self.interval = interval
        self.clients: dict[str, dict] = {}          # ws_id -> {conn, subscriptions, connected_at}
        self.subscriptions: dict[str, set] = defaultdict(set)  # code -> {ws_id set}
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._data_manager = get_data_source_manager()
        self._message_handlers: list[Callable] = []

    # ── 客户端连接管理 ──────────────────────────

    def register(self, ws_id: str, conn) -> dict:
        """注册新客户端"""
        with self._lock:
            self.clients[ws_id] = {
                "conn": conn,
                "subscriptions": set(),
                "connected_at": datetime.now().isoformat(),
            }
        self._broadcast_system(f"client_connected", {"ws_id": ws_id, "total_clients": len(self.clients)})
        return {"ws_id": ws_id, "status": "connected"}

    def unregister(self, ws_id: str):
        """注销客户端"""
        with self._lock:
            client = self.clients.pop(ws_id, None)
            if client:
                for code in list(client["subscriptions"]):
                    self.subscriptions[code].discard(ws_id)
        self._broadcast_system(f"client_disconnected", {"ws_id": ws_id, "total_clients": len(self.clients)})

    def subscribe(self, ws_id: str, codes: list[str]) -> dict:
        """订阅股票列表"""
        with self._lock:
            client = self.clients.get(ws_id)
            if not client:
                return {"success": False, "error": "客户端未连接"}

            for code in codes:
                code = code.strip().upper()
                client["subscriptions"].add(code)
                self.subscriptions[code].add(ws_id)

        return {
            "success": True,
            "subscribed": codes,
            "total_subscriptions": len(client["subscriptions"]),
        }

    def unsubscribe(self, ws_id: str, codes: list[str]) -> dict:
        """取消订阅"""
        with self._lock:
            client = self.clients.get(ws_id)
            if not client:
                return {"success": False, "error": "客户端未连接"}

            for code in codes:
                code = code.strip().upper()
                client["subscriptions"].discard(code)
                self.subscriptions[code].discard(ws_id)

        return {
            "success": True,
            "unsubscribed": codes,
            "total_subscriptions": len(client["subscriptions"]) if client else 0,
        }

    def get_client_info(self, ws_id: str) -> dict | None:
        """获取客户端信息"""
        client = self.clients.get(ws_id)
        if not client:
            return None
        return {
            "ws_id": ws_id,
            "subscriptions": list(client["subscriptions"]),
            "connected_at": client["connected_at"],
        }

    # ── 行情推送 ────────────────────────────────

    def start(self):
        """启动行情推送线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._push_loop, daemon=True)
        self._thread.start()
        print(f"[MarketMonitor] 行情推送已启动，间隔 {self.interval}s")

    def stop(self):
        """停止行情推送线程"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("[MarketMonitor] 行情推送已停止")

    def _push_loop(self):
        """行情推送主循环"""
        while self._running:
            try:
                self._fetch_and_push()
            except Exception as e:
                print(f"[MarketMonitor] 推送异常: {e}")
            time.sleep(self.interval)

    def _fetch_and_push(self):
        """获取行情并推送给订阅者"""
        with self._lock:
            codes = list(self.subscriptions.keys())

        if not codes:
            return

        # 批量获取行情（每次最多10只，避免过载）
        batch_size = 10
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            for code in batch:
                try:
                    price_data = self._fetch_price(code)
                    if price_data:
                        self._push_to_subscribers(code, price_data)
                except Exception as e:
                    print(f"[MarketMonitor] 获取 {code} 行情失败: {e}")

    def _fetch_price(self, code: str) -> dict | None:
        """获取单只股票最新行情"""
        try:
            # 获取最近1天的数据
            data = self._data_manager.get_daily_price(code, None, None)
            if data and len(data) > 0:
                latest = data[-1]
                return {
                    "code": code,
                    "timestamp": datetime.now().isoformat(),
                    "open": latest.get("open", 0),
                    "high": latest.get("high", 0),
                    "low": latest.get("low", 0),
                    "close": latest.get("close", 0),
                    "volume": latest.get("volume", 0),
                    "amount": latest.get("amount", 0),
                    "pct_change": latest.get("pct_change", 0),
                }
        except Exception as e:
            print(f"[MarketMonitor] 获取 {code} 失败: {e}")
        return None

    def _push_to_subscribers(self, code: str, data: dict):
        """推送给订阅该股票的所有客户端"""
        with self._lock:
            ws_ids = list(self.subscriptions.get(code, set()))

        message = json.dumps({
            "type": "price_update",
            "code": code,
            "data": data,
        })

        for ws_id in ws_ids:
            client = self.clients.get(ws_id)
            if client:
                try:
                    client["conn"].send(message)
                except Exception as e:
                    print(f"[MarketMonitor] 推送到 {ws_id} 失败: {e}")

    def _broadcast_system(self, event: str, data: dict):
        """广播系统事件"""
        message = json.dumps({
            "type": "system",
            "event": event,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        })

        with self._lock:
            clients = list(self.clients.values())

        for client in clients:
            try:
                client["conn"].send(message)
            except Exception:
                pass

    # ── 状态查询 ────────────────────────────────

    def get_status(self) -> dict:
        """获取监控器状态"""
        with self._lock:
            return {
                "running": self._running,
                "interval_seconds": self.interval,
                "total_clients": len(self.clients),
                "total_subscriptions": sum(len(s) for s in self.subscriptions.values()),
                "unique_stocks": len(self.subscriptions),
                "clients": [
                    {
                        "ws_id": ws_id,
                        "subscriptions": list(c["subscriptions"]),
                        "connected_at": c["connected_at"],
                    }
                    for ws_id, c in self.clients.items()
                ],
            }


# 全局单例
_market_monitor: MarketMonitor | None = None


def get_market_monitor() -> MarketMonitor:
    """获取行情监控器单例"""
    global _market_monitor
    if _market_monitor is None:
        _market_monitor = MarketMonitor(interval=5)
    return _market_monitor
