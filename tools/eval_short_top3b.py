# -*- coding: utf-8 -*-
"""eval_short_top3b.py —— Top3 实验补充：以现网口径 gate22 低扩展 Top4 为基准，
输出各配置在 对账窗(2026-07起)/近3个月/6个月 三窗的相对表现与 reason 分布。"""
import os
import sys
import sqlite3
import pickle
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from tools.eval_short_return_boost import simulate, Stats, EXCLUDE_BOARDS

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".cache", "boost_cand.pkl")
SLIP = 0.001
W6M, W3M, WREC = "2026-02-20", "2026-05-20", "2026-07-01"
CFG_A = dict(mode="trail", stop=0.05, launch=0.08, trail=0.03, hold=10)

VARIANTS = [
    ("现网基准 Top4 g22 ext",  4, 22.0, "ext"),
    ("Top1 g22 ext",           1, 22.0, "ext"),
    ("Top1 g22 fs",            1, 22.0, "fs"),
    ("Top3 g22 ext",           3, 22.0, "ext"),
    ("Top3 g24 ext",           3, 24.0, "ext"),
    ("Top3 g22 fs",            3, 22.0, "fs"),
    ("Top3 g26 fs",            3, 26.0, "fs"),
    ("Top3 g24 fs",            3, 24.0, "fs"),
    ("Top4 g22 fs",            4, 22.0, "fs"),
    ("Top2 g22 ext",           2, 22.0, "ext"),
    ("Top2 g24 ext",           2, 24.0, "ext"),
    ("Top2 g26 ext",           2, 26.0, "ext"),
    ("Top2 g22 fs",            2, 22.0, "fs"),
    ("Top2 g24 fs",            2, 24.0, "fs"),
    ("Top2 g26 fs",            2, 26.0, "fs"),
]


def main():
    with open(CACHE, "rb") as f:
        cand, _n, _e = pickle.load(f)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    conn.close()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))
    idx_cache = {}

    per_day_all = defaultdict(list)
    for d, code, f, ext in cand:
        if code.startswith(EXCLUDE_BOARDS):
            continue
        per_day_all[d].append((code, f, ext))

    out = []
    for label, topn, gate, rank in VARIANTS:
        st = {wk: Stats() for wk in ("w6m", "w3m", "rec")}
        for d in sorted(per_day_all.keys()):
            if d < W6M:
                continue
            cands = [x for x in per_day_all[d] if x[1] >= gate]
            if rank == "ext":
                picks = sorted(cands, key=lambda x: (x[2], -x[1], x[0]))[:topn]
            else:
                picks = sorted(cands, key=lambda x: (-x[1], x[0]))[:topn]
            for code, _f, _e in picks:
                imap = idx_cache.get(code)
                if imap is None:
                    rows = data.get(code)
                    if not rows:
                        continue
                    imap = ({r["trade_date"]: i for i, r in enumerate(rows)}, rows)
                    idx_cache[code] = imap
                m, rows = imap
                idx = m.get(d)
                if idx is None or idx + 1 >= len(rows):
                    continue
                entry = rows[idx + 1]["open"] * (1 + SLIP)
                if not entry or entry <= 0:
                    continue
                r = simulate(rows, idx, CFG_A, entry)
                if r is None:
                    continue
                st["w6m"].add(*r)
                if d >= W3M:
                    st["w3m"].add(*r)
                if d >= WREC:
                    st["rec"].add(*r)
        out.append((label, st))

    print("=" * 110)
    print("Top3 补充实验 · 方案A退出 · 基准=现网 Top4 g22 低扩展")
    print("=" * 110)
    hdr = f"  {'配置':<22} | {'对账窗(7月起)':^38} | {'近3个月':^38} | {'6个月':^30}"
    print(hdr)
    print(f"  {'':<22} | {'n':>4} {'胜率':>6} {'均值':>8} {'PF':>5} | {'n':>4} {'胜率':>6} {'均值':>8} {'PF':>5} | {'n':>4} {'胜率':>6} {'均值':>8}")
    for label, st in out:
        parts = []
        for wk in ("rec", "w3m", "w6m"):
            r = st[wk].row()
            if r:
                parts.append(f"{r['n']:>4} {r['win']:>5.1f}% {r['mean']:>+7.2f}% {r['pf']:>5.2f}")
            else:
                parts.append(f"{'':>4} {'':>6} {'':>8} {'':>5}")
        print(f"  {label:<22} | {parts[0]} | {parts[1]} | {parts[2]}")

    print("\n--- 对账窗 exit_reason 分布 ---")
    for label, st in out:
        reasons = st["rec"].reasons
        tot = sum(reasons.values()) or 1
        parts = " ".join(f"{k}:{v}({v*100//tot}%)" for k, v in
                         sorted(reasons.items(), key=lambda x: -x[1]))
        r = st["rec"].row()
        n_s = r["n"] if r else 0
        print(f"  {label:<22} n={n_s:<3} {parts}")


if __name__ == "__main__":
    main()
