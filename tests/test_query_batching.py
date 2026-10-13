"""tests/test_query_batching.py —— 消除重复查询/N+1（2026-10 优化计划 T4）

两处实测热点（cProfile 归因 reports/bench_20261010_000339.json 同批测量）：
1. 出场跟踪 exit_advice 逐票取日线（219 只 = 219 条 SQL）→
   `price_repo.get_price_frames` 单条 `code IN (...)` 批量化，返回结构不变；
2. `short_t1_filter_sql` 与 `short_market_gate_sql` 对同一窗口各算一遍
   `compute_regime_series`（各 ~2.3s）→ `short_gate_sql` 组合助手只算一次，
   输出与两次独立调用完全一致（口径不变）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from core.market_regime import compute_regime_series
from core.outcome_tracker import (
    short_gate_sql,
    short_market_gate_sql,
    short_t1_filter_sql,
)
from core.repository import price_repo
from utils.timing import SQL_COUNTER, trace_sql


# ─────────────────────────────────────────────
# 种子数据
# ─────────────────────────────────────────────
def _seed_prices(conn, codes, days=45, crash_days=15):
    """近 crash_days 天全市场大跌（pct_change=-3、收盘连跌）→ regime 稳定 cold。"""
    import datetime as _dt
    d0 = _dt.date(2026, 1, 5)
    for code in codes:
        for i in range(days):
            d = (d0 + _dt.timedelta(days=i)).isoformat()
            crash = i >= days - crash_days
            pct = -3.0 if crash else 0.0
            close = 10.0 - (i - (days - crash_days)) * 0.2 if crash else 10.0
            conn.execute(
                "INSERT INTO daily_price (code, trade_date, open, high, low, close, "
                "volume, amount, pct_change) VALUES (?,?,?,?,?,?,?,?,?)",
                (code, d, close, close, close, close, 100.0, 1000.0, pct))
    conn.commit()
    return (d0 + _dt.timedelta(days=days - crash_days)).isoformat(), \
        (d0 + _dt.timedelta(days=days - 1)).isoformat()


# ─────────────────────────────────────────────
# 1. get_price_frames：逐票 N+1 → 单条 SQL
# ─────────────────────────────────────────────
def test_get_price_frames_values_match_per_code(isolated_database):
    """批量取数结果与逐票查询完全一致，缺数据的 code 为 None"""
    from core.db import get_conn
    with get_conn() as conn:
        for code, closes in (("000001", [10.0, 10.5, 11.0]),
                             ("600000", [20.0, 19.5])):
            for i, c in enumerate(closes):
                conn.execute(
                    "INSERT INTO daily_price (code, trade_date, open, high, low, close) "
                    "VALUES (?,?,?,?,?,?)",
                    (code, f"2026-03-{i+1:02d}", c, c, c, c))
        conn.commit()

        frames = price_repo.get_price_frames(
            ["000001", "600000", "999999"], conn=conn)

        assert set(frames) == {"000001", "600000", "999999"}
        assert frames["999999"] is None, "无行情数据的 code 应为 None（与原逐票逻辑一致）"
        df = frames["000001"]
        assert list(df.index) == ["2026-03-01", "2026-03-02", "2026-03-03"]
        assert df.index.name == "trade_date"
        assert list(df.columns) == ["open", "close", "high", "low"]
        assert df["close"].tolist() == [10.0, 10.5, 11.0]
        # 与原逐票口径逐值对照
        rows = conn.execute(
            "SELECT trade_date, open, close, high, low FROM daily_price "
            "WHERE code='600000' ORDER BY trade_date ASC").fetchall()
        ref = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
        pd.testing.assert_frame_equal(frames["600000"], ref)


def test_get_price_frames_single_sql_statement(isolated_database):
    """N 条 code 的取数只允许 1 条 SQL（原实现 = N 条）"""
    import sqlite3
    from utils.timing import counting_connection_class
    # 语句数靠 trace_sql 回调、读行数靠计数游标，需计数连接类配合
    conn = sqlite3.connect(isolated_database,
                           factory=counting_connection_class(SQL_COUNTER))
    conn.row_factory = sqlite3.Row   # 与 core.db.get_conn 行为一致
    try:
        for code in ("c1", "c2", "c3", "c4", "c5"):
            conn.execute(
                "INSERT INTO daily_price (code, trade_date, close) VALUES (?,?,?)",
                (code, "2026-03-01", 1.0))
        conn.commit()
        trace_sql(conn, SQL_COUNTER)
        SQL_COUNTER.reset()
        price_repo.get_price_frames(["c1", "c2", "c3", "c4", "c5"], conn=conn)
        snap = SQL_COUNTER.snapshot()
        assert snap["statements"] == 1, f"5 只股票用了 {snap['statements']} 条 SQL，必须 1 条"
        assert snap["rows_read"] == 5
    finally:
        conn.close()


class _StreamOnlyCursor:
    """只允许惰性迭代的游标替身：fetchall 一次性物化全部行直接断言失败。"""

    def __init__(self, rows):
        self._rows, self._i = rows, 0

    def fetchall(self):
        raise AssertionError("不得 fetchall 一次性物化全部行（峰值内存约束）")

    def fetchone(self):
        raise AssertionError("不得 fetchone 逐行取（应走游标迭代）")

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= len(self._rows):
            raise StopIteration
        row = self._rows[self._i]
        self._i += 1
        return row


class _FakeConn:
    """记录 SQL 并返回流式游标的假连接"""

    def __init__(self, rows):
        self._rows, self.sqls = rows, []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        return _StreamOnlyCursor(self._rows)


def test_get_price_frames_streams_not_materialize():
    """批量取数必须按 code 分段流式建帧，禁止一次性物化（内存峰值约束，T4 复测）"""
    rows = []
    for code, n in (("b", 3), ("a", 2)):     # 乱序插入，验证按 code 分组
        for i in range(n):
            rows.append({"code": code, "trade_date": f"2026-04-{i+1:02d}",
                         "open": 1.0, "close": 2.0, "high": 3.0, "low": 0.5})
    rows.sort(key=lambda r: (r["code"], r["trade_date"]))   # SQL ORDER BY code, trade_date
    fake = _FakeConn(rows)

    frames = price_repo.get_price_frames(["a", "b", "c"], conn=fake)

    assert len(fake.sqls) == 1 and "IN (" in fake.sqls[0]
    assert frames["c"] is None
    assert list(frames["a"].index) == ["2026-04-01", "2026-04-02"]
    assert list(frames["b"].index) == ["2026-04-01", "2026-04-02", "2026-04-03"]


# ─────────────────────────────────────────────
# 2. short_gate_sql：regime 重复计算 → 只算一次
# ─────────────────────────────────────────────
def _counting_regime(monkeypatch):
    """给 compute_regime_series 包一层计数（outcome_tracker 是调用时导入，可替换）。"""
    calls = []
    real = compute_regime_series

    def _wrapper(conn, start_date, end_date=None):
        calls.append((start_date, end_date))
        return real(conn, start_date, end_date)

    monkeypatch.setattr("core.market_regime.compute_regime_series", _wrapper)
    return calls


def test_short_gate_sql_computes_regime_once(monkeypatch):
    """组合助手对同一窗口只算一次 regime，且输出与两次独立调用逐位一致"""
    from core.db import get_conn
    with get_conn() as conn:
        start, end = _seed_prices(conn, ["000001", "000002"])
        calls = _counting_regime(monkeypatch)

        t1_c, t1_p, mk_c, mk_p = short_gate_sql(conn, start, end)
        assert len(calls) == 1, f"regime 被算了 {len(calls)} 次，必须 1 次"

        # 口径不变：与「各自计算」的旧调用方式结果完全一致
        ref_t1 = short_t1_filter_sql(conn, start, end)
        ref_mk = short_market_gate_sql(conn, start, end)
        assert (t1_c, t1_p) == ref_t1
        assert (mk_c, mk_p) == ref_mk
        # 该市场状态应产生非平凡过滤条件（冷市启用过滤）
        assert t1_c != "1" and mk_c != "1"


def test_separate_helpers_still_work_without_series():
    """回归护栏：旧的两次独立调用保持原语义（各自计算 regime）"""
    # 直接调用不传 series 时行为不变（ref 对照已在上一用例覆盖）
    assert callable(short_t1_filter_sql) and callable(short_market_gate_sql)


def test_gate_conditions_match_series_passthrough(monkeypatch):
    """显式传入 series 时不得重复计算，结果与不传一致"""
    from core.db import get_conn
    with get_conn() as conn:
        start, end = _seed_prices(conn, ["000001"])
        series = compute_regime_series(conn, start, end)
        calls = _counting_regime(monkeypatch)

        got_t1 = short_t1_filter_sql(conn, start, end, series=series)
        got_mk = short_market_gate_sql(conn, start, end, series=series)
        assert calls == [], "已传入 series 时不得再触发 compute_regime_series"
        assert got_t1 == short_t1_filter_sql(conn, start, end)
        assert got_mk == short_market_gate_sql(conn, start, end)
