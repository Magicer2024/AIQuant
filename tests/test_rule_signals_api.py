# -*- coding: utf-8 -*-
"""tests/test_rule_signals_api.py — GET /api/investor/rule_signals 只读端点。

验证返回 200 且结构为 {success, data:{date, items:[...]}}。
依赖本地 core/quant.db 真实数据；无库自动跳过。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
pytestmark = pytest.mark.skipif(not os.path.exists(_DB), reason="需要本地 quant.db 真实数据")


@pytest.fixture(scope="module")
def client():
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_rule_signals_ok_structure(client):
    """基本调用返回 200，结构为 {success, data:{date, items:[...]}}。"""
    resp = client.get("/api/investor/rule_signals")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    data = body["data"]
    assert "date" in data
    assert "items" in data
    assert isinstance(data["items"], list)
    # 若有命中，每行含约定字段
    if data["items"]:
        row = data["items"][0]
        for key in ("code", "name", "rule_id", "rule_name", "confidence", "latest_close"):
            assert key in row


def test_rule_signals_explicit_date(client):
    """指定日期参数返回 200 且 items 为 list。"""
    resp = client.get("/api/investor/rule_signals?date=1990-01-01")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert isinstance(body["data"]["items"], list)
