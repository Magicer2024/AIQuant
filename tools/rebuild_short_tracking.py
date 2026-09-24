"""清空并重建「短线跟踪」记录（recommend_outcome）。

何时用：短线选股/过滤/排序/出场逻辑变更后，把 recommend_outcome 按**当前逻辑**
重算，使首页「推荐复盘」的胜率/收益与线上口径一致。

做法（复用每日调度同源代码，**不新造任何出场数学**）：
  1) 备份 recommend_outcome → recommend_outcome_bak_<YYYYMMDD_HHMMSS>
  2) 先显式清空短线记录（留痕），再由 insert_new_outcomes 清空整个重建窗口
  3) core.outcome_tracker.insert_new_outcomes(days_back) + evaluate_outcomes()

窗口：max(today - days_back, EXIT_TRACK_START_DATE=2026-07-20)

⚠ 局限：本脚本重建的是**选股/名额/排序/过滤/出场**逻辑与收益字段；每行的
   entry_price / stop_loss / take_profit 取自 stock_signal 写入时的值，**不会**
   自动回溯重算（若止损口径也变了，需另行重算 stock_signal）。

用法：
  python tools/rebuild_short_tracking.py                 # 干跑：只打印现状与计划
  python tools/rebuild_short_tracking.py --apply         # 执行：备份 + 清空 + 重建
  python tools/rebuild_short_tracking.py --apply --days 400
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.personal_config import EXIT_TRACK_START_DATE  # noqa: E402

DB = os.path.join("core", "quant.db")


def _con() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def snapshot(con: sqlite3.Connection) -> dict:
    by_group = [dict(r) for r in con.execute("""
        SELECT COALESCE(horizon,'(null)') h, COALESCE(strategy,'(null)') st,
               COUNT(*) n, MIN(scan_date) mn, MAX(scan_date) mx
        FROM recommend_outcome GROUP BY h, st ORDER BY h, n DESC""")]
    total = con.execute("SELECT COUNT(*) FROM recommend_outcome").fetchone()[0]
    row = con.execute("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN t1_return IS NOT NULL THEN 1 ELSE 0 END) settled,
               SUM(CASE WHEN t1_return > 0 THEN 1 ELSE 0 END) wins,
               ROUND(AVG(t1_return), 3) avg_t1,
               SUM(CASE WHEN exit_return IS NOT NULL THEN 1 ELSE 0 END) exited,
               ROUND(AVG(exit_return), 3) avg_exit
        FROM recommend_outcome WHERE COALESCE(horizon,'short')='short'""").fetchone()
    return {"by_group": by_group, "total": total, "short": dict(row)}


def print_snapshot(tag: str, s: dict) -> None:
    print(f"\n── {tag} ──")
    print(f"  recommend_outcome 总行数: {s['total']}")
    for r in s["by_group"]:
        print(f"   {r['h']:<6} {r['st']:<8} n={r['n']:<4} {r['mn']} ~ {r['mx']}")
    sh = s["short"]
    wr = (sh["wins"] / sh["settled"] * 100) if sh["settled"] else None
    print(f"  短线合计: n={sh['n']} 已结算={sh['settled']} "
          f"胜率={round(wr, 1) if wr is not None else '--'}% "
          f"均值T+1={sh['avg_t1']}% 已出场={sh['exited']} 均值出场={sh['avg_exit']}%")


def backup(con: sqlite3.Connection) -> str:
    name = "recommend_outcome_bak_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    con.execute(f"CREATE TABLE {name} AS SELECT * FROM recommend_outcome")
    con.commit()
    n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    print(f"  ✅ 备份完成: {name}（{n} 行）")
    return name


def main() -> None:
    ap = argparse.ArgumentParser(description="清空并重建短线跟踪记录（recommend_outcome）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只干跑）")
    ap.add_argument("--days", type=int, default=400, help="重建窗口回溯天数（会被 EXIT_TRACK_START_DATE 抬升）")
    args = ap.parse_args()

    start = max((date.today() - timedelta(days=args.days)).isoformat(), EXIT_TRACK_START_DATE)
    print("=" * 78)
    print(f"短线跟踪重建  |  模式 = {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"重建窗口: scan_date >= {start}"
          f"  (days_back={args.days}, 下限 EXIT_TRACK_START_DATE={EXIT_TRACK_START_DATE})")
    print("=" * 78)

    con = _con()
    print_snapshot("BEFORE", snapshot(con))

    will_del = [dict(r) for r in con.execute(
        "SELECT COALESCE(horizon,'(null)') h, COUNT(*) n FROM recommend_outcome "
        "WHERE scan_date >= ? GROUP BY h ORDER BY h", (start,))]
    below = con.execute(
        "SELECT COUNT(*) FROM recommend_outcome WHERE scan_date < ?", (start,)).fetchone()[0]
    print("\n── 计划 ──")
    print("  1) 备份 recommend_outcome → _bak_<时间戳>（可回滚）")
    print(f"  2) 清空窗口内记录（全部 horizon；先单独清 short 留痕）— scan_date >= {start}:")
    for r in will_del:
        print(f"       {r['h']:<6} {r['n']} 行")
    if below:
        print(f"       ⚠ 窗口外仍有 {below} 行（保留不动）")
    print(f"  3) 重跑 insert_new_outcomes(days_back={args.days}) + evaluate_outcomes()")

    if not args.apply:
        con.close()
        print("\n[DRY-RUN] 未做任何修改。确认无误后加 --apply 执行。")
        return

    print()
    bak = backup(con)
    d1 = con.execute(
        "DELETE FROM recommend_outcome WHERE COALESCE(horizon,'short')='short'").rowcount
    con.commit()
    print(f"  ✅ 已清空短线记录: {d1} 行")
    con.close()

    from core.outcome_tracker import evaluate_outcomes, insert_new_outcomes  # noqa: E402
    insert_new_outcomes(days_back=args.days)
    updated = evaluate_outcomes()
    print(f"  ✅ 重建 + 评估完成: {updated} 条评估更新")

    con = _con()
    print_snapshot("AFTER", snapshot(con))

    # 隔离自检：强势突破 / 缩量回踩 必须为 0。
    # ⚠ 反转首日**不在此列**：自 2026-09-24 起按用户要求纳入推荐复盘（独立成组），
    #   预期有行；它是否独立于短线推荐线由 strategy 过滤保证（见 get_merged_summary）。
    leak = [dict(r) for r in con.execute(
        "SELECT COALESCE(strategy,'(null)') st, COUNT(*) n FROM recommend_outcome "
        "WHERE COALESCE(strategy,'') IN ('强势突破','缩量回踩') GROUP BY st")]
    print("\n── 隔离自检（强势突破/缩量回踩 应无输出）──")
    print("  无泄漏 ✅" if not leak else f"  ❌ 泄漏: {leak}")
    rev = [dict(r) for r in con.execute(
        "SELECT COALESCE(strategy,'(null)') st, COUNT(*) n FROM recommend_outcome "
        "WHERE COALESCE(strategy,'') = '反转首日' GROUP BY st")]
    print(f"  （反转首日 · 已纳入复盘，预期有行）: {rev if rev else '0 行'}")
    # 板块自检：主板限制下不应出现创业板/科创板
    _bad = con.execute(
        "SELECT COUNT(*) FROM recommend_outcome o WHERE strategy = '反转首日' "
        "AND (o.code LIKE '30%' OR o.code LIKE '68%')").fetchone()[0]
    print(f"  反转首日组含创业板/科创板代码: {_bad} 行 "
          f"→ {'✅ 已按主板过滤' if _bad == 0 else '❌ 板块过滤未生效'}")

    print("\n回滚方法（如需）：")
    print("  DROP TABLE recommend_outcome;")
    print(f"  ALTER TABLE {bak} RENAME TO recommend_outcome;")
    con.close()


if __name__ == "__main__":
    main()
