# -*- coding: utf-8 -*-
"""eval_short_t1_filter3.py —— 对账窗(2026-07-20起)同口径复核（只读）
对账窗与挖掘窗重合段内，验证 C 条件（实装语义）vs 基准的 T1 表现差异，
排查 recommend_outcome 重建对账中 47.8%→44.0% 的反向现象。
差异点：额外输出「仅下跌日」切片 + 逐日明细。
"""
import os
import sys
import sqlite3
import pickle
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
from tools.eval_short_return_boost import EXCLUDE_BOARDS

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".cache", "boost_cand.pkl")
SLIP = 0.001
RECON = "2026-07-20"


def main():
    with open(CACHE, "rb") as f:
        cand, _n, _e = pickle.load(f)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    px = conn.execute(
        "SELECT code, trade_date, open, close FROM daily_price "
        "WHERE trade_date >= '2026-06-01' ORDER BY code, trade_date").fetchall()
    mkt1 = {}
    for r in conn.execute(
            "SELECT trade_date, AVG(pct_change) m FROM daily_price "
            "WHERE trade_date >= '2026-06-01' AND pct_change IS NOT NULL "
            "GROUP BY trade_date"):
        mkt1[r["trade_date"]] = r["m"]
    conn.close()

    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))

    # dev5：需要全历史算 MA5，直接用 SQL 窗口值太慢，这里只对候选票算
    dev5_cache = {}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cand_codes = {c for _d, c, _f, _e in cand if not c.startswith(EXCLUDE_BOARDS)}
    print(f"候选票 {len(cand_codes)} 只，预计算 dev5…")
    for code in cand_codes:
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE code=? "
            "ORDER BY trade_date", (code,)).fetchall()
        if len(rows) < 30:
            continue
        closes = pd.Series([r["close"] for r in rows], dtype=float)
        dates = [r["trade_date"] for r in rows]
        ma5 = closes.rolling(5).mean()
        dev = (closes / ma5 - 1) * 100
        dev5_cache[code] = {d: (None if pd.isna(dev.iloc[i]) else float(dev.iloc[i]))
                            for i, d in enumerate(dates)}
    conn.close()

    per_day = defaultdict(list)
    for d, code, f, ext in cand:
        if code.startswith(EXCLUDE_BOARDS):
            continue
        if f >= 22.0 and d >= RECON:
            per_day[d].append((code, f, ext))

    idx_cache = {}

    def t1_of(code, d):
        rows = data.get(code)
        if not rows:
            return None
        imap = idx_cache.get(code)
        if imap is None:
            imap = {r["trade_date"]: i for i, r in enumerate(rows)}
            idx_cache[code] = imap
        idx = imap.get(d)
        if idx is None or idx + 1 >= len(rows):
            return None
        entry = rows[idx + 1]["open"] * (1 + SLIP)
        c1 = rows[idx + 1]["close"]
        if not entry or entry <= 0 or not c1:
            return None
        return c1 / entry - 1

    def pick_day(d, use_filter):
        if use_filter and (mkt1.get(d) is None or mkt1.get(d) >= 0):
            return []
        cands = per_day[d]
        if use_filter:
            cands = [x for x in cands
                     if (dev5_cache.get(x[0], {}).get(d) or 0) <= -2.0]
        return sorted(cands, key=lambda x: (x[2], -x[1], x[0]))[:3]

    for label, use_f in [("基准 无过滤", False), ("C: 恐慌闸门+dev5", True)]:
        t1s_all, t1s_down = [], []
        detail = []
        for d in sorted(per_day.keys()):
            picks = pick_day(d, use_f)
            for code, _f, _e in picks:
                t1 = t1_of(code, d)
                if t1 is None:
                    continue
                t1s_all.append(t1)
                if mkt1.get(d, 0) < 0:
                    t1s_down.append(t1)
                detail.append((d, code, t1))
        win = sum(1 for v in t1s_all if v > 0) / len(t1s_all) * 100 if t1s_all else 0
        mean = sum(t1s_all) / len(t1s_all) * 100 if t1s_all else 0
        win_d = sum(1 for v in t1s_down if v > 0) / len(t1s_down) * 100 if t1s_down else 0
        mean_d = sum(t1s_down) / len(t1s_down) * 100 if t1s_down else 0
        print(f"\n[{label}] n={len(t1s_all)} T1胜率 {win:.1f}% 均值 {mean:+.2f}% "
              f"| 仅下跌日切片 n={len(t1s_down)} 胜率 {win_d:.1f}% 均值 {mean_d:+.2f}%")
        for d, code, t1 in detail:
            print(f"   {d} {code} mkt={mkt1.get(d, 0):+.2f}% "
                  f"dev5={(dev5_cache.get(code, {}).get(d) or 0):+.2f}% t1={t1*100:+.2f}%")


if __name__ == "__main__":
    main()
