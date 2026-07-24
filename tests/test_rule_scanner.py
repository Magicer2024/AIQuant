# -*- coding: utf-8 -*-
"""tests/test_rule_scanner.py — strategy.rule_scanner.scan_active_rules 每日规则扫描。

全市场逐股扫描非常耗时，故用 monkeypatch 注入合成数据（少量股票 + 单规则），
聚焦验证：命中评估、落地 strategy_signals、先删后插的幂等去重、异常不抛出。
写入使用未来日期 2099-12-31 与虚拟规则 id，避免污染真实数据；用后清理。
依赖本地 core/quant.db（写 strategy_signals 表）；无库自动跳过。
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
pytestmark = pytest.mark.skipif(not os.path.exists(_DB), reason="需要本地 quant.db 真实数据")

_TEST_DATE = "2099-12-31"


def _cleanup_signals():
    from core.db import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM strategy_signals WHERE trade_date = ?", (_TEST_DATE,))


def _install_fakes(monkeypatch):
    """注入合成的启用规则 / 流式行情 / 因子，命中一只股票。"""
    def fake_active_rules():
        return [{
            "id": -99, "rule_name": "单测规则_扫描",
            "conditions": '[{"factor":"myfactor","operator":">","threshold":0.5}]',
        }]

    def fake_stream(trade_date, batch_size=100):
        idx = pd.date_range(end=trade_date, periods=30, freq="D")
        df = pd.DataFrame({"close": range(30)}, index=idx)
        yield "TST001", df

    def fake_factors(df):
        # 返回一列恒为 1.0 的因子，最后一行索引 == trade_date → 命中
        return pd.DataFrame({"myfactor": [1.0] * len(df)}, index=df.index)

    monkeypatch.setattr("strategy.rules_store.get_active_rules", fake_active_rules)
    monkeypatch.setattr("strategy.scorer._load_stock_data_streaming", fake_stream)
    monkeypatch.setattr("strategy.factor_lib.compute_all_factors", fake_factors)


def test_scan_returns_structure_fast():
    """给一个无行情的历史日期，快速返回且结构正确、hits=0、不抛出。"""
    from strategy.rule_scanner import scan_active_rules
    result = scan_active_rules(trade_date="1990-01-01")
    assert isinstance(result, dict)
    for key in ("trade_date", "rules", "hits", "elapsed_s"):
        assert key in result
    assert result["hits"] == 0


def test_scan_hit_written(monkeypatch):
    """合成规则命中一只股票 → strategy_signals 落地该行。"""
    from core.db import get_conn
    from strategy.rule_scanner import scan_active_rules
    _install_fakes(monkeypatch)
    try:
        result = scan_active_rules(trade_date=_TEST_DATE)
        assert result["rules"] == 1
        assert result["hits"] == 1
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT code, rule_id, rule_name, confidence FROM strategy_signals WHERE trade_date = ?",
                (_TEST_DATE,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["code"] == "TST001"
        assert rows[0]["rule_id"] == -99
    finally:
        _cleanup_signals()


def test_scan_idempotent_no_dup_rows(monkeypatch):
    """连续两次扫描同一日，落库行数一致（先删后插幂等）。"""
    from core.db import get_conn
    from strategy.rule_scanner import scan_active_rules
    _install_fakes(monkeypatch)
    try:
        scan_active_rules(trade_date=_TEST_DATE)
        with get_conn() as conn:
            c1 = conn.execute(
                "SELECT COUNT(*) AS c FROM strategy_signals WHERE trade_date = ?",
                (_TEST_DATE,),
            ).fetchone()["c"]
        scan_active_rules(trade_date=_TEST_DATE)
        with get_conn() as conn:
            c2 = conn.execute(
                "SELECT COUNT(*) AS c FROM strategy_signals WHERE trade_date = ?",
                (_TEST_DATE,),
            ).fetchone()["c"]
        assert c1 == 1
        assert c1 == c2
    finally:
        _cleanup_signals()
