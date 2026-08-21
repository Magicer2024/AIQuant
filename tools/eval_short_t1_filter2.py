# -*- coding: utf-8 -*-
"""eval_short_t1_filter2.py —— 实装语义复核：过滤发生在 Top3 截取之前（有替补）
候选条件：
  A: 市场宽度当日 < 0 才推（整日开关）
  B: 距MA5 <= -2% 个股过滤（候选先筛再取 Top3，后位替补）
  C: A + B 组合
对比基准：现网 Top3 g22 ext 无过滤。
"""
import os
import sys
import sqlite3
import pickle
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
from tools.eval_short_return_boost import simulate, Stats, EXCLUDE_BOARDS

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".cache", "boost_cand.pkl")
SLIP = 0.001
CFG_A = dict(mode="trail", stop=0.05, launch=0.08, trail=0.03, hold=10)
TRAIN_END = "2025-12-31"
W6M = "2026-02-20"


def main():
    with open(CACHE, "rb") as f:
        cand, _n, _e = pickle.load(f)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("加载行情…")
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    mkt1 = {}
    for r in conn.execute(
            "SELECT trade_date, AVG(pct_change) m FROM daily_price "
            "WHERE trade_date >= '2024-01-01' AND pct_change IS NOT NULL "
            "GROUP BY trade_date"):
        mkt1[r["trade_date"]] = r["m"]
    conn.close()

    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))

    # ── 逐股预算 dev5（距MA5偏离%）──
    print("预计算 MA5 偏离…")
    dev5_cache = {}
    n_done = 0
    for code, rows in data.items():
        if len(rows) < 30:
            continue
        closes = pd.Series([r["close"] for r in rows], dtype=float)
        dates = [r["trade_date"] for r in rows]
        ma5 = closes.rolling(5).mean()
        dev = (closes / ma5 - 1) * 100
        dev5_cache[code] = {d: (None if pd.isna(dev.iloc[i])
                                else float(dev.iloc[i]))
                            for i, d in enumerate(dates)}
        n_done += 1
        if n_done % 1000 == 0:
            print(f"  {n_done} 只…")

    per_day = defaultdict(list)
    for d, code, f, ext in cand:
        if code.startswith(EXCLUDE_BOARDS):
            continue
        if f >= 22.0:
            per_day[d].append((code, f, ext))

    idx_cache = {}

    def pick_day(d, use_mkt, use_dev5):
        """实装语义：先过滤候选，再低扩展排序取 Top3（后位替补）。"""
        if use_mkt:
            m = mkt1.get(d)
            if m is None or m >= 0:
                return []
        cands = per_day[d]
        if use_dev5:
            cands = [x for x in cands
                     if (dev5_cache.get(x[0], {}).get(d) or 0) <= -2.0]
        return sorted(cands, key=lambda x: (x[2], -x[1], x[0]))[:3]

    variants = [
        ("基准 无过滤", False, False),
        ("A: mkt当日<0 才推", True, False),
        ("B: dev5<=-2% 过滤+替补", False, True),
        ("C: A+B 组合", True, True),
    ]

    out = []
    for label, use_mkt, use_dev5 in variants:
        st = {"train": Stats(), "test": Stats(), "w6m": Stats()}
        t1s = {"train": [], "test": [], "w6m": []}
        n_days_with_pick = 0
        n_days_total = 0
        for d in sorted(per_day.keys()):
            n_days_total += 1
            picks = pick_day(d, use_mkt, use_dev5)
            if picks:
                n_days_with_pick += 1
            for code, _f, _e in picks:
                rows = data.get(code)
                if not rows:
                    continue
                imap = idx_cache.get(code)
                if imap is None:
                    imap = {r["trade_date"]: i for i, r in enumerate(rows)}
                    idx_cache[code] = imap
                idx = imap.get(d)
                if idx is None or idx + 1 >= len(rows):
                    continue
                entry = rows[idx + 1]["open"] * (1 + SLIP)
                if not entry or entry <= 0:
                    continue
                c1 = rows[idx + 1]["close"]
                out_sim = simulate(rows, idx, CFG_A, entry)
                wk_train = d <= TRAIN_END
                if c1:
                    t1 = c1 / entry - 1
                    if wk_train:
                        t1s["train"].append(t1)
                    else:
                        t1s["test"].append(t1)
                    if d >= W6M:
                        t1s["w6m"].append(t1)
                if out_sim is None:
                    continue
                st["test" if not wk_train else "train"].add(*out_sim)
                if d >= W6M:
                    st["w6m"].add(*out_sim)
        out.append((label, st, t1s, n_days_with_pick, n_days_total))

    def t1stat(vs):
        if not vs:
            return "    -    "
        win = sum(1 for v in vs if v > 0) / len(vs) * 100
        return f"{win:>5.1f}%/{sum(vs)/len(vs)*100:>+6.2f}%"

    print("=" * 118)
    print("实装语义复核（先过滤再取 Top3，有替补）· 方案A退出")
    print("=" * 118)
    print(f"  {'配置':<24} | {'train T1胜率/T1均值':^20} {'train全周期':^12} | "
          f"{'test T1胜率/T1均值':^20} {'test全周期':^12} | {'6个月T1':^20} {'6个月全周期':^12} | 出票日")
    for label, st, t1s, ndp, ndt in out:
        tr = st["train"].row()
        te = st["test"].row()
        w6 = st["w6m"].row()
        f_full = lambda r: f"{r['mean']:>+6.2f}% PF{r['pf']:>4.2f}" if r else "   n/a   "
        print(f"  {label:<24} | {t1stat(t1s['train']):^20} {f_full(tr):^12} | "
              f"{t1stat(t1s['test']):^20} {f_full(te):^12} | "
              f"{t1stat(t1s['w6m']):^20} {f_full(w6):^12} | {ndp}/{ndt}")


if __name__ == "__main__":
    main()
