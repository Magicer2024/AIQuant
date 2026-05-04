"""
strategy/config.py —— 策略配置加载器

支持 YAML 配置文件热加载，无需重启服务。
"""

import os
import yaml
import threading
from typing import Any

# 配置文件路径
CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "strategies"
)
DEFAULT_CONFIG = os.path.join(CONFIG_DIR, "default.yaml")


class StrategyConfig:
    """
    策略配置单例
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
            with open(DEFAULT_CONFIG, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
            self._last_loaded = os.path.getmtime(DEFAULT_CONFIG)
            print(f"[StrategyConfig] 配置已加载: {DEFAULT_CONFIG}")
        except FileNotFoundError:
            print(f"[StrategyConfig] 配置文件不存在: {DEFAULT_CONFIG}")
            self._config = {}
        except Exception as e:
            print(f"[StrategyConfig] 加载配置失败: {e}")

    def reload(self):
        """强制重新加载配置"""
        with self._lock:
            self._load_config()

    def _auto_reload(self):
        """自动检测文件修改并热加载"""
        try:
            mtime = os.path.getmtime(DEFAULT_CONFIG)
            if mtime > self._last_loaded:
                print("[StrategyConfig] 检测到配置变更，自动热加载...")
                self.reload()
        except FileNotFoundError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项，支持点号分隔的路径
        例如: get("volume_breakout.params.vol_factor") -> 1.5
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

    def get_strategy(self, name: str) -> dict:
        """获取指定策略的完整配置"""
        cfg = self.get(name, {})
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def is_strategy_enabled(self, name: str) -> bool:
        """检查策略是否启用"""
        return self.get(f"{name}.enabled", True)

    def get_strategy_params(self, name: str) -> dict:
        """获取策略参数"""
        return self.get(f"{name}.params", {})

    def get_fusion_weights(self, preset: str = "default") -> list:
        """获取融合权重"""
        return self.get(f"fusion_weights.{preset}", [0.30, 0.15, 0.20, 0.20, 0.15])

    def all_strategies(self) -> dict:
        """返回所有策略配置"""
        self._auto_reload()
        strategies = {}
        for k, v in self._config.items():
            if isinstance(v, dict) and v.get("enabled") is not None:
                strategies[k] = v
        return strategies

    def to_dict(self) -> dict:
        """导出完整配置字典"""
        self._auto_reload()
        return self._config.copy()

    def get_backtest_params(self) -> dict:
        """获取回测参数"""
        return self.get("backtest", {})

    def get_trade_params(self) -> dict:
        """获取交易参数（止损止盈等）"""
        return self.get("trade", {})


# 全局配置实例
strategy_config = StrategyConfig()
