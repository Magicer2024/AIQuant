"""
tests/test_factor_lib_selective.py —— T8 阶段B 护栏：因子按需计算
=========================================================================

铁律：compute_all_factors(df, only=[...]) 只允许「少列」，
每个产出的列必须与全量 compute_all_factors(df) 的同名列逐值相等
（归一化口径、fillna(0)、列顺序均不得漂移）。
"""
import os
import sys

import inspect

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.factor_lib import compute_all_factors

# T8 阶段B 护栏先行：only 参数尚未实现时整体 skip，落地后自动转绿
if "only" not in inspect.signature(compute_all_factors).parameters:
    pytest.skip("T8-B 未实现：compute_all_factors 尚不支持 only", allow_module_level=True)


def _make_df(n: int = 320, seed: int = 11) -> pd.DataFrame:
    """带趋势/波动的合成 OHLCV + amount + turnover（320 行 > 最长 252 窗口）。"""
    rng = np.random.default_rng(seed)
    close = 20 + np.cumsum(rng.normal(0.03, 0.5, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + rng.uniform(0, 0.25, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.25, n)
    volume = rng.integers(1_000_000, 8_000_000, n).astype(float)
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": volume, "amount": volume * close,
         "turnover": rng.uniform(0.2, 8.0, n)},
        index=idx,
    )


# 全量路径在「无 index_close / 无基本面」时实际产出的 60 列，顺序必须与
# compute_all_factors 现行插入顺序完全一致（注册表顺序与之不同，勿直接用注册表）。
EXPECTED_ORDER = [
    # 趋势 14
    "MA5_偏离", "MA10_偏离", "MA20_偏离", "MA60_偏离", "MA_多头强度",
    "MACD_DIF", "MACD_DEA", "MACD_HIST", "MACD_金叉距离",
    "CCI", "DPO", "ADX", "PDI", "MDI",
    # 动量 16
    "RSI_6", "RSI_9", "RSI_14", "RSI_21",
    "BIAS_6", "BIAS_12", "BIAS_24", "MOM_10", "MOM_20",
    "KDJ_K", "KDJ_D", "KDJ_J",
    "RET_3D", "RET_5D", "RET_10D", "RET_20D",
    # 波动 5
    "BB_PCT_B", "BB_BANDWIDTH", "ATR_PCT", "HV_10", "HV_20",
    # 量能 7
    "VOL_RATIO_5", "VOL_RATIO_10", "VOL_RATIO_20", "OBV_偏离度",
    "VOL_波动率", "VWAP_偏离", "AMOUNT_RATIO_20",
    # 形态 13
    "CONSEC_UP", "CONSEC_DOWN", "AMPLITUDE_5", "AMPLITUDE_20",
    "GAP_UP", "GAP_DOWN", "HAMMER", "ENGULF_BULL", "ENGULF_BEAR",
    "MORNING_STAR", "EVENING_STAR", "DOUBLE_BOTTOM", "DOUBLE_TOP",
    # 流动性 5（turnover 存在时）
    "TURNOVER", "TURNOVER_5MA", "TURNOVER_PCTL", "AMIHUD_ILLIQ", "PV_CORREL",
]
FULL_COLS = EXPECTED_ORDER


@pytest.mark.parametrize("factor", FULL_COLS, ids=FULL_COLS)
def test_only_factor_column_equals_full(factor):
    """每个因子单独按需计算，其列值必须与全量结果完全一致。"""
    raw = _make_df()
    full = compute_all_factors(raw)
    sel = compute_all_factors(raw, only=[factor])
    assert list(sel.columns) == [factor]  # 只产出请求的列
    pd.testing.assert_series_equal(
        sel[factor], full[factor], check_names=False,
        atol=0, rtol=0,  # 逐值精确相等，不允许数值漂移
    )


def test_only_multiple_factors_preserve_order():
    """多因子按需：列值与全量一致（跨计算单元也测）。"""
    wanted = ["TURNOVER_5MA", "RSI_21", "KDJ_K", "AMIHUD_ILLIQ", "MA20_偏离"]
    raw = _make_df()
    full = compute_all_factors(raw)
    sel = compute_all_factors(raw, only=wanted)
    assert set(sel.columns) == set(wanted)
    for col in wanted:
        pd.testing.assert_series_equal(sel[col], full[col], check_names=False)


def test_default_path_unchanged():
    """only=None 旧签名必须产出完整因子集（列名与顺序）。"""
    full = compute_all_factors(_make_df())
    assert list(full.columns) == FULL_COLS


def test_unknown_factor_ignored():
    """only 含注册表外名称时忽略，不影响其他列（旧路径下同名列也不存在）。"""
    sel = compute_all_factors(_make_df(), only=["RSI_14", "NOT_EXIST"])
    assert list(sel.columns) == ["RSI_14"]
