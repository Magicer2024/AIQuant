"""诊断 4：short 过滤链归因 —— 逐个关掉过滤器，看 top3 的后续收益如何变化。

背景：线上 short 组（7/20 起）44 条已出场均值 -2.36%、胜率 22.7%，
      而不过滤的同排序池子 T+10 约 +1%。若成立 ⇒ 过滤器在选差票。
"""
import sys
import statistics as st

sys.path.insert(0, ".")
from core.db import get_conn
from core.outcome_tracker import (short_t1_filter_sql, short_market_gate_sql,
                                  short_observe_bottom_sql, short_order_clause)
from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

START = "2026-07-20"
CAP = max(1, int(get_param("short_top_n")))
GATE = float(get_param("short_conf_gate"))

with get_conn() as conn:
    t1, t1p = short_t1_filter_sql(conn, START)
    mk, mkp = short_market_gate_sql(conn, START)
    ob, obp = short_observe_bottom_sql()
    board = "".join(f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES) if MAIN_BOARD_ONLY else ""
    order = short_order_clause("s")

    BASE = f"""
        FROM stock_signal s
        LEFT JOIN latest_price lp ON lp.code = s.code
        WHERE s.scan_date >= '{START}'
          AND COALESCE(s.horizon,'short') = 'short'
          AND s.buy_price IS NOT NULL AND s.buy_price > 0
          AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
          AND COALESCE(s.strategy,'') NOT IN ('强势突破','缩量回踩')
          AND (CASE WHEN lp.close IS NOT NULL AND lp.close>0 AND s.buy_price>0
                    AND s.take_profit IS NOT NULL AND s.take_profit>s.buy_price
                    AND (lp.close/s.buy_price-1) > (s.take_profit/s.buy_price-1)
               THEN 1 ELSE 0 END) = 0
          {board}
    """

    def topn(extra_sql, extra_params):
        sql = f"""
        SELECT code, scan_date FROM (
            SELECT s.code, s.scan_date,
                   ROW_NUMBER() OVER (PARTITION BY s.scan_date ORDER BY {order}) rn
            {BASE} AND ({extra_sql})
        ) WHERE rn <= {CAP}
        """
        return [dict(r) for r in conn.execute(sql, extra_params).fetchall()]

    combos = {
        "B 无过滤（只基础条件）": ("1", []),
        "A 线上全链": (f"s.fusion_score >= ? AND {t1} AND {mk} AND {ob}", [GATE, *t1p, *mkp, *obp]),
        "C 只 fusion 门控 ≥22": ("s.fusion_score >= ?", [GATE]),
        "D 只 观察线门槛 ≥5.5%": (ob, obp),
        "E 只 T1 辅助过滤": (t1, t1p),
        "F 只 大盘走弱闸门": (mk, mkp),
        "G 全链但去掉 观察线门槛": (f"s.fusion_score >= ? AND {t1} AND {mk}", [GATE, *t1p, *mkp]),
        "H 全链但去掉 fusion 门控": (f"{t1} AND {mk} AND {ob}", [*t1p, *mkp, *obp]),
    }

    def fwd(code, scan_date, n=10):
        rows = conn.execute(
            "SELECT trade_date, open, close FROM daily_price WHERE code=? AND trade_date>? "
            "ORDER BY trade_date ASC LIMIT ?", (code, scan_date, n)).fetchall()
        if len(rows) < n:
            return None
        o, c = rows[0]["open"], rows[n - 1]["close"]
        return (c - o) / o * 100 if o and c and o > 0 else None

    print(f"short 名额 = {CAP} 只/日，筛选窗 {START} 起\n")
    print("=" * 92)
    print(f"{'过滤组合':<30}{'选中':>6}{'T+5均值':>10}{'T+5胜率':>9}{'T+10均值':>10}{'T+10胜率':>9}")
    print("=" * 92)
    for label, (sql_, prm) in combos.items():
        picks = topn(sql_, prm)
        v5 = [v for v in (fwd(p["code"], p["scan_date"], 5) for p in picks) if v is not None]
        v10 = [v for v in (fwd(p["code"], p["scan_date"], 10) for p in picks) if v is not None]
        def f(vs):
            return f"{st.mean(vs):+9.2f}%{sum(1 for x in vs if x>0)/len(vs)*100:8.1f}%" if vs else f"{'n/a':>18}"
        print(f"{label:<30}{len(picks):>6}{f(v5)}{f(v10)}")

    # 每日实际命中的扩展度分布
    print()
    print("=" * 92)
    print("① 线上全链选中票的扩展度 pct_above_ma20 分布（策略本意是抄底，扩展度理应低/负）")
    print("=" * 92)
    picks = topn(f"s.fusion_score >= ? AND {t1} AND {mk} AND {ob}", [GATE, *t1p, *mkp, *obp])
    exts = []
    for p in picks:
        r = conn.execute("SELECT pct_above_ma20, strategy, fusion_score FROM stock_signal "
                         "WHERE code=? AND scan_date=? AND COALESCE(horizon,'short')='short'",
                         (p["code"], p["scan_date"])).fetchone()
        if r:
            exts.append((r["pct_above_ma20"] or 0) * 100)
    if exts:
        exts.sort()
        print(f"  n={len(exts)}  最小 {exts[0]:.2f}%  中位 {st.median(exts):.2f}%  最大 {exts[-1]:.2f}%")
        for thr in (0, 2, 5.5, 8, 12):
            print(f"    扩展度 ≥ {thr:>4}% 占比: {sum(1 for e in exts if e>=thr)/len(exts)*100:5.1f}%")
    print()
    print("=" * 92)
    print("② 观察线门槛 short_observe_bottom_min_ext 敏感度（其余全链不变）")
    print("=" * 92)
    for thr in (0.0, 0.02, 0.035, 0.055, 0.08):
        c2 = f"(COALESCE(s.strategy,'') != '短线融合' OR COALESCE(s.pct_above_ma20,0) >= ?)"
        picks = topn(f"s.fusion_score >= ? AND {t1} AND {mk} AND {c2}", [GATE, *t1p, *mkp, thr])
        v10 = [v for v in (fwd(p["code"], p["scan_date"], 10) for p in picks) if v is not None]
        v5 = [v for v in (fwd(p["code"], p["scan_date"], 5) for p in picks) if v is not None]
        if v10:
            print(f"  min_ext={thr:<6} n={len(picks):<4} T+5 {st.mean(v5):+6.2f}%/{sum(1 for x in v5 if x>0)/len(v5)*100:4.1f}%  "
                  f"T+10 {st.mean(v10):+6.2f}%/{sum(1 for x in v10 if x>0)/len(v10)*100:4.1f}%")
