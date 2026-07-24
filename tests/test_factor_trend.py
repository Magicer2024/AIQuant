# -*- coding: utf-8 -*-
"""tests/test_factor_trend.py — 融合分归因/趋势只读端点 /api/investor/factor_trend。

依赖本地 core/quant.db 中 daily_price 的真实分项因子数据；无库环境自动跳过。
"""
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
pytestmark = pytest.mark.skipif(not os.path.exists(_DB), reason="需要本地 quant.db 真实数据")


@pytest.fixture(scope="module")
def client():
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_factor_trend_ok_structure(client):
    """基本调用返回 200，结构为 {success, data:{code:[...]}}。"""
    resp = client.get("/api/investor/factor_trend?codes=000001&days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    data = body["data"]
    assert isinstance(data, dict)
    # 请求的 code 必定作为 key 出现（无数据则为空数组）
    assert "000001" in data
    assert isinstance(data["000001"], list)
    # 若有数据，每行应含融合分及 5 个分项因子字段
    if data["000001"]:
        row = data["000001"][0]
        for key in ("trade_date", "fusion_score", "vol_score", "ma_score",
                    "diverge_score", "bottom_score", "whale_score"):
            assert key in row


def test_factor_trend_days_clamped_upper(client):
    """days 上越界（>90）被 clamp，返回 200 且每 code 行数不超过 90。"""
    resp = client.get("/api/investor/factor_trend?codes=000001&days=100000")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert len(body["data"].get("000001", [])) <= 90


def test_factor_trend_days_clamped_lower(client):
    """days 下越界（<2）被 clamp，不报错，返回 200。"""
    resp = client.get("/api/investor/factor_trend?codes=000001&days=0")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_factor_trend_days_non_numeric(client):
    """days 非数字时回退默认值，返回 200。"""
    resp = client.get("/api/investor/factor_trend?codes=000001&days=abc")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_factor_trend_empty_codes_400(client):
    """缺少 codes 返回 400，success=False。"""
    resp = client.get("/api/investor/factor_trend")
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_factor_trend_invalid_codes_400(client):
    """非法 codes（非 6 位数字）被过滤后为空，返回 400。"""
    resp = client.get("/api/investor/factor_trend?codes=abc,12,xyz")
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_factor_trend_multi_codes(client):
    """多 code 请求，每个合法 code 都作为 key 返回。"""
    resp = client.get("/api/investor/factor_trend?codes=000001,600519&days=5")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert "000001" in data and "600519" in data
