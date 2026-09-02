"""tools/backfill_signal_features.py —— 回填 stock_deep_signal 的排序特征列

为什么需要它
────────────
2026-09-02 给 stock_deep_signal 增加了 score / base_score / frac20 / risk_pct / atr_pct
五列（供 deep_tracker 的 top10 排序用，见 _rank_order_by）。但**历史已落库的候选行这些列
全是 NULL**，直接用新排序会让历史数据退化成旧行为（COALESCE(score,0)=0 → 全部落到低档）。
所以必须回填。

为什么不用 tools/backfill_deep_scan.py 全量重扫
──────────────────────────────────────────────
那是对全市场 4500 只重跑一遍扫描（约 45 秒/扫描日）。本脚本只对**已落库的候选行**重算
（每天约 1100 只，是前者的 1/4），且不写 signal 主字段，只 UPDATE 五列，快得多也安全得多。

无未来函数：每只股票都用 as_of=该行的 scan_date 重算，只用它当时可见的行情。

用法
────
    python tools/backfill_signal_features.py                 # 回填所有 NULL 行
    python tools/backfill_signal_features.py --all           # 全量重算（含已有值）
    python tools/backfill_signal_features.py --days 7        # 只回填最近 7 个扫描日
    python tools/backfill_signal_features.py --procs 12
    python tools/backfill_signal_features.py --dry-run       # 只统计不写库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import DB_PATH  # noqa: E402

FEATURE_COLS = ("score", "base_score", "frac20", "risk_pct", "atr_pct")

_wconn = None


def _conn():
    global _wconn
    if _wconn is None:
        _wconn = sqlite3.connect(DB_PATH, timeout=30)
        _wconn.row_factory = sqlite3.Row
        _wconn.execute("PRAGMA busy_timeout=10000")
    return _wconn


def _feat_one(args):
    """重算单行特征（进程内复用连接；spawn 安全）。"""
    scan_date, code = args
    try:
        from strategy.stock_deep import analyze_stock
        r = analyze_stock(_conn(), code, lookback=180, recent_days=0,
                          light=True, as_of=scan_date)
        if r.get("insufficient"):
            return None
        sig = r.get("signal") or {}
        return (scan_date, code, sig.get("score"), sig.get("base_score"),
                sig.get("frac20"), sig.get("risk_pct"), sig.get("atr_pct"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只回填最近 N 个扫描日（0=全部）")
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--all", action="store_true", help="全量重算（默认只补 NULL 行）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # 确保新列存在（旧库第一次跑时补列）
    try:
        from strategy.stock_deep import _ensure_market_signal_table
        _ensure_market_signal_table(conn)
    except Exception as e:
        print(f"⚠ 建表/补列失败：{e}")
        return 1

    where = "" if args.all else "WHERE score IS NULL"
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM stock_deep_signal ORDER BY scan_date")]
    if args.days:
        dates = set(dates[-args.days:])

    tasks = []
    for r in conn.execute(
            f"SELECT scan_date, code FROM stock_deep_signal {where}"):
        if args.days and r["scan_date"] not in dates:
            continue
        tasks.append((r["scan_date"], r["code"]))

    if not tasks:
        print("没有需要回填的行。")
        return 0
    n_days = len({t[0] for t in tasks})
    print(f"待回填 {len(tasks)} 行 / {n_days} 个扫描日，{args.procs} 进程"
          f"{'（dry-run，不写库）' if args.dry_run else ''}")

    t0 = time.time()
    rows, miss = [], 0
    with ProcessPoolExecutor(max_workers=args.procs) as ex:
        futs = [ex.submit(_feat_one, t) for t in tasks]
        done = 0
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if done % 2000 == 0:
                el = time.time() - t0
                print(f"    {done}/{len(tasks)}  用时 {el:.0f}s，预计剩余 "
                      f"{el / done * (len(tasks) - done):.0f}s", flush=True)
            if r:
                rows.append(r)
            else:
                miss += 1

    print(f"重算完成：{len(rows)} 行成功，{miss} 行失败（数据不足/异常）"
          f"，用时 {time.time() - t0:.0f}s")

    if args.dry_run or not rows:
        return 0

    conn.executemany(
        """UPDATE stock_deep_signal
           SET score=?, base_score=?, frac20=?, risk_pct=?, atr_pct=?
           WHERE scan_date=? AND code=?""",
        [(r[2], r[3], r[4], r[5], r[6], r[0], r[1]) for r in rows],
    )
    conn.commit()
    print(f"已写回 {len(rows)} 行。")

    # 校验
    left = conn.execute(
        "SELECT COUNT(*) c FROM stock_deep_signal WHERE score IS NULL").fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM stock_deep_signal").fetchone()["c"]
    print(f"校验：stock_deep_signal 共 {total} 行，score 仍为 NULL 的 {left} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
