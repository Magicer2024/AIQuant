"""tests/test_timing.py —— 阶段计时与测量原语（方案 F2）

覆盖 utils/timing.py 的测量脚手架原语：percentiles 分位数、StageTimer 阶段计时、
SqlCounter + counting_cursor_factory + trace_sql 的真实 SQL/行数观测、peak_memory
峰值内存。所有断言基于确定性构造，不依赖真实行情库或网络。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

from utils.timing import (
    StageTimer, SqlCounter, percentiles, peak_memory,
    trace_sql, counting_connection_class,
)


# ── percentiles ──────────────────────────────────────────────

def test_percentiles_empty_is_honest():
    stat = percentiles([])
    assert stat["count"] == 0
    # 无样本不杜撰：分位值一律 None
    assert stat["p50"] is None and stat["p95"] is None
    assert stat["min"] is None and stat["max"] is None and stat["mean"] is None


def test_percentiles_nearest_rank():
    # 1..100：最近秩 P50=50、P95=95
    stat = percentiles(range(1, 101))
    assert stat["count"] == 100
    assert stat["min"] == 1.0 and stat["max"] == 100.0
    assert stat["p50"] == 50.0
    assert stat["p95"] == 95.0
    assert abs(stat["mean"] - 50.5) < 1e-9


def test_percentiles_single_sample():
    stat = percentiles([42])
    assert stat["count"] == 1
    assert stat["p50"] == 42.0 and stat["p95"] == 42.0
    assert stat["min"] == 42.0 and stat["max"] == 42.0


def test_percentiles_ignores_none():
    stat = percentiles([1, None, 3])
    assert stat["count"] == 2
    assert stat["min"] == 1.0 and stat["max"] == 3.0


# ── StageTimer ───────────────────────────────────────────────

def test_stage_timer_accumulates_calls():
    t = StageTimer()
    for _ in range(3):
        with t.stage("load"):
            pass
    with t.stage("score"):
        pass
    rep = t.report()
    assert rep["stages"]["load"]["calls"] == 3
    assert rep["stages"]["score"]["calls"] == 1
    assert rep["stages"]["load"]["ms"] >= 0.0
    assert rep["total_ms"] >= 0.0
    # report 是快照，改动不影响内部状态
    rep["stages"]["load"]["calls"] = 999
    assert t.report()["stages"]["load"]["calls"] == 3


# ── SqlCounter + cursor factory + trace ──────────────────────

def _make_counted_conn(counter):
    conn_cls = counting_connection_class(counter)
    conn = sqlite3.connect(":memory:", factory=conn_cls)
    conn.row_factory = sqlite3.Row
    trace_sql(conn, counter)
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    conn.commit()
    return conn


def test_sql_counter_rows_and_statements():
    counter = SqlCounter()
    conn = _make_counted_conn(counter)
    counter.reset()   # 清掉建表/插入阶段的计数，只观测查询
    rows = conn.execute("SELECT a FROM t").fetchall()
    assert len(rows) == 10
    snap = counter.snapshot()
    assert snap["rows_read"] == 10
    assert snap["statements"] >= 1
    conn.close()


def test_sql_counter_fetchone_and_iteration():
    counter = SqlCounter()
    conn = _make_counted_conn(counter)
    counter.reset()
    cur = conn.execute("SELECT a FROM t ORDER BY a")
    first = cur.fetchone()
    assert first is not None
    rest = list(cur)               # 迭代剩余 9 行
    assert len(rest) == 9
    snap = counter.snapshot()
    assert snap["rows_read"] == 10  # 1 (fetchone) + 9 (迭代)
    conn.close()


def test_sql_counter_thread_local_isolation():
    counter = SqlCounter()
    counter.add_rows(5)
    counter.add_statement()
    # 另一线程读到的是各自的本地计数（初始 0），互不串扰
    seen = {}

    def worker():
        seen["snap"] = counter.snapshot()

    import threading
    th = threading.Thread(target=worker)
    th.start()
    th.join()
    assert seen["snap"] == {"statements": 0, "rows_read": 0}
    assert counter.snapshot() == {"statements": 1, "rows_read": 5}


# ── peak_memory ──────────────────────────────────────────────

def test_peak_memory_records_bytes():
    with peak_memory() as mem:
        _ = [b"x" * 1024 for _ in range(256)]   # 触发可观分配
        assert mem["peak_bytes"] is None        # 区间内尚未填充
    # 退出后必须已填充真实峰值（tracemalloc 正常工作时为正整数）
    assert isinstance(mem["peak_bytes"], int)
    assert mem["peak_bytes"] > 0
