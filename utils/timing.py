"""
utils/timing.py — 请求级性能监控 + 结构化阶段计时（方案 F2）。

两部分：
1. init_app(app)：给 Flask 应用挂 before/after 钩子，记录整请求耗时，
   慢请求（>0.5s）以 WARNING 输出，并把 request_id 一并落日志。
2. 测量脚手架原语（供 tools/bench_endpoints.py 及诊断脚本复用）：
   - StageTimer：命名阶段计时（上下文管理器），累计各阶段耗时与调用次数。
   - percentiles：从样本列表算 P50/P95/min/max/mean/count，不杜撰、不插值造数。
   - peak_memory：tracemalloc 峰值内存上下文管理器。
   - SqlCounter / counting_cursor_factory / trace_sql：统计真实执行的 SQL 语句数
     与游标读取行数（只读观测，不改业务语义）。

设计原则（方案 F2）：所有指标均来自真实测量；样本不足或无法测量时如实返回，
绝不用猜测值冒充。测量协议（同库快照 / 同机器 / 网络关闭 / 冷启动+预热 /
≥30 次采样 / P50-P95）由 tools/bench_endpoints.py 承载。
"""
import time
import logging
import sqlite3
import threading
from contextlib import contextmanager

from flask import g, request

logger = logging.getLogger("timing")


def init_app(app):
    """Register timing hooks on a Flask application."""

    @app.before_request
    def _start_timer():
        g.start_time = time.time()

    @app.after_request
    def _log_time(response):
        if hasattr(g, "start_time"):
            elapsed = time.time() - g.start_time
            level = logging.WARNING if elapsed > 0.5 else logging.INFO
            rid = getattr(g, "request_id", "-")
            logger.log(level, "[%s] %s %s - %.3fs", rid, request.method, request.path, elapsed)
        return response


# ─────────────────────────────────────────────
# 阶段计时
# ─────────────────────────────────────────────

class StageTimer:
    """命名阶段计时器：累计每个阶段的墙钟耗时与调用次数。

    用法::

        t = StageTimer()
        with t.stage("load_prices"):
            ...
        with t.stage("score"):
            ...
        print(t.report())   # {"load_prices": {"ms": .., "calls": ..}, ...}

    同一阶段名可多次进入，耗时与次数累加。非线程安全，按请求/任务实例使用。
    """

    def __init__(self):
        self._stages = {}
        self._started = time.perf_counter()

    @contextmanager
    def stage(self, name):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            rec = self._stages.setdefault(name, {"ms": 0.0, "calls": 0})
            rec["ms"] += elapsed_ms
            rec["calls"] += 1

    def total_ms(self):
        """从计时器创建到现在的墙钟总耗时（毫秒）。"""
        return (time.perf_counter() - self._started) * 1000.0

    def report(self):
        """返回各阶段 {ms, calls} 及 total_ms 的快照（浅拷贝）。"""
        return {
            "stages": {k: dict(v) for k, v in self._stages.items()},
            "total_ms": self.total_ms(),
        }


# ─────────────────────────────────────────────
# 统计
# ─────────────────────────────────────────────

def percentiles(samples):
    """从样本列表算 count/min/P50/P95/max/mean。

    - 空样本返回 {"count": 0, ...}，各分位为 None（不杜撰）。
    - P50/P95 用最近秩法（nearest-rank），与常见监控口径一致，不做插值造数。
    """
    vals = [float(s) for s in samples if s is not None]
    n = len(vals)
    if n == 0:
        return {"count": 0, "min": None, "p50": None, "p95": None,
                "max": None, "mean": None}
    ordered = sorted(vals)

    def _nearest_rank(pct):
        # 最近秩：ceil(pct/100 * n)，至少取第 1 个
        import math
        idx = max(1, math.ceil(pct / 100.0 * n))
        return ordered[min(idx, n) - 1]

    return {
        "count": n,
        "min": ordered[0],
        "p50": _nearest_rank(50),
        "p95": _nearest_rank(95),
        "max": ordered[-1],
        "mean": sum(ordered) / n,
    }


@contextmanager
def peak_memory():
    """tracemalloc 峰值内存上下文管理器。

    yield 一个可变 holder，退出后 holder["peak_bytes"] 为区间内 Python 层
    分配峰值（不含未 tracemalloc 跟踪的 C 扩展内部缓冲）。测量结束后由
    调用方读取，绝不在无数据时给出估计值。
    """
    import tracemalloc
    holder = {"peak_bytes": None}
    already = tracemalloc.is_tracing()
    if not already:
        tracemalloc.start()
    try:
        yield holder
    finally:
        if not already:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        else:
            _, peak = tracemalloc.get_traced_memory()
        holder["peak_bytes"] = peak


# ─────────────────────────────────────────────
# SQL / 行数观测（只读统计，不改业务语义）
# ─────────────────────────────────────────────

class SqlCounter:
    """线程本地的 SQL 语句数与游标读取行数计数器。

    供测量脚手架挂到连接上观测真实数据库负载；生产路径不启用。
    """

    def __init__(self):
        self._local = threading.local()

    def reset(self):
        self._local.statements = 0
        self._local.rows = 0

    def add_statement(self):
        self._local.statements = getattr(self._local, "statements", 0) + 1

    def add_rows(self, n):
        self._local.rows = getattr(self._local, "rows", 0) + int(n)

    def snapshot(self):
        return {
            "statements": getattr(self._local, "statements", 0),
            "rows_read": getattr(self._local, "rows", 0),
        }


# 全局共享计数器；bench 脚手架与 trace_sql/counting_cursor_factory 都指向它。
SQL_COUNTER = SqlCounter()


def trace_sql(conn, counter=None):
    """给连接挂 set_trace_callback，统计真实执行的 SQL 语句数。"""
    counter = counter or SQL_COUNTER

    def _cb(_statement):
        counter.add_statement()

    conn.set_trace_callback(_cb)
    return conn


def counting_connection_class(counter=None):
    """返回一个 sqlite3.Connection 子类，把游标读取行数记入 counter。

    仅用于测量。跨版本可靠：Python 3.11 的 Connection 无 cursor_factory 属性，
    故这里重写 cursor()/execute() 显式使用计数游标（Connection.execute 的 C 实现
    不走 Python 层 cursor()，必须一并重写才能覆盖 conn.execute(...) 读路径）。

    用法（脚手架）::

        ConnCls = counting_connection_class(SQL_COUNTER)
        real_connect = sqlite3.connect
        sqlite3.connect = lambda *a, **k: real_connect(*a, **{**k, "factory": ConnCls})
    """
    counter = counter or SQL_COUNTER

    class CountingCursor(sqlite3.Cursor):
        def fetchone(self):
            row = super().fetchone()
            if row is not None:
                counter.add_rows(1)
            return row

        def fetchall(self):
            rows = super().fetchall()
            counter.add_rows(len(rows))
            return rows

        def fetchmany(self, size=None):
            rows = super().fetchmany(size) if size is not None else super().fetchmany()
            counter.add_rows(len(rows))
            return rows

        def __next__(self):
            row = super().__next__()
            counter.add_rows(1)
            return row

    class CountingConnection(sqlite3.Connection):
        def cursor(self, factory=None, *args, **kwargs):
            return super().cursor(factory or CountingCursor, *args, **kwargs)

        def execute(self, *args, **kwargs):
            return self.cursor(CountingCursor).execute(*args, **kwargs)

    CountingConnection.CountingCursor = CountingCursor
    return CountingConnection
