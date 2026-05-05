"""
ministries/personnel/strategy_registry.py —— 吏部·策略注册中心

职责：
  1. 定义选股策略元数据（名称、描述、权重、启用状态）
  2. 策略生命周期管理（注册、启用/禁用、热更新权重）
  3. 为回测和每日选股提供统一的策略配置源
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrategyDef:
    """选股策略定义"""
    key: str                    # 唯一标识: bottom_fishing, volume_breakout, ...
    name: str                   # 中文名称
    description: str            # 策略描述
    category: str               # fusion / v4 / custom
    enabled: bool = True        # 是否启用
    weight: float = 0.0         # 融合权重（仅 fusion 类有效，合计应为 1.0）
    function_name: str = ""     # 对应 strategy/strategies.py 中的函数名
    parameters: dict = field(default_factory=dict)  # 策略参数


# 策略注册表（进程内单例）
class StrategyRegistry:
    """吏部·策略注册中心"""

    def __init__(self):
        self._strategies: dict[str, StrategyDef] = {}
        self._init_defaults()

    def _init_defaults(self):
        """初始化内置策略库"""
        defaults = [
            StrategyDef(
                key="volume_breakout",
                name="放量突破",
                description="成交量放大 + 均线多头排列，捕捉突破信号",
                category="fusion",
                weight=0.00,
                function_name="strategy_volume_breakout",
                parameters={"vol_factor": 2.0, "rise_3d": 0.03, "score_min": 1.0},
            ),
            StrategyDef(
                key="ma_convergence",
                name="均线粘合",
                description="均线粘合后向上发散，捕捉趋势启动点",
                category="fusion",
                weight=0.00,
                function_name="strategy_ma_convergence",
                parameters={"ma_narrow": 0.03},
            ),
            StrategyDef(
                key="price_volume_divergence",
                name="量价背离",
                description="价稳量缩，筹码集中，捕捉反转信号",
                category="fusion",
                weight=0.00,
                function_name="strategy_price_volume_divergence",
                parameters={},
            ),
            StrategyDef(
                key="bottom_fishing",
                name="抄底",
                description="超跌后放量反弹，捕捉底部反转",
                category="fusion",
                weight=1.00,
                function_name="strategy_bottom_fishing",
                parameters={"drop_threshold": -0.15},
            ),
            StrategyDef(
                key="whale_accumulation",
                name="主力建仓",
                description="低位放量不涨，识别主力吸筹行为",
                category="fusion",
                weight=0.00,
                function_name="strategy_whale_accumulation",
                parameters={"vol_ratio": 1.5},
            ),
            StrategyDef(
                key="oversold_rebound",
                name="超跌反弹v4",
                description="连续超跌后止跌企稳，RSI回归，捕捉反弹机会",
                category="v4",
                weight=0.0,
                function_name="strategy_oversold_rebound",
                parameters={
                    "drop_threshold": -0.12,
                    "vol_20d_min": 8e7,
                    "rsi_low": 35,
                    "rsi_high": 50,
                    "score_threshold": 1.8,
                },
            ),
        ]
        for s in defaults:
            self._strategies[s.key] = s

    # ── 查询 ──────────────────────────────

    def get(self, key: str) -> Optional[StrategyDef]:
        return self._strategies.get(key)

    def get_all(self) -> list[StrategyDef]:
        return list(self._strategies.values())

    def get_enabled(self) -> list[StrategyDef]:
        return [s for s in self._strategies.values() if s.enabled]

    def get_fusion_weights(self) -> dict[str, float]:
        """返回当前启用的融合策略权重字典"""
        return {s.key: s.weight for s in self.get_enabled() if s.category == "fusion"}

    def get_fusion_weights_list(self) -> list[float]:
        """返回按固定顺序排列的权重列表 [vol, ma, diverge, bottom, whale]"""
        order = ["volume_breakout", "ma_convergence", "price_volume_divergence",
                 "bottom_fishing", "whale_accumulation"]
        return [self._strategies[k].weight if k in self._strategies else 0.0 for k in order]

    # ── 管理 ──────────────────────────────

    def register(self, strategy: StrategyDef):
        self._strategies[strategy.key] = strategy

    def enable(self, key: str) -> bool:
        s = self._strategies.get(key)
        if s:
            s.enabled = True
            return True
        return False

    def disable(self, key: str) -> bool:
        s = self._strategies.get(key)
        if s:
            s.enabled = False
            return True
        return False

    def set_weight(self, key: str, weight: float) -> bool:
        s = self._strategies.get(key)
        if s:
            s.weight = max(0.0, min(1.0, weight))
            return True
        return False

    def set_weights(self, weights: dict[str, float]):
        for key, w in weights.items():
            self.set_weight(key, w)

    def to_dict(self) -> list[dict]:
        return [
            {
                "key": s.key,
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "enabled": s.enabled,
                "weight": s.weight,
                "function_name": s.function_name,
                "parameters": s.parameters,
            }
            for s in self._strategies.values()
        ]


# 单例
_registry: Optional[StrategyRegistry] = None


def get_strategy_registry() -> StrategyRegistry:
    global _registry
    if _registry is None:
        _registry = StrategyRegistry()
    return _registry
