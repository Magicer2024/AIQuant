"""冻结推荐发布：选择、去重、名额、发布与幂等回取的隔离验收。"""
import json

import pytest

from core import db
from core.input_snapshot import RunContext, SnapshotStore
from core.repository.signal_repo import persist_signal_run, get_run_signals
from core import recommendation_service as svc
from core.repository import recommendation_repo as repo


def _record(code, strategy, *, name="测试股", horizon="short", fusion=35.0,
            ext=0.01, chip=None, buy=10.0, stop=9.4, tp=10.6, **extra):
    return {"code": code, "name": name, "strategy": strategy, "horizon": horizon,
            "scan_date": "2026-01-05", "buy_price": buy, "price": buy,
            "stop_loss": stop, "take_profit": tp, "fusion_score": fusion,
            "pct_above_ma20": ext, "chip_conc": chip, **extra}


@pytest.fixture
def run_with_signals(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    block = store.put("daily_price", "2026-01-05", [{"code": "600001", "trade_date": "2026-01-05", "close": 10}])
    codes = ["600001", "600002", "300003", "600004"]
    parameters = {
        "effective": {"short_conf_gate": 22.0, "short_top_n": 3, "long_lowvol_sort_enabled": 0},
        "strategy_config": {"RECO_REGIME_CAP": {"short": {"cold": 0, "cool": 2}, "mid": {"cool": 2}, "long": {"cool": 1}}},
        "runtime": {},
    }
    context = RunContext.create(
        scan_date="2026-01-05", as_of="2026-01-05", scope="all", codes=codes,
        parameters=parameters, version={"commit": "test"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block], "schemas": {}, "source": "stock_signal"},
        quality={"market_state": "warm", "data_ready": True, "cross_section_complete": True})
    records = [
        _record("600001", "隔日动量", fusion=40, ext=0.05),
        _record("600001", "短线融合", fusion=38, ext=0.02),   # 同股：应转支持信息
        _record("600002", "短线融合", name="*ST风险", fusion=45),  # ST 应排除
        _record("300003", "隔日动量", fusion=50),             # 创业板应排除
        _record("600004", "强势突破", fusion=60),             # 观察线不入正式短线
        _record("600004", "反转首日", fusion=55),             # 观察线不入正式短线
    ]
    run_id = persist_signal_run(context, records, source="stock_signal", status="complete", store=store)
    return run_id, store


def test_production_short_excludes_observation_st_and_restricted_boards(run_with_signals):
    run_id, _ = run_with_signals
    batch_id = svc.select_recommendations(run_id, "short")
    batch = repo.get_batch(batch_id)
    assert batch["status"] == "draft" and batch["cohort"] == "production"
    codes = [it["code"] for it in batch["items"]]
    assert codes == ["600001"]  # 仅主板非 ST 的正式短线信号入选
    reasons = {e["code"]: e["reason"] for e in batch["exclusions"]}
    assert reasons["600002"] == "st_or_delisting"
    assert reasons["300003"] == "board_restricted"
    assert reasons["600004"] in {"not_in_list"}


def test_primary_signal_dedup_keeps_momentum_and_records_support(run_with_signals):
    run_id, _ = run_with_signals
    batch = repo.get_batch(svc.select_recommendations(run_id, "short"))
    item = batch["items"][0]
    assert item["reason"]["strategy_key"] == "next_day_momentum"
    assert [s["strategy_key"] for s in item["support"]] == ["short_composite"]
    # 冻结计划保留原始参考价与止损止盈，不改基准。
    assert item["plan"]["entry_target"] == 10.0 and item["plan"]["stop_loss"] == 9.4


def test_observation_list_uses_separate_slots(run_with_signals):
    run_id, _ = run_with_signals
    prod = repo.get_batch(svc.select_recommendations(run_id, "short"))
    surge = repo.get_batch(svc.select_recommendations(run_id, "observe_surge"))
    first = repo.get_batch(svc.select_recommendations(run_id, "observe_first_reversal"))
    assert surge["cohort"] == "observation" and [i["code"] for i in surge["items"]] == ["600004"]
    assert [i["code"] for i in first["items"]] == ["600004"]
    # 观察线不占正式名额：正式短线仍只有 600001。
    assert [i["code"] for i in prod["items"]] == ["600001"]


def test_regime_cap_limits_slots(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    block = store.put("daily_price", "2026-01-05", [{"code": "600001", "trade_date": "2026-01-05", "close": 10}])
    codes = [f"60000{i}" for i in range(1, 6)]
    context = RunContext.create(
        scan_date="2026-01-05", as_of="2026-01-05", scope="all", codes=codes,
        parameters={"effective": {"short_conf_gate": 22.0, "short_top_n": 3},
                    "strategy_config": {"RECO_REGIME_CAP": {"short": {"cool": 2}}}, "runtime": {}},
        version={"commit": "t"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block], "schemas": {}, "source": "stock_signal"},
        quality={"market_state": "cool", "data_ready": True})
    records = [_record(c, "隔日动量", fusion=30 + i) for i, c in enumerate(codes)]
    run_id = persist_signal_run(context, records, status="complete", store=store)
    batch = repo.get_batch(svc.select_recommendations(run_id, "short"))
    assert batch["policy"]["cap"] == 2 and len(batch["items"]) == 2


def test_publish_then_frozen_read_is_idempotent(run_with_signals):
    run_id, _ = run_with_signals
    batch_id = svc.select_and_publish(run_id, "short")
    published = svc.get_published_batch("2026-01-05", "short")
    assert published["id"] == batch_id and published["status"] == "published"
    # 重复选择不新增批次、不追加条目。
    again = svc.select_recommendations(run_id, "short")
    assert again == batch_id
    assert len(repo.get_batch(again)["items"]) == len(published["items"])
    # 再次发布已发布批次幂等，不报错。
    assert svc.publish_batch(batch_id) == batch_id


def test_republish_supersedes_and_blocks_after_fill(run_with_signals):
    run_id, _ = run_with_signals
    first = svc.select_and_publish(run_id, "short")
    # 新参数产生新草稿（不同 policy_hash），显式重新发布替代旧批次。
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM recommendation_batch WHERE id=?", (first,)).fetchone()
    second = repo.create_draft_batch(
        run_id=run_id, scan_date="2026-01-05", list_key="short", cohort="production",
        policy={"rev": 2}, market_state="warm", source="test")
    sig_600001 = next(s for s in get_run_signals(run_id)
                      if s["code"] == "600001" and s["horizon"] == "short")
    repo.add_items(second, [{"signal_id": sig_600001["id"], "code": "600001",
                             "horizon": "short", "rank": 1, "reason": {}, "plan": {}, "support": []}])
    # 旧批次已产生模拟成交 → 拒绝重新发布。
    item_id = repo.get_batch(first)["items"][0]["id"]
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO simulated_trade(id,first_recommendation_id,code,horizon,list_key,cohort,"
            "execution_json,merge_policy,status,exec_entry_date,exec_entry_price) "
            "VALUES ('t',?,'600001','short','short','production','{}','consecutive','holding','2026-01-06',10)",
            (item_id,))
    with pytest.raises(repo.BatchConflictError, match="模拟成交"):
        repo.republish_batch(second)


def test_publish_run_lists_covers_stock_signal_lists(run_with_signals):
    run_id, _ = run_with_signals
    published = svc.publish_run_lists(run_id)
    # stock_signal 运行发布短/中/长 + 三条观察线，共 6 榜（deep 属深度运行，不在此）。
    assert set(published) == {"short", "mid", "long",
                              "observe_first_reversal", "observe_surge", "observe_pullback"}
    assert all(isinstance(v, str) for v in published.values())
    short_batch = repo.get_batch(published["short"])
    assert short_batch["status"] == "published" and [i["code"] for i in short_batch["items"]] == ["600001"]
    # 重复发布幂等：返回同一批次 ID，不新增。
    assert svc.publish_run_lists(run_id) == published


def test_publish_run_lists_comparison_mode_is_bypass(run_with_signals):
    run_id, _ = run_with_signals
    published = svc.publish_run_lists(run_id, comparison=True)
    # 旁路对照批次落 comparison 状态，不冒充已向用户发布，也不占用 published 唯一槽。
    assert repo.get_batch(published["short"])["status"] == "comparison"
    assert svc.get_published_batch("2026-01-05", "short") is None


def test_failed_run_cannot_generate_list(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    block = store.put("daily_price", "2026-01-05", [])
    context = RunContext.create(
        scan_date="2026-01-05", as_of="2026-01-05", scope="all", codes=["600001"],
        parameters={"effective": {}, "strategy_config": {}, "runtime": {}}, version={"commit": "t"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block], "schemas": {}, "source": "stock_signal"},
        quality={"data_ready": False})
    run_id = persist_signal_run(context, [], status="failed", store=store)
    with pytest.raises(ValueError, match="失败运行"):
        svc.select_recommendations(run_id, "short")


def test_empty_list_still_publishes_traceable_batch(tmp_path):
    store = SnapshotStore(tmp_path / "s")
    block = store.put("daily_price", "2026-01-05", [{"code": "600001", "trade_date": "2026-01-05", "close": 10}])
    context = RunContext.create(
        scan_date="2026-01-05", as_of="2026-01-05", scope="all", codes=["600001"],
        parameters={"effective": {"short_conf_gate": 22.0, "short_top_n": 3},
                    "strategy_config": {"RECO_REGIME_CAP": {"short": {"cold": 0}}}, "runtime": {}},
        version={"commit": "t"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block], "schemas": {}, "source": "stock_signal"},
        quality={"market_state": "cold", "data_ready": True})
    run_id = persist_signal_run(context, [_record("600001", "隔日动量", fusion=40)],
                                status="complete", store=store)
    batch_id = svc.select_and_publish(run_id, "short")
    batch = svc.get_published_batch("2026-01-05", "short")
    assert batch["id"] == batch_id and batch["items"] == [] and batch["policy"]["cap"] == 0
