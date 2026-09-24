"""诊断 5：mid/long 出场参数网格 —— 用实际推荐票回放，量化参数倒挂的代价。

mid 现状：partial_tp=0.06（+6% 启动） / trailing_pct=0.10（回撤 10%）
  → 启动后最低出场 = 1.06 × 0.90 = 0.954 → 锁亏 -4.6%
long 现状：partial_tp=0.20 / trailing_pct=0.15 → 锁 +2%（合理）
"""
import sys
import statistics as st

sys.path.insert(0, ".")
import pandas as pd
from core.db import get_conn
from strategy.exit_advisor import evaluate_exit_by_prices, get_max_hold

START = "2026-07-20"

with get_conn() as conn:
    recs = [dict(r) for r in conn.execute(
        f"SELECT code, scan_date, COALESCE(horizon,'short') hz, entry_price, "
        f"stop_loss, take_profit, exit_return FROM recommend_outcome "
        f"WHERE scan_date >= '{START}' AND COALESCE(horizon,'short') IN ('mid','long')")]

    # 实际建仓价 = 信号日次日开盘
    def build(r):
        rows = conn.execute(
            "SELECT trade_date, open, close, high, low FROM daily_price "
            "WHERE code=? AND trade_date>=? ORDER BY trade_date ASC", (r["code"], r["scan_date"])).fetchall()
        if len(rows) < 3:
            return None
        df = pd.DataFrame([dict(x) for x in rows]).set_index("trade_date")
        df.index = pd.to_datetime(df.index)
        after = df[df.index > pd.Timestamp(r["scan_date"])]
        if after.empty or not after.iloc[0]["open"]:
            return None
        return after, float(after.iloc[0]["open"])

    cases = []
    for r in recs:
        b = build(r)
        if b:
            cases.append((r, b[0], b[1]))
    print(f"样本：mid/long 推荐 {len(cases)} 条（基准 = 信号日次日开盘）\n")

    def run(hz, trailing, partial, stop_cap):
        rets = []
        for r, after, e in cases:
            if r["hz"] != hz:
                continue
            adv = evaluate_exit_by_prices(
                entry_price=e, entry_date=r["scan_date"], df=after,
                stop_loss=r["stop_loss"], take_profit=r["take_profit"],
                max_hold_days=get_max_hold(hz), trailing_pct=trailing,
                partial_tp=partial, stop_cap_pct=stop_cap)
            d = adv.get("detail") or {}
            if adv["status"] == "clear" and d.get("current_pnl_pct") is not None:
                rets.append(d["current_pnl_pct"])
        return rets

    def line(label, rets):
        if not rets:
            print(f"  {label:<44} n=0")
            return
        win = sum(1 for v in rets if v > 0) / len(rets) * 100
        print(f"  {label:<44} n={len(rets):<4} 均值 {st.mean(rets):+6.2f}%  "
              f"中位 {st.median(rets):+6.2f}%  胜率 {win:5.1f}%  最差 {min(rets):+6.2f}%")

    print("=" * 100)
    print("【中线】移动止盈参数网格（stop_cap = mid_stop_max_width 0.12）")
    print("=" * 100)
    line("现状 trailing=10% / 启动=+6%  ← 倒挂，锁亏 -4.6%", run("mid", 0.10, 0.06, 0.12))
    for tr, pa in ((0.045, 0.06), (0.05, 0.06), (0.06, 0.06), (0.08, 0.06), (0.10, 0.10), (0.10, 0.12)):
        line(f"trailing={tr*100:.1f}% / 启动=+{pa*100:.0f}%  (锁 {((1+pa)*(1-tr)-1)*100:+.1f}%)",
             run("mid", tr, pa, 0.12))

    print()
    print("=" * 100)
    print("【长线】止损宽度上限网格（现状：不叠加，用信号自带止损）")
    print("=" * 100)
    line("现状 trailing=15% / 启动=+20% / 无止损上限", run("long", 0.15, 0.20, None))
    for cap in (0.08, 0.10, 0.12, 0.15):
        line(f"trailing=15% / 启动=+20% / 止损上限 -{cap*100:.0f}%", run("long", 0.15, 0.20, cap))
    for tr, pa in ((0.10, 0.15), (0.12, 0.15), (0.15, 0.15)):
        line(f"trailing={tr*100:.0f}% / 启动=+{pa*100:.0f}% / 止损上限 -12%", run("long", tr, pa, 0.12))
