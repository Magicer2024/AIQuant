"""C2 出场与复盘口径验收：诊断/成交分离、入场出场、幂等、合并拆段、统计合同。"""
import pytest

from core import db
from core.input_snapshot import RunContext, SnapshotStore
from core.repository.signal_repo import persist_signal_run, get_run_signals
from core import recommendation_service as svc
from core import signal_outcome_tracker as diag
from core import simulated_trading as sim
from core.repository import outcome_repo
from core.execution_config import DIAGNOSIS_DEFINITION_VERSION


def _record(code, strategy, *, horizon="short", buy=10.0, stop=9.4, tp=10.6,
            fusion=40.0, name="测试股", **extra):
    return {"code": code, "name": name, "strategy": strategy, "horizon": horizon,
            "scan_date": "2026-01-05", "buy_price": buy, "price": buy,
            "stop_loss": stop, "take_profit": tp, "fusion_score": fusion,
            "pct_above_ma20": 0.01, **extra}


def _put_prices(rows):
    with db.get_conn() as conn:
        conn.executemany(
            """INSERT INTO daily_price(code,trade_date,open,high,low,close,volume,amount,pct_change,turnover)
               VALUES (?,?,?,?,?,?,0,0,0,0)
               ON CONFLICT(code,trade_date) DO UPDATE SET
                 open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close""",
            rows)


def _p(code, d, o, h, l, c):
    return (code, d, o, h, l, c)


_PARAMS = {
    "effective": {"short_conf_gate": 22.0, "short_top_n": 3, "short_pullback_entry": 0,
                  "short_entry_window_days": 2, "short_stop_loss": -0.05,
                  "short_take_profit": 0.08, "short_trailing_pct": 0.05,
                  "short_max_hold_days": 3, "long_lowvol_sort_enabled": 0},
    "strategy_config": {"RECO_REGIME_CAP": {}},
    "runtime": {},
}


def _make_run(store, records, codes, *, scan_date="2026-01-05", params=None, market_state="warm"):
    block = store.put("daily_price", scan_date, [{"code": codes[0], "trade_date": scan_date, "close": 10}])
    context = RunContext.create(
        scan_date=scan_date, as_of=scan_date, scope="all", codes=codes,
        parameters=params or _PARAMS, version={"commit": "t"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block], "schemas": {}, "source": "stock_signal"},
        quality={"market_state": market_state, "data_ready": True, "cross_section_complete": True})
    records = [dict(r, scan_date=scan_date) for r in records]   # 信号日期须与运行一致
    return persist_signal_run(context, records, source="stock_signal", status="complete", store=store)


@pytest.fixture
def momentum_run(tmp_path):
    """隔日动量（次日开盘入场）+ 止损出场的完整场景。"""
    store = SnapshotStore(tmp_path / "s")
    run_id = _make_run(store, [_record("600001", "隔日动量", buy=10.0, stop=9.4, tp=10.6)], ["600001"])
    svc.select_and_publish(run_id, "short")
    # 01-05 信号日收盘 10（参考价）；01-06 开盘 10.2 建仓；01-07 收盘 9.35 破止损 9.4。
    _put_prices([
        _p("600001", "2026-01-05", 9.9, 10.0, 9.8, 10.0),
        _p("600001", "2026-01-06", 10.2, 10.3, 10.0, 10.1),
        _p("600001", "2026-01-07", 10.0, 10.1, 9.30, 9.35),
        _p("600001", "2026-01-08", 9.4, 9.5, 9.2, 9.3),
    ])
    return run_id


def test_diagnosis_uses_reference_price_separate_from_execution(momentum_run):
    diag.advance_signal_outcomes(run_id=momentum_run, as_of="2026-01-08")
    outcomes = outcome_repo.get_signal_outcomes(run_id=momentum_run,
                                                definition_version=DIAGNOSIS_DEFINITION_VERSION)
    assert len(outcomes) == 1 and outcomes[0]["status"] == "evaluated"
    # 诊断参考价 = 信号参考价 10（非成交价 10.2）；T1 用 01-06 收盘 10.1。
    assert outcomes[0]["reference_price"] == 10.0
    assert outcomes[0]["t1_return"] == 1.0        # (10.1-10)/10
    assert outcomes[0]["t2_return"] == -6.5       # (9.35-10)/10


def test_diagnosis_since_window_reaccumulates(momentum_run):
    """区间模式跨日重评估：since 覆盖信号日即可补全 T+n（供每日前向推进）。"""
    result = diag.advance_signal_outcomes(since="2026-01-01", as_of="2026-01-08")
    assert result["evaluated"] >= 1
    outcomes = outcome_repo.get_signal_outcomes(run_id=momentum_run,
                                                definition_version=DIAGNOSIS_DEFINITION_VERSION)
    assert outcomes and outcomes[0]["t3_return"] == -7.0   # (9.3-10)/10


def test_next_open_entry_and_stop_exit_closed(momentum_run):
    result = sim.advance_simulated_trades(as_of="2026-01-08")
    assert result["closed"] == 1
    trades = outcome_repo.list_trades(cohort="production", list_key="short")
    t = trades[0]
    # 成交价 = 次日开盘 10.2（与诊断参考价 10 分离），出场 = 止损日收盘 9.35。
    assert t["exec_entry_date"] == "2026-01-06" and t["exec_entry_price"] == 10.2
    assert t["exec_exit_date"] == "2026-01-07" and t["exec_exit_price"] == 9.35
    assert t["status"] == "closed"
    assert round(t["gross_return"], 2) == round((9.35 - 10.2) / 10.2 * 100, 2)
    kinds = [e["event_kind"] for e in outcome_repo.get_trade_events(t["id"])]
    assert "entry_fill" in kinds and "exit" in kinds


def test_advance_is_idempotent_no_double_settle(momentum_run):
    sim.advance_simulated_trades(as_of="2026-01-08")
    first = outcome_repo.list_trades(cohort="production", list_key="short")[0]
    sim.advance_simulated_trades(as_of="2026-01-08")   # 重复推进
    second = outcome_repo.list_trades(cohort="production", list_key="short")[0]
    assert second["id"] == first["id"] and second["status"] == "closed"
    assert second["gross_return"] == first["gross_return"]
    # 事件不因重复推进而翻倍。
    assert len(outcome_repo.get_trade_events(second["id"])) == \
        len(outcome_repo.get_trade_events(first["id"]))


def test_no_exit_on_entry_day_t_plus_1(tmp_path):
    """建仓日盘中跌破止损也不得卖出（A股 T+1）；出场最早在次一交易日。"""
    store = SnapshotStore(tmp_path / "s")
    run_id = _make_run(store, [_record("600002", "隔日动量", buy=10.0, stop=9.4)], ["600002"])
    svc.select_and_publish(run_id, "short")
    _put_prices([
        _p("600002", "2026-01-05", 9.9, 10.0, 9.8, 10.0),
        # 建仓日 01-06 盘中 low 8.0 远低于止损，但收盘回到 10；不得当日卖出。
        _p("600002", "2026-01-06", 10.0, 10.5, 8.0, 10.0),
    ])
    sim.advance_simulated_trades(as_of="2026-01-06")
    t = outcome_repo.list_trades(cohort="production", list_key="short")[0]
    assert t["status"] == "holding" and t["exec_exit_date"] is None


def test_pullback_entry_fills_on_dip_and_expires_without_dip(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    params = dict(_PARAMS)
    params["effective"] = dict(_PARAMS["effective"], short_pullback_entry=1, short_entry_window_days=2)
    # 短线融合（抄底类，非动量/反转，在正式短线榜）→ 回踩确认入场。两码同一批次。
    run_id = _make_run(store, [
        _record("600003", "短线融合", buy=10.0, stop=9.0, tp=11.0),   # 会回踩成交
        _record("600004", "短线融合", buy=10.0, stop=9.0, tp=11.0),   # 窗口内不回踩 → expired
    ], ["600003", "600004"], params=params)
    svc.select_and_publish(run_id, "short")
    _put_prices([
        _p("600003", "2026-01-05", 9.9, 10.0, 9.8, 10.0),
        _p("600003", "2026-01-06", 10.4, 10.6, 10.2, 10.5),   # 未回踩到买点 10
        _p("600003", "2026-01-07", 10.1, 10.2, 9.9, 10.0),    # low 9.9<=10 → 成交 min(10,10.1)=10
        _p("600003", "2026-01-08", 10.0, 10.1, 9.95, 10.0),
        _p("600004", "2026-01-05", 9.9, 10.0, 9.8, 10.0),
        _p("600004", "2026-01-06", 10.4, 10.6, 10.3, 10.5),   # 始终高于买点 10
        _p("600004", "2026-01-07", 10.5, 10.7, 10.4, 10.6),
    ])
    sim.advance_simulated_trades(as_of="2026-01-08")
    filled = [t for t in outcome_repo.list_trades() if t["code"] == "600003"][0]
    assert filled["exec_entry_date"] == "2026-01-07" and filled["exec_entry_price"] == 10.0
    assert filled["status"] in ("holding", "closed")
    # 窗口内始终未回踩 → expired + no_fill，不计入成绩。
    expired = [t for t in outcome_repo.list_trades() if t["code"] == "600004"][0]
    assert expired["status"] == "expired" and expired["exec_entry_price"] is None


def test_consecutive_recommendations_merge_into_one_trade(tmp_path):
    """未了结期间同股复推并入同一交易（只追加 recommend 事件），不重复建单。"""
    store = SnapshotStore(tmp_path / "s")
    run1 = _make_run(store, [_record("600005", "隔日动量", buy=10.0, stop=9.0, tp=12.0)],
                     ["600005"], scan_date="2026-01-05")
    svc.select_and_publish(run1, "short")
    store2 = SnapshotStore(tmp_path / "s2")
    run2 = _make_run(store2, [_record("600005", "隔日动量", buy=10.1, stop=9.0, tp=12.0)],
                     ["600005"], scan_date="2026-01-06")
    svc.select_and_publish(run2, "short")
    _put_prices([
        _p("600005", "2026-01-05", 9.9, 10.0, 9.8, 10.0),
        _p("600005", "2026-01-06", 10.0, 10.4, 9.9, 10.3),   # 建仓日
        _p("600005", "2026-01-07", 10.3, 10.6, 10.2, 10.5),   # 仍持有（未破止损/未到 max_hold）
    ])
    result = sim.advance_simulated_trades(as_of="2026-01-07")
    trades = [t for t in outcome_repo.list_trades(cohort="production", list_key="short")
              if t["code"] == "600005"]
    assert len(trades) == 1        # 两日复推合并为一单
    rec_events = [e for e in outcome_repo.get_trade_events(trades[0]["id"]) if e["event_kind"] == "recommend"]
    assert len(rec_events) == 2    # 每日推荐 ID 均保留为事件


def test_settlement_stats_contract():
    """结算胜率只统计已平仓有效单；持仓/未成交分别返回；无亏损时 profit factor 为 None。"""
    trades = [
        {"id": "a", "status": "closed", "net_return": 5.0},
        {"id": "b", "status": "closed", "net_return": 3.0},
        {"id": "c", "status": "holding", "net_return": None},
        {"id": "d", "status": "watching", "net_return": None},
        {"id": "e", "status": "expired", "net_return": None},
    ]
    stats = outcome_repo.settlement_stats(trades)
    assert stats["settled"] == 2 and stats["settled_wins"] == 2
    assert stats["settlement_win_rate"] == 1.0
    assert stats["holding"] == 1 and stats["unfilled"] == 1 and stats["expired"] == 1
    # 无亏损样本 → profit factor 无定义（None + 说明），不用 99 伪装。
    assert stats["profit_factor"] is None and stats["profit_factor_note"]
    assert stats["avg_loss"] is None


def test_tn_win_stats_counts_samples():
    outcomes = [{"t1_return": 2.0}, {"t1_return": -1.0}, {"t1_return": None}, {"t1_return": 0.5}]
    s = outcome_repo.tn_win_stats(outcomes, "t1_return")
    # 有效值 3 个（None 不计入分母），正收益 2 个。
    assert s["valid"] == 3 and s["positive"] == 2 and round(s["win_rate"], 4) == round(2 / 3, 4)
