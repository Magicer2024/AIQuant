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
                 max_return, min_return, hit_stop, hit_tp, exit_return)
            VALUES (?, date('now', ?), 'short', '短线融合', 10.0, ?,
                    ?, ?, ?, ?, ?, ?, ?, 0, ?)
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

def _insert_suggestion(param_key="sig_threshold", suggest="17.0", current="15.0"):
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO param_suggestion
                (created_date, param_key, current_value, suggest_value, reason, metrics_json)
            VALUES (date('now'), ?, ?, ?, '测试建议', '{}')
        """, (param_key, current, suggest))
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
