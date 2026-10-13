"""tools/profile_build_signal.py —— cProfile 下钻 _build_signal_records 内部热点

目标：确认 build_signal 的 72% 里隐性 I/O（筹码 warmup 2600 自然日补读 / vol_ratio
520 自然日补读）占多少，避免凭假设优化。

协议（轻量，约 ~30s 完成）：
  1. 复用 bench_recalc 克隆好的快照（不传则现场克隆）；
  2. monkeypatch 把 codes 截前 N 只（默认 200，含自然分布）；
  3. cProfile 跑一次 recalc_incremental_signals，落 .prof + pstats top 30。

用法：
  python tools/profile_build_signal.py --db data_cache/bench_snapshot_20261009.db
  python tools/profile_build_signal.py --db <clone.db> --sample 200
"""
import argparse
import cProfile
import os
import pstats
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _clone(src, dst):
    t0 = time.time()
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return time.time() - t0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(root, "core", "quant.db"))
    parser.add_argument("--sample", type=int, default=200, help="抽样股票数")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"源快照不存在：{args.db}", file=sys.stderr)
        return 2

    # 复用已有克隆 / 现场克隆
    if os.path.basename(args.db).startswith("recalc_probe") and os.path.basename(args.db).endswith(".db"):
        clone_path = args.db
        print(f"复用克隆：{clone_path}")
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        clone_path = os.path.join(root, "data_cache", f"recalc_probe_{ts}.db")
        print(f"克隆快照 → {clone_path} ...")
        print(f"克隆完成 {_clone(args.db, clone_path):.1f}s")

    from config import settings
    settings.DB_PATH = clone_path
    import core.db as db
    db.DB_PATH = clone_path

    with db.get_conn() as conn:
        scan_date = str(conn.execute("SELECT MAX(trade_date) FROM daily_price").fetchone()[0])
        codes = [r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_price WHERE trade_date=?", (scan_date,)).fetchall()]
    print(f"信号评估日：{scan_date}，抽样 {args.sample}/{len(codes)} 只")
    codes = codes[:args.sample]

    # monkeypatch: 让 recalc_incremental_signals 的 codes 只保留前 N 只
    import core.sync as sync
    _orig_recalc = sync.recalc_incremental_signals

    def _patched(trade_dates, **kw):
        # 先跑原函数拿到 codes 列表再替换
        import core.db as _db
        with _db.get_conn() as _c:
            _all = [r["code"] for r in _c.execute(
                "SELECT DISTINCT code FROM daily_price WHERE trade_date=?",
                (scan_date,)).fetchall()]
        # 临时替换 get_daily_price 内部的实际 codes 列表不易，
        # 换个更直接的办法：让 recalc 里的 DISTINCT code 查询只返回我们要的前 N 只
        # → 在 daily_price 里删除当日其它 code 的行（克隆库，随便造）
        with _db.get_conn() as _c:
            _ph = ",".join("?" * len(codes))
            _c.execute(f"DELETE FROM daily_price WHERE trade_date=? AND code NOT IN ({_ph})",
                       [scan_date] + codes)
        return _orig_recalc(trade_dates, **kw)

    sync.recalc_incremental_signals = _patched

    out = args.out or os.path.join(root, "reports", f"profile_build_signal_{datetime.now():%Y%m%d_%H%M%S}")
    prof = out + ".prof"
    print(f"开始 profile → {prof} ...")

    pr = cProfile.Profile()
    pr.enable()
    t0 = time.time()
    try:
        result = sync.recalc_incremental_signals([scan_date.replace("-", "")], verbose=False)
    finally:
        pr.disable()
        sync.recalc_incremental_signals = _orig_recalc
    wall = time.time() - t0

    pr.dump_stats(prof)
    with open(out + ".txt", "w", encoding="utf-8") as fh:
        fh.write(f"sample={args.sample} scan_date={scan_date} wall={wall:.1f}s\n")
        fh.write(f"result: signals={result.get('signals')} codes={result.get('codes')}\n\n")
        ps = pstats.Stats(prof, stream=fh).sort_stats("cumulative")
        ps.print_stats(40)
        fh.write("\n── by tottime (self time) ──\n")
        ps2 = pstats.Stats(prof, stream=fh).sort_stats("tottime")
        ps2.print_stats(30)
    print(f"wall={wall:.1f}s  signals={result.get('signals')} 结果 → {out}.txt")


if __name__ == "__main__":
    raise SystemExit(main())
