"""迁移与测试护栏、统计语义固定用例；只使用隔离数据库。"""
import sqlite3
import subprocess
import sys

import pytest
from core import db


def test_init_is_idempotent_and_preserves_history():
    with db.get_conn() as conn:
        conn.execute("CREATE TABLE stock_score (evidence TEXT)")
        conn.execute("INSERT INTO stock_score VALUES ('legacy')")
    db.init_db()
    db.init_db()
    with db.get_conn() as conn:
        assert conn.execute("SELECT evidence FROM stock_score").fetchone()[0] == "legacy"
        assert conn.execute("SELECT COUNT(*) FROM schema_migration WHERE version='001_safe_baseline'").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_failure_rolls_back(tmp_path, monkeypatch):
    target = tmp_path / "failure.db"
    monkeypatch.setattr(db, "DB_PATH", str(target))
    with db.get_conn() as conn:
        conn.execute("CREATE TABLE evidence (id INTEGER)")
        conn.execute("INSERT INTO evidence VALUES (42)")
    def fail(*args):
        raise sqlite3.OperationalError("注入迁移失败")
    monkeypatch.setattr(db, "_safe_add_column", fail)
    with pytest.raises(sqlite3.OperationalError, match="注入迁移失败"):
        db.init_db()
    with db.get_conn(readonly=True) as conn:
        assert conn.execute("SELECT id FROM evidence").fetchone()[0] == 42
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migration'").fetchone() is None


def test_safe_add_column_propagates_error():
    with db.get_conn() as conn:
        with pytest.raises(sqlite3.OperationalError):
            db._safe_add_column(conn, "absent_table", "field", "TEXT")


def test_readonly_never_creates_or_writes(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        with db.get_conn(readonly=True, path=tmp_path / "absent.db"):
            pass
    assert not (tmp_path / "absent.db").exists()
    with db.get_conn(readonly=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden (id INTEGER)")


def test_production_guard_covers_direct_connections():
    from config.settings import PRODUCTION_DB_PATH
    with pytest.raises(RuntimeError, match="测试禁止连接"):
        sqlite3.connect(PRODUCTION_DB_PATH)


def test_import_app_has_no_runtime_side_effects(tmp_path, monkeypatch):
    path = tmp_path / "import.db"
    monkeypatch.setenv("AIQUANT_DB_PATH", str(path))
    result = subprocess.run([sys.executable, "-c",
        "import threading; before=set(threading.enumerate()); import app; "
        "assert set(threading.enumerate()) == before; "
        "assert app.app.test_client().get('/api/health').status_code == 200"],
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert not path.exists()


def test_test_mode_blocks_network_and_scheduler():
    import socket
    from scheduler.runner import start_scheduler
    from scheduler.state import SCHEDULER_THREAD
    with pytest.raises(RuntimeError, match="测试禁止外网"):
        socket.getaddrinfo("example.com", 443)
    start_scheduler()
    assert SCHEDULER_THREAD["t"] is None


def test_settlement_does_not_use_unclosed_diagnostics():
    from core.outcome_tracker import get_merged_summary
    items = [
        {"exit_reason": "stop_loss", "exit_date": "2026-01-06", "exit_return": -5,
         "t1_return": -1, "t3_return": -2},
        {"exit_reason": None, "exit_date": None, "exit_return": None,
         "t1_return": 5, "t5_return": 9},
    ]
    result = get_merged_summary(items=items)
    assert result["win_rate"] == 0
    assert result["settled_count"] == 1
    assert result["unsettled_count"] == 1
    assert result["t1"]["n"] == 2
    assert result["t3"]["n"] == 1
    assert result["t5"]["win_rate"] == 100


def test_profit_factor_and_average_ratio_are_distinct():
    from core.outcome_tracker import settlement_stats
    result = settlement_stats([{"exit_date": "2026-01-06", "exit_reason": "max_hold", "exit_return": v} for v in [2, 2, -1]])
    assert result["profit_factor"] == 4
    assert result["average_win_loss_ratio"] == 2
    assert settlement_stats([])["profit_factor"] is None


def test_optimizer_ignores_observation_and_unsettled():
    from strategy.optimizer import run_daily_diagnosis, run_weekly_tuning
    with db.get_conn() as conn:
        conn.executemany("INSERT INTO recommend_outcome(code,scan_date,horizon,strategy,entry_price,exit_return,exit_date,exit_reason,t5_return) VALUES (?,date('now'),'short',?,10,?,? ,?,?)", [
            ("600001", "隔日动量", -5, "2026-01-06", "stop_loss", -4),
            ("600002", "隔日动量", None, None, None, 10),
            ("600003", "反转首日", 15, "2026-01-06", "max_hold", 12)])
    result = run_daily_diagnosis()["summary"]["short"]
    assert result["win_rate"] == 0
    assert result["signal_count"] == 2
    assert run_weekly_tuning()["sample"] == 1


def test_api_http_alias_and_reserved_fields():
    from app import create_app
    from utils.api import fail, ok
    app = create_app({"TESTING": True})
    with app.test_request_context():
        response, status = fail("失败", http=503)
        assert status == response.json["code"] == 503
        assert response.json["error"] == response.json["message"]
        with pytest.raises(ValueError):
            fail("失败", 404, http=500)
        with pytest.raises(ValueError):
            ok([], success=False)
