# -*- coding: utf-8 -*-
"""一次性诊断：短线推荐实盘复盘基线（recommend_outcome）。只读。

口径：推荐胜率统计口径规范 —— T1/T2/T3/T5 胜率 = 有该T收益且>0 的数量 / 有该T数据的总数。
"""
import os, sys, sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")


def pct(a, b):
    return a / b * 100 if b else 0.0


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    all_rows = conn.execute("""
        SELECT * FROM recommend_outcome WHERE horizon='short' ORDER BY scan_date
    """).fetchall()
    strats = sorted({r["strategy"] for r in all_rows})
    print(f"短线策略: {strats}  窗口 {all_rows[0]['scan_date']} ~ {all_rows[-1]['scan_date']}")
    rows = all_rows

    for col in ("t1_return", "t2_return", "t3_return", "t5_return", "t10_return",
                "max_return", "min_return", "exit_return"):
        vals = [r[col] for r in rows if r[col] is not None]
        if not vals:
            print(f"  {col:14s}: 无数据")
            continue
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        mx, mn = max(vals), min(vals)
        print(f"  {col:14s}: n={len(vals):3d}  胜率={pct(win, len(vals)):5.1f}%  "
              f"均值={avg:+6.2f}%  中位={sorted(vals)[len(vals)//2]:+6.2f}%  "
              f"最大={mx:+6.2f}%  最小={mn:+6.2f}%")

    # 出场原因分布
    print("\n出场原因分布:")
    for r in conn.execute("""
        SELECT COALESCE(exit_reason,'(未出场)') er, COUNT(*) n,
               ROUND(AVG(exit_return),2) avg_ret,
               SUM(CASE WHEN exit_return>0 THEN 1 ELSE 0 END) wins
        FROM recommend_outcome WHERE horizon='short'
        GROUP BY er ORDER BY n DESC"""):
        avg = r["avg_ret"] if r["avg_ret"] is not None else float("nan")
        print(f"  {r['er']:14s} n={r['n']:3d}  均收益={avg:+6.2f}%  其中盈利={r['wins']}")

    # 止损/止盈价设置分布（验证当前线上参数）
    print("\n止损/止盈设置 (entry 相对):")
    stops = [round((r["stop_loss"] / r["entry_price"] - 1) * 100, 2)
             for r in rows if r["stop_loss"] and r["entry_price"]]
    tps = [round((r["take_profit"] / r["entry_price"] - 1) * 100, 2)
           for r in rows if r["take_profit"] and r["entry_price"]]
    from collections import Counter
    print("  止损分布:", Counter(stops).most_common(5))
    print("  止盈分布:", Counter(tps).most_common(5))

    # 盈亏比分析
    ex = [r["exit_return"] for r in rows if r["exit_return"] is not None]
    wins = [v for v in ex if v > 0]
    losses = [v for v in ex if v <= 0]
    if wins and losses:
        aw = sum(wins) / len(wins)
        al = -sum(losses) / len(losses)
        print(f"\n出场盈亏比: 平均盈利 {aw:+.2f}% × {len(wins)} 笔 / 平均亏损 {-al:+.2f}% × {len(losses)} 笔"
              f" → 盈亏比 {aw/al:.2f}")
    # T+N 漂移（纯持有 edge 分布）
    print("\n纯持有漂移（无止损止盈干预）:")
    for col in ("t1_return", "t2_return", "t3_return", "t5_return", "t10_return"):
        vals = [r[col] for r in rows if r[col] is not None]
        if vals:
            print(f"  {col}: 均值 {sum(vals)/len(vals):+.3f}%  (n={len(vals)})")

    # 按推荐日聚合（组合视角：每日等权）
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in rows:
        if r["exit_return"] is not None:
            by_day[r["scan_date"]].append(r["exit_return"])
    day_rets = [sum(v) / len(v) for v in by_day.values()]
    pos = sum(1 for v in day_rets if v > 0)
    cum = 1.0
    for v in day_rets:
        cum *= (1 + v / 100)
    print(f"\n组合视角（每日等权，按出场收益）: 推荐日 {len(day_rets)} 个, "
          f"日均 {sum(day_rets)/len(day_rets):+.3f}%, 日胜率 {pct(pos, len(day_rets)):.1f}%, "
          f"复利累计 {(cum-1)*100:+.2f}%")
    conn.close()


if __name__ == "__main__":
    main()
