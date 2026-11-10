# -*- coding: utf-8 -*-
"""core/stock_flags 回归测试（研究用标记，纯函数 + 建表回补 + 停牌补齐）"""
import sqlite3

import pytest

from core.stock_flags import (TABLE_DDL, compute_flags_for_row,
                              research_limit_pct, rebuild)


def test_research_limit_pct_layers():
    assert research_limit_pct("600519") == 0.10
    assert research_limit_pct("300750") == 0.20
    assert research_limit_pct("688981") == 0.20
    assert research_limit_pct("832004") == 0.30
    assert research_limit_pct("430047") == 0.30
    assert research_limit_pct("600519", is_st=True) == 0.05


def test_flags_normal_day():
    f = compute_flags_for_row(10.0, 10.8, 9.9, 10.5, 10.0, 1000, 0.10)
    assert f["suspended"] == 0 and f["limit_up_sealed"] == 0 \
        and f["limit_down_sealed"] == 0 and f["prev_close"] == 10.0


def test_flags_sealed_limit_up_and_open_seal():
    # 前收 10 → 涨停 11.0；收盘=11.0 封板；开盘也 11.0 → 开盘封板
    f = compute_flags_for_row(11.0, 11.0, 10.9, 11.0, 10.0, 5000, 0.10)
    assert f["limit_up_sealed"] == 1 and f["open_sealed_up"] == 1
    assert f["one_word_up"] == 0  # low != high
    # 一字涨停：open=high=low=close=11
    f2 = compute_flags_for_row(11.0, 11.0, 11.0, 11.0, 10.0, 500, 0.10)
    assert f2["one_word_up"] == 1 and f2["limit_up_sealed"] == 1


def test_flags_tolerance_boundary():
    # close=10.978, up*0.998=10.978 → 边界内判封板（tol=0.002）
    f = compute_flags_for_row(10.5, 11.0, 10.5, 10.978, 10.0, 9000, 0.10)
    assert f["limit_up_sealed"] == 1
    # 再低一分钱不判
    f2 = compute_flags_for_row(10.5, 11.0, 10.5, 10.968, 10.0, 9000, 0.10)
    assert f2["limit_up_sealed"] == 0


def test_flags_suspend_volume_zero():
    f = compute_flags_for_row(10.0, 10.0, 10.0, 10.0, 10.0, 0, 0.10)
    assert f["suspended"] == 1


def test_flags_no_prev_close_first_day():
    f = compute_flags_for_row(11.0, 11.0, 11.0, 11.0, None, 100, 0.10)
    assert f["limit_up_sealed"] == 0 and f["suspended"] == 0  # 保守不判


@pytest.fixture()
def mini_db(isolated_database):
    conn = sqlite3.connect(isolated_database)
    conn.execute(TABLE_DDL)
    # isolated_database 已由 init_db 建好 daily_price/stock_info
    conn.executemany(
        "INSERT INTO stock_info (code, name, market, is_active) VALUES (?,?,?,1)",
        [("600111", "甲", "SH"), ("300222", "乙ST", "SZ")])
    days = ["2026-11-05", "2026-11-06", "2026-11-09"]
    rows = []
    for d in days:
        rows.append(("600111", d, 10, 11.0, 10, 11.0, 1000, 1e6, 10.0, 1.0))
        if d != "2026-11-06":  # 300222 在 11-06 停牌（缺行）
            rows.append(("300222", d, 5, 6.0, 5, 6.0, 500, 3e5, 9.9, 2.0))
    conn.executemany(
        "INSERT INTO daily_price (code, trade_date, open, high, low, close,"
        " volume, amount, pct_change, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    yield conn
    conn.close()


def test_rebuild_flags_and_suspend_fill(mini_db):
    res = rebuild(mini_db, days=3)
    assert res["dates"] == 3
    # 600111 三个交易日 close 恒为 11：11-05 首日无前收不判；
    # 11-06/11-09 prev_close=11、涨停价 12.1 → 均不构成涨停（基准跳查正确）
    n_up = mini_db.execute(
        "SELECT COUNT(*) FROM stock_daily_flags WHERE code='600111'"
        " AND limit_up_sealed=1").fetchone()[0]
    assert n_up == 0
    rows = mini_db.execute(
        "SELECT trade_date, suspended, limit_up_sealed, prev_close"
        " FROM stock_daily_flags WHERE code='600111' ORDER BY trade_date").fetchall()
    assert rows[0][2] == 0 and rows[0][3] is None          # 首日无基准
    assert rows[1][2] == 0 and rows[1][3] == 11.0          # 11→11 非涨停
    # 300222 缺行的 11-06 被补成 suspended=1，且 ST 判定 limit_pct=0.05
    s = mini_db.execute(
        "SELECT suspended, limit_pct, is_st FROM stock_daily_flags"
        " WHERE code='300222' AND trade_date='2026-11-06'").fetchone()
    assert s == (1, 0.05, 1)


def test_rebuild_idempotent(mini_db):
    r1 = rebuild(mini_db, days=3)
    r2 = rebuild(mini_db, days=3)
    assert r1["rows"] == r2["rows"]
    n = mini_db.execute("SELECT COUNT(*) FROM stock_daily_flags").fetchone()[0]
    assert n == r2["rows"]  # 无重复行
