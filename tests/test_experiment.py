"""C3 筹码影子与参数实验：同 run 同资格、仅排序参数不同、实验编号冻结、整池无筹码跳过。"""
import pytest

from core.input_snapshot import RunContext, SnapshotStore
from core.repository.signal_repo import persist_signal_run
from core import recommendation_service as svc
from core import experiment as exp
from core.repository import recommendation_repo as repo


def _rec(code, *, ext, chip, fusion=30.0, strategy="短线融合", buy=10.0):
    return {"code": code, "name": "测试股", "strategy": strategy, "horizon": "short",
            "scan_date": "2026-01-05", "buy_price": buy, "price": buy,
            "stop_loss": 9.0, "take_profit": 12.0, "fusion_score": fusion,
            "pct_above_ma20": ext, "chip_conc": chip}


def _params(**eff):
    base = {"short_conf_gate": 22.0, "short_top_n": 2, "short_pullback_entry": 0,
            "long_lowvol_sort_enabled": 0}
    base.update(eff)
    return {"effective": base, "strategy_config": {"RECO_REGIME_CAP": {}}, "runtime": {}}


def _run(tmp_path, records, codes, params):
    store = SnapshotStore(tmp_path / "s")
    block = store.put("daily_price", "2026-01-05", [{"code": codes[0], "trade_date": "2026-01-05", "close": 10}])
    context = RunContext.create(
        scan_date="2026-01-05", as_of="2026-01-05", scope="all", codes=codes,
        parameters=params, version={"commit": "t"},
        manifest={"format": "jsonl-gzip-v1", "blocks": [block], "schemas": {}, "source": "stock_signal"},
        quality={"market_state": "warm", "data_ready": True, "cross_section_complete": True})
    records = [dict(r, scan_date="2026-01-05") for r in records]
    return persist_signal_run(context, records, source="stock_signal", status="complete", store=store)


@pytest.fixture
def chip_run(tmp_path):
    """三条同档融合信号：ext 与 chip 排序相反，使基线与筹码影子选出不同 top2。"""
    codes = ["600001", "600002", "600003"]
    records = [
        _rec("600001", ext=0.01, chip=0.9),   # 基线首选（ext 最低），筹码最差
        _rec("600002", ext=0.02, chip=0.5),   # 两者都入选（重叠）
        _rec("600003", ext=0.03, chip=0.1),   # 筹码首选，基线落选
    ]
    return _run(tmp_path, records, codes, _params())


def test_baseline_and_shadow_same_run_different_sort(chip_run):
    base_id = svc.select_and_publish(chip_run, "short")           # 基线：ext ASC
    shadow = exp.select_chip_shadow(chip_run, "short")            # 影子：chip ASC
    assert "batch_id" in shadow and shadow["with_chip"] == 3
    base = repo.get_batch(base_id)
    expb = repo.get_batch(shadow["batch_id"])
    # 同一 run、同资格（都排除到 3 选 2），唯一差异是排序参数。
    assert base["run_id"] == expb["run_id"] == chip_run
    assert [i["code"] for i in base["items"]] == ["600001", "600002"]      # ext 升序 top2
    assert [i["code"] for i in expb["items"]] == ["600003", "600002"]      # chip 升序 top2
    # 影子批次是 comparison 状态，不冒充正式发布；基线仍为唯一 published。
    assert expb["status"] == "comparison" and base["status"] == "published"
    assert svc.get_published_batch("2026-01-05", "short")["id"] == base_id


def test_experiment_config_frozen_in_batch(chip_run):
    svc.select_and_publish(chip_run, "short")
    shadow = exp.select_chip_shadow(chip_run, "short")
    expb = repo.get_batch(shadow["batch_id"])
    assert expb["experiment_id"] == exp.CHIP_SHADOW_EXPERIMENT_ID
    assert expb["policy"]["sort_variant"] == {"chip_first": True} and expb["policy"]["chip_first"] is True
    # 基线批次 chip_first=False（冻结开关默认关）。
    base = repo.get_batch(svc.get_published_batch("2026-01-05", "short")["id"])
    assert base["policy"]["chip_first"] is False and base["experiment_id"] == ""


def test_skip_when_pool_has_no_chip_data(tmp_path):
    codes = ["600001", "600002"]
    records = [_rec("600001", ext=0.01, chip=None), _rec("600002", ext=0.02, chip=None)]
    run_id = _run(tmp_path, records, codes, _params())
    svc.select_and_publish(run_id, "short")
    result = exp.select_chip_shadow(run_id, "short")
    # 整池无筹码 → 跳过并记录原因，不生成假对照批次。
    assert result["skipped"] == "no_chip_data" and result["total"] == 2
    assert repo.get_published_batch("2026-01-05", "short", cohort="production",
                                    experiment_id=exp.CHIP_SHADOW_EXPERIMENT_ID) is None


def test_shadow_enabled_by_default(chip_run):
    svc.select_and_publish(chip_run, "short")
    result = exp.select_chip_shadow(chip_run, "short")
    assert "batch_id" in result   # 冻结开关默认开启（short_chip_shadow_enabled 缺省=1）


def test_skip_when_shadow_switch_disabled(tmp_path):
    """运行冻结 short_chip_shadow_enabled=0 → 停记，不建对照批次。"""
    codes = ["600001", "600002", "600003"]
    records = [
        _rec("600001", ext=0.01, chip=0.9),
        _rec("600002", ext=0.02, chip=0.5),
        _rec("600003", ext=0.03, chip=0.1),
    ]
    run_id = _run(tmp_path, records, codes, _params(short_chip_shadow_enabled=0))
    svc.select_and_publish(run_id, "short")
    result = exp.select_chip_shadow(run_id, "short")
    assert result["skipped"] == "shadow_disabled"
    # 停记时不得生成任何 comparison 对照批次。
    assert repo.get_published_batch("2026-01-05", "short", cohort="production",
                                    experiment_id=exp.CHIP_SHADOW_EXPERIMENT_ID) is None


def test_chip_shadow_report_overlap(chip_run):
    svc.select_and_publish(chip_run, "short")
    exp.select_chip_shadow(chip_run, "short")
    report = exp.chip_shadow_report(scan_date="2026-01-05", list_key="short")
    assert report["n_dates"] == 1
    day = report["dates"][0]
    # 重叠 = {600002} / 2 = 0.5，直接反映排序换票程度。
    assert day["overlap"] == 0.5 and day["overlap_n"] == 1
    assert report["mean_overlap"] == 0.5 and report["full_overlap_dates"] == 0
    # 基线与实验成绩分开返回，不混算。
    assert "baseline" in report and "experiment" in report
    assert report["note"]
