"""
governance/chancellery/scoring_engine.py —— 中书省多因子评分引擎

职责：
  1. 计算单只股票的多维度因子得分
  2. 支持权重配置和自定义因子
  3. 输出标准化评分 (0-100)

因子体系：
  - 趋势因子 (Trend): MA多头排列、价格位置
  - 动量因子 (Momentum): RSI、MACD、KDJ
  - 波动率因子 (Volatility): ATR、布林带宽度
  - 成交量因子 (Volume): 量比、资金流入
  - 质量因子 (Quality):  ROE、负债率、营收增长
"""

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd


@dataclass
class FactorScore:
    """单因子得分"""
    name: str
    score: float       # 0-10
    weight: float      # 权重
    raw_value: float   # 原始值
    description: str


@dataclass
class MultiFactorResult:
    """多因子评分结果"""
    code: str
    name: str
    total_score: float
    grade: str         # A/B/C/D/F
    factors: list[FactorScore]
    signals: list[str]
    recommendation: str


class FactorRegistry:
    """因子注册表"""

    _factors: dict[str, Callable] = {}

    @classmethod
    def register(cls, name: str, fn: Callable):
        cls._factors[name] = fn

    @classmethod
    def get(cls, name: str) -> Callable | None:
        return cls._factors.get(name)

    @classmethod
    def list_factors(cls) -> list[str]:
        return list(cls._factors.keys())


# ── 内置因子计算函数 ──────────────────────────────

def calc_trend_score(df: pd.DataFrame) -> FactorScore:
    """趋势因子：均线多头排列 + 价格在MA之上"""
    if len(df) < 60:
        return FactorScore("趋势", 5.0, 0.25, 0, "数据不足")

    close = df["close"].values
    ma5 = df["close"].rolling(5).mean().values[-1]
    ma20 = df["close"].rolling(20).mean().values[-1]
    ma60 = df["close"].rolling(60).mean().values[-1]

    score = 5.0
    signals = []

    # 均线多头排列
    if ma5 > ma20 > ma60:
        score += 2.5
        signals.append("多头排列")
    elif ma5 < ma20 < ma60:
        score -= 2.5
        signals.append("空头排列")

    # 价格位置
    latest = close[-1]
    if latest > ma5:
        score += 1.5
    if latest > ma20:
        score += 1.0
    if latest > ma60:
        score += 0.5

    score = max(0, min(10, score))
    return FactorScore("趋势", round(score, 2), 0.25, latest, ";".join(signals) or "震荡")


def calc_momentum_score(df: pd.DataFrame) -> FactorScore:
    """动量因子：RSI + 价格变化率"""
    if len(df) < 14:
        return FactorScore("动量", 5.0, 0.20, 0, "数据不足")

    close = df["close"].values

    # RSI (14日)
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.mean(gain[-14:])
    avg_loss = np.mean(loss[-14:])
    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))

    # 价格变化率 (20日)
    pct_change = (close[-1] - close[-min(20, len(close))]) / close[-min(20, len(close))] * 100

    # RSI评分：30-70为中性，越靠近50越好；超卖(<30)加分，超买(>70)减分
    rsi_score = 10 - abs(rsi - 50) / 5
    if rsi < 30:
        rsi_score += 2  # 超卖反弹潜力
    elif rsi > 70:
        rsi_score -= 2  # 超买风险

    # 动量评分
    mom_score = 5 + pct_change * 0.5

    score = (rsi_score + mom_score) / 2
    score = max(0, min(10, score))

    desc = f"RSI={rsi:.1f}, 20日涨跌={pct_change:.1f}%"
    return FactorScore("动量", round(score, 2), 0.20, round(rsi, 2), desc)


def calc_volatility_score(df: pd.DataFrame) -> FactorScore:
    """波动率因子：ATR + 布林带"""
    if len(df) < 20:
        return FactorScore("波动率", 5.0, 0.15, 0, "数据不足")

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    # ATR (14日)
    tr1 = high[-14:] - low[-14:]
    tr2 = np.abs(high[-14:] - np.roll(close[-14:], 1))
    tr3 = np.abs(low[-14:] - np.roll(close[-14:], 1))
    tr = np.maximum(np.maximum(tr1, tr2[1:]), tr3[1:])
    atr = np.mean(tr)
    atr_pct = atr / close[-1] * 100

    # 波动率评分：适中最好（2%-5%），太低没机会，太高风险大
    if 2 <= atr_pct <= 5:
        score = 8.0
    elif atr_pct < 2:
        score = 5.0 + atr_pct
    elif atr_pct <= 8:
        score = 8.0 - (atr_pct - 5) * 0.5
    else:
        score = 5.5 - (atr_pct - 8) * 0.3

    score = max(0, min(10, score))
    desc = f"ATR={atr_pct:.2f}%"
    return FactorScore("波动率", round(score, 2), 0.15, round(atr_pct, 2), desc)


def calc_volume_score(df: pd.DataFrame) -> FactorScore:
    """成交量因子：量比 + 价量配合"""
    if len(df) < 20:
        return FactorScore("成交量", 5.0, 0.20, 0, "数据不足")

    vol = df["volume"].values
    close = df["close"].values

    # 量比 (今日/5日均量)
    vol_ratio = vol[-1] / (np.mean(vol[-5:]) + 1)

    # 价量配合：涨时放量、跌时缩量为佳
    price_change = (close[-1] - close[-2]) / close[-2] * 100 if len(close) > 1 else 0
    vol_change = (vol[-1] - vol[-2]) / (vol[-2] + 1) * 100 if len(vol) > 1 else 0

    score = 5.0
    if price_change > 0 and vol_change > 20:
        score += 3  # 上涨放量
    elif price_change < 0 and vol_change > 20:
        score -= 2  # 下跌放量
    elif price_change < 0 and vol_change < -20:
        score += 1  # 下跌缩量（企稳信号）

    # 量比评分
    if 1.5 <= vol_ratio <= 3:
        score += 1.5
    elif vol_ratio > 3:
        score += 0.5  # 天量，谨慎
    elif vol_ratio < 0.8:
        score -= 1  # 缩量

    score = max(0, min(10, score))
    desc = f"量比={vol_ratio:.2f}, 价涨={price_change:.1f}%/量涨={vol_change:.1f}%"
    return FactorScore("成交量", round(score, 2), 0.20, round(vol_ratio, 2), desc)


def calc_quality_score(df: pd.DataFrame) -> FactorScore:
    """质量因子：基于价格行为的质量评估"""
    if len(df) < 60:
        return FactorScore("质量", 5.0, 0.20, 0, "数据不足")

    close = df["close"].values

    # 趋势稳定性（60日收益率/波动率）
    returns_60 = (close[-1] - close[-60]) / close[-60] * 100
    volatility = np.std(np.diff(close[-60:]) / close[-60:-1] * 100)
    sharpe_like = returns_60 / (volatility + 1e-10)

    # 近期持续性（近5日 vs 近20日）
    ret_5 = (close[-1] - close[-5]) / close[-5] * 100
    ret_20 = (close[-1] - close[-20]) / close[-20] * 100

    score = 5.0
    if sharpe_like > 1:
        score += 2
    elif sharpe_like < -0.5:
        score -= 1.5

    if ret_5 > ret_20 > 0:
        score += 2  # 加速上涨
    elif ret_5 < 0 and ret_20 > 0:
        score -= 1  # 短期回调

    score = max(0, min(10, score))
    desc = f"60日收益={returns_60:.1f}%, 夏普比={sharpe_like:.2f}"
    return FactorScore("质量", round(score, 2), 0.20, round(sharpe_like, 2), desc)


# 注册所有因子
FactorRegistry.register("trend", calc_trend_score)
FactorRegistry.register("momentum", calc_momentum_score)
FactorRegistry.register("volatility", calc_volatility_score)
FactorRegistry.register("volume", calc_volume_score)
FactorRegistry.register("quality", calc_quality_score)


# ── 评分引擎 ──────────────────────────────────────

class MultiFactorScoringEngine:
    """多因子评分引擎"""

    DEFAULT_WEIGHTS = {
        "trend": 0.25,
        "momentum": 0.20,
        "volatility": 0.15,
        "volume": 0.20,
        "quality": 0.20,
    }

    def __init__(self, weights: dict | None = None):
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()

    def set_weights(self, weights: dict):
        """设置因子权重"""
        self.weights = weights

    def score(self, code: str, name: str, df: pd.DataFrame) -> MultiFactorResult:
        """
        对股票进行多因子评分
        :param code: 股票代码
        :param name: 股票名称
        :param df: 日线数据 DataFrame
        :return: 评分结果
        """
        factors = []
        signals = []
        total_weighted = 0
        total_weight = 0

        for factor_name, weight in self.weights.items():
            fn = FactorRegistry.get(factor_name)
            if fn is None:
                continue

            try:
                factor = fn(df)
                factor.weight = weight
                factors.append(factor)
                total_weighted += factor.score * weight
                total_weight += weight

                if factor.score >= 8:
                    signals.append(f"{factor.name}强势")
                elif factor.score <= 3:
                    signals.append(f"{factor.name}弱势")
            except Exception as e:
                factors.append(FactorScore(factor_name, 5.0, weight, 0, f"计算错误:{e}"))
                total_weight += weight

        if total_weight == 0:
            total_score = 50
        else:
            total_score = total_weighted / total_weight * 10

        total_score = max(0, min(100, total_score))
        grade = self._score_to_grade(total_score)
        recommendation = self._recommendation(total_score, signals)

        return MultiFactorResult(
            code=code,
            name=name,
            total_score=round(total_score, 1),
            grade=grade,
            factors=factors,
            signals=signals,
            recommendation=recommendation,
        )

    @staticmethod
    def _score_to_grade(score: float) -> str:
        if score >= 85: return "A"
        if score >= 70: return "B"
        if score >= 55: return "C"
        if score >= 40: return "D"
        return "F"

    @staticmethod
    def _recommendation(score: float, signals: list) -> str:
        if score >= 80:
            return "强烈推荐 - 多因子共振，趋势明确"
        elif score >= 65:
            return "推荐 - 多数因子正向，可择机介入"
        elif score >= 50:
            return "中性 - 信号混杂，观望为主"
        elif score >= 35:
            return "谨慎 - 弱势信号较多，注意风险"
        else:
            return "回避 - 多因子走弱，不建议参与"

    def get_available_factors(self) -> list[dict]:
        """获取可用因子列表"""
        return [
            {
                "name": name,
                "weight": self.weights.get(name, 0),
                "description": self._get_factor_description(name),
            }
            for name in FactorRegistry.list_factors()
        ]

    @staticmethod
    def _get_factor_description(name: str) -> str:
        descriptions = {
            "trend": "均线多头排列、价格相对位置",
            "momentum": "RSI、价格变化率、动能强度",
            "volatility": "ATR、布林带宽度、波动适中度",
            "volume": "量比、价量配合、资金流向",
            "quality": "趋势稳定性、收益风险比",
        }
        return descriptions.get(name, "")


# 全局单例
_scoring_engine: MultiFactorScoringEngine | None = None


def get_scoring_engine() -> MultiFactorScoringEngine:
    """获取评分引擎单例"""
    global _scoring_engine
    if _scoring_engine is None:
        _scoring_engine = MultiFactorScoringEngine()
    return _scoring_engine
