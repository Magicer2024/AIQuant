"""
tests/test_backtest_indicators.py —— T8 护栏：指标按需计算与全量预计算结果等价
=========================================================================

铁律：按需计算只允许「少算」，不允许「算得不一样」。
对 _eval_condition 支持的每一类条件，断言：
  选择性预计算（_add_indicators(df, need=from_conditions(conds))）
  == 全量预计算（旧行为 _add_indicators(df)）
在合成行情上逐 bool 掩码一致。
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import engine
from backtest.engine import _add_indicators, _eval_condition

# T8 护栏先行（TDD）：IndicatorNeed 尚未实现时整体 skip，T8 落地后自动转绿
try:
    from backtest.engine import IndicatorNeed
except ImportError:  # pragma: no cover - 仅在 T8 实现前的窗口期生效
    pytest.skip("T8 未实现：IndicatorNeed 尚不存在", allow_module_level=True)


def _make_df(n: int = 180, seed: int = 7) -> pd.DataFrame:
    """带趋势 + 波动的合成 OHLCV（避免常量序列导致指标退化）。"""
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0.05, 0.3, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + rng.uniform(0, 0.2, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.2, n)
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2025-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


# 覆盖 _eval_condition 全部分支（自定义周期也测，验证按需推导不写死默认周期）
ALL_CONDS = [
    {"indicator": "ma_cross", "operator": "golden_cross",
     "params": {"short_period": 5, "long_period": 20}},
    {"indicator": "ma_cross", "operator": "death_cross",
     "params": {"short_period": 7, "long_period": 35}},
    {"indicator": "ma_cross", "operator": "bull_above", "params": {}},
    {"indicator": "macd_cross", "operator": "golden_cross"},
    {"indicator": "macd_cross", "operator": "death_cross"},
    {"indicator": "macd_cross", "operator": "hist_positive"},
    {"indicator": "macd_cross", "operator": "hist_negative"},
    {"indicator": "kdj_cross", "operator": "golden_cross"},
    {"indicator": "kdj_cross", "operator": "death_cross"},
    {"indicator": "kdj_cross", "operator": "oversold", "params": {"oversold": 20}},
    {"indicator": "kdj_cross", "operator": "overbought", "params": {"overbought": 80}},
    {"indicator": "rsi", "operator": "oversold", "params": {"period": 14}},
    {"indicator": "rsi", "operator": "overbought", "params": {"period": 7, "overbought": 70}},
    {"indicator": "price_breakout", "operator": "breakout_high", "params": {"period": 20}},
    {"indicator": "price_breakout", "operator": "breakout_low", "params": {"period": 10}},
    {"indicator": "close_above_ma", "operator": "above", "params": {"period": 20}},
    {"indicator": "close_above_ma", "operator": "below", "params": {"period": 60}},
    {"indicator": "bias", "operator": "gt", "params": {"period": 6, "value": 3}},
    {"indicator": "bias", "operator": "lt", "params": {"period": 12, "value": -3}},
    {"indicator": "volume_ratio", "operator": "gt", "params": {"min": 1.5}},
    {"indicator": "pct_change", "params": {"min": -5, "max": 5}},
    {"indicator": "fusion_score", "operator": "gt", "params": {"min": 60}},
    {"indicator": "whale_score", "operator": "lt", "params": {"value": 30}},
]


@pytest.mark.parametrize("cond", ALL_CONDS, ids=lambda c: f"{c['indicator']}.{c.get('operator')}")
def test_selective_matches_full(cond):
    """每类条件：按需预计算的命中掩码必须与全量预计算逐行一致。"""
    df_full = _add_indicators(_make_df())
    df_sel = _add_indicators(_make_df(), IndicatorNeed.from_conditions([cond]))

    mask_full = _eval_condition(cond, df_full)
    mask_sel = _eval_condition(cond, df_sel)

    pd.testing.assert_series_equal(
        mask_sel.astype(bool), mask_full.astype(bool), check_names=False)


def test_unknown_indicator_still_raises():
    df = _add_indicators(_make_df())
    with pytest.raises(engine.ConditionError):
        _eval_condition({"indicator": "not_exist"}, df)


def test_need_derivation_groups():
    """条件 → 所需指标的推导：分组与周期精确（不多算也不少算）。"""
    need = IndicatorNeed.from_conditions([
        {"indicator": "ma_cross", "params": {"short_period": 5, "long_period": 20}},
        {"indicator": "close_above_ma", "params": {"period": 30}},
        {"indicator": "rsi", "params": {"period": 7}},
        {"indicator": "macd_cross"},
    ])
    assert need.ma_periods == frozenset({5, 20, 30})
    assert need.rsi_periods == frozenset({7})
    assert need.macd is True
    assert need.kdj is False


def test_need_defaults_applied():
    """缺省参数也要推导出默认周期（ma_cross=5/20、bias=6、rsi=14）。"""
    need = IndicatorNeed.from_conditions([
        {"indicator": "ma_cross"},
        {"indicator": "bias", "operator": "gt"},
        {"indicator": "rsi", "operator": "oversold"},
        {"indicator": "kdj_cross"},
    ])
    assert need.ma_periods == frozenset({5, 20, 6})
    assert need.rsi_periods == frozenset({14})
    assert need.kdj is True
    assert need.macd is False


def test_light_conditions_compute_no_heavy_columns():
    """纯内联条件（涨跌幅/量比/因子分）不应产生 MA/MACD/RSI/KDJ 列。"""
    df = _make_df()
    need = IndicatorNeed.from_conditions([
        {"indicator": "pct_change"},
        {"indicator": "volume_ratio", "operator": "gt"},
    ])
    out = _add_indicators(df, need)
    for col in ("MA5", "MA20", "RSI14", "KDJ_K", "MACD_DIF"):
        assert col not in out.columns
    assert "prev_close" in out.columns  # 成交判定始终需要


def test_add_indicators_default_stays_full():
    """单参旧签名 = 全量预计算（tools/ 下旧调用方的向后兼容保证）。"""
    out = _add_indicators(_make_df())
    for col in ("MA5", "MA10", "MA20", "MA30", "MA60",
                "RSI6", "RSI14", "RSI24",
                "KDJ_K", "KDJ_D", "KDJ_J",
                "MACD_DIF", "MACD_DEA", "MACD_HIST", "prev_close"):
        assert col in out.columns


def test_custom_period_values_identical():
    """自定义周期（不在默认全量集合里）按需列的值必须等于直接计算值。"""
    raw = _make_df()
    out = _add_indicators(raw, IndicatorNeed.from_conditions(
        [{"indicator": "close_above_ma", "params": {"period": 13}}]))
    from strategy.indicators import calc_ma
    expected = calc_ma(raw["close"].astype(float), [13])["MA13"]
    pd.testing.assert_series_equal(out["MA13"], expected, check_names=False)
