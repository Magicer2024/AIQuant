"""tools/rebuild_deep_track.py —— 用当前排序键重建 deep_track 跟踪单，并给出新旧对比

用途
────
改了候选排序键（见 stock_deep.candidate_order_by）之后，历史已建的跟踪单仍是**旧排序**选的。
本脚本在不重跑全市场扫描的前提下（扫描结果 stock_deep_signal 已在库，且 score 等特征列
已由 tools/backfill_signal_features.py 回填），按新排序重建全部跟踪单，并统计新旧口径差异。

为什么不需要重扫
────────────────
sync_from_scan 只读 stock_deep_signal + deep_track + daily_price，不调用 analyze_stock，
所以改排序键后重建只需几分钟（不是回补扫描那种几十分钟）。

两种模式
────────
    python tools/rebuild_deep_track.py              # dry-run（默认）：在临时表跑完就删，不动真实数据
    python tools/rebuild_deep_track.py --apply      # 真实重建：先备份到 deep_track_bak_YYYYMMDD

⚠ --apply 会清空并重建 deep_track。备份表会一直保留，确认无误后可自行 DROP。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics as st
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import DB_PATH  # noqa: E402
import strategy.deep_tracker as dt  # noqa: E402

SIM_TABLE = "deep_track__sim"


def _stats(conn, table: str) -> dict:
    rows = conn.execute(
        f"""SELECT return_pct, exit_reason, hold_tdays FROM {table}
            WHERE status = 'closed' AND return_pct IS NOT NULL""").fetchall()
    if not rows:
        return {"n": 0}
    rets = [r[0] for r in rows]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    reasons = [r[1] for r in rows]
    pf = (sum(wins) or 0.0) / (abs(sum(losses)) or 1e-9)
    return {
        "n": len(rets),
        "mean": st.mean(rets),
        "median": st.median(rets),
        "win": len(wins) / len(rets) * 100,
        "pf": pf,
        "avg_hold": st.mean([r[2] or 0 for r in rows]),
        "stop_loss": reasons.count("stop_loss") / len(reasons) * 100,
        "trailing": reasons.count("trailing_stop") / len(reasons) * 100,
        "max_hold": reasons.count("max_hold_days") / len(reasons) * 100,
    }


def _print(tag: str, s: dict):
    if not s.get("n"):
        print(f"  {tag:<10} 无已平仓单")
        return
    print(f"  {tag:<10} n={s['n']:>4}  均值={s['mean']:>7.2f}%  中位={s['median']:>7.2f}%  "
          f"胜率={s['win']:>5.1f}%  PF={s['pf']:>5.2f}  均持={s['avg_hold']:>4.1f}天  "
          f"止损{s['stop_loss']:>4.1f}% / 移动止盈{s['trailing']:>4.1f}% / 到期{s['max_hold']:>4.1f}%")


def _rebuild(conn, table: str, dates: list) -> dict:
    dt._TABLE = table
    dt.ensure_table(conn)
    conn.execute(f"DELETE FROM {table}")
    conn.commit()
    added = skipped = 0
    for d in dates:
        try:
            s = dt.sync_from_scan(conn, d)
            added += s["added"]
            skipped += s["skipped"]
        except Exception as e:
            print(f"    ⚠ 建单失败 {d}: {e}")
    u = dt.update_open_tracks(conn, limit=10000)
    return {"added": added, "skipped": skipped, **u}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真实重建（默认 dry-run，在临时表跑完即删）")
    ap.add_argument("--days", type=int, default=0, help="只重建最近 N 个扫描日")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM stock_deep_signal ORDER BY scan_date")]
    if args.days:
        dates = dates[-args.days:]
    print(f"扫描日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）")

    old = _stats(conn, "deep_track")
    print("\n【旧口径】当前 deep_track（按改动前的排序键建的）")
    _print("旧", old)

    table = "deep_track" if args.apply else SIM_TABLE
    if args.apply:
        bak = f"deep_track_bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"\n⚠ --apply：先备份 deep_track → {bak}")
        conn.execute(f"DROP TABLE IF EXISTS {bak}")
        conn.execute(f"CREATE TABLE {bak} AS SELECT * FROM deep_track")
        conn.commit()
    else:
        print(f"\n[dry-run] 在临时表 {SIM_TABLE} 上重建，真实数据不动")

    print("重建中（建单 + 推进出场）…")
    info = _rebuild(conn, table, dates)
    print(f"  建单 {info['added']}，跳过 {info['skipped']}，"
          f"建仓 {info['filled']}，失效 {info['expired']}，出场 {info['closed']}，"
          f"持仓中 {info['holding']}，观察中 {info['watching']}，异常 {info.get('errors', 0)}")

    new = _stats(conn, table)
    print("\n【新口径】按当前排序键重建")
    _print("新", new)

    if old.get("n") and new.get("n"):
        print(f"\n差异：均值 {new['mean'] - old['mean']:+.2f}pp，"
              f"胜率 {new['win'] - old['win']:+.1f}pp，PF {new['pf'] - old['pf']:+.2f}")

    if not args.apply:
        conn.execute(f"DROP TABLE IF EXISTS {SIM_TABLE}")
        conn.commit()
        print(f"\n[dry-run] 已清理临时表 {SIM_TABLE}。要真实重建请加 --apply。")
    else:
        print(f"\n重建完成，备份在 {bak}（确认无误后可 DROP TABLE {bak}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
