"""tests/test_indicators.py —— strategy/indicators.py 指标库回归测试（T1）

覆盖 2026-10 优化计划 T1 的两类缺陷：
1. 模块内重复函数定义（calc_volume_ratio / calc_dpo / calc_bollinger /
   calc_historical_volatility 各定义两次，后者静默覆盖前者）
2. calc_kdj 暖机期 NaN 污染递归（rsv 未满窗为 NaN → K/D 从第 2 行起永久 NaN，
   实测 60 行里 59 行 NaN），stock_deep 侧曾以 _calc_kdj_safe 私有副本规避，
   本测试要求合并为单一 NaN 安全实现
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from strategy import indicators
from strategy.indicators import (
    calc_all_indicators,
    calc_bollinger,
    calc_dpo,
    calc_historical_volatility,
    calc_kdj,
    calc_volume_ratio,
)

IND_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "strategy", "indicators.py",
)


# ─────────────────────────────────────────────
# 1. 重复定义守卫
# ─────────────────────────────────────────────
def test_no_duplicate_top_level_function_definitions():
    """模块内顶层函数名必须唯一——重复定义会让后定义静默覆盖前定义"""
    tree = ast.parse(open(IND_PATH, encoding="utf-8").read())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    dupes = {x for x in names if names.count(x) > 1}
    assert not dupes, f"indicators.py 存在重复定义: {sorted(dupes)}"


# ─────────────────────────────────────────────
# 2. KDJ NaN 安全
# ─────────────────────────────────────────────
def _ohlc(n=60, seed=0):
    """构造确定性的高低收序列"""
    rng = np.random.RandomState(seed)
    close = pd.Series(10 + np.cumsum(rng.randn(n) * 0.3))
    return close + 0.5, close - 0.5, close


def test_calc_kdj_no_nan_pollution():
    """暖机期（rolling 未满窗）不得把 NaN 传染给后续递归，全序列应有限值"""
    high, low, close = _ohlc()
    kdj = calc_kdj(high, low, close)
    for col in ("KDJ_K", "KDJ_D", "KDJ_J"):
        assert kdj[col].notna().all(), f"{col} 被暖机期 NaN 污染: NaN={kdj[col].isna().sum()}"


def test_calc_kdj_snapshot():
    """固定样例序列 → KDJ 数值快照（NaN 安全语义：暖机期 rsv 视为中性 50）"""
    high = pd.Series([10.0, 11, 12, 11, 13, 14])
    low = pd.Series([9.0, 10, 10, 9, 11, 12])
    close = pd.Series([10.0, 11, 11, 10, 13, 14])
    kdj = calc_kdj(high, low, close, n=3, m1=3, m2=3)

    exp_k = [50.0, 50.0, 55.5556, 48.1481, 65.4321, 76.9547]
    exp_d = [50.0, 50.0, 51.8519, 50.6173, 55.5556, 62.6886]
    exp_j = [50.0, 50.0, 62.9630, 43.2099, 85.1852, 105.4870]
    np.testing.assert_allclose(kdj["KDJ_K"].values, exp_k, atol=1e-3)
    np.testing.assert_allclose(kdj["KDJ_D"].values, exp_d, atol=1e-3)
    np.testing.assert_allclose(kdj["KDJ_J"].values, exp_j, atol=1e-3)


def test_calc_all_indicators_kdj_columns_finite():
    """calc_all_indicators 输出的 KDJ 列是下游 factor_lib / 回测共用口径，必须有效"""
    high, low, close = _ohlc()
    df = pd.DataFrame({
        "open": close, "high": high, "low": low, "close": close,
        "volume": pd.Series(np.full(len(close), 1000.0)),
    })
    out = calc_all_indicators(df)
    for col in ("KDJ_K", "KDJ_D", "KDJ_J"):
        assert out[col].notna().all(), f"calc_all_indicators 的 {col} 存在 NaN"


def test_stock_deep_uses_single_kdj_implementation():
    """KDJ 只能有一个实现：stock_deep 不得再保留私有副本 _calc_kdj_safe"""
    from strategy import stock_deep
    assert not hasattr(stock_deep, "_calc_kdj_safe"), \
        "stock_deep._calc_kdj_safe 应删除，统一引用 indicators.calc_kdj"


# ─────────────────────────────────────────────
# 3. 去重后的行为特征测试（防删除副本时改坏语义）
# ─────────────────────────────────────────────
def test_calc_volume_ratio_semantics():
    """量比 = 当日量 / 前 N 日均量（不含当日）"""
    volume = pd.Series([100.0, 100, 100, 100, 100, 200])
    vr = calc_volume_ratio(volume, period=5)
    assert vr.name == "VOLUME_RATIO"
    assert np.isnan(vr.iloc[4])          # 前 5 日均量窗口未满（shift 后）
    assert vr.iloc[5] == pytest.approx(200 / 100)


def test_calc_dpo_semantics():
    """DPO = close - MA(n) 前移 n/2+1 期"""
    close = pd.Series(np.arange(1, 21, dtype=float))
    dpo = calc_dpo(close, n=4)
    assert list(dpo.columns) == ["DPO"]
    # 第 12 行: close=12, MA4 经 shift(int(4/2)+1=3) 取 iloc[8] → mean(6,7,8,9)=7.5
    assert dpo["DPO"].iloc[11] == pytest.approx(12 - 7.5)


def test_calc_bollinger_semantics():
    """布林带含 %B 与带宽列"""
    close = pd.Series(np.arange(1, 31, dtype=float))
    bb = calc_bollinger(close, n=5, k=2.0)
    assert set(bb.columns) == {"BB_UPPER", "BB_LOWER", "BB_MA", "BB_PCT_B", "BB_BANDWIDTH"}
    assert bb["BB_MA"].iloc[4] == pytest.approx(3.0)          # mean(1..5)
    assert bb["BB_UPPER"].iloc[4] == pytest.approx(3 + 2 * close[:5].std())
    assert 0 < bb["BB_PCT_B"].iloc[29] < 1                     # 线性爬坡收盘落在带内
    assert (bb["BB_BANDWIDTH"].dropna() > 0).all()


def test_calc_historical_volatility_semantics():
    """历史波动率 = 对数收益滚动标准差 × sqrt(252)"""
    close = pd.Series([100.0, 101, 102, 101, 103, 104])
    hv = calc_historical_volatility(close, n=3)
    assert list(hv.columns) == ["HV_3"]
    log_ret = np.log(close / close.shift(1))
    expected = log_ret.rolling(3).std() * np.sqrt(252)
    np.testing.assert_allclose(hv["HV_3"].values, expected.values)
