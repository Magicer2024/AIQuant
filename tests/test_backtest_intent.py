"""
tests/test_backtest_intent.py —— 回测自然语言解析测试

覆盖：
  1. catalog 校验（非法指标/操作符/参数过滤与默认值补齐）
  2. 回测参数白名单校验（日期格式、百分数换算、类型归一）
  3. LLM 主路径（mock chat_completion）
  4. LLM 失败/未配置时本地正则兜底
  5. POST /api/backtest/parse_intent 路由
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import nl_parser  # noqa: E402


# ──────────── catalog 校验 ────────────

class TestValidateConditions:
    def test_valid_condition_passes(self):
        warns = []
        out = nl_parser._validate_conditions([
            {"indicator": "turnover_rate", "operator": "between", "params": {"min": 1, "max": 20}},
        ], warns)
        assert len(out) == 1
        c = out[0]
        assert c["category"] == "volume"
        assert c["indicator"] == "turnover_rate"
        assert c["operator"] == "between"
        assert c["params"] == {"min": 1, "max": 20}
        assert warns == []

    def test_unknown_indicator_dropped(self):
        warns = []
        out = nl_parser._validate_conditions([
            {"indicator": "not_exist", "operator": "gt", "params": {}},
        ], warns)
        assert out == []
        assert any("not_exist" in w for w in warns)

    def test_bad_operator_falls_back_to_first(self):
        warns = []
        out = nl_parser._validate_conditions([
            {"indicator": "rsi", "operator": "between", "params": {"period": 14}},
        ], warns)
        assert out[0]["operator"] == "oversold"  # rsi 首个操作符
        assert any("操作符" in w for w in warns)

    def test_missing_params_filled_with_default_and_clamped(self):
        out = nl_parser._validate_conditions([
            {"indicator": "ma_cross", "operator": "golden_cross", "params": {"long_period": 9999}},
        ], [])
        c = out[0]
        assert c["params"]["short_period"] == 5      # 缺省补默认
        assert c["params"]["long_period"] == 250     # 超界被钳制到 max

    def test_max_8_conditions(self):
        out = nl_parser._validate_conditions(
            [{"indicator": "rsi", "operator": "oversold", "params": {}}] * 20, [])
        assert len(out) == 8


# ──────────── 参数校验 ────────────

class TestValidateParams:
    def test_dates_and_percent_conversion(self):
        warns = []
        out = nl_parser._validate_params({
            "start_date": "2025/01/01",      # 斜杠归一为横杠
            "end_date": "2026-01-01",
            "take_profit_pct": 15,           # 百分数 → 小数
            "stop_loss_pct": 0.05,           # 小数原样
            "max_hold_days": 20,
            "initial_cash": 500000,
            "exclude_kcb": True,
        }, warns)
        assert out["start_date"] == "2025-01-01"
        assert out["take_profit_pct"] == 0.15
        assert out["stop_loss_pct"] == 0.05
        assert out["max_hold_days"] == 20
        assert out["initial_cash"] == 500000.0
        assert out["exclude_kcb"] is True
        assert warns == []

    def test_bad_values_ignored(self):
        warns = []
        out = nl_parser._validate_params({
            "start_date": "昨天",
            "max_hold_days": -3,
            "unknown_key": 1,
        }, warns)
        assert "start_date" not in out
        assert "max_hold_days" not in out
        assert "unknown_key" not in out
        assert any("start_date" in w for w in warns)


# ──────────── JSON 提取容错 ────────────

def test_extract_json_with_markdown_fence():
    raw = "```json\n{\"conditions\": [], \"params\": {}}\n```"
    assert nl_parser._extract_json(raw) == {"conditions": [], "params": {}}


def test_extract_json_with_surrounding_noise():
    raw = "好的，解析结果：{\"params\": {\"max_hold_days\": 5}} 请查收"
    assert nl_parser._extract_json(raw)["params"]["max_hold_days"] == 5


# ──────────── LLM 主路径 ────────────

_LLM_PAYLOAD = {
    "conditions": [
        {"indicator": "turnover_rate", "operator": "between", "params": {"min": 1, "max": 20}},
        {"indicator": "ma_cross", "operator": "golden_cross", "params": {"short_period": 5, "long_period": 20}},
        {"indicator": "fake_indicator", "operator": "gt", "params": {}},
    ],
    "params": {
        "start_date": "2025-07-19", "end_date": "2026-07-19",
        "stop_loss_pct": 0.05, "take_profit_pct": 0.15, "max_hold_days": 20,
    },
    "explanation": "换手率介于1%~20%且MA5上穿MA20，回测近一年",
}


def test_parse_intent_llm_path(monkeypatch):
    monkeypatch.setattr(nl_parser, "is_llm_available", lambda: True)
    monkeypatch.setattr(nl_parser, "chat_completion",
                        lambda *a, **kw: json.dumps(_LLM_PAYLOAD, ensure_ascii=False))
    r = nl_parser.parse_backtest_intent("换手率1%~20%，MA5上穿MA20，回测近一年，止损5%止盈15%，最多持20天")
    assert r["success"] is True
    assert r["source"] == "llm"
    assert len(r["conditions"]) == 2                    # 非法指标被过滤
    assert r["conditions"][0]["category"] == "volume"
    assert r["params"]["stop_loss_pct"] == 0.05
    assert r["params"]["max_hold_days"] == 20
    assert "换手率" in r["explanation"]
    assert any("fake_indicator" in w for w in r["warnings"])


def test_parse_intent_llm_failure_falls_back(monkeypatch):
    monkeypatch.setattr(nl_parser, "is_llm_available", lambda: True)

    def _boom(*a, **kw):
        raise RuntimeError("LLM 未配置或已禁用")
    monkeypatch.setattr(nl_parser, "chat_completion", _boom)
    r = nl_parser.parse_backtest_intent("止损5%，最多持有20天")
    assert r["success"] is True
    assert r["source"] == "local"
    assert r["params"]["stop_loss_pct"] == 0.05
    assert r["params"]["max_hold_days"] == 20
    assert "LLM 调用失败" in r["note"]


def test_parse_intent_empty_text():
    assert nl_parser.parse_backtest_intent("   ")["success"] is False


# ──────────── 本地兜底 ────────────

def test_local_fallback_extracts_params(monkeypatch):
    monkeypatch.setattr(nl_parser, "is_llm_available", lambda: False)
    r = nl_parser.parse_backtest_intent(
        "止损5% 止盈15%，最多持有20天，回测近1年，初始资金50万，持仓最多5只，排除科创板")
    assert r["success"] is True
    assert r["source"] == "local"
    assert r["conditions"] == []
    p = r["params"]
    assert p["stop_loss_pct"] == 0.05
    assert p["take_profit_pct"] == 0.15
    assert p["max_hold_days"] == 20
    assert p["initial_cash"] == 500000.0
    assert p["max_holdings"] == 5
    assert p["exclude_kcb"] is True
    today = date.today()
    assert p["start_date"] == (today - timedelta(days=365)).isoformat()
    assert p["end_date"] == today.isoformat()
    assert "LLM_API_KEY" in r["note"]


def test_local_fallback_half_year(monkeypatch):
    """"近半年"（无阿拉伯数字）也要能换算"""
    monkeypatch.setattr(nl_parser, "is_llm_available", lambda: False)
    r = nl_parser.parse_backtest_intent("回测近半年")
    assert r["params"]["start_date"] == (date.today() - timedelta(days=182)).isoformat()


# ──────────── 路由 ────────────

@pytest.fixture()
def client():
    from flask import Flask
    from routes.backtest import backtest_bp
    app = Flask(__name__)
    app.register_blueprint(backtest_bp)
    return app.test_client()


def test_route_parse_intent_llm(client, monkeypatch):
    monkeypatch.setattr(nl_parser, "is_llm_available", lambda: True)
    monkeypatch.setattr(nl_parser, "chat_completion",
                        lambda *a, **kw: json.dumps(_LLM_PAYLOAD, ensure_ascii=False))
    resp = client.post("/api/backtest/parse_intent",
                       json={"text": "换手率1%~20%，MA5上穿MA20，回测近一年"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    data = body["data"]
    assert data["source"] == "llm"
    assert len(data["conditions"]) == 2
    assert data["params"]["end_date"] == "2026-07-19"


def test_route_parse_intent_empty(client):
    resp = client.post("/api/backtest/parse_intent", json={"text": ""})
    assert resp.status_code == 400  # fail() 默认 http=400
    assert resp.get_json()["success"] is False


def test_route_parse_intent_local_degraded(client, monkeypatch):
    monkeypatch.setattr(nl_parser, "is_llm_available", lambda: False)
    resp = client.post("/api/backtest/parse_intent",
                       json={"text": "止损5%，最多持有30天"})
    body = resp.get_json()
    assert body["success"] is True
    assert body["data"]["source"] == "local"
    assert body["data"]["params"]["max_hold_days"] == 30
