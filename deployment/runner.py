"""
deployment/runner.py —— 策略运行时

职责：
  1. 在独立线程中运行策略
  2. 定时触发信号扫描
  3. 通过 Broker 执行交易
  4. 记录运行日志和绩效
"""

import os
import json
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional


class RunnerStatus(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class RunnerStats:
    """运行统计"""
    total_signals: int = 0
    total_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    start_time: str = ""
    last_run_time: str = ""
    error_count: int = 0
    error_log: list = field(default_factory=list)


class StrategyRunner:
    """策略运行时"""

    def __init__(self, strategy_id: str, strategy_name: str,
                 interval_seconds: int = 60,
                 strategy_func: Callable = None,
                 config: dict = None,
                 strategy_content: str = ""):
        self.strategy_id = strategy_id
        self.strategy_name = strategy_name
        self.interval = interval_seconds
        self.strategy_func = strategy_func
        self.config = config or {}
        self.strategy_content = strategy_content  # 策略内容（JSON文本或描述）

        self.status = RunnerStatus.STOPPED
        self.stats = RunnerStats()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 运行时日志
        self.logs: list[dict] = []
        self.max_logs = 500

    def start(self) -> bool:
        """启动策略运行"""
        with self._lock:
            if self.status in (RunnerStatus.RUNNING, RunnerStatus.STARTING):
                return False
            self.status = RunnerStatus.STARTING
            self._stop_event.clear()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._log("info", "策略启动")
        return True

    def stop(self) -> bool:
        """停止策略运行"""
        with self._lock:
            if self.status == RunnerStatus.STOPPED:
                return False
            self.status = RunnerStatus.STOPPED
            self._stop_event.set()

        self._log("info", "策略停止")
        return True

    def pause(self) -> bool:
        """暂停（保留线程，跳过执行）"""
        with self._lock:
            if self.status != RunnerStatus.RUNNING:
                return False
            self.status = RunnerStatus.PAUSED
        self._log("info", "策略暂停")
        return True

    def resume(self) -> bool:
        """恢复运行"""
        with self._lock:
            if self.status != RunnerStatus.PAUSED:
                return False
            self.status = RunnerStatus.RUNNING
        self._log("info", "策略恢复")
        return True

    def _run_loop(self):
        """主运行循环"""
        self.stats.start_time = datetime.now().isoformat()
        self.status = RunnerStatus.RUNNING

        while not self._stop_event.is_set():
            try:
                if self.status == RunnerStatus.RUNNING:
                    self._execute_cycle()
                time.sleep(self.interval)
            except Exception as e:
                self.stats.error_count += 1
                self.stats.error_log.append({
                    "time": datetime.now().isoformat(),
                    "error": str(e),
                })
                self._log("error", f"运行异常: {e}")
                self.status = RunnerStatus.ERROR
                time.sleep(5)  # 异常后等待 5 秒再重试

        self.status = RunnerStatus.STOPPED

    def _execute_cycle(self):
        """执行一个策略周期"""
        self.stats.last_run_time = datetime.now().isoformat()

        if self.strategy_func:
            try:
                result = self.strategy_func(self.config)
                if result:
                    signals = result.get("signals", [])
                    self.stats.total_signals += len(signals)
                    for sig in signals:
                        self._log("signal", f"信号: {sig.get('code')} {sig.get('action')}")
            except Exception as e:
                raise

    def _log(self, level: str, message: str):
        """记录运行日志"""
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

    def get_status(self) -> dict:
        """获取运行状态"""
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "status": self.status.value,
            "interval": self.interval,
            "strategy_content": self.strategy_content,
            "stats": asdict(self.stats),
            "logs": self.logs[-20:],  # 最近 20 条
        }

    def update_content(self, content: str):
        """更新策略内容"""
        with self._lock:
            self.strategy_content = content
        self._log("info", "策略内容已更新")

    def update_config(self, config: dict):
        """热更新配置"""
        with self._lock:
            self.config.update(config)
        self._log("info", "配置已更新")
