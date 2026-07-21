"""tests/test_rule_backtest.py —— 策略规则一键回测（rule_engine）测试

覆盖：
1. 临时库造 2 只股 300 日合成数据 + 简单 RSI 规则 → run_rule_backtest 结构断言
2. 基准对比：插入 000300 指数行 → result["benchmark"] 存在且口径正确
3. 无信号规则 → success=False 且不产生交易
4. fitness 回写：_writeback_rule_fitness 更新 strategy_rules 绩效字段
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import math

import pandas as pd
import pytest

import core.db as db
from core.db import get_conn, init_db


# ─────────────────────────────────────────────
# 临时库 + 合成行情
# ─────────────────────────────────────────────
@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把数据库句柄重定向到临时文件并造 2 只股 300 日数据"""
    db_file = tmp_path / "quant_bt_test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    init_db()
    _seed_market_data()
    return str(db_file)


DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-06", periods=300)]
BT_START, BT_END = DATES[150], DATES[-1]   # 前 150 日做因子 warmup，后 150 日回测


def _seed_market_data():
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO stock_info (code, name, market, is_active) VALUES (?, ?, ?, 1)",
            [("600000", "测试银行", "SH"), ("000001", "测试平安", "SZ")],
        )
        rows = []
        for i, dt in enumerate(DATES):
            # 股票 A：温和上行 + 正弦波动（RSI 多数时间 > 30）
            ca = 10 + 0.03 * i + 0.5 * math.sin(i / 5.0)
            # 股票 B：横盘震荡（RSI 在 30~70 间摆动）
            cb = 20 + 0.8 * math.sin(i / 7.0 + 1.0)
            for code, close in (("600000", ca), ("000001", cb)):
                close = round(close, 4)
                rows.append((code, dt, round(close * 0.998, 4), round(close * 1.01, 4),
                             round(close * 0.99, 4), close, 1_000_000.0,
                             round(close * 1_000_000, 2), 0.5, 1.0))
        conn.executemany(
            "INSERT INTO daily_price (code, trade_date, open, high, low, close, "
            "volume, amount, pct_change, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        # 基准：沪深300 同步缓慢上行（4000 → 4598）
        conn.executemany(
            "INSERT INTO index_daily (code, trade_date, open, high, low, close) "
            "VALUES ('000300', ?, ?, ?, ?, ?)",
            [(dt, c, round(c * 1.005, 2), round(c * 0.995, 2), c)
             for dt, c in ((dt, round(4000 + 2 * i, 2)) for i, dt in enumerate(DATES))],
        )


def _make_rule(conditions, holding_max=5, rule_id=1):
    return {
        "id": rule_id,
        "rule_name": "测试规则",
        "conditions": json.dumps(conditions, ensure_ascii=False),
        "holding_max": holding_max,
        "horizon": "short",
    }


def _make_params():
    from backtest.engine import BacktestParams
    return BacktestParams(
        start_date=BT_START,
        end_date=BT_END,
        initial_cash=100_000.0,
        max_holdings=2,
        max_buy_per_day=1,
        max_hold_days=None,   # 用 rule.holding_max
    )


# ─────────────────────────────────────────────
# 1. 规则回测主流程
# ─────────────────────────────────────────────
def test_run_rule_backtest_structure(tmp_db):
    from backtest.rule_engine import run_rule_backtest

    # RSI_14 为 0~1 归一化口径（见 factor_lib），0.3 阈值在上升趋势中必然大量命中
    rule = _make_rule({"RSI_14": {">": 0.3}}, holding_max=5)
    result = run_rule_backtest(rule, _make_params())

    assert result.get("success") is True, f"回测应成功: {result.get('error')}"
    # 结构字段完整（与 VisualBacktestEngine 对齐的键）
    for key in ("total_return", "annual_return", "sharpe_ratio", "max_drawdown",
                "win_rate", "total_trades", "trades", "equity_curve", "rule"):
        assert key in result, f"缺少字段 {key}"
    assert result["total_trades"] >= 1, "合成数据应至少产生 1 笔交易"
    assert len(result["equity_curve"]) > 100
    assert result["rule"]["holding_max"] == 5
    # 交易明细字段
    t0 = result["trades"][0]
    for key in ("code", "buy_date", "buy_price", "sell_date", "sell_price",
                "shares", "hold_days", "pnl", "pnl_pct", "exit_reason"):
        assert key in t0, f"交易明细缺少 {key}"
    assert t0["hold_days"] <= 5, "持仓天数不应超过 holding_max"
    assert t0["shares"] % 100 == 0, "A 股应按整手交易"


def test_run_rule_backtest_benchmark(tmp_db):
    from backtest.rule_engine import run_rule_backtest

    rule = _make_rule({"RSI_14": {">": 0.3}}, holding_max=5)
    result = run_rule_backtest(rule, _make_params())

    bm = result.get("benchmark")
    assert bm is not None, "插入 000300 指数后应输出基准"
    assert bm["code"] == "000300"
    assert bm["name"] == "沪深300"
    # 基准 4000 → 4598，区间收益应为正
    assert bm["total_return"] > 0
    assert len(bm["curve"]) > 100
    # 净值曲线以 initial_cash 为基数
    assert bm["curve"][0]["total"] == pytest.approx(100_000.0, rel=0.01)


def test_run_rule_backtest_no_signal(tmp_db):
    from backtest.rule_engine import run_rule_backtest

    rule = _make_rule({"RSI_14": {">": 0.99}}, holding_max=5)  # 不可能条件（0~1 归一化）
    result = run_rule_backtest(rule, _make_params())
    assert result.get("success") is False
    assert result.get("total_trades") == 0
    assert "无" in (result.get("error") or "")


def test_run_rule_backtest_bad_conditions(tmp_db):
    from backtest.rule_engine import run_rule_backtest

    rule = _make_rule({"RSI_14": {">": 0.3}}, holding_max=5)
    rule["conditions"] = "{invalid"  # 非法 JSON
    result = run_rule_backtest(rule, _make_params())
    assert result.get("success") is False
    assert "conditions" in (result.get("error") or "")


# ─────────────────────────────────────────────
# 2. fitness 回写（推荐-回测闭环）
# ─────────────────────────────────────────────
def test_writeback_rule_fitness(tmp_db):
    from backtest.service import _writeback_rule_fitness

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO strategy_rules (rule_name, rule_type, encoding, holding_max) "
            "VALUES ('回写测试', 'evolved', 'x', 5)")
        rule_id = cur.lastrowid

    fake_result = {
        "annual_return": 0.30,   # 30%
        "win_rate": 0.60,        # 60%
        "sharpe_ratio": 1.23456,
        "max_drawdown": -0.10,   # -10%
        "total_trades": 42,
    }
    _writeback_rule_fitness(rule_id, fake_result)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT annual_return, win_rate, sharpe_ratio, max_drawdown, "
            "total_trades, fitness FROM strategy_rules WHERE id = ?",
            (rule_id,)).fetchone()

    assert row is not None
    # 库存百分数口径
    assert row["annual_return"] == pytest.approx(30.0)
    assert row["win_rate"] == pytest.approx(60.0)
    assert row["sharpe_ratio"] == pytest.approx(1.235, abs=0.001)
    assert row["max_drawdown"] == pytest.approx(-10.0)
    assert row["total_trades"] == 42
    # fitness = 0.30*1.5 + 0.60 + min(1.23456,3)*0.5 - 0.10 = 0.45+0.60+0.61728-0.10
    expected = round(0.30 * 1.5 + 0.60 + min(1.23456, 3.0) * 0.5 - 0.10, 4)
    assert row["fitness"] == pytest.approx(expected, abs=1e-4)


def test_writeback_rule_fitness_floor_zero(tmp_db):
    """巨亏结果 fitness 不得为负"""
    from backtest.service import _writeback_rule_fitness

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO strategy_rules (rule_name, rule_type, encoding, holding_max) "
            "VALUES ('回写测试2', 'evolved', 'x', 5)")
        rule_id = cur.lastrowid

    _writeback_rule_fitness(rule_id, {
        "annual_return": -0.50, "win_rate": 0.10,
        "sharpe_ratio": -2.0, "max_drawdown": -0.60, "total_trades": 10,
    })
    with get_conn() as conn:
        fitness = conn.execute(
            "SELECT fitness FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()["fitness"]
    assert fitness == 0
