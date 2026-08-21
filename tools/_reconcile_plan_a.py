# -*- coding: utf-8 -*-
"""一次性对账：方案A（移动止盈 止损-5%/启动+8%/回撤3%/持10）落地后，
重建复盘窗并按新口径重新评估 recommend_outcome，打印前后对比。只写复盘表。

执行：python tools/_reconcile_plan_a.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core.db import get_conn
from config.strategy_params import get_param


def summary(where=""):
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT exit_return, exit_reason, hit_stop, hit_tp
            FROM recommend_outcome
            WHERE horizon='short' AND exit_return IS NOT NULL {where}
        """).fetchall()
    if not rows:
        print("  无已评估样本")
        return
    rets = [r["exit_return"] for r in rows]
    wins = sum(1 for v in rets if v > 0)
    aw = [v for v in rets if v > 0]
    al = [v for v in rets if v <= 0]
    pf = (sum(aw) / -sum(al)) if al and sum(al) < 0 else float("inf")
    print(f"  n={len(rows)} 胜率 {wins/len(rows)*100:.1f}% "
          f"均值 {sum(rets)/len(rets):+.2f}% PF {pf:.2f} "
          f"均盈 {sum(aw)/len(aw):+.2f}% 均亏 {sum(al)/len(al):+.2f}%" if aw and al else
          f"  n={len(rows)} 胜率 {wins/len(rows)*100:.1f}% 均值 {sum(rets)/len(rets):+.2f}%")
    dist = {}
    for r in rows:
        k = r["exit_reason"] or "?"
        dist.setdefault(k, []).append(r["exit_return"])
    for k, vs in sorted(dist.items(), key=lambda x: -len(x[1])):
        print(f"    {k:<15} n={len(vs):>3} 均值 {sum(vs)/len(vs):+.2f}%")


def main():
    print("当前短线退出参数："
          f"止损 {get_param('short_stop_loss'):.0%} / "
          f"启动线 {get_param('short_take_profit'):.0%} / "
          f"回撤 {get_param('short_trailing_pct'):.0%} / "
          f"持 {get_param('short_max_hold_days')} 天")

    print("\n【旧口径评估结果（重建前，固定止损止盈）】")
    summary("AND scan_date >= '2026-07-20'")

    from core.outcome_tracker import insert_new_outcomes, evaluate_outcomes
    print("\n重建复盘窗（清窗口内旧记录 → 重写 → 按新口径重评估）…")
    insert_new_outcomes()
    n = evaluate_outcomes()
    print(f"本轮评估 {n} 条")

    print("\n【新口径评估结果（移动止盈 方案A）】")
    summary("AND scan_date >= '2026-07-20'")


if __name__ == "__main__":
    main()
