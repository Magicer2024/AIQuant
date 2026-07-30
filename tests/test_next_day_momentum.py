"""tests/test_next_day_momentum.py —— 隔日动量（龙虎榜净买占比）信号测试

覆盖 strategy.next_day_momentum.scan_next_day_momentum：
1. 命中：净买占比≥阈值 且 非涨停 且 龙虎榜日期==最新交易日
2. 净买占比不足 → 不命中
3. 当日涨停（普通/20cm）→ 剔除不命中
4. 龙虎榜日期与最新交易日不一致 → 不命中
5. 无龙虎榜记录 / 数据不足 → None
6. fusion_score 映射（10→30、20→40、≥30→50）与盈亏比≥1.5
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from strategy.next_day_momentum import scan_next_day_momentum
from config.strategy_params import NEXT_DAY_MOMENTUM


def _make_df(closes, end="2026-07-27"):
    """构造带工作日 DatetimeIndex 的日线 df（仅需 close 列）"""
    idx = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({"close": [float(c) for c in closes]}, index=idx)


def _last_date(df):
    idx = df.index[-1]
    return str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]


def _lhb(df, code="600000", ratio=15.0, pct=3.0, name="测试股", reason="机构专用"):
    return {
        "trade_date": _last_date(df),
        "code": code,
        "name": name,
        "pct_change": pct,
        "net_buy": 1e7,
        "net_buy_ratio": ratio,
        "reason": reason,
    }


# ── 1. 命中 ──────────────────────────────────────────
def test_hit_basic():
    df = _make_df([10 + 0.01 * i for i in range(30)])
    sig = scan_next_day_momentum(df, _lhb(df, ratio=15.0, pct=3.0),
                                 name="测试股", total_shares=1e9,
                                 params=NEXT_DAY_MOMENTUM)
    assert sig is not None
    assert sig["horizon"] == "short"
    assert sig["strategy"] == "隔日动量"
    close = sig["buy_price"]
    assert sig["stop_loss"] < close < sig["take_profit"]
    # 盈亏比 ≈ 6.5/4 = 1.625 ≥ 1.5（规避信号灯 avoid）
    risk = close - sig["stop_loss"]
    reward = sig["take_profit"] - close
    assert reward / risk >= 1.5
    assert any("净买占比" in t for t in sig["triggers"])


# ── 2. 净买占比不足 ──────────────────────────────────
def test_below_ratio_no_hit():
    df = _make_df([10 + 0.01 * i for i in range(30)])
    assert scan_next_day_momentum(df, _lhb(df, ratio=8.0),
                                  params=NEXT_DAY_MOMENTUM) is None


# ── 3. 涨停剔除 ──────────────────────────────────────
def test_limit_up_excluded():
    df = _make_df([10 + 0.01 * i for i in range(30)])
    # 普通票涨幅 10% ≥ 9.8 → 剔除
    assert scan_next_day_momentum(df, _lhb(df, code="600000", ratio=25.0, pct=10.0),
                                  params=NEXT_DAY_MOMENTUM) is None
    # 20cm（创业板 300）涨幅 15% < 19.8 → 命中
    sig = scan_next_day_momentum(df, _lhb(df, code="300750", ratio=25.0, pct=15.0),
                                 params=NEXT_DAY_MOMENTUM)
    assert sig is not None
    # 20cm 涨幅 20% ≥ 19.8 → 剔除
    assert scan_next_day_momentum(df, _lhb(df, code="300750", ratio=25.0, pct=20.0),
                                  params=NEXT_DAY_MOMENTUM) is None


# ── 4. 日期不一致 ────────────────────────────────────
def test_date_mismatch_no_hit():
    df = _make_df([10 + 0.01 * i for i in range(30)])
    row = _lhb(df, ratio=25.0)
    row["trade_date"] = "2020-01-01"  # 与最新交易日不一致
    assert scan_next_day_momentum(df, row, params=NEXT_DAY_MOMENTUM) is None


# ── 5. 边界：无记录 / 数据不足 ───────────────────────
def test_none_and_insufficient():
    df = _make_df([10 + 0.01 * i for i in range(30)])
    assert scan_next_day_momentum(df, None, params=NEXT_DAY_MOMENTUM) is None
    assert scan_next_day_momentum(_make_df([10]), _lhb(_make_df([10]), ratio=25.0),
                                  params=NEXT_DAY_MOMENTUM) is None


# ── 6. fusion_score 映射 ─────────────────────────────
@pytest.mark.parametrize("ratio,expected", [(10.0, 30.0), (20.0, 40.0), (35.0, 50.0)])
def test_fusion_mapping(ratio, expected):
    df = _make_df([10 + 0.01 * i for i in range(30)])
    sig = scan_next_day_momentum(df, _lhb(df, ratio=ratio, pct=3.0),
                                 params=NEXT_DAY_MOMENTUM)
    assert sig is not None
    assert sig["fusion_score"] == pytest.approx(expected)
