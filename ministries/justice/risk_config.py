"""
ministries/justice/risk_config.py —— 风控配置加载器

支持 YAML 配置文件热加载，无需重启服务。
"""

import os
import yaml
import threading
import time
from typing import Any

# 配置文件路径（从 ministries/justice/ 往上 3 级到项目根）
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "risk_config.yaml"
)


class RiskConfig:
    """
    风控配置单例
    支持热加载：调用 reload() 或访问属性时自动检测文件修改
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._config = {}
        self._last_loaded = 0
        self._load_config()
        self._initialized = True

    def _load_config(self):
        """从 YAML 文件加载配置"""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
            self._last_loaded = os.path.getmtime(CONFIG_PATH)
            print(f"[RiskConfig] 配置已加载: {CONFIG_PATH}")
        except FileNotFoundError:
            print(f"[RiskConfig] 配置文件不存在: {CONFIG_PATH}，使用默认配置")
            self._config = {}
        except Exception as e:
            print(f"[RiskConfig] 加载配置失败: {e}")

    def reload(self):
        """强制重新加载配置"""
        with self._lock:
            self._load_config()

    def _auto_reload(self):
        """自动检测文件修改并热加载"""
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime > self._last_loaded:
                print("[RiskConfig] 检测到配置变更，自动热加载...")
                self.reload()
        except FileNotFoundError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项，支持点号分隔的路径
        例如: get("max_drawdown.thresholds.block") -> 0.15
        """
        self._auto_reload()
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def get_rule_config(self, rule_name: str) -> dict:
        """获取指定规则的完整配置"""
        cfg = self.get(rule_name, {})
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def is_rule_enabled(self, rule_name: str) -> bool:
        """检查规则是否启用"""
        return self.get(f"{rule_name}.enabled", True)

    def get_threshold(self, rule_name: str, level: str) -> float | int:
        """获取指定规则的阈值"""
        return self.get(f"{rule_name}.thresholds.{level}", 0)

    def get_message(self, rule_name: str, level: str, **kwargs) -> str:
        """获取格式化后的消息模板"""
        template = self.get(f"{rule_name}.messages.{level}", "")
        if template and kwargs:
            try:
                return template.format(**kwargs)
            except KeyError:
                return template
        return template

    def get_suggestion(self, rule_name: str, level: str) -> str:
        """获取建议文本"""
        return self.get(f"{rule_name}.suggestions.{level}", "")

    def all_rules(self) -> dict:
        """返回所有规则配置（排除元数据）"""
        self._auto_reload()
        return {k: v for k, v in self._config.items()
                if isinstance(v, dict) and k not in ("version", "last_modified")}

    def to_dict(self) -> dict:
        """导出完整配置字典"""
        self._auto_reload()
        return self._config.copy()


# 全局配置实例
risk_config = RiskConfig()
