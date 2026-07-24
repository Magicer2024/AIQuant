# -*- coding: utf-8 -*-
"""tests/test_save_as_rule.py — POST /api/backtest/save_as_rule 把回测结果存为常驻规则。

向 backtest.service._TASKS 注入一个完成态的可视化回测任务（含 conditions + result），
验证：成功入库(200)、相同条件异名(409)、rule_id 型任务被拒。
依赖本地 core/quant.db（save_rule 需写 strategy_rules 表）；无库自动跳过。
用后清理注入任务与入库规则。
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
pytestmark = pytest.mark.skipif(not os.path.exists(_DB), reason="需要本地 quant.db 真实数据")


@pytest.fixture(scope="module")
def client():
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


_CONDITIONS = [{"factor": "rsi", "operator": "<", "threshold": 30}]
_RESULT = {
    "annual_return": 0.25, "win_rate": 0.6, "sharpe_ratio": 1.5,
    "max_drawdown": 0.1, "total_trades": 12, "total_return": 0.4,
}


def _inject_task(conditions=None, rule_id=None, status="done"):
    from backtest import service as bt_service
    task_id = uuid.uuid4().hex[:16]
    payload = {"conditions": conditions, "params": {"max_hold_days": 15}}
    if rule_id is not None:
        payload["rule_id"] = rule_id
    with bt_service._LOCK:
        bt_service._TASKS[task_id] = {
            "id": task_id, "status": status, "progress": 100,
            "params": payload, "result": dict(_RESULT), "error": None,
            "cancelled": False,
        }
    return task_id


def _cleanup_task(task_id):
    from backtest import service as bt_service
    with bt_service._LOCK:
        bt_service._TASKS.pop(task_id, None)


def _cleanup_rule(rule_id):
    if rule_id is None:
        return
    from strategy.rules_store import delete_rule
    try:
        delete_rule(int(rule_id))
    except Exception:
        pass


def test_save_as_rule_ok(client):
    """完成态可视化回测 → 存为规则返回 200 且入库。"""
    task_id = _inject_task(conditions=_CONDITIONS)
    rule_id = None
    try:
        resp = client.post("/api/backtest/save_as_rule",
                           json={"task_id": task_id, "rule_name": "单测规则_保存"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        rule_id = body["data"]["rule_id"]
        assert body["data"]["rule_name"] == "单测规则_保存"
        assert body["data"]["horizon"] in ("short", "mid", "long")
    finally:
        _cleanup_rule(rule_id)
        _cleanup_task(task_id)


def test_save_as_rule_dup_conditions_conflict(client):
    """相同条件、不同规则名 → 409。"""
    t1 = _inject_task(conditions=_CONDITIONS)
    t2 = _inject_task(conditions=_CONDITIONS)
    rule_id = None
    try:
        r1 = client.post("/api/backtest/save_as_rule",
                        json={"task_id": t1, "rule_name": "单测规则_原名"})
        assert r1.status_code == 200
        rule_id = r1.get_json()["data"]["rule_id"]
        r2 = client.post("/api/backtest/save_as_rule",
                        json={"task_id": t2, "rule_name": "单测规则_异名"})
        assert r2.status_code == 409
        assert r2.get_json()["success"] is False
    finally:
        _cleanup_rule(rule_id)
        _cleanup_task(t1)
        _cleanup_task(t2)


def test_save_as_rule_rejects_rule_id_task(client):
    """rule_id 型任务（已是规则）→ 被拒。"""
    task_id = _inject_task(conditions=_CONDITIONS, rule_id=999999)
    try:
        resp = client.post("/api/backtest/save_as_rule",
                          json={"task_id": task_id, "rule_name": "单测规则_规则型"})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
    finally:
        _cleanup_task(task_id)


def test_save_as_rule_missing_task_404(client):
    """不存在的 task_id → 404。"""
    resp = client.post("/api/backtest/save_as_rule",
                      json={"task_id": "notexist123", "rule_name": "x"})
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False
