# -*- coding: utf-8 -*-
"""eval_short_top3c.py —— 大盘状态过滤实验（Top3 g22 ext 口径上叠加）
假设：短线抄底策略在弱市负期望（6个月窗全负），强市才赚钱。
若按「信号日全市场平均涨跌 >= 阈值」过滤弱市日，6个月窗能否转正/显著减亏？
过滤口径对齐 core/sync.py _mid_weak_market_ok（AVG(pct_change) 全市场）。
"""
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

# (label, topn, gate, rank, mkt_gate or None)
VARIANTS = [
    ("Top3 g22 ext 无过滤",      3, 22.0, "ext", None),
    ("Top3 g22 ext mkt>=-0.5",   3, 22.0, "ext", -0.5),
    ("Top3 g22 ext mkt>=0",      3, 22.0, "ext", 0.0),
    ("Top3 g22 ext mkt>=+0.3",   3, 22.0, "ext", 0.3),
    ("Top3 g22 ext mkt>=+0.5",   3, 22.0, "ext", 0.5),
    ("Top4 g22 ext mkt>=0",      4, 22.0, "ext", 0.0),
    ("Top4 g22 ext 无过滤",      4, 22.0, "ext", None),
]


def main():
    with open(CACHE, "rb") as f:
        cand, _n, _e = pickle.load(f)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    # 每日全市场平均涨跌幅（对齐 _mid_weak_market_ok 口径）
    print("计算每日市场宽度…")
    mkt = {}
    for r in conn.execute(
            "SELECT trade_date, AVG(pct_change) m FROM daily_price "
            "WHERE trade_date >= '2026-02-01' AND pct_change IS NOT NULL "
            "GROUP BY trade_date"):
        mkt[r["trade_date"]] = r["m"]
    conn.close()
    print(f"市场日数 {len(mkt)}")

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
    for label, topn, gate, rank, mgate in VARIANTS:
        st = {wk: Stats() for wk in ("w6m", "w3m", "rec")}
        n_skipped = 0
        for d in sorted(per_day_all.keys()):
            if d < W6M:
                continue
            m = mkt.get(d)
            if mgate is not None and (m is None or m < mgate):
                n_skipped += 1
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
                m2, rows = imap
                idx = m2.get(d)
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
        out.append((label, st, n_skipped))

    print("=" * 112)
    print("大盘状态过滤实验 · 方案A退出 · 信号日全市场平均涨跌幅门控")
    print("=" * 112)
    print(f"  {'配置':<24} | {'对账窗(7月起)':^36} | {'近3个月':^36} | {'6个月':^36} | 弃日")
    for label, st, nsk in out:
        parts = []
        for wk in ("rec", "w3m", "w6m"):
            r = st[wk].row()
            if r:
                parts.append(f"{r['n']:>4} {r['win']:>5.1f}% {r['mean']:>+7.2f}% PF{r['pf']:>5.2f}")
            else:
                parts.append(f"{'':>4} {'':>6} {'':>8} {'':>9}")
        print(f"  {label:<24} | {parts[0]} | {parts[1]} | {parts[2]} | {nsk}")


if __name__ == "__main__":
    main()
