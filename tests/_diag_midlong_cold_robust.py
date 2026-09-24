# -*- coding: utf-8 -*-
"""稳健性检验：cold 日 mid/long 正期望是否单月依赖？

按项目铁律 3「单月依赖压力测试」：逐个剔除单月，看 cold 组的正期望是否仍成立。
同时给出分月明细，检查是否某一个月贡献了全部收益。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn
from core.market_regime import compute_regime_series

HOLD = {"mid": 20, "long": 60}


def fwd_oc(conn, code, scan_date, n):
    rows = conn.execute("""
        SELECT trade_date, open, close FROM daily_price
        WHERE code=? AND trade_date>? ORDER BY trade_date LIMIT ?
    """, (code, scan_date, n + 1)).fetchall()
    if len(rows) < 2:
        return None
    buy, sell = rows[0]["open"], rows[-1]["close"]
    if not buy or not sell:
        return None
    return (sell - buy) / buy * 100


with get_conn() as conn:
    dates = [r["scan_date"] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM stock_signal ORDER BY scan_date"
    ).fetchall()]
    dates = [d for d in dates if d >= "2025-01-01"]
    reg = compute_regime_series(conn, dates[0], dates[-1])

    for hz in ("long", "mid"):
        recs = []
        rows = conn.execute("""
            SELECT s.code, s.scan_date FROM stock_signal s
            WHERE COALESCE(s.horizon,'short')=?
              AND s.scan_date >= ?
              AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
              AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
        """, (hz, dates[0])).fetchall()
        for r in rows:
            sg = reg.get(r["scan_date"])
            if sg not in ("cold", "cool"):
                continue
            ret = fwd_oc(conn, r["code"], r["scan_date"], HOLD[hz])
            if ret is not None:
                recs.append((r["scan_date"][:7], ret))

        if not recs:
            print(f"\n### {hz}: 无可评估样本"); continue
        vals = [x[1] for x in recs]
        base = sum(vals) / len(vals)
        print(f"\n{'='*54}\n### {hz}  cold+cool  T+{HOLD[hz]}   n={len(vals)}  "
              f"均值 {base:.2f}%  胜率 {sum(1 for x in vals if x>0)/len(vals)*100:.1f}%")

        # 分月明细
        bym = {}
        for m, v in recs:
            bym.setdefault(m, []).append(v)
        print(f"{'月份':<10}{'笔数':>7}{'均值%':>9}{'胜率%':>8}")
        for m in sorted(bym):
            v = bym[m]
            print(f"{m:<10}{len(v):>7}{sum(v)/len(v):>9.2f}"
                  f"{sum(1 for x in v if x>0)/len(v)*100:>8.1f}")

        # 剔除单月压力测试
        print("剔除单月后：")
        worst = None
        for m in sorted(bym):
            rest = [v for mm, v in recs if mm != m]
            if not rest:
                continue
            mv = sum(rest) / len(rest)
            flag = "  <-- 转负!" if mv < 0 else ""
            print(f"  剔除 {m}: n={len(rest):>5}  均值 {mv:>6.2f}%{flag}")
            if worst is None or mv < worst[1]:
                worst = (m, mv)
        if worst:
            print(f"  ⇒ 最差情况（剔除 {worst[0]}）仍为 {worst[1]:.2f}%"
                  f" → {'非单月依赖 ✓' if worst[1] > 0 else '存在单月依赖 ✗'}")
