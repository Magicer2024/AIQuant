"""
backtest/conditions.py —— 可视化回测选股条件元数据

为前端配置面板提供"指标字典"。每个条目包含：
  - id       内部唯一标识
  - name     中文显示名
  - category 所属类目（technical/fundamental/volume）
  - operators  可选的比较/触发操作
  - params     参数定义（类型/范围/默认值）
"""
from __future__ import annotations

from typing import Any, Dict, List


# ──────────── 选股条件元数据 ────────────
CONDITION_CATALOG: List[Dict[str, Any]] = [
    {
        "category": "technical",
        "label": "技术指标",
        "items": [
            {
                "id": "ma_cross",
                "name": "MA 均线交叉",
                "params": [
                    {"key": "short_period", "name": "短周期", "type": "int", "default": 5, "min": 2, "max": 60},
                    {"key": "long_period", "name": "长周期", "type": "int", "default": 20, "min": 5, "max": 250},
                ],
                "operators": [
                    {"id": "golden_cross", "name": "金叉"},
                    {"id": "death_cross", "name": "死叉"},
                    {"id": "bull_above", "name": "多头排列(短>长)"},
                ],
            },
            {
                "id": "macd_cross",
                "name": "MACD 金叉/死叉",
                "params": [
                    {"key": "fast", "name": "快线", "type": "int", "default": 12, "min": 2, "max": 60},
                    {"key": "slow", "name": "慢线", "type": "int", "default": 26, "min": 5, "max": 120},
                    {"key": "signal", "name": "信号线", "type": "int", "default": 9, "min": 2, "max": 60},
                ],
                "operators": [
                    {"id": "golden_cross", "name": "金叉(DIF 上穿 DEA)"},
                    {"id": "death_cross", "name": "死叉(DIF 下穿 DEA)"},
                    {"id": "hist_positive", "name": "柱体为正"},
                    {"id": "hist_negative", "name": "柱体为负"},
                ],
            },
            {
                "id": "kdj_cross",
                "name": "KDJ 交叉/超买超卖",
                "params": [
                    {"key": "n", "name": "RSV 周期", "type": "int", "default": 9, "min": 3, "max": 60},
                    {"key": "overbought", "name": "超买阈值", "type": "float", "default": 80, "min": 50, "max": 100},
                    {"key": "oversold", "name": "超卖阈值", "type": "float", "default": 20, "min": 0, "max": 50},
                ],
                "operators": [
                    {"id": "golden_cross", "name": "金叉(K 上穿 D)"},
                    {"id": "death_cross", "name": "死叉(K 下穿 D)"},
                    {"id": "oversold", "name": "超卖(J<阈值)"},
                    {"id": "overbought", "name": "超买(J>阈值)"},
                ],
            },
            {
                "id": "rsi",
                "name": "RSI 超买超卖",
                "params": [
                    {"key": "period", "name": "周期", "type": "int", "default": 14, "min": 5, "max": 60},
                    {"key": "overbought", "name": "超买阈值", "type": "float", "default": 70, "min": 50, "max": 100},
                    {"key": "oversold", "name": "超卖阈值", "type": "float", "default": 30, "min": 0, "max": 50},
                ],
                "operators": [
                    {"id": "oversold", "name": "超卖"},
                    {"id": "overbought", "name": "超买"},
                ],
            },
            {
                "id": "price_breakout",
                "name": "价格突破 N 日新高/新低",
                "params": [
                    {"key": "period", "name": "周期", "type": "int", "default": 20, "min": 5, "max": 250},
                ],
                "operators": [
                    {"id": "breakout_high", "name": "突破 N 日新高"},
                    {"id": "breakout_low", "name": "跌破 N 日新低"},
                ],
            },
            {
                "id": "close_above_ma",
                "name": "收盘在 MA 之上/之下",
                "params": [
                    {"key": "period", "name": "MA 周期", "type": "int", "default": 20, "min": 3, "max": 250},
                ],
                "operators": [
                    {"id": "above", "name": "收盘 > MA"},
                    {"id": "below", "name": "收盘 < MA"},
                ],
            },
            {
                "id": "bias",
                "name": "BIAS 乖离率",
                "params": [
                    {"key": "period", "name": "周期", "type": "int", "default": 6, "min": 3, "max": 60},
                ],
                "operators": [
                    {"id": "gt", "name": "大于阈值(%)"},
                    {"id": "lt", "name": "小于阈值(%)"},
                ],
            },
        ],
    },
    {
        "category": "fundamental",
        "label": "基本面筛选",
        "items": [
            {
                "id": "market_cap",
                "name": "总市值(亿)",
                "params": [
                    {"key": "min", "name": "最小值", "type": "float", "default": 50, "min": 0, "max": 100000},
                    {"key": "max", "name": "最大值", "type": "float", "default": 1000, "min": 0, "max": 100000},
                ],
                "operators": [
                    {"id": "between", "name": "介于"},
                ],
            },
            {
                "id": "pe_ttm",
                "name": "PE(TTM) 滚动市盈率",
                "params": [
                    {"key": "min", "name": "最小值", "type": "float", "default": 0, "min": 0, "max": 1000},
                    {"key": "max", "name": "最大值", "type": "float", "default": 50, "min": 0, "max": 1000},
                ],
                "operators": [
                    {"id": "between", "name": "介于"},
                ],
            },
            {
                "id": "pb",
                "name": "PB 市净率",
                "params": [
                    {"key": "min", "name": "最小值", "type": "float", "default": 0, "min": 0, "max": 100},
                    {"key": "max", "name": "最大值", "type": "float", "default": 10, "min": 0, "max": 100},
                ],
                "operators": [
                    {"id": "between", "name": "介于"},
                ],
            },
        ],
    },
    {
        "category": "volume",
        "label": "量价指标",
        "items": [
            {
                "id": "turnover_rate",
                "name": "换手率(%)",
                "params": [
                    {"key": "min", "name": "最小", "type": "float", "default": 1, "min": 0, "max": 100},
                    {"key": "max", "name": "最大", "type": "float", "default": 20, "min": 0, "max": 100},
                ],
                "operators": [
                    {"id": "between", "name": "介于"},
                ],
            },
            {
                "id": "volume_ratio",
                "name": "量比(当日/5日均量)",
                "params": [
                    {"key": "min", "name": "最小", "type": "float", "default": 1.5, "min": 0, "max": 100},
                ],
                "operators": [
                    {"id": "gt", "name": "大于"},
                ],
            },
            {
                "id": "pct_change",
                "name": "涨跌幅(%)",
                "params": [
                    {"key": "min", "name": "最小", "type": "float", "default": -5, "min": -100, "max": 100},
                    {"key": "max", "name": "最大", "type": "float", "default": 5, "min": -100, "max": 100},
                ],
                "operators": [
                    {"id": "between", "name": "介于"},
                ],
            },
        ],
    },
    {
        "category": "factor",
        "label": "策略因子评分",
        "items": [
            {
                "id": "fusion_score",
                "name": "综合因子分(fusion_score)",
                "params": [
                    {"key": "min", "name": "最小", "type": "float", "default": 60, "min": 0, "max": 100},
                ],
                "operators": [
                    {"id": "gt", "name": "大于"},
                ],
            },
            {
                "id": "whale_score",
                "name": "主力建仓分(whale_score)",
                "params": [
                    {"key": "min", "name": "最小", "type": "float", "default": 60, "min": 0, "max": 100},
                ],
                "operators": [
                    {"id": "gt", "name": "大于"},
                ],
            },
            {
                "id": "bottom_score",
                "name": "抄底分(bottom_score)",
                "params": [
                    {"key": "min", "name": "最小", "type": "float", "default": 60, "min": 0, "max": 100},
                ],
                "operators": [
                    {"id": "gt", "name": "大于"},
                ],
            },
        ],
    },
]


def get_catalog() -> List[Dict[str, Any]]:
    """返回指标元数据副本"""
    return CONDITION_CATALOG


def find_indicator(indicator_id: str):
    """按 id 查找指标元数据"""
    for cat in CONDITION_CATALOG:
        for item in cat["items"]:
            if item["id"] == indicator_id:
                return item
    return None
