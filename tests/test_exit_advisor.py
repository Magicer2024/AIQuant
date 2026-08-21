"""tests/test_exit_advisor.py —— 移动止盈出场引擎测试

覆盖 evaluate_exit_by_prices 的移动止盈模式（2026-08 落地）：
1. 移动止盈状态机：盘中 high 达启动线 → 启用；回撤未达不清（到期卖出）
2. 回撤达阈值 → 移动止盈清仓（优先于到期）
3. 移动止盈线只上移不下移
4. T+1 买入日当天不检查出场（即使大跌也不触发止损）
5. trailing_pct=None 保持旧固定止盈行为（达价即卖）
6. 非法 trailing_pct 防御 → 退化固定止盈
7. 止损优先于移动止盈
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from strategy.exit_advisor import evaluate_exit_by_prices


def _df(rows):
    """rows: [(open, high, low, close), ...] → DataFrame（6 个交易日，B 频率）"""
    return pd.DataFrame(
        [dict(zip(("open", "high", "low", "close"), r)) for r in rows],
        index=pd.date_range("2026-01-05", periods=len(rows), freq="B"),
    )


# 通用场景：买入日 open=100，随后一路走高
RISING = _df([
    (100, 102, 99, 101),   # 买入日
    (102, 106, 101, 105),
    (105, 110, 104, 109),  # 盘中 high=110 = 启动线（+10%）
    (108, 112, 107, 111),
    (110, 113, 109, 112),
])


def test_trailing_not_triggered_expire():
    """浮盈过启动线但未回撤 8% → 到期卖出（不达价即卖）"""
    adv = evaluate_exit_by_prices(100, "2026-01-02", RISING, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=5,
                                  trailing_pct=0.08)
    assert adv["status"] == "clear"
    assert "到期" in adv["reason"]
    assert adv["detail"]["partial_done"] is True          # 已启动移动止盈
    assert adv["detail"]["trailing_stop_price"] is not None
    assert abs(adv["detail"]["current_pnl_pct"] - 12.0) < 0.01  # 到期@112


def test_trailing_triggered_clear():
    """高点 113 后收盘 101（回撤 10.6% ≥ 8%）→ 移动止盈清仓，保住 +1%"""
    df = _df([
        (100, 102, 99, 101),
        (102, 106, 101, 105),
        (105, 110, 104, 109),
        (108, 112, 107, 111),
        (110, 113, 100, 101),   # 当日回撤触发
    ])
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=5,
                                  trailing_pct=0.08)
    assert adv["status"] == "clear"
    assert "移动止盈" in adv["reason"]
    assert abs(adv["detail"]["current_pnl_pct"] - 1.0) < 0.01  # 清仓@101


def test_trailing_line_only_rises():
    """移动止盈线随最高价上移：高点 115 → 线 105.8，随后收盘 106 不触发"""
    df = _df([
        (100, 102, 99, 101),
        (102, 106, 101, 105),
        (105, 110, 104, 109),
        (108, 112, 107, 111),
        (110, 115, 109, 114),   # 新高 → 线=115*0.92=105.8
    ])
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=5,
                                  trailing_pct=0.08)
    assert adv["status"] == "clear" and "到期" in adv["reason"]
    assert abs(adv["detail"]["trailing_stop_price"] - 115 * 0.92) < 0.01


def test_t1_buy_day_never_checked():
    """买入日当天大跌也不触发止损（A股 T+1）"""
    df = _df([
        (100, 100, 85, 90),    # 买入日，盘中-15%，不可卖
        (90, 91, 89, 90),
        (90, 92, 89, 91),
    ])
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=3,
                                  trailing_pct=0.08)
    # 第 2 日 close=90 <= 94 → 止损触发（不是买入日当天）
    assert adv["status"] == "clear" and "止损" in adv["reason"]
    assert adv["detail"]["hold_days"] == 2


def test_fixed_take_profit_legacy():
    """trailing_pct=None：旧行为不变，收盘达止盈价即卖"""
    adv = evaluate_exit_by_prices(100, "2026-01-02", RISING, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=5)
    assert adv["status"] == "clear" and "止盈" in adv["reason"]
    assert adv["detail"]["trailing_pct"] is None
    assert adv["detail"]["partial_done"] is None


def test_invalid_trailing_fallback():
    """非法 trailing_pct（≥1 或 <0.01）→ 退化固定止盈"""
    for bad in (1.5, 0.0, -0.1):
        adv = evaluate_exit_by_prices(100, "2026-01-02", RISING, stop_loss=94.0,
                                      take_profit=110.0, max_hold_days=5,
                                      trailing_pct=bad)
        assert adv["status"] == "clear" and "止盈" in adv["reason"], bad


def test_stop_loss_priority():
    """收盘破止损优先于移动止盈"""
    df = _df([
        (100, 102, 99, 101),
        (102, 106, 101, 105),
        (105, 110, 104, 109),
        (108, 112, 107, 111),
        (110, 113, 92, 93),    # 当日先破止损
    ])
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=5,
                                  trailing_pct=0.08)
    assert adv["status"] == "clear" and "止损" in adv["reason"]


def test_mid_long_no_trailing():
    """中线/长线不传 trailing → 保持固定止盈（不受短线移动止盈影响）"""
    adv = evaluate_exit_by_prices(100, "2026-01-02", RISING, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=60)
    assert adv["status"] == "clear" and "止盈" in adv["reason"]


def test_partial_tp_overrides_take_profit_as_launch():
    """partial_tp 覆盖 take_profit 作启动线（long 的 take_profit=+50% 不宜当启动线）：
    take_profit=150（+50%），partial_tp=110（+10%）→ 浮盈 +12% 即启用移动止盈，
    回撤 8% 触发清仓，而不是等到 +50%。"""
    df = _df([
        (100, 102, 99, 101),
        (102, 106, 101, 105),
        (105, 110, 104, 109),   # 盘中 high=110 达启动线（partial_tp=110）
        (108, 112, 107, 111),
        (110, 113, 100, 101),   # 高点 113 → 线=103.96，收盘 101 破线 → 移动止盈
    ])
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=150.0, max_hold_days=60,
                                  trailing_pct=0.08, partial_tp=110.0)
    assert adv["status"] == "clear" and "移动止盈" in adv["reason"], adv["reason"]
    assert adv["detail"]["partial_tp"] == 110.0
    # 对照：不传 partial_tp → take_profit=150 作启动线，永远不启动 → 到期
    adv2 = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                   take_profit=150.0, max_hold_days=5,
                                   trailing_pct=0.08)
    assert adv2["status"] == "clear" and "到期" in adv2["reason"], adv2["reason"]


def test_invalid_partial_tp_fallback():
    """非法 partial_tp（<=0）→ 回退用 take_profit 作启动线"""
    df = _df([
        (100, 102, 99, 101),
        (102, 106, 101, 105),
        (105, 110, 104, 109),
        (108, 112, 107, 111),
        (110, 113, 100, 101),
    ])
    # partial_tp=0 非法 → 回退 take_profit=110 作启动线，移动止盈仍触发
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=110.0, max_hold_days=5,
                                  trailing_pct=0.08, partial_tp=0.0)
    assert adv["status"] == "clear" and "移动止盈" in adv["reason"], adv["reason"]
    assert adv["detail"]["partial_tp"] is None


def test_partial_tp_ratio_semantics():
    """比例语义防复发：partial_tp=0.10（比例）→ 启动线=100×1.10=110，
    浮盈 +12% 才启用；绝不能因 0.1 元被当绝对价而在买入日即激活移动止盈。"""
    df = _df([
        (100, 100, 99, 99),      # 买入日，浮盈为负
        (100, 101, 99, 100),     # 高点 101 < 110，未达启动线
        (101, 103, 100, 102),    # 高点 103 < 110，未达启动线
        (102, 105, 101, 104),    # 高点 105 < 110，未达启动线
        (104, 107, 103, 106),    # 高点 107 < 110，未达启动线 → 到期
    ])
    adv = evaluate_exit_by_prices(100, "2026-01-02", df, stop_loss=94.0,
                                  take_profit=120.0, max_hold_days=5,
                                  trailing_pct=0.08, partial_tp=0.10)
    # 全程未达 +10% 启动线 → 不应触发移动止盈，且收盘未达固定止盈价 → 到期卖出
    assert adv["status"] == "clear" and "到期" in adv["reason"], adv["reason"]
    assert adv["detail"]["partial_done"] is False, "未达启动线，不应启用移动止盈"
    # detail 中 partial_tp 展示换算后的绝对价
    assert abs(adv["detail"]["partial_tp"] - 110.0) < 0.01


# ── evaluate_exit（快照版，持仓诊断用）：max_hold_days=None 与 get_max_hold ──
from strategy.exit_advisor import evaluate_exit, get_max_hold, HORIZON_MAX_HOLD


def test_evaluate_exit_none_max_hold_no_expire():
    """evaluate_exit max_hold_days=None：长线持仓不触发超期清仓"""
    df = pd.DataFrame({
        "close": [100.0] * 40, "high": [101.0] * 40, "low": [99.0] * 40,
    }, index=pd.date_range("2026-01-01", periods=40, freq="B"))
    # 40 个交易日，价格横盘不触止损/止盈 → None 上限应一直 hold
    adv = evaluate_exit(100.0, "2025-12-31", df, max_hold_days=None)
    assert adv["status"] == "hold", adv["reason"]


def test_get_max_hold_by_horizon():
    """get_max_hold：short 读 TUNABLE_PARAMS（10，2026-08-20 方案A），mid=60，long=None"""
    assert get_max_hold("mid") == 60
    assert get_max_hold("long") is None
    assert HORIZON_MAX_HOLD["short"] == 10  # 与 TUNABLE_PARAMS 同值，避免漂移
    short_v = get_max_hold("short")
    assert isinstance(short_v, int) and short_v >= 1
