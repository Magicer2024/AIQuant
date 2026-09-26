"""统一信号身份、冻结输入与事务迁移的隔离验收。"""
from dataclasses import replace
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import db
from core.input_snapshot import RunContext, SnapshotStore, SnapshotUnavailable, canonical_json
from core.repository.signal_repo import (
    SignalConsistencyError, adapt_signal, persist_signal_run, get_run_signals,
)


@pytest.fixture
def frozen_run(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    block = store.put("daily_price", "2026-01-05", [
        {"code": "600001", "trade_date": "2026-01-05", "close": 10}])
    context = RunContext.create(
        scan_date="2026-01-05", as_of="2026-01-05", scope="all", codes=["600001"],
        parameters={"effective": {"sig_threshold": 15}}, version={"commit": "test"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block]})
    return context, store


@pytest.fixture
def price_inputs():
    import pandas as pd
    days = pd.bdate_range(end="2026-01-05", periods=320)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO stock_info(code,name) VALUES ('600001','测试股票')")
        conn.executemany("INSERT INTO daily_price(code,trade_date,open,high,low,close,volume,amount,pct_change,turnover) VALUES ('600001',?,?,?,?,?,10000000,100000000,0.1,2)",
                         [(d.date().isoformat(), 10 + i * 0.01, 10.1 + i * 0.01, 9.9 + i * 0.01, 10 + i * 0.01) for i, d in enumerate(days)])
    return "2026-01-05"


def test_frozen_runtime_ignores_current_prices_params_and_metadata(price_inputs, monkeypatch):
    from core.signal_runtime import prepare_signal_run, calculate_signal_run
    from config.strategy_params import get_param, get_strategy_config
    import core.sync as sync
    def compute(df, code, name, *args, **kwargs):
        return [{"code": code, "name": name, "buy_price": float(df.iloc[-1]["close"]),
                 "strategy": "隔日动量", "score": get_param("sig_threshold"),
                 "enabled": get_strategy_config("NEXT_DAY_MOMENTUM")["enabled"]}]
    monkeypatch.setattr(sync, "_build_signal_records", compute)
    context = prepare_signal_run(price_inputs)
    before, failed = calculate_signal_run(context)
    assert not failed and len(before) == 1
    with db.get_conn() as conn:
        conn.execute("UPDATE daily_price SET close=999")
        conn.execute("UPDATE stock_info SET name='*ST改变'")
        conn.execute("INSERT INTO strategy_param_override(param_key,value) VALUES ('sig_threshold','22')")
    from config import strategy_params
    monkeypatch.setitem(strategy_params.NEXT_DAY_MOMENTUM, "enabled", False)
    after, failed = calculate_signal_run(context)
    assert not failed and after == before
    assert json.loads(context.quality_json)["point_in_time_complete"] is False


@pytest.mark.parametrize("source", ["stock_signal", "stock_deep_signal", "strategy_signals"])
def test_real_calculators_consume_only_snapshot(price_inputs, source):
    from core.signal_runtime import prepare_signal_run, calculate_signal_run
    context = prepare_signal_run(price_inputs, source=source)
    records, failures = calculate_signal_run(context)
    assert not failures
    assert all(r.get("scan_date", price_inputs) == price_inputs for r in records)


def test_input_connection_rejects_writes_and_recovers(price_inputs):
    from core.signal_runtime import prepare_signal_run
    from core.input_snapshot import open_snapshot
    context = prepare_signal_run(price_inputs)
    with open_snapshot(json.loads(context.manifest_json)) as conn, db.input_connection(conn):
        with db.get_conn() as borrowed:
            assert borrowed is conn
            with pytest.raises(sqlite3.OperationalError):
                borrowed.execute("UPDATE daily_price SET close=1")
        with pytest.raises(ValueError):
            with db.get_conn(path=db.DB_PATH):
                pass
    with db.get_conn() as conn:
        conn.execute("UPDATE daily_price SET close=12")


def test_partial_calculation_retains_failure_and_cannot_project(price_inputs, monkeypatch):
    from core.signal_runtime import prepare_signal_run, execute_signal_run
    from core.repository.signal_repo import get_signal_run, write_legacy_projection
    from config import settings
    import core.sync as sync
    def fail(*args, **kwargs):
        raise RuntimeError("注入股票计算失败")
    monkeypatch.setattr(sync, "_build_signal_records", fail)
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "shadow")
    result = execute_signal_run(prepare_signal_run(price_inputs))
    assert result["status"] == "failed"
    quality = json.loads(get_signal_run(result["run_id"])["quality_json"])
    assert not quality["cross_section_complete"] and quality["failures"][0]["code"] == "600001"
    with pytest.raises(ValueError):
        write_legacy_projection(result["run_id"])


def test_local_scope_cannot_impersonate_market(price_inputs):
    from core.signal_runtime import prepare_signal_run
    with pytest.raises(ValueError, match="局部股票范围"):
        prepare_signal_run(price_inputs, codes=[])
    context = prepare_signal_run(price_inputs, scope="watchlist", codes=["600001"])
    assert not json.loads(context.quality_json)["cross_section_complete"]


@pytest.mark.parametrize("lookback", [0, -1, None])
def test_rules_unlimited_lookback_preserves_history(price_inputs, lookback):
    from core.signal_runtime import prepare_signal_run
    from core.input_snapshot import open_snapshot
    context = prepare_signal_run(price_inputs, source="strategy_signals", runtime={"lookback": lookback})
    with open_snapshot(json.loads(context.manifest_json)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0] == 320


def test_missing_dataset_is_not_an_empty_dataset(price_inputs):
    from core.signal_runtime import prepare_signal_run
    from core.input_snapshot import open_snapshot
    context = prepare_signal_run(price_inputs)
    manifest = json.loads(context.manifest_json)
    manifest["blocks"] = [b for b in manifest["blocks"] if b["dataset"] != "stock_lhb_detail"]
    with pytest.raises(SnapshotUnavailable, match="内容块不完整"):
        with open_snapshot(manifest):
            pass


def test_stale_deep_price_is_never_stamped_today(price_inputs, monkeypatch):
    from core.signal_runtime import prepare_signal_run, calculate_signal_run
    with db.get_conn() as conn:
        conn.execute("INSERT INTO stock_info(code,name) VALUES ('600002','停牌测试')")
        conn.execute("INSERT INTO daily_price(code,trade_date,close) VALUES ('600002','2026-01-02',10)")
    import strategy.stock_deep as deep
    seen = []
    def scan(code, *args, **kwargs):
        seen.append(code)
        return {"code": code, "action_plan": {}}
    monkeypatch.setattr(deep, "_scan_one", scan)
    context = prepare_signal_run(price_inputs, source="stock_deep_signal")
    records, errors = calculate_signal_run(context)
    assert not errors and seen == ["600001"]
    assert {r["code"] for r in records} == {"600001"}
    assert not json.loads(context.quality_json)["cross_section_complete"]


def test_local_market_regime_uses_full_breadth(price_inputs):
    from core.signal_runtime import prepare_signal_run
    from core.input_snapshot import open_snapshot
    from core.market_regime import compute_regime_on
    with db.get_conn() as conn:
        conn.execute("INSERT INTO daily_price(code,trade_date,close,pct_change) SELECT '600002',trade_date,100-close,-10 FROM daily_price WHERE code='600001'")
        expected = compute_regime_on(conn, price_inputs)
    context = prepare_signal_run(price_inputs, scope="watchlist", codes=["600001"])
    with open_snapshot(json.loads(context.manifest_json)) as conn:
        assert compute_regime_on(conn, price_inputs) == expected
        assert conn.execute("SELECT COUNT(DISTINCT code) FROM market_breadth").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(DISTINCT code) FROM daily_price").fetchone()[0] == 1


def test_failed_preamble_is_persisted(price_inputs, monkeypatch):
    from core import signal_runtime as runtime
    from core.repository.signal_repo import get_signal_run
    context = runtime.prepare_signal_run(price_inputs)
    monkeypatch.setattr(runtime, "code_version", lambda: {"commit": "changed"})
    result = runtime.execute_signal_run(context)
    assert result["status"] == "failed" and result["signals"] == 0
    assert get_signal_run(result["run_id"])["status"] == "failed"
    assert result["failures"][0]["type"] == "SnapshotUnavailable"
    assert runtime.execute_signal_run(context)["run_id"] == result["run_id"]


def test_direct_writers_require_frozen_context(frozen_run, monkeypatch):
    from config import settings
    from core.repository.signal_repo import save_scan_signals, save_signals
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "shadow")
    context, store = frozen_run
    for writer in (save_scan_signals, save_signals):
        with pytest.raises(ValueError, match="run_context"):
            writer([])
    run_id = save_scan_signals(candidates(), run_context=context, store=store)
    assert len(get_run_signals(run_id)) == 3
    with pytest.raises(ValueError, match="范围"):
        adapt_signal({"code": "600002"}, context)


def test_v2_is_blocked_until_cutover_acceptance():
    from app import create_app
    app = create_app({"TESTING": True, "SIGNAL_MODEL_MODE": "v2"})
    client = app.test_client()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code == 503
    assert client.get("/api/investor/today").status_code == 503


def test_full_recalculation_is_frozen_and_does_not_replay_history(price_inputs, monkeypatch):
    from config import settings
    from core.sync import recalc_all_scores
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "shadow")
    first = recalc_all_scores()
    second = recalc_all_scores()
    assert first["status"] == second["status"] == "complete"
    assert first["run_id"] == second["run_id"]
    assert first["days"] == 320 and first["replay_runs"] == []
    with db.get_conn(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal_run").fetchone()[0] == 1


def test_incomplete_cross_section_never_overwrites_legacy_candidates(price_inputs, monkeypatch):
    """当日无行情的运行即便无失败也不能用空榜替换旧候选。"""
    from config import settings
    from core.signal_runtime import prepare_signal_run, execute_signal_run
    from core.repository.signal_repo import get_signal_run, write_legacy_projection
    with db.get_conn() as conn:
        conn.execute("INSERT INTO stock_signal(scan_date,trade_date,code,name,price,buy_price,horizon,strategy) "
                     "VALUES ('2026-01-06','2026-01-06','600001','旧候选',10,10,'short','隔日动量')")
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "shadow")
    # 2026-01-06 无任何行情：universe 为空，运行完成但 data_ready=False。
    context = prepare_signal_run("2026-01-06", scope="all")
    result = execute_signal_run(context)
    assert result["status"] == "complete" and result["signals"] == 0
    quality = json.loads(get_signal_run(result["run_id"])["quality_json"])
    assert not quality["data_ready"] and not quality["cross_section_complete"]
    with pytest.raises(ValueError, match="输入未就绪"):
        write_legacy_projection(result["run_id"])
    with db.get_conn(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_signal WHERE scan_date='2026-01-06'").fetchone()[0] == 1


def test_deep_environment_override_is_frozen_into_parameters(price_inputs, monkeypatch):
    """新冻结解析环境覆盖后的深度辅助配置；传入旧快照时不重读环境。"""
    import strategy.stock_deep as deep
    from core.signal_runtime import prepare_signal_run
    monkeypatch.setattr(deep, "_EMA20_AUX", {"enabled": True, "weight": 0.3})
    context = prepare_signal_run(price_inputs, source="stock_deep_signal")
    frozen = context.parameters["strategy_config"]["DEEP_EMA20_AUX"]
    assert frozen["enabled"] is True
    # 复用已冻结参数时不再重新解析当前环境覆盖。
    monkeypatch.setattr(deep, "_EMA20_AUX", {"enabled": False, "weight": 0.9})
    replayed = prepare_signal_run(price_inputs, source="stock_deep_signal", parameters=context.parameters)
    assert replayed.parameters["strategy_config"]["DEEP_EMA20_AUX"]["enabled"] is True



def candidates():
    return [{"code": "600001", "horizon": "short", "strategy": name,
             "buy_price": 10, "fusion_score": 35, "created_at": "ignored"}
            for name in ("隔日动量", "反转首日", "强势突破")]


def test_all_strategies_survive_idempotent_reordered_write(frozen_run):
    context, store = frozen_run
    run_id = persist_signal_run(context, candidates(), store=store)
    before = get_run_signals(run_id)
    assert {r["strategy_key"] for r in before} == {
        "next_day_momentum", "first_reversal", "surge_breakout"}
    records = [dict(r, created_at="another time") for r in reversed(candidates())]
    assert persist_signal_run(context, records, store=store) == run_id
    assert get_run_signals(run_id) == before


def test_concurrent_run_has_one_identity(frozen_run):
    context, store = frozen_run
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: persist_signal_run(context, candidates(), store=store), range(2)))
    assert ids[0] == ids[1]
    assert len(get_run_signals(ids[0])) == 3


def test_conflicting_content_is_never_replaced(frozen_run):
    context, store = frozen_run
    run_id = persist_signal_run(context, candidates(), store=store)
    changed = candidates()
    changed[0]["buy_price"] = 11
    with pytest.raises(SignalConsistencyError):
        persist_signal_run(context, changed, store=store)
    with pytest.raises(SignalConsistencyError):
        persist_signal_run(context, candidates() + changed, store=store)
    assert {r["signal_reference_price"] for r in get_run_signals(run_id)} == {10}


def test_parameters_inputs_and_code_create_new_runs(frozen_run):
    context, store = frozen_run
    original = persist_signal_run(context, candidates(), store=store)
    new_block = store.put("daily_price", context.scan_date, [{"code": "600001", "close": 11}])
    variants = [replace(context, params_json=canonical_json({"effective": {"sig_threshold": 18}})),
                replace(context, code_version_json=canonical_json({"commit": "new"})),
                replace(context, manifest_json=canonical_json({"format": "jsonl-gzip-v1", "blocks": [new_block]}))]
    ids = {persist_signal_run(c, candidates(), store=store) for c in variants}
    assert original not in ids and len(ids) == 3
    assert len(get_run_signals(original)) == 3


@pytest.mark.parametrize("statement", [
    "UPDATE signal_run SET status='running' WHERE id=?",
    "DELETE FROM signal_run WHERE id=?",
    "UPDATE signal_event SET entry_target=12 WHERE run_id=?",
    "DELETE FROM signal_event WHERE run_id=?",
])
def test_finished_evidence_is_immutable(frozen_run, statement):
    context, store = frozen_run
    run_id = persist_signal_run(context, candidates(), store=store)
    with db.get_conn() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, (run_id,))


def test_source_adapters_keep_unknown_names_and_source_ids(frozen_run):
    context, _ = frozen_run
    unknown = adapt_signal({"id": 7, "code": "600001", "strategy": "旧策略甲"}, context)
    another = adapt_signal({"code": "600001", "strategy": "旧策略乙"}, context)
    assert unknown["strategy_key"] == "legacy_unknown"
    assert unknown["signal_kind"] != another["signal_kind"]
    assert json.loads(unknown["payload_json"])["source_id"] == 7
    deep = adapt_signal({"code": "600001", "action_plan": {
        "entry_price": 11, "stop_loss": 10, "take_profit": 13}}, context, source="stock_deep_signal")
    assert deep["horizon"] == "deep" and deep["entry_target"] == 11
    rule = adapt_signal({"code": "600001", "rule_id": 42, "confidence": 0.8}, context, source="strategy_signals")
    assert rule["strategy_key"] == "rule:42" and rule["raw_score"] == 0.8


def test_snapshot_content_dedup_and_missing_fails_closed(tmp_path):
    store = SnapshotStore(tmp_path)
    rows = [{"b": 2, "a": 1}, {"a": 2, "b": None}]
    block = store.put("daily_price", "2026-01-05", rows)
    assert store.put("daily_price", "2026-01-05", list(reversed(rows))) == block
    assert len(list(tmp_path.rglob("*.jsonl.gz"))) == 1
    assert store.read(block) == rows
    with pytest.raises(SnapshotUnavailable):
        store.read(dict(block, sha256="0" * 64))
    with pytest.raises(SnapshotUnavailable):
        store.verify({"format": "jsonl-gzip-v1", "blocks": []})
    with pytest.raises(SnapshotUnavailable):
        store.read(dict(block, rows=3))


def test_context_is_deep_frozen_and_historical_is_replay(frozen_run):
    context, _ = frozen_run
    copy = context.parameters
    copy["effective"]["sig_threshold"] = 99
    assert context.parameters["effective"]["sig_threshold"] == 15
    assert context.run_type == "replay"


def test_frozen_effective_parameters_do_not_read_new_overrides():
    from config.strategy_params import freeze_effective_parameters, parameter_context, get_param
    with db.get_conn() as conn:
        frozen = freeze_effective_parameters(conn)
    expected = frozen["effective"]["sig_threshold"]
    with parameter_context(frozen["effective"]):
        with db.get_conn() as conn:
            conn.execute("INSERT INTO strategy_param_override(param_key,value) VALUES ('sig_threshold','22')")
        assert get_param("sig_threshold") == expected
        with pytest.raises(KeyError):
            get_param("unrecorded_parameter")


def test_versioned_migration_rolls_back_all_statements():
    with pytest.raises(sqlite3.OperationalError):
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            db._apply_versioned_migration(conn, "test_failed", "CREATE TABLE staged(id);\nINVALID SQL;\n")
    with db.get_conn(readonly=True) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='staged'").fetchone() is None
        assert conn.execute("SELECT 1 FROM schema_migration WHERE version='test_failed'").fetchone() is None


@pytest.mark.parametrize("values", [
    ("closed", None, None, None, None, None),
    ("holding", None, None, None, None, None),
    ("watching", None, None, None, None, 5),
    ("closed", "2026-01-06", 10, "2026-01-06", 11, 10),
])
def test_simulated_trade_rejects_unknown_fill_and_same_day_exit(frozen_run, values):
    context, store = frozen_run
    run_id = persist_signal_run(context, candidates(), store=store)
    signal_id = get_run_signals(run_id)[0]["id"]
    with db.get_conn() as conn:
        conn.execute("INSERT INTO recommendation_batch(id,run_id,scan_date,list_key,cohort,policy_json,policy_hash,market_state,status,source,exclusions_json,content_hash) VALUES ('b',?,'2026-01-05','short','production','{}','p','range','draft','test','[]','hash')", (run_id,))
        conn.execute("INSERT INTO recommendation_item(id,batch_id,signal_id,code,horizon,rank,reason_json,plan_json,support_json,content_hash) VALUES ('i','b',?,'600001','short',1,'[]','{}','[]','hash')", (signal_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO simulated_trade(id,first_recommendation_id,code,horizon,list_key,cohort,execution_json,merge_policy,status,exec_entry_date,exec_entry_price,exec_exit_date,exec_exit_price,gross_return) VALUES ('t','i','600001','short','short','production','{}','consecutive',?,?,?,?,?,?)", values)
