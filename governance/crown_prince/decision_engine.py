"""
governance/crown_prince/decision_engine.py —— 太子院决策引擎

职责：
  1. 接收策略意图（如" today 激进做多"、"防守为主"）
  2. 结合市场环境生成执行计划
  3. 动态调整策略权重和风险偏好
  4. 输出给中书省和尚书省执行
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MarketStance(Enum):
    """市场立场"""
    BULLISH = "bullish"      # 看多
    NEUTRAL = "neutral"      # 中性
    BEARISH = "bearish"      # 看空
    DEFENSE = "defense"      # 防守


class RiskAppetite(Enum):
    """风险偏好"""
    CONSERVATIVE = "conservative"  # 保守
    MODERATE = "moderate"          # 稳健
    AGGRESSIVE = "aggressive"      # 激进


@dataclass
class StrategyIntent:
    """策略意图"""
    intent_id: str
    created_at: str
    stance: MarketStance
    risk_appetite: RiskAppetite
    target_sectors: list[str] = field(default_factory=list)
    exclude_sectors: list[str] = field(default_factory=list)
    max_positions: int = 5
    single_position_ratio: float = 0.30
    notes: str = ""


@dataclass
class ExecutionPlan:
    """执行计划"""
    plan_id: str
    intent_id: str
    created_at: str
    fusion_weights: list[float] = field(default_factory=list)
    score_threshold: float = 20.0
    stop_loss: float = -0.06
    take_profit: float = 0.20
    trailing_stop: bool = True
    market_timing: bool = True
    max_daily_trades: int = 3


class DecisionEngine:
    """太子院决策引擎"""

    def __init__(self):
        self.current_intent: Optional[StrategyIntent] = None
        self.current_plan: Optional[ExecutionPlan] = None

    def set_intent(self, stance: str, risk: str = "moderate",
                   notes: str = "", **kwargs) -> StrategyIntent:
        """
        设置策略意图

        :param stance: bullish/neutral/bearish/defense
        :param risk: conservative/moderate/aggressive
        """
        intent = StrategyIntent(
            intent_id=f"intent-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            created_at=datetime.now().isoformat(),
            stance=MarketStance(stance),
            risk_appetite=RiskAppetite(risk),
            notes=notes,
            **kwargs
        )
        self.current_intent = intent
        return intent

    def generate_plan(self, intent: Optional[StrategyIntent] = None) -> ExecutionPlan:
        """根据意图生成执行计划"""
        intent = intent or self.current_intent
        if not intent:
            # 默认计划
            return self._default_plan()

        # 根据市场立场调整权重
        weights_map = {
            MarketStance.BULLISH: [0.35, 0.10, 0.15, 0.25, 0.15],   # 激进：突破+抄底
            MarketStance.NEUTRAL: [0.30, 0.15, 0.20, 0.20, 0.15],   # 均衡
            MarketStance.BEARISH: [0.15, 0.25, 0.30, 0.20, 0.10],   # 看空：背离+均线
            MarketStance.DEFENSE: [0.10, 0.20, 0.20, 0.40, 0.10],   # 防守：抄底为主
        }

        # 根据风险偏好调整阈值
        threshold_map = {
            RiskAppetite.CONSERVATIVE: 22.0,
            RiskAppetite.MODERATE: 20.0,
            RiskAppetite.AGGRESSIVE: 18.0,
        }

        plan = ExecutionPlan(
            plan_id=f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            intent_id=intent.intent_id,
            created_at=datetime.now().isoformat(),
            fusion_weights=weights_map.get(intent.stance, [0.30, 0.15, 0.20, 0.20, 0.15]),
            score_threshold=threshold_map.get(intent.risk_appetite, 20.0),
            stop_loss=-0.05 if intent.risk_appetite == RiskAppetite.AGGRESSIVE else -0.06,
            take_profit=0.15 if intent.risk_appetite == RiskAppetite.CONSERVATIVE else 0.20,
            trailing_stop=intent.risk_appetite != RiskAppetite.CONSERVATIVE,
            market_timing=True,
            max_daily_trades=5 if intent.risk_appetite == RiskAppetite.AGGRESSIVE else 3,
        )
        self.current_plan = plan
        return plan

    def _default_plan(self) -> ExecutionPlan:
        """默认执行计划"""
        return ExecutionPlan(
            plan_id=f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            intent_id="default",
            created_at=datetime.now().isoformat(),
            fusion_weights=[0.30, 0.15, 0.20, 0.20, 0.15],
            score_threshold=20.0,
            stop_loss=-0.06,
            take_profit=0.20,
            trailing_stop=True,
            market_timing=True,
            max_daily_trades=3,
        )

    def get_current_status(self) -> dict:
        """获取当前决策状态"""
        return {
            "intent": self.current_intent.__dict__ if self.current_intent else None,
            "plan": self.current_plan.__dict__ if self.current_plan else None,
        }


# 全局单例
_decision_engine: DecisionEngine | None = None


def get_decision_engine() -> DecisionEngine:
    """获取决策引擎单例"""
    global _decision_engine
    if _decision_engine is None:
        _decision_engine = DecisionEngine()
    return _decision_engine
