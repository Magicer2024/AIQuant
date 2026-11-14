# -*- coding: utf-8 -*-
"""tests/test_market_breadth.py —— /api/market/breadth 聚合接口（管理A seq=477 A2）

覆盖：涨跌平家数与市场速览同口径、主板/创业板涨停判定、炸板判定、
ST/缺前收显式排除、连板分层、date 参数与无未来函数（只用 ≤查询日）。
沿用 conftest isolated_database + test_api_identity 的 client 模式。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.db import get_conn, init_db


@pytest.fixture()
def client():
    init_db()
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def _seed_stock(code, name="测试", industry=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stock_info(code, name, market, is_active, industry) "
            "VALUES(?,?,?,1,?)",
            (code, name, "sh" if code.startswith("6") else "sz", industry),
        )


def _seed_day(code, date, prev_close, *, pct_open_move=0.0, high=None, close=None, volume=1000.0):
    """按前收生成一行行情：close 默认 = prev_close*(1+pct)；high 默认 = close。"""
    close = round(prev_close * (1 + pct_open_move), 2) if close is None else close
    high = close if high is None else high
    pct = round((close / prev_close - 1) * 100, 2)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_price(code, trade_date, open, high, low, close, "
            "volume, amount, pct_change) VALUES(?,?,?,?,?,?,?,?,?)",
            (code, date, close, high, min(close, prev_close), close, volume, volume * close, pct),
        )
    return close


def test_breadth_up_down_flat_caliber(client):
    """涨/跌/平 = 当日全行 CASE pct_change，与市场速览口径一致（含 ST、新股全行）。"""
    _seed_stock("600001")
    _seed_stock("600002")
    _seed_stock("600003")
    _seed_stock("600004")
    # 前收基准日
    for c, p in [("600001", 10.0), ("600002", 10.0), ("600003", 10.0), ("600004", 10.0)]:
        _seed_day(c, "2026-11-12", p / (1 + 0.05))  # 任意前日
    _seed_day("600001", "2026-11-13", 10.0, pct_open_move=0.02)   # 涨
    _seed_day("600002", "2026-11-13", 10.0, pct_open_move=-0.02)  # 跌
    _seed_day("600003", "2026-11-13", 10.0, pct_open_move=0.0)    # 平
    _seed_day("600004", "2026-11-13", 10.0, pct_open_move=0.10)   # 涨（涨停）
    j = client.get("/api/market/breadth?date=2026-11-13").get_json()
    assert j["ok"] is True
    b = j["breadth"]
    with get_conn() as conn:
        row = conn.execute(
            """SELECT SUM(CASE WHEN pct_change>0 THEN 1 ELSE 0 END) u,
                      SUM(CASE WHEN pct_change<0 THEN 1 ELSE 0 END) dn,
                      SUM(CASE WHEN pct_change=0 THEN 1 ELSE 0 END) f
               FROM daily_price WHERE trade_date='2026-11-13'"""
        ).fetchone()
    assert (b["up_count"], b["down_count"], b["flat_count"]) == (row["u"], row["dn"], row["f"])
    assert (b["up_count"], b["down_count"], b["flat_count"]) == (2, 1, 1)


def test_limit_up_main_and_gem(client):
    """主板 10% 封板/炸板；创业板 20% 封板。"""
    _seed_stock("600010")  # 主板封涨停 close=11.00
    _seed_stock("300010")  # 创业板封涨停 close=12.00
    _seed_stock("600011")  # 主板炸板 high=11.00 close=10.50
    _seed_day("600010", "2026-11-12", 10.0)
    _seed_day("300010", "2026-11-12", 10.0)
    _seed_day("600011", "2026-11-12", 10.0)
    _seed_day("600010", "2026-11-13", 10.0, pct_open_move=0.10)
    _seed_day("300010", "2026-11-13", 10.0, pct_open_move=0.20)
    _seed_day("600011", "2026-11-13", 10.0, close=10.50, high=11.00)
    j = client.get("/api/market/breadth?date=2026-11-13").get_json()
    lu = j["limit_up"]
    assert lu["sealed"] == 2  # 600010 + 300010
    assert lu["blown"] == 1   # 600011
    assert lu["streaks"]["1"] == 2


def test_st_and_noprev_excluded(client):
    """ST 与缺前收不计入涨停统计，但照常进涨跌平家数。"""
    _seed_stock("600020", name="ST某某")
    _seed_stock("600021")  # 首日：无前收
    _seed_day("600020", "2026-11-12", 9.0)
    _seed_day("600020", "2026-11-13", 10.0, pct_open_move=0.05)  # ST 5% 触板也排除
    _seed_day("600021", "2026-11-13", 10.0, pct_open_move=0.30)  # 无 prev → 排除
    j = client.get("/api/market/breadth?date=2026-11-13").get_json()
    assert j["limit_up"]["sealed"] == 0
    assert j["excluded"]["st"] >= 1
    assert j["excluded"]["no_prev_close"] >= 1
    assert j["breadth"]["up_count"] == 2  # 涨跌平不排除任何行


def test_streak_layering(client):
    """三连板落 3plus 层，两连板落 2 层（显式价格链：9.09→10.00→11.00→12.10）。"""
    _seed_stock("600030")
    _seed_stock("600031")
    with get_conn() as conn:
        def put(code, dd, close):
            conn.execute(
                "INSERT OR REPLACE INTO daily_price(code, trade_date, open, high, low, close, "
                "volume, amount, pct_change) VALUES(?,?,?,?,?,?,?,?,?)",
                (code, dd, close, close, close, close, 1000.0, 1000.0 * close, 0.0),
            )
        # 600030：11-10 基准，11-11/12/13 连续封板 → 3板+
        put("600030", "2026-11-10", 9.09)
        put("600030", "2026-11-11", 10.00)   # round(9.09*1.1,2)=10.00
        put("600030", "2026-11-12", 11.00)
        put("600030", "2026-11-13", 12.10)
        # 600031：11-11 基准未封，11-12/13 封 → 2板（11-11 无前收，链路自然截断）
        put("600031", "2026-11-11", 10.00)
        put("600031", "2026-11-12", 11.00)
        put("600031", "2026-11-13", 12.10)
    j = client.get("/api/market/breadth?date=2026-11-13").get_json()
    st = j["limit_up"]["streaks"]
    assert st["3plus"] == 1 and st["2"] == 1 and st["1"] == 0
    assert j["limit_up"]["sealed"] == 2


def test_date_param_default_latest(client):
    """不传 date = daily_price 最新交易日。"""
    _seed_stock("600040")
    _seed_day("600040", "2026-11-11", 9.5)
    _seed_day("600040", "2026-11-12", 10.0)
    j = client.get("/api/market/breadth").get_json()
    assert j["trade_date"] == "2026-11-12"
    # 无未来函数：查询早期日，之后的行情不得进入统计
    j2 = client.get("/api/market/breadth?date=2026-11-11").get_json()
    assert j2["breadth"]["total_rows"] == 1
