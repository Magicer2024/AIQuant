"""
config/strategy_params.py —— 各策略默认参数
"""
from typing import Dict, Any

# ── 5策略融合权重 ──────────────────────────────
# 顺序：[放量突破, 均线粘合, 量价背离, 抄底, 主力建仓]
DEFAULT_WEIGHTS = [0.30, 0.15, 0.20, 0.20, 0.15]

# 纯抄底策略（历史实验配置）
PURE_BOTTOM_WEIGHTS = [0.00, 0.00, 0.00, 1.00, 0.00]

# ── 策略1：放量突破 ────────────────────────────
VOLUME_BREAKOUT = {
    "vol_factor": 1.5,    # 放量倍数
    "rise_3d": 0.02,      # 3日涨幅最小值
    "score_min": 1.0,     # 触发所需最少条件数
}

# ── 策略2：均线粘合 ────────────────────────────
MA_CONVERGENCE = {
    "ma_diff_pct": 0.03,  # MA5/10/20 最大偏差
    "score_min": 1.0,
}

# ── 策略3：量价背离 ────────────────────────────
PRICE_VOLUME_DIVERGENCE = {
    "lookback": 20,       # 回溯天数
    "score_min": 1.0,
}

# ── 策略4：抄底 ────────────────────────────────
BOTTOM_FISHING = {
    "drop_5d": -0.08,     # 5日跌幅阈值
    "rsi_threshold": 30,  # RSI 超卖线
    "score_min": 1.0,
}

# ── 策略5：主力建仓 ────────────────────────────
WHALE_ACCUMULATION = {
    "volume_spike": 2.0,  # 成交量突增倍数
    "score_min": 1.0,
}

# ── 超跌反弹 v4 策略 ───────────────────────────
OVERSOLD_REBOUND_V4 = {
    "score_threshold": 1.8,
    "stop_loss": -0.06,
    "trailing_pct": 0.10,
    "single_pos_ratio": 0.40,
    "use_market_timing": True,
    "use_trailing_stop": True,
}

# ── v4 参数扫描网格 ────────────────────────────
V4_PARAM_GRID = [
    {"name": "v4 tr10% pos30% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.10, "single_pos_ratio": 0.30},
    {"name": "v4 tr10% pos40% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.10, "single_pos_ratio": 0.40},
    {"name": "v4 tr12% pos40% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.12, "single_pos_ratio": 0.40},
    {"name": "v4 tr15% pos40% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.15, "single_pos_ratio": 0.40},
    {"name": "v4 tr10% pos50% sl=-5%", "score_threshold": 1.8, "stop_loss": -0.05, "trailing_pct": 0.10, "single_pos_ratio": 0.50},
]


# ── Phase 1: 模板穷举配置 ──
PHASE1_CONFIG = {
    "ic_min_abs": 0.02,           # IC 过滤阈值
    "top_per_cluster": 3,         # 每类因子保留数
    "thresholds": [0.2, 0.35, 0.5, 0.65, 0.8],
    "sample_stocks": 200,         # 快速回测采样数
    "min_trades": 20,             # 最少交易次数
    "top_n_rules": 50,            # 入库数量
    "backtest_start": "20220101",
}

# 交叉信号预定义对 (Phase 1 T3 模板)
CROSS_PAIRS = [
    ("MACD_DIF", "MACD_DEA"),
    ("KDJ_K", "KDJ_D"),
    ("MA5_偏离", "MA20_偏离"),
    ("PDI", "MDI"),
]


def get_strategy_params(name: str) -> Dict[str, Any]:
    """按名称获取策略参数"""
    mapping = {
        "volume_breakout": VOLUME_BREAKOUT,
        "ma_convergence": MA_CONVERGENCE,
        "price_volume_divergence": PRICE_VOLUME_DIVERGENCE,
        "bottom_fishing": BOTTOM_FISHING,
        "whale_accumulation": WHALE_ACCUMULATION,
        "oversold_rebound_v4": OVERSOLD_REBOUND_V4,
    }
    return mapping.get(name, {})
