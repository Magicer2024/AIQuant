"""
tests/test_backtest_recovery.py —— 回测结果可恢复（方案 E1）

覆盖验收：
  - 清空任务内存/重启后，完整新结果仍可查看、分页、导出、存为规则；
  - 取消与失败不会产生已成功成绩（不写 backtest_results）；
  - 结果持久化失败时任务不标 done、不回写规则 fitness；
  - 旧记录（task_id 混写在 rule_id、无 result_json）兼容读取，缺失字段不凭空补出；
  - 执行配置/数据版本随结果持久化并可恢复。
"""
from __future__ import annotations

import json
import threading

import pytest

import backtest.service as svc
from core.db import get_conn


# ──────────── 测试夹具与助手 ────────────

def _fake_result(task_id: str) -> dict:
    """构造一份与引擎输出同构的成功结果（含权益曲线/未平仓/两笔交易）。"""
    return {
        "success": True,
        "cancelled": False,
        "start_date": "2025-01-01",
        "end_date": "2025-06-01",
        "init_cash": 100000,
        "final_assets": 110000,
        "total_return": 0.10,
        "annual_return": 0.20,
        "max_drawdown": -0.05,
        "sharpe_ratio": 1.2,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_win_pct": 0.1,
        "avg_loss_pct": 0.1,
        "avg_hold_days": 42.5,
        "max_consecutive_wins": 1,
        "max_consecutive_losses": 1,
        "total_trades": 2,
        "win_trades": 1,
        "loss_trades": 1,
        "equity_curve": [
            {"date": "2025-01-01", "total": 100000, "cash": 100000,
             "position_value": 0, "position_count": 0},
            {"date": "2025-06-01", "total": 110000, "cash": 110000,
             "position_value": 0, "position_count": 0},
        ],
        "monthly_returns": [{"month": "2025-01", "return": 0.05}],
        "positions": [{"code": "600000", "name": "X", "shares": 100}],
        "trades": [
            {"code": "600000", "name": "X", "buy_date": "2025-01-02", "buy_price": 10.0,
             "sell_date": "2025-02-01", "sell_price": 11.0, "shares": 100, "pnl": 90.0,
             "pnl_pct": 0.10, "hold_days": 30, "exit_reason": "take_profit"},
            {"code": "000001", "name": "Y", "buy_date": "2025-01-05", "buy_price": 20.0,
             "sell_date": "2025-03-01", "sell_price": 18.0, "shares": 200, "pnl": -400.0,
             "pnl_pct": -0.10, "hold_days": 55, "exit_reason": "stop_loss"},
        ],
        "stock_pool_size": 2,
        "trade_days": 100,
        "task_id": task_id,
    }


def _params() -> svc.BacktestParams:
    return svc.BacktestParams(
        start_date="2025-01-01", end_date="2025-06-01",
        stop_loss_pct=-0.10, take_profit_pct=0.10, max_hold_days=60)


def _payload() -> dict:
    return {
        "params": {"start_date": "2025-01-01", "end_date": "2025-06-01",
                   "stop_loss_pct": -0.10, "take_profit_pct": 0.10},
        "conditions": [{"indicator": "ma_cross", "operator": "golden_cross",
                        "params": {"short_period": 5, "long_period": 20}}],
    }


def _register_task(task_id: str, payload: dict | None = None) -> None:
    """在内存任务表登记一个 running 任务（绕过 create_task 的异步线程）。"""
    with svc._LOCK:
        svc._TASKS[task_id] = {
            "id": task_id, "status": "running", "progress": 0, "stage": "init",
            "message": "", "started_at": "2025-06-01T09:00:00",
            "params": payload if payload is not None else _payload(),
            "result": None, "result_id": None, "error": None,
            "cancelled": False, "cancel_flag": threading.Event(),
        }


def _clear_memory() -> None:
    with svc._LOCK:
        svc._TASKS.clear()


# ──────────── 持久化 + 内存清空后恢复 ────────────

def test_save_result_persists_full_payload():
    task_id = "t_persist_full"
    rid = svc._save_result(_fake_result(task_id), "恢复测试", payload=_payload(),
                           actual_rule_id=None, params=_params(), data_version="2025-06-01")
    assert rid is not None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT task_id, actual_rule_id, params_json, conditions_json, "
            "execution_config_json, data_version, result_json, status "
            "FROM backtest_results WHERE id=?", (rid,)).fetchone()
    assert row["task_id"] == task_id
    assert row["actual_rule_id"] is None
    assert row["status"] == "done"
    assert row["data_version"] == "2025-06-01"
    exec_cfg = json.loads(row["execution_config_json"])
    assert exec_cfg["lot_size"] == 100
    assert exec_cfg["settlement"] == "T+1"
    assert exec_cfg["min_commission"] == 5.0
    rj = json.loads(row["result_json"])
    assert len(rj["equity_curve"]) == 2
    assert "trades" not in rj  # 交易明细单独入库，不在 result_json 重复
    # 交易明细含 shares/pnl
    with get_conn() as conn:
        trows = conn.execute(
            "SELECT shares, pnl FROM backtest_trades WHERE result_id=? ORDER BY id",
            (rid,)).fetchall()
    assert [t["shares"] for t in trows] == [100, 200]
    assert [t["pnl"] for t in trows] == [90.0, -400.0]


def test_result_recovers_after_memory_clear():
    task_id = "t_recover_1"
    svc._save_result(_fake_result(task_id), "恢复测试", payload=_payload(),
                     actual_rule_id=None, params=_params(), data_version="2025-06-01")
    _clear_memory()  # 模拟重启/内存清空

    rec = svc.get_task_result(task_id)
    assert rec is not None
    assert rec["_recovered"] is True
    assert rec["annual_return"] == 0.20
    assert rec["total_return"] == 0.10
    assert len(rec["equity_curve"]) == 2
    assert len(rec["trades"]) == 2
    assert rec["trades"][0]["shares"] == 100
    assert rec["_data_version"] == "2025-06-01"
    assert rec["_execution_config"]["lot_size"] == 100


def test_trades_pagination_recovers():
    task_id = "t_recover_page"
    svc._save_result(_fake_result(task_id), "分页", payload=_payload(),
                     actual_rule_id=None, params=_params())
    _clear_memory()
    page1 = svc.get_task_trades(task_id, page=1, page_size=1)
    assert page1["total"] == 2
    assert len(page1["trades"]) == 1
    assert page1["trades"][0]["code"] == "600000"
    page2 = svc.get_task_trades(task_id, page=2, page_size=1)
    assert page2["trades"][0]["code"] == "000001"


def test_payload_recovers_for_save_as_rule():
    task_id = "t_recover_payload"
    svc._save_result(_fake_result(task_id), "存规则", payload=_payload(),
                     actual_rule_id=None, params=_params())
    _clear_memory()
    pl = svc.get_task_payload(task_id)
    assert pl is not None
    assert pl["conditions"][0]["indicator"] == "ma_cross"
    assert pl["params"]["stop_loss_pct"] == -0.10
    # 可视化回测无 actual_rule_id → 不注入 rule_id（存为规则不被误拒）
    assert "rule_id" not in pl


def test_task_view_and_export_recover():
    task_id = "t_recover_export"
    svc._save_result(_fake_result(task_id), "导出", payload=_payload(),
                     actual_rule_id=None, params=_params())
    _clear_memory()
    t = svc.get_task(task_id)
    assert t["status"] == "done"
    assert t["_recovered"] is True
    csv_exp = svc.export_task_result(task_id, fmt="csv")
    assert csv_exp is not None
    assert "600000" in csv_exp["content"]
    assert "数量" in csv_exp["content"]
    json_exp = svc.export_task_result(task_id, fmt="json")
    assert json_exp is not None
    assert json.loads(json_exp["content"])["annual_return"] == 0.20


def test_list_history_prefers_task_id_column():
    task_id = "t_hist_1"
    svc._save_result(_fake_result(task_id), "历史", payload=_payload(),
                     actual_rule_id=None, params=_params())
    hist = svc.list_history(limit=10)
    item = next(h for h in hist if h["name"] == "历史")
    assert item["id"] == task_id
    assert item["task_id"] == task_id
    assert item["status"] == "done"


# ──────────── 旧记录兼容读取，缺失字段不凭空补出 ────────────

def test_legacy_record_readable_and_missing_fields_flagged():
    legacy_task = "legacy_task_abc"
    with get_conn() as conn:
        # 旧写法：task_id 混写进 rule_id，无 result_json/新列
        conn.execute(
            """INSERT INTO backtest_results
               (rule_id, rule_name, start_date, end_date, annual_return,
                cumulative_return, win_rate, sharpe_ratio, max_drawdown,
                total_trades, win_trades)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (legacy_task, "旧记录", "2024-01-01", "2024-06-01", 0.1, 0.2,
             0.5, 1.0, -0.05, 3, 2))
        conn.commit()
    hist = svc.list_history(limit=20)
    legacy = next(h for h in hist if h["name"] == "旧记录")
    # task_id 列为空 → id 回退 rule_id（兼容读取）
    assert legacy["id"] == legacy_task
    row = svc._load_result_row(legacy_task)
    assert row is not None
    rec = svc._rebuild_result_from_row(row)
    assert rec["annual_return"] == 0.1
    assert rec["total_trades"] == 3
    # 旧结果没有保存权益曲线 → 不凭空补出，明确标记缺失
    assert not rec.get("equity_curve")
    assert "equity_curve" in rec.get("_missing_fields", [])
    assert rec["trades"] == []


# ──────────── 取消/失败不产生已成功成绩 ────────────

def test_cancelled_result_not_persisted(monkeypatch):
    task_id = "t_cancel"
    fake = _fake_result(task_id)
    fake["cancelled"] = True
    monkeypatch.setattr(svc.VisualBacktestEngine, "run",
                        lambda self, conditions: dict(fake))
    _register_task(task_id)
    svc._run_task(task_id, _params(), "取消测试")
    t = svc.get_task(task_id)
    assert t["status"] == "cancelled"
    # 取消不写 backtest_results（不产生已成功成绩）
    assert svc._load_result_row(task_id) is None


def test_failed_result_not_persisted(monkeypatch):
    task_id = "t_failed"
    fake = {"success": False, "error": "区间内无行情数据", "start_date": "2025-01-01",
            "end_date": "2025-06-01", "trades": [], "equity_curve": [], "positions": []}
    monkeypatch.setattr(svc.VisualBacktestEngine, "run",
                        lambda self, conditions: dict(fake))
    _register_task(task_id)
    svc._run_task(task_id, _params(), "失败测试")
    t = svc.get_task(task_id)
    assert t["status"] == "failed"
    assert svc._load_result_row(task_id) is None


def test_persist_failure_does_not_mark_done(monkeypatch):
    task_id = "t_persist_fail"
    monkeypatch.setattr(svc.VisualBacktestEngine, "run",
                        lambda self, conditions: _fake_result(task_id))
    # 持久化失败（返回 None）
    monkeypatch.setattr(svc, "_save_result", lambda *a, **k: None)
    # fitness 回写若被调用则记为违规
    called = {"writeback": False}
    monkeypatch.setattr(svc, "_writeback_rule_fitness",
                        lambda *a, **k: called.__setitem__("writeback", True))
    _register_task(task_id)
    svc._run_task(task_id, _params(), "持久化失败")
    t = svc.get_task(task_id)
    assert t["status"] == "failed"
    assert "持久化失败" in (t["error"] or "")
    assert called["writeback"] is False
    assert svc._load_result_row(task_id) is None


def test_run_task_success_persists_and_marks_done(monkeypatch):
    task_id = "t_run_ok"
    monkeypatch.setattr(svc.VisualBacktestEngine, "run",
                        lambda self, conditions: _fake_result(task_id))
    _register_task(task_id)
    svc._run_task(task_id, _params(), "成功")
    t = svc.get_task(task_id)
    assert t["status"] == "done"
    assert svc._load_result_row(task_id) is not None
    # 内存清空后仍可恢复
    _clear_memory()
    assert svc.get_task_result(task_id)["_recovered"] is True


# ──────────── 路由级：重启后详情/分页仍可用 ────────────

@pytest.fixture
def client():
    from flask import Flask
    from routes.backtest import backtest_bp
    app = Flask(__name__)
    app.register_blueprint(backtest_bp)
    return app.test_client()


def test_route_result_recovers_after_restart(client):
    task_id = "t_route_recover"
    svc._save_result(_fake_result(task_id), "路由恢复", payload=_payload(),
                     actual_rule_id=None, params=_params())
    _clear_memory()  # 模拟服务重启
    resp = client.get(f"/api/backtest/{task_id}/result")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    # 既有契约：ok({"data": result}) → body["data"]["data"] 才是结果体
    data = body["data"]["data"]
    assert data["annual_return"] == 0.20
    assert len(data["trades"]) == 2
    # 分页
    resp2 = client.get(f"/api/backtest/{task_id}/trades?page=1&page_size=1")
    assert resp2.status_code == 200
    assert resp2.get_json()["data"]["total"] == 2
    # 历史列表
    resp3 = client.get("/api/backtest/history?limit=10")
    assert resp3.status_code == 200
    assert any(i["task_id"] == task_id for i in resp3.get_json()["data"]["items"])


def test_route_unknown_task_404(client):
    resp = client.get("/api/backtest/no_such_task/result")
    assert resp.status_code == 404
