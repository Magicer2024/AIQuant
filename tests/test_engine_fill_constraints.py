"""tests/test_engine_fill_constraints.py —— 回测引擎保守成交约束（方案 E2c）

直接驱动 VisualBacktestEngine._run_portfolio（不依赖 DB），用合成 bar 校验引擎
在真实回测循环里确实执行了保守成交约束：

  1. T+1：当日买入不可当日止损离场（最早次日），杜绝「当日买当日卖」的失真收益
  2. 一字涨停：信号日次日一字板 → 开盘买不进（回测最大的乐观偏差来源）
  3. 停牌（量为 0）：买不进
  4. 一字跌停：卖不出，持仓 carry forward 到可成交日才离场
  5. 跳空高开但未涨停：正常买入（保守约束只挡涨跌停/停牌，不误伤跳空）

与 utils.trade_constraints 黄金样例同口径（各入口共用 classify_fill_bar）。
"""
from __future__ import annotations

import pandas as pd

from backtest.engine import BacktestParams, VisualBacktestEngine


# 连续工作日，日历差 = 持仓天数（1/2/3），便于 T+1 与 max_hold 断言
DATES = pd.to_datetime(["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09"])


def _make_df(bars, fusion_score=50.0):
    """bars: 按 DATES 顺序的 (open, high, low, close, volume) 列表；prev_close 由 close.shift(1) 得出。"""
    rows = []
    prev = None
    for (o, h, l, c, v) in bars:
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v,
                     "prev_close": prev, "fusion_score": fusion_score})
        prev = c
    return pd.DataFrame(rows, index=DATES[:len(bars)])


def _engine(**kw):
    params = BacktestParams(start_date="2025-01-06", end_date="2025-01-09",
                            initial_cash=100_000.0, max_holdings=5, max_buy_per_day=3,
                            buy_timing="next_day_open", **kw)
    return VisualBacktestEngine(params)


def _sig(true_idx=0, n=4):
    vals = [False] * n
    vals[true_idx] = True
    return pd.Series(vals, index=DATES[:n])


def _run(engine, bars, sig_idx=0, code="600000"):
    stock_data = {code: _make_df(bars)}
    signals = {code: _sig(sig_idx)}
    info_map = {code: {"name": code}}
    return engine._run_portfolio(stock_data, signals, info_map)


# ──────────── 1. T+1：当日买入不可当日卖出 ────────────

def test_t1_no_same_day_stop_loss():
    # D1 信号 → D2 开盘买入；D2 收盘已跌破 -5% 止损，但 T+1 当日不可卖 → 拖到 D3 才止损
    bars = [
        (10.0, 10.2, 9.9, 10.0, 1000),   # D1 信号日
        (10.0, 10.0, 9.4, 9.5, 1000),    # D2 开盘买、收盘 -5%（触发止损但被 T+1 挡住）
        (9.5, 9.6, 9.3, 9.4, 1000),      # D3 次日止损卖出
        (9.4, 9.5, 9.3, 9.45, 1000),     # D4
    ]
    res = _run(_engine(stop_loss_pct=-0.05), bars)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["buy_date"] == "2025-01-07"
    assert t["sell_date"] == "2025-01-08"     # 次日才卖，非买入当日
    assert t["exit_reason"] == "stop_loss"
    assert t["hold_days"] >= 1


# ──────────── 2. 一字涨停：次日开盘买不进 ────────────

def test_one_price_limit_up_blocks_open_buy():
    # D2 一字涨停（全天 11，前收 10）：开盘无卖盘，买不进 → 全程不建仓
    bars = [
        (10.0, 10.2, 9.9, 10.0, 1000),   # D1 信号
        (11.0, 11.0, 11.0, 11.0, 500),   # D2 一字涨停
        (11.0, 11.5, 10.8, 11.2, 1000),  # D3
        (11.2, 11.3, 11.0, 11.1, 1000),  # D4
    ]
    res = _run(_engine(), bars)
    assert res["trades"] == []
    assert res["positions"] == []


# ──────────── 3. 停牌（量为 0）：买不进 ────────────

def test_suspended_zero_volume_blocks_buy():
    bars = [
        (10.0, 10.2, 9.9, 10.0, 1000),   # D1 信号
        (10.0, 10.1, 9.9, 10.0, 0),      # D2 停牌（量 0）
        (10.0, 10.3, 9.9, 10.1, 1000),   # D3
        (10.1, 10.2, 10.0, 10.15, 1000), # D4
    ]
    res = _run(_engine(), bars)
    assert res["trades"] == []
    assert res["positions"] == []


# ──────────── 4. 一字跌停：卖不出，持仓 carry forward ────────────

def test_one_price_limit_down_blocks_sell_carries_forward():
    # D2 买入；D3 一字跌停触发 max_hold 但卖不出 → carry 到 D4 恢复交易才离场
    bars = [
        (10.0, 10.2, 9.9, 10.0, 1000),   # D1 信号
        (10.0, 10.1, 9.9, 10.0, 1000),   # D2 开盘买入，收盘平
        (9.0, 9.0, 9.0, 9.0, 500),       # D3 一字跌停（前收 10）→ 卖不出
        (9.0, 9.3, 8.9, 9.2, 1000),      # D4 恢复交易 → 卖出
    ]
    res = _run(_engine(max_hold_days=1), bars)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["buy_date"] == "2025-01-07"
    assert t["sell_date"] == "2025-01-09"     # D3 跌停卖不出，D4 才成交
    assert t["exit_reason"] == "max_hold_days"


# ──────────── 5. 跳空高开但未涨停：正常买入 ────────────

def test_gap_up_normal_day_allows_buy():
    bars = [
        (10.0, 10.2, 9.9, 10.0, 1000),    # D1 信号
        (10.5, 10.8, 10.4, 10.6, 5000),   # D2 跳空高开 +5%，未涨停
        (10.6, 10.7, 10.3, 10.4, 5000),   # D3
        (10.4, 10.5, 10.2, 10.3, 5000),   # D4
    ]
    res = _run(_engine(max_hold_days=1), bars)
    assert len(res["trades"]) == 1
    assert res["trades"][0]["buy_date"] == "2025-01-07"


# ──────────── 对照：涨停未封板（尾盘涨停）收盘买不进，但开盘买可成交 ────────────

def test_close_limit_up_blocks_open_buy_only_when_sealed():
    # D2 开盘正常（10.2）、尾盘拉涨停（11.0）：以开盘价成交仍可买入（price_basis=open）
    bars = [
        (10.0, 10.2, 9.9, 10.0, 1000),    # D1 信号
        (10.2, 11.0, 10.1, 11.0, 5000),   # D2 尾盘涨停但开盘可成交
        (11.0, 11.2, 10.8, 11.0, 5000),   # D3
        (11.0, 11.1, 10.7, 10.9, 5000),   # D4
    ]
    res = _run(_engine(max_hold_days=1), bars)
    # 开盘未封板 → 买得进；持仓在 D3 触发 max_hold 卖出
    assert len(res["trades"]) == 1
    assert res["trades"][0]["buy_date"] == "2025-01-07"
