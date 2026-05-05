"""
ministries/works/deployment_manager.py —— 策略部署管理器（工部）

职责：
  1. 注册/注销策略
  2. 部署策略到运行时（启动实盘）
  3. 监控所有运行中策略的状态
  4. 策略版本管理
"""

import os
import json
import time
from datetime import datetime
from typing import Optional

from ministries.works.deployment_runner import StrategyRunner, RunnerStatus


DEPLOYMENT_DIR = os.path.join(os.path.dirname(__file__), "records")
os.makedirs(DEPLOYMENT_DIR, exist_ok=True)


class DeploymentManager:
    """策略部署管理器"""

    def __init__(self):
        self._runners: dict[str, StrategyRunner] = {}
        self._deployments: dict[str, dict] = {}  # 部署记录
        self._load_records()

    def register_strategy(self, strategy_id: str, strategy_name: str,
                          strategy_func=None, config: dict = None,
                          interval: int = 60,
                          strategy_content: str = "") -> dict:
        """
        注册策略（仅注册，不启动）
        """
        if strategy_id in self._runners:
            return {"success": False, "error": "策略已存在"}

        runner = StrategyRunner(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            interval_seconds=interval,
            strategy_func=strategy_func,
            config=config or {},
            strategy_content=strategy_content,
        )
        self._runners[strategy_id] = runner

        record = {
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "interval": interval,
            "config": config or {},
            "strategy_content": strategy_content,
            "registered_at": datetime.now().isoformat(),
            "deployed_at": None,
            "status": "registered",
        }
        self._deployments[strategy_id] = record
        self._save_records()

        return {"success": True, "strategy_id": strategy_id, "status": "registered"}

    def deploy(self, strategy_id: str) -> dict:
        """部署并启动策略"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}

        if runner.status == RunnerStatus.RUNNING:
            return {"success": False, "error": "策略已在运行中"}

        success = runner.start()
        if success:
            self._deployments[strategy_id]["deployed_at"] = datetime.now().isoformat()
            self._deployments[strategy_id]["status"] = "running"
            self._save_records()
            return {"success": True, "strategy_id": strategy_id, "status": "running"}
        return {"success": False, "error": "启动失败"}

    def stop(self, strategy_id: str) -> dict:
        """停止策略"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}

        success = runner.stop()
        if success:
            self._deployments[strategy_id]["status"] = "stopped"
            self._save_records()
            return {"success": True, "strategy_id": strategy_id, "status": "stopped"}
        return {"success": False, "error": "停止失败（可能已停止）"}

    def pause(self, strategy_id: str) -> dict:
        """暂停策略"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}
        success = runner.pause()
        if success:
            self._deployments[strategy_id]["status"] = "paused"
            self._save_records()
        return {"success": success, "strategy_id": strategy_id}

    def resume(self, strategy_id: str) -> dict:
        """恢复策略"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}
        success = runner.resume()
        if success:
            self._deployments[strategy_id]["status"] = "running"
            self._save_records()
        return {"success": success, "strategy_id": strategy_id}

    def undeploy(self, strategy_id: str) -> dict:
        """注销策略"""
        runner = self._runners.get(strategy_id)
        if runner:
            runner.stop()
            del self._runners[strategy_id]
        if strategy_id in self._deployments:
            del self._deployments[strategy_id]
        self._save_records()
        return {"success": True, "strategy_id": strategy_id, "status": "removed"}

    def get_status(self, strategy_id: str = None) -> dict:
        """获取策略状态"""
        if strategy_id:
            runner = self._runners.get(strategy_id)
            if not runner:
                return {"success": False, "error": "策略未注册"}
            return {"success": True, "runner": runner.get_status()}

        # 全部状态
        all_status = []
        for sid, runner in self._runners.items():
            s = runner.get_status()
            s["deployed_at"] = self._deployments.get(sid, {}).get("deployed_at")
            s["registered_at"] = self._deployments.get(sid, {}).get("registered_at")
            all_status.append(s)

        running_count = sum(1 for s in all_status if s["status"] == "running")
        return {
            "success": True,
            "total": len(all_status),
            "running": running_count,
            "strategies": all_status,
        }

    def update_config(self, strategy_id: str, config: dict) -> dict:
        """热更新策略配置"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}
        runner.update_config(config)
        self._deployments[strategy_id]["config"].update(config)
        self._save_records()
        return {"success": True, "strategy_id": strategy_id}

    def update_content(self, strategy_id: str, content: str) -> dict:
        """更新策略内容"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}
        runner.update_content(content)
        self._deployments[strategy_id]["strategy_content"] = content
        self._save_records()
        return {"success": True, "strategy_id": strategy_id}

    def get_content(self, strategy_id: str) -> dict:
        """获取策略内容"""
        runner = self._runners.get(strategy_id)
        if not runner:
            return {"success": False, "error": "策略未注册"}
        return {
            "success": True,
            "strategy_id": strategy_id,
            "strategy_name": runner.strategy_name,
            "strategy_content": runner.strategy_content,
        }

    def _save_records(self):
        """保存部署记录到文件"""
        path = os.path.join(DEPLOYMENT_DIR, "deployments.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._deployments, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[DeploymentManager] 保存记录失败: {e}")

    def _load_records(self):
        """加载部署记录"""
        path = os.path.join(DEPLOYMENT_DIR, "deployments.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._deployments = json.load(f)
            except Exception:
                self._deployments = {}


# 全局单例
_manager: DeploymentManager | None = None


def get_deployment_manager() -> DeploymentManager:
    """获取部署管理器单例"""
    global _manager
    if _manager is None:
        _manager = DeploymentManager()
    return _manager
