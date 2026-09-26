"""tests/test_api_identity.py —— 推荐 API 身份链元数据（方案 F1）

验证 /api/investor 推荐端点在响应中携带 meta 身份链（cohort / data_as_of /
param_version / signal_model_mode / source），legacy 模式下稳定 ID 为空、不伪造；
同时保证旧字段（date / count / groups / items）向后兼容。

沿用 test_horizon.py 的最小栈：conftest 的 isolated_database（autouse）已隔离数据库，
此处 init_db() 模拟服务启动迁移，app.config['TESTING']=True 禁用同步/调度副作用。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.db import init_db


@pytest.fixture()
def client():
    init_db()
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_today_meta_production(client):
    """/today 携带正式主榜身份链；legacy 下稳定 ID 全空、旧字段兼容。"""
    resp = client.get("/api/investor/today?limit=5")
    assert resp.status_code == 200
    data = (resp.get_json() or {}).get("data") or {}
    # 旧字段向后兼容
    for k in ("date", "count", "groups", "items"):
        assert k in data
    meta = data.get("meta")
    assert meta is not None
    assert meta["cohort"] == "production"
    assert meta["signal_model_mode"] == "legacy"
    assert meta["source"] == "legacy_recommendation"
    assert meta["identity_chain_available"] is False
    assert isinstance(meta["param_version"], int) and meta["param_version"] >= 0
    # legacy 无冻结批次/信号运行 → 稳定 ID 为空，绝不伪造
    for idk in ("batch_id", "signal_id", "recommendation_id", "trade_id"):
        assert meta[idk] is None


def test_surge_picks_meta_observation(client):
    """次日强势观察属观察池（cohort=observation, list_key=observe_surge）。"""
    resp = client.get("/api/investor/surge_picks?limit=3")
    assert resp.status_code == 200
    meta = ((resp.get_json() or {}).get("data") or {}).get("meta") or {}
    assert meta.get("cohort") == "observation"
    assert meta.get("list_key") == "observe_surge"
    assert meta.get("signal_model_mode") == "legacy"


def test_reversal_picks_meta_observation(client):
    """反转首日观察属观察池（cohort=observation, list_key=observe_first_reversal）。"""
    resp = client.get("/api/investor/reversal_picks?limit=3")
    assert resp.status_code == 200
    meta = ((resp.get_json() or {}).get("data") or {}).get("meta") or {}
    assert meta.get("cohort") == "observation"
    assert meta.get("list_key") == "observe_first_reversal"


def test_identity_meta_v2_surfaces_ids(tmp_db_isolated, monkeypatch):
    """v2 模式：source/identity_chain_available 翻转，显式稳定 ID 透传（不伪造）。"""
    import config.settings as settings
    monkeypatch.setattr(settings, "SIGNAL_MODEL_MODE", "v2")
    from utils.api import identity_meta
    meta = identity_meta(cohort="production", data_as_of="2026-09-25",
                         list_key="short", batch_id="b1", signal_id="s1",
                         recommendation_id="r1", trade_id="t1")
    assert meta["source"] == "unified_signal_model"
    assert meta["identity_chain_available"] is True
    assert meta["batch_id"] == "b1" and meta["signal_id"] == "s1"
    assert meta["recommendation_id"] == "r1" and meta["trade_id"] == "t1"
    assert meta["data_as_of"] == "2026-09-25"
    assert meta["list_key"] == "short"


@pytest.fixture()
def tmp_db_isolated():
    """identity_meta 直调需已迁移的库（读 param_tune_log 计版本）。"""
    init_db()
    yield
