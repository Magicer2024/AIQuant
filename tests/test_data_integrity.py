# -*- coding: utf-8 -*-
"""core/data_integrity 回归测试（P0：缺日自愈 + 行数护栏）
覆盖：检测、告警、自愈编排、逐股补拉口径（mock 网络）、幂等。
"""
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from core import data_integrity as di

CAL_DAYS = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]


@pytest.fixture()
def conn(isolated_database):
    c = sqlite3.connect(isolated_database)
    _seed(c)
    yield c
    c.close()


def _seed(c):
    """init_db 已建表；塞 10 只活跃股票 + 4 个交易日数据。"""
    c.executemany(
        "INSERT INTO stock_info (code, name, market, is_active) VALUES (?,?,?,1)",
        [(f"60000{i}", f"股票{i}", "SH") for i in range(10)])
    rows = []
    for d in CAL_DAYS:
        if d in ("2026-10-29",):  # 全市场缺日
            continue
        n = 10 if d != "2026-10-30" else 5  # 10-30 行数不足（5/10 < 90%）
        for i in range(n):
            rows.append((f"60000{i}", d, 1.0, 1.5, 0.5, 1.2, 100.0, 120.0, 0.5, 1.0))
    c.executemany(
        "INSERT INTO daily_price (code, trade_date, open, high, low, close,"
        " volume, amount, pct_change, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    idx = []
    for d in CAL_DAYS:
        codes = ["000001", "000300", "000852", "000905", "399001"]
        if d == "2026-10-29":
            continue
        if d == "2026-10-23":
            codes = codes[:2]
        for code in codes:
            idx.append((code, d, 1.0, 1.1, 0.9, 1.05, 1000.0, 1050.0, 0.1))
    c.executemany(
        "INSERT INTO index_daily (code, trade_date, open, high, low, close,"
        " volume, amount, pct_change) VALUES (?,?,?,?,?,?,?,?,?)", idx)
    c.commit()


def _fake_days(monkeypatch):
    monkeypatch.setattr(di, "_recent_trade_days",
                        lambda n, u: list(CAL_DAYS))


# ── 检测 ────────────────────────────────────────────
def test_find_missing_detects_zero_and_short_rows(conn, monkeypatch):
    _fake_days(monkeypatch)
    miss = di.find_missing_trade_dates(conn, lookback_days=60,
                                       until="2026-10-30")
    dates = {m["trade_date"]: m for m in miss}
    assert "2026-10-29" in dates and dates["2026-10-29"]["price_missing"]
    assert "2026-10-30" in dates and dates["2026-10-30"]["price_missing"]  # 5<9
    assert "2026-10-27" not in dates and "2026-10-28" not in dates


def test_find_missing_index_only(conn, monkeypatch):
    _fake_days(monkeypatch)
    conn.execute("DELETE FROM index_daily WHERE trade_date='2026-10-28'")
    conn.commit()
    miss = di.find_missing_trade_dates(conn, until="2026-10-30")
    m28 = {m["trade_date"]: m for m in miss}["2026-10-28"]
    assert m28["index_missing"] and not m28["price_missing"]


def test_recent_trade_days_falls_back_to_weekday(monkeypatch):
    import core.trade_calendar as tc
    monkeypatch.setattr(tc, "get_trade_days", lambda refresh=False: None)
    days = di._recent_trade_days(14, "2026-11-09")
    assert all(len(d) == 10 for d in days)
    assert "2026-11-06" in days   # 周五
    assert "2026-11-08" not in days  # 周日
    assert "2026-11-07" not in days  # 周六


# ── 行数护栏 ────────────────────────────────────────
def test_row_guard_fail_writes_risk_event(conn):
    before = conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='risk_events'").fetchone() else 0
    r = di.check_daily_row_guard(conn, "2026-10-30")  # 5/10 < 90%
    assert r["ok"] is False and r["rows"] == 5 and r["threshold"] == 9
    after = conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0]
    assert after == before + 1
    ev = conn.execute("SELECT rule_name, level, message FROM risk_events "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    assert ev[0] == "data_integrity_daily_rows" and ev[1] == "critical"
    assert "2026-10-30" in ev[2]


def test_row_guard_pass_no_event(conn):
    before = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='risk_events'").fetchone()[0]
    r = di.check_daily_row_guard(conn, "2026-10-27")
    assert r["ok"] is True and r["rows"] == 10
    # 正常日不得新增告警（表不存在也应保持不存在或 0 行）
    n = conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='risk_events'").fetchone() else 0
    assert n == 0


def test_row_guard_zero_active_is_ok(conn):
    conn.execute("UPDATE stock_info SET is_active = 0")
    conn.commit()
    assert di.check_daily_row_guard(conn, "2026-10-29")["ok"] is True


# ── 逐股补拉（mock 网络）─────────────────────────────
def _fake_em_response(date_str_prices: dict):
    """构造东财 kline JSON：每天一行 [date,open,close,high,low,vol手,amt,amp,pct,chg,turn]"""
    klines = [",".join([d, "1.0", str(p), "1.6", "0.9", "1234", "1500.5",
                        "3.0", "0.5", "0.01", "2.2"])
              for d, p in date_str_prices.items()]
    return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": {"klines": klines}})


def test_backfill_price_date_maps_fields_and_filters(conn, monkeypatch):
    captured = {}

    def fake_write(df):
        captured["df"] = df
        return len(df)

    import core.sync as sync
    import core.em_kline as ek
    monkeypatch.setattr(sync, "_batch_write_daily_price", fake_write)
    monkeypatch.setattr(ek, "_SESSION", SimpleNamespace(
        get=lambda url, params=None, timeout=None: _fake_em_response(
            {"2026-10-26": 1.1, "2026-10-28": 1.2, "2026-10-29": 1.3})))
    monkeypatch.setattr(di.time, "sleep", lambda s: None)

    r = di.backfill_price_date(conn, "2026-10-29")
    df = captured["df"]
    assert r["written"] == len(df) == 10  # 全部 10 只都缺 10-29
    assert r["failed"] == 0
    assert set(df["trade_date"]) == {"2026-10-29"}          # 非目标日过滤
    row = df[df["code"] == "600001"].iloc[0]
    assert row["close"] == 1.3 and row["volume"] == 1234     # 手（写库层转股）
    assert row["pct_change"] == 0.5 and row["turnover"] == 2.2  # f[8]/f[10]


def test_backfill_price_date_skips_existing(conn, monkeypatch):
    """幂等：已有 10-28 行的股票不应再次拉取。"""
    import core.em_kline as ek
    import core.sync as sync
    monkeypatch.setattr(sync, "_batch_write_daily_price", lambda df: len(df))
    calls = []

    def spy_get(url, params=None, timeout=None):
        calls.append(params["secid"])
        return _fake_em_response({"2026-10-28": 1.0})
    monkeypatch.setattr(ek, "_SESSION", SimpleNamespace(get=spy_get))
    monkeypatch.setattr(di.time, "sleep", lambda s: None)
    r = di.backfill_price_date(conn, "2026-10-28")  # 该日已有 10 只
    assert r["attempted"] == 0 and r["already_had"] == 10
    assert calls == []


# ── 自愈编排 ────────────────────────────────────────
def test_self_heal_orchestrates_and_respects_skip(conn, monkeypatch):
    _fake_days(monkeypatch)
    price_calls, index_calls = [], []
    monkeypatch.setattr(di, "backfill_price_date",
                        lambda c, d: price_calls.append(d) or {"written": 1})
    monkeypatch.setattr(di, "backfill_index_dates",
                        lambda c, ds: index_calls.extend(ds) or len(ds))
    r = di.self_heal(conn, skip_dates={"2026-10-29"}, until="2026-10-30")
    assert price_calls == ["2026-10-30"]        # 29 被跳过（本轮主链在同步，
    assert index_calls == []                    # 含指数整天交给主链，不重复处理）
    assert "2026-10-27" not in price_calls      # 正常日不动


def test_self_heal_swallows_errors(conn, monkeypatch):
    _fake_days(monkeypatch)

    def boom(c, d):
        raise RuntimeError("network down")
    monkeypatch.setattr(di, "backfill_price_date", boom)
    r = di.self_heal(conn, until="2026-10-30")
    assert r["ok"] is True  # 自愈失败不阻断，但标记条目失败
    fails = [b for b in r["backfilled"] if not b.get("ok")]
    assert fails and "network down" in fails[0]["error"]


def test_self_heal_detection_failure_returns_error(conn, monkeypatch):
    def boom(c, lookback_days=60, until=None):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(di, "find_missing_trade_dates", boom)
    r = di.self_heal(conn)
    assert r["ok"] is False and "calendar down" in r["error"]

