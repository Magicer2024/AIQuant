"""tests/test_surge_breakout.py —— 强势突破信号测试

覆盖 strategy.surge_breakout.scan_surge_breakout：
1~8 为 K 线/市值/门控条件（params 关闭龙虎榜硬过滤，聚焦单项条件）：
1. 命中：大阳(5~9.8%) + 创20日新高 + 量比≥1.5 + 收盘上段 + 市值达标
2. 当日涨停（≥9.8%）→ 剔除（次日高开买不到）
3. 涨幅不足 5% → 不命中
4. 未突破前 20 日高点 → 不命中
5. 量比不足 → 不命中
6. 市值超上限 → 不命中；缺股本时跳过市值项
7. 大盘门控 market_ok=False → 不命中
8. fusion_score 映射（基础 30，量比/涨幅各 10 分，封顶 50）与盈亏比 2.0
9~12 为龙虎榜硬过滤（默认开启，宁缺毋滥）：
9. 同日上榜且净买>0 → 命中并带龙虎榜触发理由
10. 无龙虎榜记录 → 不命中（含默认参数行为验证）
11. 龙虎榜日期与信号日不符 → 不命中
12. 上榜但净卖（net_buy<=0）→ 不命中
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from strategy.surge_breakout import scan_surge_breakout
from config.strategy_params import SURGE_BREAKOUT

# 纯 K 线条件测试用配置：关闭龙虎榜硬过滤，聚焦单项 K 线条件
KL = {**SURGE_BREAKOUT, "lhb_required": False}


def _make_df(last_pct=7.0, last_close=10.7, last_high=10.8, last_low=10.05,
             last_vol=3e6, n=30, end="2026-08-18"):
    """构造日线 df：前 n-1 天平台（high 上限 10.2），最后一天大阳突破"""
    idx = pd.bdate_range(end=end, periods=n)
    rows = []
    for i in range(n - 1):
        rows.append({"open": 9.95, "high": 10.2, "low": 9.8, "close": 10.0,
                     "volume": 1e6, "pct_change": 0.0})
    rows.append({"open": 10.1, "high": last_high, "low": last_low,
                 "close": last_close, "volume": last_vol, "pct_change": last_pct})
    return pd.DataFrame(rows, index=idx)


def _lhb_row(df, net_buy=5e6, ratio=8.0, offset_days=0):
    idx = df.index[-1] + pd.Timedelta(days=offset_days)
    return {"trade_date": str(idx.date()), "code": "600000",
            "net_buy": net_buy, "net_buy_ratio": ratio}


# ── 1. 命中 ──────────────────────────────────────────
def test_hit_basic():
    df = _make_df()
    sig = scan_surge_breakout(df, name="测试股", total_shares=1e8,
                              params=KL, market_ok=True)
    assert sig is not None
    assert sig["horizon"] == "short"
    assert sig["strategy"] == "强势突破"
    close = sig["buy_price"]
    assert sig["stop_loss"] < close < sig["take_profit"]
    # 盈亏比 = 8%/4% = 2.0
    risk = close - sig["stop_loss"]
    reward = sig["take_profit"] - close
    assert abs(reward / risk - 2.0) < 0.05
    assert any("新高" in t for t in sig["triggers"])
    assert sig["vol_ratio"] >= 1.5


# ── 2. 涨停剔除 ──────────────────────────────────────
def test_limit_up_excluded():
    df = _make_df(last_pct=9.9, last_close=10.99, last_high=10.99)
    assert scan_surge_breakout(df, total_shares=1e8, params=KL) is None


# ── 3. 涨幅不足 ──────────────────────────────────────
def test_small_gain_no_hit():
    df = _make_df(last_pct=4.9, last_close=10.49)
    assert scan_surge_breakout(df, total_shares=1e8, params=KL) is None


# ── 4. 未突破前高 ────────────────────────────────────
def test_no_breakout_no_hit():
    # 收盘 10.1 <= 前 20 日最高 10.2
    df = _make_df(last_pct=5.5, last_close=10.1, last_high=10.15)
    assert scan_surge_breakout(df, total_shares=1e8, params=KL) is None


# ── 5. 量比不足 ──────────────────────────────────────
def test_low_vol_ratio_no_hit():
    df = _make_df(last_vol=1.2e6)  # 量比 1.2 < 1.5
    assert scan_surge_breakout(df, total_shares=1e8, params=KL) is None


# ── 6. 市值上限 ──────────────────────────────────────
def test_mktcap_cap():
    df = _make_df()
    # 市值 2e9 股 × 10.7 = 214 亿 > 150 亿 → 剔除
    assert scan_surge_breakout(df, total_shares=2e9, params=KL) is None
    # 缺股本 → 跳过市值项，命中
    sig = scan_surge_breakout(df, total_shares=None, params=KL)
    assert sig is not None


# ── 7. 大盘门控 ──────────────────────────────────────
def test_market_gate():
    df = _make_df()
    assert scan_surge_breakout(df, total_shares=1e8, params=KL,
                               market_ok=False) is None


# ── 8. fusion 映射 ──────────────────────────────────
def test_fusion_mapping():
    # 基础命中（量比 3.0、涨幅 7%）：30 + (3-1.5)/3*10 + (7-5)/4.8*10 ≈ 49.17
    df = _make_df()
    sig = scan_surge_breakout(df, total_shares=1e8, params=KL)
    assert 30 <= sig["fusion_score"] <= 50
    # 极端强势（量比≥4.5、涨幅贴上限）→ 接近封顶（涨幅永 <9.8，取不到整 50）
    df2 = _make_df(last_pct=9.7, last_close=10.97, last_high=10.98, last_vol=5e6)
    sig2 = scan_surge_breakout(df2, total_shares=1e8, params=KL)
    assert sig2 is not None
    assert sig2["fusion_score"] >= 49.0


# ── 9. 龙虎榜硬过滤：同日上榜净买>0 → 命中 ───────────
def test_lhb_filter_hit():
    df = _make_df()
    sig = scan_surge_breakout(df, total_shares=1e8, params=SURGE_BREAKOUT,
                              lhb_row=_lhb_row(df))
    assert sig is not None
    assert sig["lhb_net_buy_ratio"] == 8.0
    assert any("龙虎榜" in t for t in sig["triggers"])


# ── 10. 龙虎榜硬过滤：无记录 → 不命中（默认行为）──────
def test_lhb_filter_missing():
    df = _make_df()
    # 显式 SURGE_BREAKOUT（lhb_required=True）且无 lhb_row
    assert scan_surge_breakout(df, total_shares=1e8,
                               params=SURGE_BREAKOUT) is None
    # 默认参数（不传 params）也默认开启硬过滤
    assert scan_surge_breakout(df, total_shares=1e8) is None


# ── 11. 龙虎榜硬过滤：日期不符 → 不命中 ──────────────
def test_lhb_filter_date_mismatch():
    df = _make_df()
    assert scan_surge_breakout(df, total_shares=1e8, params=SURGE_BREAKOUT,
                               lhb_row=_lhb_row(df, offset_days=-1)) is None


# ── 12. 龙虎榜硬过滤：上榜但净卖 → 不命中 ─────────────
def test_lhb_filter_net_sell():
    df = _make_df()
    assert scan_surge_breakout(
        df, total_shares=1e8, params=SURGE_BREAKOUT,
        lhb_row=_lhb_row(df, net_buy=-3e6, ratio=-5.0)) is None
