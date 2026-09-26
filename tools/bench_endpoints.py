"""接口性能测量脚手架（方案 F2「先测量，再优化」）。

固定测量协议：
  - 相同数据库快照（--db，默认 core/quant.db）与同一台机器；
  - 默认关闭网络（socket 被守卫拦截，任何联网尝试都会显式失败而非静默加延迟）；
  - 冷启动首次调用单独记录，随后预热若干次再正式采样；
  - 每个接口采样 ≥30 次（--samples），记录墙钟耗时的 P50/P95/min/max/mean、
    真实执行的 SQL 语句数、游标读取行数、tracemalloc 峰值内存；
  - 结果原样落 reports/bench_<ts>.json，供前后对照与验收门槛核对。

诚实原则：本工具只汇报真实测量值。数据库缺失、接口报错或样本不足时如实标注，
绝不用猜测或占位数字冒充性能结论。若未运行（无真实库/未执行），就没有数字。

用法：
  python tools/bench_endpoints.py                       # 默认四接口、30 采样
  python tools/bench_endpoints.py --samples 50 --warmup 5
  python tools/bench_endpoints.py --db path/to/snapshot.db --allow-network
  python tools/bench_endpoints.py --endpoint /api/investor/today --endpoint /api/investor/exit_advice
"""
import argparse
import json
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.timing import (  # noqa: E402
    SQL_COUNTER, percentiles, peak_memory, trace_sql, counting_connection_class,
)

# 计划 F2 指定的四个目标接口（今日推荐 / 出场跟踪 / 个股深度 / 历史列表）。
DEFAULT_ENDPOINTS = [
    "/api/investor/today?limit=4",
    "/api/investor/exit_advice",
    "/api/investor/stock_deep/market?limit=10",
    "/api/investor/reversal_history?days=60&limit=600",
]


class _NetworkBlocked(RuntimeError):
    pass


def _block_network():
    """把 socket 连接替换为显式抛错，确保测量在离线快照上进行。"""
    def _guard(*_a, **_k):
        raise _NetworkBlocked("网络已在性能测量中关闭（--allow-network 可解除）")

    socket.socket = _guard          # type: ignore[assignment]
    socket.create_connection = _guard  # type: ignore[assignment]


def _patch_db_instrumentation():
    """拦截 sqlite3.connect：注入计数 Connection 子类 + SQL 语句 trace。

    core.db.connect_db 内部调用 sqlite3.connect，无法事后改 factory，故直接在
    sqlite3 层注入。观测失败不影响被测路径（退回原始 connect）。
    """
    conn_cls = counting_connection_class(SQL_COUNTER)
    real_connect = sqlite3.connect

    def instrumented_connect(*args, **kwargs):
        kwargs.setdefault("factory", conn_cls)
        conn = real_connect(*args, **kwargs)
        try:
            trace_sql(conn, SQL_COUNTER)
        except Exception:
            print("[warn] 无法挂载 SQL trace：%s" % sys.exc_info()[1], file=sys.stderr)
        return conn

    sqlite3.connect = instrumented_connect


def _point_db(path):
    """把 DB_PATH 指向指定快照；不建库、不迁移、不写入。"""
    from config import settings
    settings.DB_PATH = path
    import core.db as db
    db.DB_PATH = path


def _measure_one(client, endpoint, samples, warmup):
    """对单个接口冷启动 + 预热 + 采样，返回真实测量字典。"""
    # 冷启动：全新 client 的首次调用（缓存未热）
    cold = _single_call(client, endpoint)

    for _ in range(max(0, warmup)):
        _single_call(client, endpoint)

    wall, stmts, rows, peaks, statuses = [], [], [], [], []
    errors = 0
    for _ in range(samples):
        r = _single_call(client, endpoint)
        if r["error"]:
            errors += 1
            continue
        wall.append(r["wall_ms"])
        stmts.append(r["statements"])
        rows.append(r["rows_read"])
        peaks.append(r["peak_bytes"])
        statuses.append(r["status"])

    ok_samples = len(wall)
    result = {
        "endpoint": endpoint,
        "requested_samples": samples,
        "ok_samples": ok_samples,
        "errors": errors,
        "cold_start": cold,
        "wall_ms": percentiles(wall),
        "sql_statements": percentiles(stmts),
        "rows_read": percentiles(rows),
        "peak_memory_bytes": percentiles(peaks),
        "http_statuses": sorted(set(statuses)),
    }
    return result


def _single_call(client, endpoint):
    SQL_COUNTER.reset()
    err = None
    status = None
    with peak_memory() as mem:
        t0 = time.perf_counter()
        try:
            resp = client.get(endpoint)
            status = resp.status_code
            if status >= 500:
                err = "HTTP %s" % status
        except Exception as exc:  # noqa: BLE001
            err = "%s: %s" % (type(exc).__name__, exc)
        wall_ms = (time.perf_counter() - t0) * 1000.0
    snap = SQL_COUNTER.snapshot()
    return {
        "wall_ms": wall_ms,
        "statements": snap["statements"],
        "rows_read": snap["rows_read"],
        "peak_bytes": mem["peak_bytes"],
        "status": status,
        "error": err,
    }


def _fmt_stat(label, stat, unit=""):
    if not stat or stat.get("count", 0) == 0:
        return "  %-22s 无有效样本" % label
    return "  %-22s P50=%.1f%s P95=%.1f%s min=%.1f max=%.1f mean=%.1f (n=%d)" % (
        label, stat["p50"], unit, stat["p95"], unit, stat["min"], stat["max"],
        stat["mean"], stat["count"],
    )


def _print_report(results, meta):
    print("=" * 78)
    print("接口性能测量（方案 F2）")
    print("  数据库快照 : %s" % meta["db"])
    print("  网络       : %s" % ("开启" if meta["network_allowed"] else "关闭"))
    print("  预热/采样  : warmup=%d samples=%d" % (meta["warmup"], meta["samples"]))
    print("  信号模式   : %s" % meta["signal_model_mode"])
    print("  时间       : %s" % meta["timestamp"])
    print("=" * 78)
    for r in results:
        print("\n> %s" % r["endpoint"])
        if r["ok_samples"] == 0:
            cold = r["cold_start"]
            reason = cold.get("error") or "无成功样本"
            print("  [!] 未获得有效测量（%s）。不杜撰数字。" % reason)
            continue
        cold = r["cold_start"]
        if not cold.get("error"):
            print("  冷启动首次        : %.1fms  SQL=%d rows=%d" % (
                cold["wall_ms"], cold["statements"], cold["rows_read"]))
        print(_fmt_stat("墙钟耗时", r["wall_ms"], "ms"))
        print(_fmt_stat("SQL 语句数", r["sql_statements"]))
        print(_fmt_stat("读取行数", r["rows_read"]))
        print(_fmt_stat("峰值内存", r["peak_memory_bytes"], "B"))
        if r["errors"]:
            print("  采样错误数        : %d / %d" % (r["errors"], r["requested_samples"]))
    print("\n" + "=" * 78)


def main():
    # Windows 控制台默认 GBK；重配为 UTF-8 容错，避免报告中的符号触发编码错误。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="AIQuant 接口性能测量脚手架（方案 F2）")
    parser.add_argument("--db", default=None,
                        help="数据库快照路径（默认 core/quant.db）")
    parser.add_argument("--samples", type=int, default=30, help="每接口采样次数（≥30）")
    parser.add_argument("--warmup", type=int, default=3, help="预热次数")
    parser.add_argument("--endpoint", action="append", default=None,
                        help="指定接口（可重复）；缺省用计划 F2 的四个目标接口")
    parser.add_argument("--allow-network", action="store_true",
                        help="不关闭网络（偏离固定协议，仅用于排查）")
    parser.add_argument("--out", default=None, help="JSON 结果输出路径")
    args = parser.parse_args()

    if args.samples < 1:
        print("samples 必须 ≥1", file=sys.stderr)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = args.db or os.path.join(root, "core", "quant.db")
    if not os.path.exists(db_path):
        print("数据库快照不存在：%s\n未运行测量，无数字可汇报。" % db_path, file=sys.stderr)
        return 2

    endpoints = args.endpoint or DEFAULT_ENDPOINTS

    _point_db(db_path)
    if not args.allow_network:
        _block_network()
    _patch_db_instrumentation()

    # create_app 无数据库/网络/线程副作用；不调用 initialize_runtime（不建库、不调度）。
    from app import create_app
    from config.settings import SIGNAL_MODEL_MODE
    app = create_app()
    app.config["TESTING"] = True   # 关调度/自动同步副作用；仅影响后台任务，不改 GET 读路径
    client = app.test_client()

    results = []
    for ep in endpoints:
        results.append(_measure_one(client, ep, args.samples, args.warmup))

    meta = {
        "db": db_path,
        "network_allowed": bool(args.allow_network),
        "warmup": args.warmup,
        "samples": args.samples,
        "signal_model_mode": SIGNAL_MODEL_MODE,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
    }
    _print_report(results, meta)

    out = args.out or os.path.join(
        root, "reports", "bench_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "results": results}, fh, ensure_ascii=False, indent=2)
    print("结果已写入：%s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
