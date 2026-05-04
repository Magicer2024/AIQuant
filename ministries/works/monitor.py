"""
ministries/works/monitor.py —— 工部监控模块

职责：
  1. 系统健康检查
  2. 性能指标收集
  3. 告警触发
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SystemMetrics:
    """系统指标"""
    timestamp: str
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_usage_percent: float = 0.0
    db_size_mb: float = 0.0
    active_connections: int = 0
    pending_orders: int = 0
    error_count_1h: int = 0


class SystemMonitor:
    """系统监控器"""

    def __init__(self):
        self.metrics_history: list[SystemMetrics] = []
        self.alerts: list[dict] = []

    def collect_metrics(self) -> SystemMetrics:
        """收集系统指标"""
        import os
        import psutil

        metrics = SystemMetrics(
            timestamp=datetime.now().isoformat(),
            cpu_percent=psutil.cpu_percent(interval=0.1),
            memory_percent=psutil.virtual_memory().percent,
            disk_usage_percent=psutil.disk_usage("/").percent,
        )

        # 数据库大小
        try:
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "core", "quant.db")
            if os.path.exists(db_path):
                metrics.db_size_mb = os.path.getsize(db_path) / 1024 / 1024
        except Exception:
            pass

        self.metrics_history.append(metrics)
        if len(self.metrics_history) > 1440:  # 保留最近24小时（每分钟一条）
            self.metrics_history = self.metrics_history[-1440:]

        return metrics

    def check_alerts(self) -> list[dict]:
        """检查告警条件"""
        alerts = []

        if not self.metrics_history:
            return alerts

        latest = self.metrics_history[-1]

        if latest.cpu_percent > 80:
            alerts.append({"level": "warning", "metric": "cpu", "value": latest.cpu_percent,
                          "message": f"CPU使用率过高: {latest.cpu_percent:.1f}%"})

        if latest.memory_percent > 85:
            alerts.append({"level": "warning", "metric": "memory", "value": latest.memory_percent,
                          "message": f"内存使用率过高: {latest.memory_percent:.1f}%"})

        if latest.disk_usage_percent > 90:
            alerts.append({"level": "critical", "metric": "disk", "value": latest.disk_usage_percent,
                          "message": f"磁盘使用率过高: {latest.disk_usage_percent:.1f}%"})

        self.alerts.extend(alerts)
        return alerts

    def get_status(self) -> dict:
        """获取系统状态摘要"""
        if not self.metrics_history:
            return {"status": "unknown", "metrics": None}

        latest = self.metrics_history[-1]
        return {
            "status": "healthy" if latest.cpu_percent < 70 and latest.memory_percent < 80 else "warning",
            "metrics": {
                "cpu_percent": latest.cpu_percent,
                "memory_percent": latest.memory_percent,
                "disk_usage_percent": latest.disk_usage_percent,
                "db_size_mb": round(latest.db_size_mb, 2),
            },
            "alert_count": len(self.alerts),
        }


# 全局单例
_system_monitor: SystemMonitor | None = None


def get_system_monitor() -> SystemMonitor:
    """获取系统监控器单例"""
    global _system_monitor
    if _system_monitor is None:
        _system_monitor = SystemMonitor()
    return _system_monitor
