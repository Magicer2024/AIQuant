"""
governance/chancellery/strategy_engine.py —— 中书省策略引擎

职责：
  1. 加载太子院的执行计划
  2. 执行多策略融合评分
  3. 输出交易信号
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from strategy.config import strategy_config


@dataclass
class Signal:
    """交易信号"""
    code: str
    name: str
    price: float
    score: float
    trigger_list: list[str] = field(default_factory=list)
    stop_loss: float = -0.06
    take_profit: float = 0.20
    buy_volume: int = 0
    buy_money: float = 0.0
    strategy_notes: str = ""


class StrategyEngine:
    """中书省策略引擎"""

    def __init__(self):
        self.plan = None
        self.config = strategy_config

    def load_plan(self, plan: dict):
        """加载太子院的执行计划"""
        self.plan = plan

    def run_scan(self, stock_codes: list[str] = None) -> list[Signal]:
        """
        执行策略扫描
        :param stock_codes: 指定扫描的股票列表，None=全市场
        :return: 信号列表
        """
        # TODO: 接入现有的 quant.py 扫描逻辑
        # 这里提供框架，实际扫描由 signal_agent 完成
        return []

    def score_stock(self, code: str, data: dict) -> float:
        """
        对单只股票进行多因子评分
        :param code: 股票代码
        :param data: 股票数据
        :return: 融合评分
        """
        weights = self.plan.get("fusion_weights", [0.30, 0.15, 0.20, 0.20, 0.15]) if self.plan else \
                  self.config.get_fusion_weights("default")

        # 各策略子评分（0-10）
        s1 = data.get("vol_score", 0)
        s2 = data.get("ma_score", 0)
        s3 = data.get("diverge_score", 0)
        s4 = data.get("bottom_score", 0)
        s5 = data.get("whale_score", 0)

        # 加权融合
        fusion = s1 * weights[0] + s2 * weights[1] + s3 * weights[2] + \
                 s4 * weights[3] + s5 * weights[4]

        return round(fusion, 2)

    def get_params(self) -> dict:
        """获取当前策略参数"""
        if self.plan:
            return {
                "fusion_weights": self.plan.get("fusion_weights"),
                "score_threshold": self.plan.get("score_threshold", 20.0),
                "stop_loss": self.plan.get("stop_loss", -0.06),
                "take_profit": self.plan.get("take_profit", 0.20),
            }
        return {
            "fusion_weights": self.config.get_fusion_weights("default"),
            "score_threshold": self.config.get("fusion.threshold", 20.0),
            "stop_loss": self.config.get("trade.stop_loss", -0.06),
            "take_profit": self.config.get("trade.take_profit", 0.20),
        }


# 全局单例
_strategy_engine: StrategyEngine | None = None


def get_strategy_engine() -> StrategyEngine:
    """获取策略引擎单例"""
    global _strategy_engine
    if _strategy_engine is None:
        _strategy_engine = StrategyEngine()
    return _strategy_engine
