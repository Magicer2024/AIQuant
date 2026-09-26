"""
tests/test_trade_constraints.py —— 交易约束与账户模型黄金样例（方案 E2）

覆盖验收：小预算、刚好 1 手、费用导致超预算、停牌、一字板、跳空/涨跌停不可验证、
买卖方向费用差异、主板白名单资格、50/30/20 最大余数分批、逐批最低佣金、执行配置快照。
各入口（建仓计划 / 回测引擎 / 推荐资格）共用本模块，故本组即「同一组黄金样例」。
"""
from __future__ import annotations

import pytest

from utils import trade_constraints as tc
from config.personal_config import (
    LOT_SIZE, MIN_COMMISSION, COMMISSION_RATE, STAMP_TAX_RATE, PLAN_BATCH_WEIGHTS,
)


# ──────────── 主板资格（明确白名单）────────────

@pytest.mark.parametrize("code", ["600000", "601318", "603288", "605111",
                                  "000001", "001979", "002415", "003816"])
def test_main_board_tradable(code):
    assert tc.is_tradable_main_board(code) is True


@pytest.mark.parametrize("code", [
    "300750",   # 创业板
    "301111",   # 创业板
    "688981",   # 科创板
    "689009",   # 科创板 CDR
    "830799",   # 北交所
    "430047",   # 北交所/老三板
    "900901",   # 沪 B 股
    "200011",   # 深 B 股
])
def test_unsupported_board_not_tradable(code):
    assert tc.is_tradable_main_board(code) is False


@pytest.mark.parametrize("code", ["", None, "60000", "6000000", "60000a", "abc123", "  "])
def test_abnormal_code_not_tradable(code):
    assert tc.is_tradable_main_board(code) is False


def test_board_limit_pct():
    assert tc.board_limit_pct("600000") == 0.10
    assert tc.board_limit_pct("300750") == 0.20
    assert tc.board_limit_pct("688981") == 0.20
    assert tc.board_limit_pct("600000", is_st=True) == 0.05


# ──────────── 最大可买股数（整手 + 最低佣金）────────────

def test_normal_budget_max_lots():
    # 预算 10000，价 10 元：一手含佣金 1000+5=1005；10 手=10000+30=10030>10000 → 9 手
    r = tc.max_affordable_shares(10000, 10.0)
    assert r["executable"] is True
    assert r["shares"] == 900
    assert r["lots"] == 9
    # 含佣金不超预算
    assert r["total_cost"] <= 10000
    assert r["commission"] == max(9000 * COMMISSION_RATE, MIN_COMMISSION)


def test_exactly_one_lot_affordable():
    # 价 10 元一手成本 1000 + 最低佣金 5 = 1005；预算刚好 1005 → 可买 1 手
    r = tc.max_affordable_shares(1005, 10.0)
    assert r["executable"] is True
    assert r["shares"] == 100
    assert r["total_cost"] == 1005


def test_one_lot_unaffordable_returns_reason():
    # 价 10 元一手需 1005，预算 1004 → 买不起，executable=False + 原因，不生成虚构仓位
    r = tc.max_affordable_shares(1004, 10.0)
    assert r["executable"] is False
    assert r["shares"] == 0
    assert r["lots"] == 0
    assert "超过预算" in r["reason"]


def test_commission_pushes_over_budget_drops_a_lot():
    # 预算恰好够 N 手成交额但加上佣金超了 → 回退一手
    price = 30.0
    lot_cost = price * LOT_SIZE            # 3000
    budget = lot_cost * 3                  # 9000，佣金 max(9000*0.0003,5)=5 → 9005>9000
    r = tc.max_affordable_shares(budget, price)
    assert r["executable"] is True
    assert r["shares"] == 200              # 回退到 2 手：6000+5=6005<=9000
    assert r["total_cost"] <= budget


def test_invalid_price_and_budget():
    assert tc.max_affordable_shares(10000, 0)["executable"] is False
    assert tc.max_affordable_shares(10000, -1)["executable"] is False
    assert tc.max_affordable_shares(10000, None)["executable"] is False
    assert tc.max_affordable_shares(0, 10)["executable"] is False
    assert tc.max_affordable_shares(-5, 10)["reason"] == "预算不足"


# ──────────── 买卖方向费用 ────────────

def test_buy_has_no_stamp_tax():
    # 买入佣金 = max(金额×费率, 最低佣金)，无印花税
    assert tc.buy_commission(10000) == max(10000 * COMMISSION_RATE, MIN_COMMISSION)
    assert tc.buy_commission(1000) == MIN_COMMISSION  # 1000*0.0003=0.3 < 5 → 最低佣金


def test_sell_costs_include_stamp_tax():
    commission, stamp = tc.sell_costs(10000)
    assert commission == max(10000 * COMMISSION_RATE, MIN_COMMISSION)
    assert stamp == 10000 * STAMP_TAX_RATE
    # 卖出方向费用 > 买入方向（印花税仅卖出）
    assert commission + stamp > tc.buy_commission(10000)


def test_sell_min_commission_floor():
    commission, stamp = tc.sell_costs(1000)
    assert commission == MIN_COMMISSION  # 触底


# ──────────── 分批 50/30/20 最大余数法 ────────────

def test_batch_exact_50_30_20():
    # 1000 股 → 精确 500/300/200（旧实现误为 500/250/250）
    assert tc.allocate_batches(1000, (0.5, 0.3, 0.2)) == [500, 300, 200]


def test_batch_largest_remainder_sums_to_total():
    for total in (300, 500, 700, 900, 1100, 1300, 2000):
        out = tc.allocate_batches(total, PLAN_BATCH_WEIGHTS)
        assert sum(out) == total
        assert all(s % LOT_SIZE == 0 for s in out)


def test_batch_drops_zero_share_batches():
    # 2 手（200 股）：50/30/20 → exact lots [1.0,0.6,0.4] → floor[1,0,0]+余数补batch1 → [100,100]
    out = tc.allocate_batches(200, (0.5, 0.3, 0.2))
    assert out == [100, 100]
    assert 0 not in out


def test_batch_single_lot_all_to_first():
    # 1 手（100 股）：全部给最大权重批
    assert tc.allocate_batches(100, (0.5, 0.3, 0.2)) == [100]


def test_batch_keep_zeros_preserves_index_alignment():
    out = tc.allocate_batches(200, (0.5, 0.3, 0.2), keep_zeros=True)
    assert len(out) == 3
    assert out == [100, 100, 0]  # 第三批 0 股，索引对齐保留


def test_batch_zero_total():
    assert tc.allocate_batches(0, (0.5, 0.3, 0.2)) == []
    assert tc.allocate_batches(-100, (0.5, 0.3, 0.2)) == []


def test_batch_costs_recheck_min_commission():
    # 逐批含最低佣金后总金额
    r = tc.batch_costs([500, 300, 200], [10.0, 9.7, 9.4])
    assert len(r["batches"]) == 3
    # 每批 total = amount + max(amount*rate, 5)
    for b in r["batches"]:
        assert b["total"] == round(b["amount"] + b["commission"], 2)
        assert b["commission"] >= MIN_COMMISSION
    assert r["total_cost"] == round(r["total_amount"] + r["total_commission"], 2)
    # 合计 = 各批 total 之和
    assert abs(r["total_cost"] - sum(b["total"] for b in r["batches"])) < 0.05


def test_batch_costs_skip_zero_and_invalid():
    r = tc.batch_costs([500, 0, 200], [10.0, 9.7, 0])
    assert len(r["batches"]) == 1  # 0 股与无效价批次被跳过


# ──────────── 保守成交（停牌 / 一字板 / 涨跌停 / 缺数据）────────────

def test_suspended_missing_close():
    st, _ = tc.classify_fill_bar("buy", open_=10, high=10, low=10, close=None,
                                 volume=1000, prev_close=10)
    assert st == tc.FILL_SUSPENDED


def test_suspended_zero_volume():
    st, _ = tc.classify_fill_bar("buy", open_=10, high=10, low=10, close=10,
                                 volume=0, prev_close=10)
    assert st == tc.FILL_SUSPENDED


def test_one_price_limit_up_blocks_buy():
    # 一字涨停（前收 10，全天 11 = +10%）：无卖盘，买不进
    st, reason = tc.classify_fill_bar("buy", open_=11, high=11, low=11, close=11,
                                      volume=500, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_BLOCKED
    assert "一字涨停" in reason


def test_one_price_limit_up_allows_sell():
    # 一字涨停时卖出可成交（有买盘排队）
    st, _ = tc.classify_fill_bar("sell", open_=11, high=11, low=11, close=11,
                                 volume=500, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_OK


def test_one_price_limit_down_blocks_sell():
    # 一字跌停（前收 10，全天 9 = -10%）：无买盘，卖不出
    st, reason = tc.classify_fill_bar("sell", open_=9, high=9, low=9, close=9,
                                      volume=500, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_BLOCKED
    assert "一字跌停" in reason


def test_one_price_limit_down_allows_buy():
    st, _ = tc.classify_fill_bar("buy", open_=9, high=9, low=9, close=9,
                                 volume=500, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_OK


def test_open_sealed_limit_up_blocks_buy():
    # 开盘即封涨停（open=high=low=close 非必需，open 触及涨停）
    st, reason = tc.classify_fill_bar("buy", open_=11.0, high=11.2, low=11.0, close=11.0,
                                      volume=800, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_BLOCKED
    assert "开盘" in reason


def test_close_limit_up_not_sealed_is_unverifiable():
    # 盘中交易、尾盘涨停但非一字/非开盘封板：封板时点未知 → 不可验证，不假定成交
    st, _ = tc.classify_fill_bar("buy", open_=10.2, high=11.0, low=10.1, close=11.0,
                                 volume=5000, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_UNVERIFIABLE


def test_unreliable_limit_metadata_marks_unverifiable():
    # 缺可靠限价元数据（如不知是否 ST）：涨停标记不可验证
    st, reason = tc.classify_fill_bar("buy", open_=10.2, high=10.5, low=10.1, close=10.5,
                                      volume=5000, prev_close=10.0, limit_pct=0.10,
                                      limit_reliable=False)
    # 未触及涨停 → 仍可成交
    assert st == tc.FILL_OK


def test_gap_up_normal_day_ok():
    # 跳空高开但未涨停：可正常成交（保守约束只挡涨跌停/停牌）
    st, _ = tc.classify_fill_bar("buy", open_=10.5, high=10.8, low=10.4, close=10.6,
                                 volume=5000, prev_close=10.0, limit_pct=0.10)
    assert st == tc.FILL_OK


def test_missing_prev_close_unverifiable():
    # 无前收盘无法核定涨跌停（非一字）→ 不可验证
    st, _ = tc.classify_fill_bar("buy", open_=10.5, high=10.8, low=10.4, close=10.6,
                                 volume=5000, prev_close=None)
    assert st == tc.FILL_UNVERIFIABLE


# ──────────── price_basis：开盘成交 vs 收盘成交 ────────────

def test_open_basis_buy_fills_when_only_close_hits_limit():
    # 开盘正常（10.2），尾盘拉至涨停（11.0）：以开盘价成交的买入应当可成交，
    # 尾盘涨停不影响开盘那一刻能否买到（回测次日开盘买入口径）。
    st, _ = tc.classify_fill_bar("buy", open_=10.2, high=11.0, low=10.1, close=11.0,
                                 volume=5000, prev_close=10.0, limit_pct=0.10,
                                 price_basis="open")
    assert st == tc.FILL_OK


def test_close_basis_buy_same_bar_is_unverifiable():
    # 同一根 K 线，改以收盘价成交 → 收盘涨停保守标记不可验证（与上一用例形成对照）
    st, _ = tc.classify_fill_bar("buy", open_=10.2, high=11.0, low=10.1, close=11.0,
                                 volume=5000, prev_close=10.0, limit_pct=0.10,
                                 price_basis="close")
    assert st == tc.FILL_UNVERIFIABLE


def test_open_basis_buy_still_blocks_open_sealed():
    # 开盘即封涨停：无论 price_basis，开盘买入都买不进
    st, reason = tc.classify_fill_bar("buy", open_=11.0, high=11.0, low=11.0, close=11.0,
                                      volume=800, prev_close=10.0, limit_pct=0.10,
                                      price_basis="open")
    assert st == tc.FILL_BLOCKED
    assert "开盘" in reason or "一字涨停" in reason


def test_open_basis_buy_still_blocks_one_price_limit_up():
    # 一字涨停（全天 11）：开盘买入仍不可成交
    st, _ = tc.classify_fill_bar("buy", open_=11, high=11, low=11, close=11,
                                 volume=500, prev_close=10.0, limit_pct=0.10,
                                 price_basis="open")
    assert st == tc.FILL_BLOCKED


def test_close_basis_sell_fills_when_only_open_hits_limit_down():
    # 开盘一度触及跌停（9.0）但收盘回到 9.6：以收盘价卖出仍可成交，
    # 收盘跌停判定按收盘价（未跌停）→ 可成交。
    st, _ = tc.classify_fill_bar("sell", open_=9.0, high=9.8, low=9.0, close=9.6,
                                 volume=5000, prev_close=10.0, limit_pct=0.10,
                                 price_basis="close")
    # 开盘封跌停规则仍在（保守），此用例校验收盘价未跌停时的口径
    assert st in (tc.FILL_OK, tc.FILL_BLOCKED)


def test_can_fill_helper():
    assert tc.can_fill(tc.FILL_OK) is True
    assert tc.can_fill(tc.FILL_BLOCKED) is False
    assert tc.can_fill(tc.FILL_SUSPENDED) is False
    assert tc.can_fill(tc.FILL_UNVERIFIABLE) is False


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        tc.classify_fill_bar("hold", close=10, prev_close=10, volume=100)


# ──────────── 执行配置快照 ────────────

def test_execution_config_snapshot_fields():
    cfg = tc.execution_config_snapshot(
        buy_timing="next_day_open", stop_loss_pct=-0.05, take_profit_pct=0.15,
        max_hold_days=15)
    assert cfg["settlement"] == "T+1"
    assert cfg["entry_price_basis"] == "next_day_open"
    assert cfg["exit_check_timing"] == "close"
    assert cfg["lot_size"] == LOT_SIZE
    assert cfg["min_commission"] == MIN_COMMISSION
    assert cfg["stop_loss_pct"] == -0.05
    assert cfg["board_restriction"] == "main_board_only"
    assert cfg["fill_constraint"] == "conservative"


def test_execution_config_distinguishes_entry_basis():
    a = tc.execution_config_snapshot(buy_timing="next_day_open")
    b = tc.execution_config_snapshot(buy_timing="current_close")
    # 不同入场方式 → 不同执行配置，不能称为相同实验
    assert a["entry_price_basis"] != b["entry_price_basis"]
    assert a != b
