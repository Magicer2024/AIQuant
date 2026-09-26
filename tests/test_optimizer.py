"""tests/test_optimizer.py —— 策略优化器（参数覆盖层 + 诊断 + 建议采纳/回滚）

使用 monkeypatch 将 core.db.DB_PATH 指向临时库，隔离运行，不写生产 quant.db。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest

import core.db as db
from core.db import get_conn, init_db
from config.strategy_params import (
    get_param, invalidate_param_cache, TUNABLE_PARAMS,
)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把数据库句柄重定向到临时文件，并清空参数覆盖缓存"""
    db_file = tmp_path / "quant_test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    init_db()
    invalidate_param_cache()
    yield str(db_file)
    invalidate_param_cache()


# ─────────────────────────────────────────────
# 参数覆盖层
# ─────────────────────────────────────────────

def test_get_param_default(tmp_db):
    assert get_param("sig_threshold") == TUNABLE_PARAMS["sig_threshold"]["default"]
    assert get_param("short_stop_loss") == -0.05


def test_get_param_override_and_clamp(tmp_db):
    with get_conn() as conn:
        conn.execute("INSERT INTO strategy_param_override (param_key, value) VALUES (?, ?)",
                     ("sig_threshold", "17.0"))
        conn.execute("INSERT INTO strategy_param_override (param_key, value) VALUES (?, ?)",
                     ("short_stop_loss", "-0.50"))  # 越界，应截断到 min=-0.12
    invalidate_param_cache()
    assert get_param("sig_threshold") == 17.0
    assert get_param("short_stop_loss") == -0.12


def test_get_param_bad_value_falls_back(tmp_db):
    with get_conn() as conn:
        conn.execute("INSERT INTO strategy_param_override (param_key, value) VALUES (?, ?)",
                     ("trend_gate_ma", "not-a-number"))
    invalidate_param_cache()
    assert get_param("trend_gate_ma") == TUNABLE_PARAMS["trend_gate_ma"]["default"]


# ─────────────────────────────────────────────
# 每日诊断
# ─────────────────────────────────────────────

def _insert_outcomes(rows):
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO recommend_outcome
                (code, scan_date, horizon, strategy, entry_price, fusion_score,
                 t1_return, t3_return, t5_return, t10_return,
                 max_return, min_return, hit_stop, hit_tp, exit_return, exit_date, exit_reason)
            VALUES (?, date('now', ?), 'short', '短线融合', 10.0, ?,
                    ?, ?, ?, ?, ?, ?, ?, 0, ?, date('now'), 'max_hold')
        """, rows)


def test_daily_diagnosis_summary(tmp_db):
    from strategy.optimizer import run_daily_diagnosis
    # 6 赢 4 输（exit_return 口径），其中 2 条止损
    rows = []
    for i in range(6):
        rows.append((f"60000{i}", f"-{i+1} days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 3.0))
    for i in range(4):
        rows.append((f"00000{i}", f"-{i+1} days", 20.0, -2.0, -3.0, -4.0, -4.5, 1.0, -9.0,
                     1 if i < 2 else 0, -5.0))
    _insert_outcomes(rows)

    payload = run_daily_diagnosis()
    short = payload["summary"]["short"]
    assert short["total"] == 10
    assert short["win_rate"] == 60.0
    assert short["stop_hit"] == 2
    assert isinstance(payload["findings"], list)

    # 报告落库
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM optimizer_report WHERE report_type='diagnosis'"
        ).fetchone()
    assert row is not None
    assert json.loads(row["payload_json"])["summary"]["short"]["total"] == 10


def test_weekly_tuning_skips_on_small_sample(tmp_db):
    from strategy.optimizer import run_weekly_tuning
    payload = run_weekly_tuning()
    assert "skipped" in payload
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM optimizer_report WHERE report_type='tuning'").fetchone()
    assert row is not None
    # 未产出建议
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM param_suggestion").fetchone()[0]
    assert n == 0


# ─────────────────────────────────────────────
# 建议采纳 / 回滚
# ─────────────────────────────────────────────

def _insert_suggestion(param_key="sig_threshold", suggest="17.0", current="15.0",
                       baseline_hash=None):
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO param_suggestion
                (created_date, param_key, current_value, suggest_value, reason,
                 metrics_json, baseline_hash)
            VALUES (date('now'), ?, ?, ?, '测试建议', '{}', ?)
        """, (param_key, current, suggest, baseline_hash))
        return cur.lastrowid


def test_apply_suggestion_writes_override_and_log(tmp_db):
    from strategy.optimizer import apply_suggestion
    sid = _insert_suggestion()
    result = apply_suggestion(sid)
    assert result["new_value"] == "17.0"
    assert get_param("sig_threshold") == 17.0

    with get_conn() as conn:
        sug = conn.execute("SELECT status FROM param_suggestion WHERE id=?", (sid,)).fetchone()
        log = conn.execute("SELECT * FROM param_tune_log WHERE param_key='sig_threshold'").fetchone()
    assert sug["status"] == "applied"
    assert log["action"] == "apply"
    assert log["old_value"] == "15.0"


def test_apply_nonexistent_suggestion_raises(tmp_db):
    from strategy.optimizer import apply_suggestion
    with pytest.raises(ValueError):
        apply_suggestion(9999)


def test_rollback_restores_default(tmp_db):
    from strategy.optimizer import apply_suggestion, rollback_param
    sid = _insert_suggestion()
    apply_suggestion(sid)
    assert get_param("sig_threshold") == 17.0

    result = rollback_param("sig_threshold")
    assert float(result["restored_value"]) == 15.0
    assert get_param("sig_threshold") == 15.0
    # 调整前就是默认值 → 覆盖行应被删除
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM strategy_param_override WHERE param_key='sig_threshold'"
        ).fetchone()
    assert row is None


def test_dismiss_suggestion(tmp_db):
    from strategy.optimizer import dismiss_suggestion
    sid = _insert_suggestion()
    dismiss_suggestion(sid)
    with get_conn() as conn:
        sug = conn.execute("SELECT status FROM param_suggestion WHERE id=?", (sid,)).fetchone()
    assert sug["status"] == "dismissed"
    # 未生效
    assert get_param("sig_threshold") == 15.0


def test_get_latest_state(tmp_db):
    from strategy.optimizer import run_daily_diagnosis, get_latest_state
    _insert_outcomes([("600000", "-1 days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 3.0)])
    run_daily_diagnosis()
    _insert_suggestion()
    state = get_latest_state()
    assert state["diagnosis"] is not None
    assert len(state["suggestions"]) == 1
    assert "sig_threshold" in state["params"]
    # 版本 + 基线哈希 surface（bullet 7）；无基线哈希的历史建议 stale=False（向后兼容）
    assert state["param_version"] == 0
    assert isinstance(state["baseline_hash"], str) and state["baseline_hash"]
    assert state["suggestions"][0]["stale"] is False


# ─────────────────────────────────────────────
# 方案 E3：诊断分组 / 观察线分离 / 冻结管道寻优降级 / 采纳追溯
# ─────────────────────────────────────────────

def test_diagnosis_groups_and_metadata(tmp_db):
    """正式诊断按周期/策略分组，并打参数版本 + 基线哈希（bullet 1/7）。"""
    from strategy.optimizer import run_daily_diagnosis
    rows = [(f"60000{i}", f"-{i+1} days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 3.0)
            for i in range(6)]
    rows += [(f"00000{i}", f"-{i+1} days", 20.0, -2.0, -3.0, -4.0, -4.5, 1.0, -9.0, 0, -5.0)
             for i in range(4)]
    _insert_outcomes(rows)
    payload = run_daily_diagnosis()
    short = payload["summary"]["short"]
    # 按策略再分组
    assert "by_strategy" in short
    assert short["by_strategy"]["短线融合"]["n"] == 10
    assert short["by_strategy"]["短线融合"]["win_rate"] == 60.0
    # 版本 + 基线哈希追溯
    assert payload["param_version"] == 0
    assert isinstance(payload["baseline_hash"], str) and len(payload["baseline_hash"]) >= 8
    # 观察线单独诊断、明确标注不入正式门槛
    assert payload["observation"]["not_formal"] is True
    assert "不进入正式样本门槛" in payload["observation"]["note"]


def test_observation_line_separate_from_formal(tmp_db):
    """观察策略（反转首日等）单独汇总，绝不进入正式样本门槛（bullet 1）。"""
    from strategy.optimizer import run_daily_diagnosis
    _insert_outcomes([
        ("600001", "-1 days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 3.0),
        ("600002", "-2 days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 2.0),
        ("600003", "-3 days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 1.0),
    ])
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO recommend_outcome
                (code, scan_date, horizon, strategy, entry_price, fusion_score,
                 t1_return, t3_return, t5_return, t10_return, max_return, min_return,
                 hit_stop, hit_tp, exit_return, exit_date, exit_reason)
            VALUES (?, date('now', ?), 'short', '反转首日', 10.0, 20.0,
                    1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 0, ?, date('now'), 'max_hold')
        """, [("600101", "-1 days", 5.0), ("600102", "-2 days", 6.0)])
    payload = run_daily_diagnosis()
    # 正式样本只含 3 条融合线；观察线被 LEGACY_PRODUCTION_FILTER 排除
    assert payload["summary"]["short"]["total"] == 3
    assert "反转首日" not in payload["summary"]["short"]["by_strategy"]
    # 观察线单独汇总 2 条
    assert payload["observation"]["by_strategy"]["反转首日"]["n"] == 2


def test_weekly_tuning_diagnosis_only_in_legacy(tmp_db, monkeypatch):
    """样本充足但 legacy 无冻结回放 ⇒ 仅诊断、不产出可采纳建议（bullet 2/8）。"""
    import config.settings as settings
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "legacy")
    from strategy.optimizer import run_weekly_tuning
    rows = [(f"6000{i:02d}", f"-{i+1} days", 30.0, 1.0, 2.0, 3.0, 3.5, 4.0, -1.0, 0, 3.0)
            for i in range(16)]
    _insert_outcomes(rows)
    payload = run_weekly_tuning()
    assert payload["diagnosis_only"] is True
    assert payload["sample"] >= 15
    assert "不具备完整执行回放条件" in payload["skipped"]
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM param_suggestion").fetchone()[0]
    assert n == 0


def test_frozen_pipeline_unavailable_in_legacy(tmp_db, monkeypatch):
    import config.settings as settings
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "legacy")
    from strategy.optimizer import _frozen_pipeline_available
    with get_conn() as conn:
        available, why = _frozen_pipeline_available(conn, "2026-01-01")
    assert available is False
    assert "legacy" in why


def test_param_formal_line_applicability(tmp_db):
    """参数 → 正式信号线映射（bullet 3）：正式线返回 list_key，未登记返回 None。"""
    from strategy.optimizer import _param_formal_line, _production_lines
    prods = _production_lines()
    assert "short" in prods and "deep" in prods
    assert _param_formal_line("sig_threshold") == "short"
    assert _param_formal_line("deep_atr_stop_cap") == "deep"
    # 未登记映射的参数 → None（不适用于正式推荐）
    assert _param_formal_line("some_unregistered_param") is None


def test_apply_suggestion_stale_baseline_expires(tmp_db):
    """采纳前基线校验：基线哈希不一致 ⇒ 标记 expired 并拒绝（bullet 7）。"""
    from strategy.optimizer import apply_suggestion
    sid = _insert_suggestion(baseline_hash="stale_hash_deadbeef")
    with pytest.raises(ValueError):
        apply_suggestion(sid)
    with get_conn() as conn:
        sug = conn.execute("SELECT status FROM param_suggestion WHERE id=?", (sid,)).fetchone()
        n_log = conn.execute("SELECT COUNT(*) FROM param_tune_log").fetchone()[0]
    assert sug["status"] == "expired"
    assert n_log == 0            # 未写覆盖层/审计
    assert get_param("sig_threshold") == 15.0   # 参数未变


def test_apply_suggestion_matching_baseline_bumps_version(tmp_db):
    """基线一致 ⇒ 采纳成功并生成新参数版本（bullet 7）。"""
    from strategy.optimizer import apply_suggestion, current_param_version
    from config.strategy_params import compute_params_baseline_hash
    sid = _insert_suggestion(baseline_hash=compute_params_baseline_hash())
    with get_conn() as conn:
        assert current_param_version(conn) == 0
    result = apply_suggestion(sid)
    assert result["param_version"] == 1
    assert get_param("sig_threshold") == 17.0
    with get_conn() as conn:
        assert current_param_version(conn) == 1
        log = conn.execute(
            "SELECT new_version FROM param_tune_log WHERE action='apply'").fetchone()
    assert log["new_version"] == 1


def test_rollback_bumps_version(tmp_db):
    """回滚也生成新参数版本，保持版本单调（bullet 7）。"""
    from strategy.optimizer import apply_suggestion, rollback_param, current_param_version
    sid = _insert_suggestion()
    apply_suggestion(sid)
    result = rollback_param("sig_threshold")
    assert result["param_version"] == 2
    with get_conn() as conn:
        assert current_param_version(conn) == 2


# ── 纯决策助手（无需 DB）：切分 / 门槛 ──────────────────

def test_split_train_test_drops_cross_boundary():
    """持仓跨分界（entry < 界 <= exit）剔除，避免同笔在两段落复用（bullet 5）。"""
    from strategy.optimizer import _split_train_test
    trades = [
        {"entry_date": "2026-01-01", "exit_date": "2026-01-05", "ret": 1.0},  # train
        {"entry_date": "2026-02-01", "exit_date": "2026-02-05", "ret": 2.0},  # test
        {"entry_date": "2026-01-20", "exit_date": "2026-02-03", "ret": 3.0},  # 跨分界
    ]
    train, test, dropped = _split_train_test(trades, "2026-02-01")
    assert dropped == 1
    assert [t["ret"] for t in train] == [1.0]
    assert [t["ret"] for t in test] == [2.0]


def test_settled_metric_and_score_segments():
    from strategy.optimizer import _settled_metric, _score_segments
    assert _settled_metric([]) == {"n": 0, "win": None, "avg": None}
    m = _settled_metric([{"ret": 1.0}, {"ret": -1.0}, {"ret": 2.0}, {"ret": None}])
    assert m["n"] == 3 and m["win"] == round(2 / 3 * 100, 2)
    # <10 样本 → None
    assert _score_segments(
        [{"entry_date": "2026-01-01", "exit_date": "2026-01-02", "ret": 1.0}] * 5) is None
    # 样本够但日期跨度不足（<10 distinct）→ None
    assert _score_segments(
        [{"entry_date": "2026-01-01", "exit_date": "2026-01-02", "ret": 1.0}] * 12) is None
    # 充足样本 + 足够跨度 → 分段
    trades = [{"entry_date": f"2026-01-{d:02d}", "exit_date": f"2026-01-{d:02d}",
               "ret": float(d % 3 - 1)} for d in range(1, 21)]
    seg = _score_segments(trades)
    assert seg is not None and seg["all"]["n"] == 20
    assert seg["train"]["n"] + seg["test"]["n"] + seg["dropped_cross"] == 20
    assert seg["boundary"] == "2026-01-15"


def test_better_settled_threshold():
    """train 选优 / test 确认 + 整体改善幅度门槛（bullet 5/6）。"""
    from strategy.optimizer import _better_settled
    base = {"all": {"n": 30, "win": 50.0, "avg": 0.1},
            "train": {"n": 15, "win": 50.0, "avg": 0.1},
            "test": {"n": 15, "win": 50.0, "avg": 0.1}}
    # 整体 +5pp、train/test 均不劣 ⇒ 通过
    assert _better_settled(
        {"all": {"n": 30, "win": 55.0, "avg": 0.1},
         "train": {"n": 15, "win": 52.0, "avg": 0.12},
         "test": {"n": 15, "win": 53.0, "avg": 0.11}}, base) is True
    # test 段劣于基线 ⇒ 拒绝
    assert _better_settled(
        {"all": {"n": 30, "win": 55.0, "avg": 0.1},
         "train": {"n": 15, "win": 60.0, "avg": 0.2},
         "test": {"n": 15, "win": 40.0, "avg": 0.0}}, base) is False
    # 改善幅度不足（整体 <+1pp 且均值 <+0.05）⇒ 拒绝
    assert _better_settled(
        {"all": {"n": 30, "win": 50.5, "avg": 0.11},
         "train": {"n": 15, "win": 50.0, "avg": 0.1},
         "test": {"n": 15, "win": 50.0, "avg": 0.1}}, base) is False
    # 验证段样本不足（test n<10）⇒ 拒绝
    assert _better_settled(
        {"all": {"n": 30, "win": 60.0, "avg": 0.3},
         "train": {"n": 15, "win": 60.0, "avg": 0.3},
         "test": {"n": 5, "win": 60.0, "avg": 0.3}}, base) is False
