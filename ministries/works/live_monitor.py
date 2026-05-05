"""
live/monitor.py —— 实盘监控器

职责：
  1. 定时扫描持仓，检查止损/止盈/风控线
  2. 价格异动告警（暴涨暴跌）
  3. 推送通知（Webhook / 日志 / 回调）
  4. 监控任务调度
"""

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

from ministries.rites.data_source_manager import get_data_source_manager
from ministries.war.order_manager import get_order_manager


class AlertLevel(Enum):
    """告警等级"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """告警记录"""
    id: str
    code: str
    level: AlertLevel
    category: str
    message: str
    triggered_at: str
    resolved_at: str = ""
    is_resolved: bool = False


@dataclass
class MonitorConfig:
    """监控配置"""
    stop_loss_pct: float = -0.06          # 默认止损 -6%
    take_profit_pct: float = 0.15         # 默认止盈 +15%
    price_spike_pct: float = 0.07         # 异动阈值 ±7%
    check_interval: int = 30              # 扫描间隔 30秒
    max_alerts_per_stock: int = 5         # 单股最大告警数
    webhook_url: str = ""                 # Webhook推送地址


class LiveMonitor:
    """实盘监控器"""

    def __init__(self, config: MonitorConfig | None = None):
        self.config = config or MonitorConfig()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.alerts: list[Alert] = []
        self.alert_handlers: list[Callable] = []
        self._alert_counts: dict[str, int] = defaultdict(int)
        self._last_prices: dict[str, float] = {}

    # ── 生命周期 ────────────────────────────────

    def start(self):
        """启动监控"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print(f"[LiveMonitor] 实盘监控已启动，间隔 {self.config.check_interval}s")

    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        print("[LiveMonitor] 实盘监控已停止")

    def is_running(self) -> bool:
        return self._running

    # ── 主循环 ──────────────────────────────────

    def _monitor_loop(self):
        """监控主循环"""
        while self._running:
            try:
                self._scan_positions()
            except Exception as e:
                print(f"[LiveMonitor] 扫描异常: {e}")
            time.sleep(self.config.check_interval)

    # ── 持仓扫描 ────────────────────────────────

    def _scan_positions(self):
        """扫描所有持仓"""
        om = get_order_manager()
        ds = get_data_source_manager()
        positions = om.get_positions(status="holding")

        for pos in positions:
            try:
                data = ds.get_daily_price(pos.code)
                if not data or len(data) == 0:
                    continue

                latest = data[-1]
                current_price = latest.get("close", pos.current_price)
                prev_close = data[-2].get("close", current_price) if len(data) > 1 else current_price

                # 更新价格
                om.update_position_price(pos.code, current_price)

                # 检查止损
                pnl_pct = pos.unrealized_pnl_pct
                if pnl_pct <= self.config.stop_loss_pct * 100:
                    self._trigger_alert(
                        code=pos.code,
                        level=AlertLevel.CRITICAL,
                        category="stop_loss",
                        message=f"{pos.code} 止损触发：浮亏 {pnl_pct:.2f}%",
                    )

                # 检查止盈
                elif pnl_pct >= self.config.take_profit_pct * 100:
                    self._trigger_alert(
                        code=pos.code,
                        level=AlertLevel.WARNING,
                        category="take_profit",
                        message=f"{pos.code} 止盈触发：浮盈 {pnl_pct:.2f}%",
                    )

                # 检查异动
                daily_change = (current_price - prev_close) / prev_close * 100 if prev_close else 0
                if abs(daily_change) >= self.config.price_spike_pct * 100:
                    self._trigger_alert(
                        code=pos.code,
                        level=AlertLevel.INFO,
                        category="price_spike",
                        message=f"{pos.code} 价格异动：{daily_change:+.2f}%",
                    )

                # 更新上次价格
                self._last_prices[pos.code] = current_price

            except Exception as e:
                print(f"[LiveMonitor] 扫描 {pos.code} 失败: {e}")

    # ── 告警处理 ────────────────────────────────

    def _trigger_alert(self, code: str, level: AlertLevel, category: str, message: str):
        """触发告警"""
        # 限制单股告警频率
        if self._alert_counts[code] >= self.config.max_alerts_per_stock:
            return

        import uuid
        alert = Alert(
            id=f"ALERT-{uuid.uuid4().hex[:8].upper()}",
            code=code,
            level=level,
            category=category,
            message=message,
            triggered_at=datetime.now().isoformat(),
        )

        with self._lock:
            self.alerts.append(alert)
            self._alert_counts[code] += 1

        print(f"[LiveMonitor] {level.value.upper()}: {message}")

        # 执行回调
        for handler in self.alert_handlers:
            try:
                handler(alert)
            except Exception as e:
                print(f"[LiveMonitor] 回调错误: {e}")

    def resolve_alert(self, alert_id: str) -> bool:
        """解决告警"""
        with self._lock:
            for alert in self.alerts:
                if alert.id == alert_id and not alert.is_resolved:
                    alert.is_resolved = True
                    alert.resolved_at = datetime.now().isoformat()
                    self._alert_counts[alert.code] = max(0, self._alert_counts[alert.code] - 1)
                    return True
        return False

    def add_alert_handler(self, handler: Callable):
        """添加告警处理器"""
        self.alert_handlers.append(handler)

    # ── 状态查询 ────────────────────────────────

    def get_status(self) -> dict:
        """获取监控状态"""
        with self._lock:
            active_alerts = [a for a in self.alerts if not a.is_resolved]
            return {
                "running": self._running,
                "interval": self.config.check_interval,
                "total_alerts": len(self.alerts),
                "active_alerts": len(active_alerts),
                "stop_loss_threshold": self.config.stop_loss_pct,
                "take_profit_threshold": self.config.take_profit_pct,
                "price_spike_threshold": self.config.price_spike_pct,
            }

    def get_alerts(self, code: str = None, level: str = None, active_only: bool = False,
                   limit: int = 100) -> list[dict]:
        """获取告警列表"""
        with self._lock:
            result = self.alerts[:]

        if code:
            result = [a for a in result if a.code == code]
        if level:
            result = [a for a in result if a.level.value == level]
        if active_only:
            result = [a for a in result if not a.is_resolved]

        result = sorted(result, key=lambda x: x.triggered_at, reverse=True)[:limit]

        return [
            {
                "id": a.id,
                "code": a.code,
                "level": a.level.value,
                "category": a.category,
                "message": a.message,
                "triggered_at": a.triggered_at,
                "resolved_at": a.resolved_at,
                "is_resolved": a.is_resolved,
            }
            for a in result
        ]

    def clear_alerts(self):
        """清空所有告警"""
        with self._lock:
            self.alerts.clear()
            self._alert_counts.clear()


# ── Webhook 推送器 ────────────────────────────

def webhook_alert_handler(url: str):
    """创建 Webhook 告警处理器"""
    def handler(alert: Alert):
        if not url:
            return
        try:
            import requests
            requests.post(url, json={
                "alert_id": alert.id,
                "code": alert.code,
                "level": alert.level.value,
                "category": alert.category,
                "message": alert.message,
                "time": alert.triggered_at,
            }, timeout=5)
        except Exception as e:
            print(f"[Webhook] 推送失败: {e}")
    return handler


# 全局单例
_live_monitor: LiveMonitor | None = None


def get_live_monitor() -> LiveMonitor:
    """获取实盘监控器单例"""
    global _live_monitor
    if _live_monitor is None:
        _live_monitor = LiveMonitor()
    return _live_monitor
