"""
strategy/adaptive_weights.py —— 融合权重自适应
===============================================

根据上证指数(000001)近期状态判定市场环境，输出动态权重：

| 市场状态 | 判定条件 | 权重倾向 |
|----------|----------|----------|
| 牛市/上升趋势 | MA20 斜率 > 0 且 close > MA20 | 加重放量突破+均线 |
| 震荡市 | MA20 斜率 ≈ 0 或 close 在 MA20 附近 | 默认均衡 |
| 熊市/下跌趋势 | MA20 斜率 < 0 且 close < MA20 | 加重抄底+背离 |

额外维度：20 日波动率 > 2% 时，提高开仓阈值（更保守）。

供 core/sync.py 的 recalc_all_scores 调用。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from config.strategy_params import (
    ADAPTIVE_WEIGHTS_ENABLED,
    BULL_WEIGHTS,
    RANGE_WEIGHTS,
    BEAR_WEIGHTS,
    DEFAULT_SIG_THRESHOLD,
    HIGH_VOL_THRESHOLD,
    HIGH_VOL_SIG_THRESHOLD,
)


def detect_market_state(index_code: str = "000001") -> dict:
    """检测当前市场状态。

    Returns:
        dict: {
            regime: "bull" | "range" | "bear",
            volatility: float,      # 20日波动率（std of pct_change）
            ma20_slope: float,      # MA20 斜率（近5日变化率）
            close_vs_ma20: float,   # close 相对 MA20 的偏离度
            detail: str,            # 中文描述
        }
    """
    try:
        from core.db import get_index_daily
        idx = get_index_daily(index_code, None, None)
    except Exception:
        idx = None

    if idx is None or idx.empty or len(idx) < 30:
        return {
            "regime": "range",
            "volatility": 0.0,
            "ma20_slope": 0.0,
            "close_vs_ma20": 0.0,
            "detail": "数据不足，默认震荡市",
        }

    close = idx["close"].astype(float).sort_index()

    # MA20
    ma20 = close.rolling(20).mean()
    latest_close = float(close.iloc[-1])
    latest_ma20 = float(ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else latest_close

    # MA20 斜率：近5日 MA20 的变化率
    if len(ma20) >= 6 and not pd.isna(ma20.iloc[-6]):
        ma20_slope = (latest_ma20 - float(ma20.iloc[-6])) / float(ma20.iloc[-6])
    else:
        ma20_slope = 0.0

    # close 相对 MA20 的偏离度
    close_vs_ma20 = (latest_close - latest_ma20) / latest_ma20 if latest_ma20 > 0 else 0.0

    # 20 日波动率（pct_change 的标准差）
    if "pct_change" in idx.columns:
        pct = idx["pct_change"].astype(float).dropna()
        volatility = float(pct.tail(20).std()) if len(pct) >= 20 else 0.0
    else:
        # 用 close 计算
        pct = close.pct_change().dropna()
        volatility = float(pct.tail(20).std()) if len(pct) >= 20 else 0.0

    # 判定市场状态
    # 牛市：MA20 斜率 > 0.5% 且 close > MA20
    # 熊市：MA20 斜率 < -0.5% 且 close < MA20
    # 震荡：其他
    slope_threshold = 0.005  # 0.5%

    if ma20_slope > slope_threshold and close_vs_ma20 > 0:
        regime = "bull"
        detail = f"上升趋势（MA20斜率 +{ma20_slope*100:.2f}%，指数站上MA20）"
    elif ma20_slope < -slope_threshold and close_vs_ma20 < 0:
        regime = "bear"
        detail = f"下跌趋势（MA20斜率 {ma20_slope*100:.2f}%，指数跌破MA20）"
    else:
        regime = "range"
        detail = f"震荡整理（MA20斜率 {ma20_slope*100:.2f}%）"

    return {
        "regime": regime,
        "volatility": round(volatility, 4),
        "ma20_slope": round(ma20_slope, 4),
        "close_vs_ma20": round(close_vs_ma20, 4),
        "detail": detail,
    }


def get_adaptive_weights(market_state: Optional[dict] = None) -> list:
    """根据市场状态返回 5 策略融合权重。

    权重顺序：[放量突破, 均线粘合, 量价背离, 抄底, 主力建仓]
    """
    if not ADAPTIVE_WEIGHTS_ENABLED:
        from config.strategy_params import DEFAULT_WEIGHTS
        return DEFAULT_WEIGHTS

    if market_state is None:
        market_state = detect_market_state()

    regime = market_state.get("regime", "range")

    if regime == "bull":
        return BULL_WEIGHTS
    elif regime == "bear":
        return BEAR_WEIGHTS
    else:
        return RANGE_WEIGHTS


def get_adaptive_threshold(market_state: Optional[dict] = None) -> float:
    """根据波动率调整融合分阈值。

    高波动时提高阈值（更保守），低波动时用默认阈值。
    """
    if not ADAPTIVE_WEIGHTS_ENABLED:
        return DEFAULT_SIG_THRESHOLD

    if market_state is None:
        market_state = detect_market_state()

    volatility = market_state.get("volatility", 0.0)

    # 波动率 > 2% 时提高阈值
    if volatility > HIGH_VOL_THRESHOLD:
        return HIGH_VOL_SIG_THRESHOLD
    return DEFAULT_SIG_THRESHOLD


def get_market_summary() -> dict:
    """获取市场状态摘要（供 API 返回）。"""
    state = detect_market_state()
    weights = get_adaptive_weights(state)
    threshold = get_adaptive_threshold(state)

    regime_label = {
        "bull": "牛市/上升趋势",
        "range": "震荡市",
        "bear": "熊市/下跌趋势",
    }

    weight_labels = ["放量突破", "均线粘合", "量价背离", "抄底", "主力建仓"]
    weight_detail = {label: w for label, w in zip(weight_labels, weights)}

    return {
        "regime": state["regime"],
        "regime_label": regime_label.get(state["regime"], "未知"),
        "detail": state["detail"],
        "volatility": state["volatility"],
        "ma20_slope": state["ma20_slope"],
        "close_vs_ma20": state["close_vs_ma20"],
        "weights": weights,
        "weight_detail": weight_detail,
        "sig_threshold": threshold,
        "adaptive_enabled": ADAPTIVE_WEIGHTS_ENABLED,
    }
