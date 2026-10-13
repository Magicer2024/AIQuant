"""增量重算耗时测量（2026-10 优化计划 T3 基线 / T5 验收复测用）。

协议：
  - 源快照只读（sqlite backup 克隆出可写副本，测量绝不改动源快照/线上库）；
  - 用 StageTimer 记录 recalc_incremental_signals 整体与返回的 elapsed_s；
  - 每次 run 后对当日 stock_signal 全行业务列做指纹快照（排除自增 id 与每次
    INSERT OR REPLACE 都会刷新的 created_at），相邻两次 run 逐行对比，
    产出对称差行数 + 内容哈希，作为 T5「同日重算两次 diff=0」的硬证据；
  - 结果原样落 reports/bench_recalc_<ts>.json，供前后对照。

用法：
  python tools/bench_recalc_incremental.py --db data_cache/bench_snapshot_20261009.db
  python tools/bench_recalc_incremental.py --db <snapshot> --runs 3
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.timing import StageTimer  # noqa: E402


def _clone_db(src_path: str, dst_path: str) -> float:
    """sqlite 备份 API 克隆快照（WAL 一致），返回耗时秒。"""
    t0 = time.time()
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return time.time() - t0


def _point_db(path: str):
    """把 DB_PATH 指向可写克隆；不建库、不迁移。"""
    from config import settings
    settings.DB_PATH = path
    import core.db as db
    db.DB_PATH = path


# INSERT OR REPLACE 每次都会变的列：自增主键与写入时间戳，不属于信号业务内容
_DIFF_EXCLUDE_COLUMNS = ("id", "created_at")
_DIFF_SAMPLE_LIMIT = 10  # 出现差异时最多带出的样例行，防止报告爆量


def snapshot_day_signals(conn, scan_date: str) -> dict:
    """取评估日 stock_signal 全行业务列的排序快照。

    业务列由 PRAGMA 动态获取（排除 id/created_at），避免迁移新增列时漏比；
    行按全列排序后既保留逐行集合（供对称差定位差异），又算 sha256 指纹（快速判等）。
    """
    import hashlib
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(stock_signal)")]
    business_cols = [c for c in columns if c not in _DIFF_EXCLUDE_COLUMNS]
    order = ", ".join(business_cols)
    rows = conn.execute(
        f"SELECT {order} FROM stock_signal WHERE scan_date=? ORDER BY {order}",
        (scan_date,)).fetchall()
    row_tuples = [tuple(r[c] for c in business_cols) for r in rows]
    digest = hashlib.sha256(repr(row_tuples).encode("utf-8")).hexdigest()
    return {"columns": business_cols, "rows": row_tuples, "sha256": digest,
            "row_count": len(row_tuples)}


def diff_consecutive_snapshots(snaps: list) -> list:
    """相邻两次 run 的快照逐行对称差对比；diff_rows=0 即内容幂等。"""
    reports = []
    for idx in range(1, len(snaps)):
        prev, curr = snaps[idx - 1], snaps[idx]
        prev_set, curr_set = set(prev["rows"]), set(curr["rows"])
        reports.append({
            "between_runs": [idx, idx + 1],
            "rows_a": prev["row_count"],
            "rows_b": curr["row_count"],
            "diff_rows": len(prev_set ^ curr_set),
            "sha256_equal": prev["sha256"] == curr["sha256"],
            "sample_only_in_a": list(prev_set - curr_set)[:_DIFF_SAMPLE_LIMIT],
            "sample_only_in_b": list(curr_set - prev_set)[:_DIFF_SAMPLE_LIMIT],
        })
    return reports


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="增量重算耗时测量")
    parser.add_argument("--db", default=os.path.join(root, "core", "quant.db"),
                        help="源快照路径（只读，测量前自动克隆）")
    parser.add_argument("--runs", type=int, default=2, help="重复次数（同一信号评估日，幂等）")
    parser.add_argument("--out", default=None, help="JSON 结果输出路径")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print("源快照不存在：%s\n未运行测量，无数字可汇报。" % args.db, file=sys.stderr)
        return 2

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    clone_path = os.path.join(root, "data_cache", "recalc_probe_%s.db" % ts)
    print("克隆快照 %s → %s ..." % (args.db, clone_path))
    clone_s = _clone_db(args.db, clone_path)
    print("克隆完成 %.1fs" % clone_s)

    _point_db(clone_path)

    from core.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
    scan_date = str(row["d"])
    print("信号评估日：%s" % scan_date)

    from core.sync import recalc_incremental_signals

    runs = []
    snapshots = []
    for i in range(max(1, args.runs)):
        timer = StageTimer()
        t0 = time.time()
        with timer.stage("recalc_incremental"):
            result = recalc_incremental_signals(trade_dates=[scan_date], verbose=False)
        wall_s = time.time() - t0
        # 每次 run 后立即取当日信号全量指纹（含 horizon 三周期），供相邻 run 逐行 diff
        with get_conn() as conn:
            snap = snapshot_day_signals(conn, scan_date)
        sig_hash = snap["sha256"][:12]
        snapshots.append(snap)
        runs.append({
            "run": i + 1,
            "wall_s": round(wall_s, 2),
            "stage_ms": timer.report(),
            "signal_rows": snap["row_count"],
            "signal_sha256_12": sig_hash,
            "returned": result,
        })
        print("第 %d 次: wall=%.1fs elapsed_s=%s signals=%s codes=%s 当日信号行=%d hash=%s" % (
            i + 1, wall_s, result.get("elapsed_s"), result.get("signals"),
            result.get("codes"), snap["row_count"], sig_hash))

    diffs = diff_consecutive_snapshots(snapshots)
    for d in diffs:
        verdict = "一致（diff=0）" if d["diff_rows"] == 0 else "不一致！diff=%d" % d["diff_rows"]
        print("第 %s 次产出对比: 行 %d→%d, %s, sha256 %s" % (
            d["between_runs"], d["rows_a"], d["rows_b"], verdict,
            "相等" if d["sha256_equal"] else "不等"))

    out = args.out or os.path.join(root, "reports", "bench_recalc_%s.json" % ts)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "meta": {
                "db_source": args.db,
                "clone": clone_path,
                "clone_seconds": round(clone_s, 1),
                "scan_date": scan_date,
                "runs": args.runs,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "python": sys.version.split()[0],
            },
            "runs": runs,
            "signal_diff": diffs,
        }, fh, ensure_ascii=False, indent=2)
    print("结果已写入：%s" % out)
    print("（可写克隆保留在 %s，确认无用后可删）" % clone_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
