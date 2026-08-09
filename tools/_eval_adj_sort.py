# -*- coding: utf-8 -*-
"""P2-3.1 排序对比（只读，不写库）：fusion_score 排序 vs 扩展度调整排序。

同一候选池（历史 stock_signal short 行），两种排序各取 Top8，对比 T+1 OC 表现。
扩展度调整分（文档 P2-3.1）：
  adj = fusion_score × (1 - clamp(price/ma20 - 1 - 0.08, 0, 0.3) / 0.3 × 0.4)
口径：T+1 OC = 次日开盘买入、次日收盘卖出（同 diag）。
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
LIMIT = 8
TOP_N = 8


def adj_score(fs: float, dev_pct: float) -> float:
    """dev_pct: 相对 MA20 偏离百分数（如 6.3 = +6.3%）"""
    if dev_pct is None:
        return fs
    penalty = max(0.0, min((dev_pct - 8.0) / 30.0, 1.0)) * 0.4
    return fs * (1.0 - penalty)


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT scan_date AS d FROM stock_signal WHERE horizon='short' "
        "AND strategy IN ('短线融合','超跌反弹v3') ORDER BY scan_date DESC"
    ).fetchall()]

    px = conn.execute(
        "SELECT code, trade_date, open, close FROM daily_price ORDER BY code, trade_date"
    ).fetchall()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(r)

    def find_idx(rows, date):
        for i, r in enumerate(rows):
            if r["trade_date"] >= date:
                return i
        return -1

    def ma20_at(rows, date):
        idx = find_idx(rows, date)
        if idx < 0:
            return None
        closes = [r["close"] for r in rows[: idx + 1]]
        if len(closes) < 20:
            return None
        return sum(closes[-20:]) / 20.0

    res = {"fusion": [], "adj": []}
    pooled = 0
    for d in dates[1:40]:
        sigs = conn.execute(
            "SELECT code, price, fusion_score FROM stock_signal "
            "WHERE scan_date=? AND horizon='short' AND strategy IN ('短线融合','超跌反弹v3')",
            (d,),
        ).fetchall()
        if not sigs:
            continue
        rows0 = data.get(sigs[0]["code"])
        if not rows0 or find_idx(rows0, d) < 0 or find_idx(rows0, d) + 1 >= len(rows0):
            continue
        # 候选 + dev
        cands = []
        for s in sigs:
            rows = data.get(s["code"])
            if not rows:
                continue
            idx = find_idx(rows, d)
            if idx < 0 or idx + 1 >= len(rows):
                continue
            ma20 = ma20_at(rows, d)
            dev = None
            if ma20 and ma20 > 0 and rows[idx]["close"]:
                dev = (rows[idx]["close"] / ma20 - 1) * 100
            cands.append({
                "code": s["code"], "fs": s["fusion_score"] or 0,
                "adj": adj_score(s["fusion_score"] or 0, dev),
                "idx": idx, "rows": rows, "dev": dev,
            })
        if len(cands) < TOP_N:
            continue
        pooled += 1
        for key, sort_key in [("fusion", lambda c: c["fs"]), ("adj", lambda c: c["adj"])]:
            top = sorted(cands, key=sort_key, reverse=True)[:TOP_N]
            ocs = []
            for c in top:
                r1 = c["rows"][c["idx"] + 1]
                base = r1["open"]
                if base and base > 0:
                    ocs.append(r1["close"] / base - 1)
            res[key].extend(ocs)

    def stats(lst):
        if not lst:
            return (float("nan"), float("nan"), 0)
        return (sum(1 for x in lst if x > 0) / len(lst) * 100, sum(lst) / len(lst) * 100, len(lst))

    print("=" * 72)
    print(f"P2-3.1 排序对比：同一候选池 Top{TOP_N}，cohort={pooled}，T+1 OC（次日开盘买/当日收盘卖）")
    print("=" * 72)
    for key, lbl in [("fusion", "fusion_score 排序"), ("adj", "adj_score 排序  ")]:
        w, m, n = stats(res[key])
        print(f"  {lbl}: 胜率 {w:.1f}%  均值 {m:+.3f}%  (n={n})")


if __name__ == "__main__":
    main()
