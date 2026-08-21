# -*- coding: utf-8 -*-
"""eval_short_t1_filter4.py —— regime 自动切换实装语义全窗复核（只读）
变体：
  基准：无过滤 Top3
  C 常开：恐慌闸门 + dev5 全期启用
  R regime 切换：仅信号日 regime ∈ (cold, cool) 时启用 C，其余日不过滤
窗口：train 2024~2025 / test 2026+ / 6个月(2026-02-20+) / 对账窗(2026-07-20+)
regime 用 core/market_regime.compute_regime_series（与前端信号灯同口径）。
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
from core.market_regime import compute_regime_series

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".cache", "boost_cand.pkl")
SLIP = 0.001
CFG_A = dict(mode="trail", stop=0.05, launch=0.08, trail=0.03, hold=10)
TRAIN_END = "2025-12-31"
W6M = "2026-02-20"
RECON = "2026-07-20"


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
    print("计算 regime 序列（2024-01 起，与前端信号灯同口径）…")
    regime = compute_regime_series(conn, "2024-01-01")
    conn.close()
    cnt = defaultdict(int)
    for r in regime.values():
        cnt[r] += 1
    print(f"regime 分布: {dict(cnt)}")

    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))

    print("预计算 MA5 偏离…")
    dev5_cache = {}
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

    per_day = defaultdict(list)
    for d, code, f, ext in cand:
        if code.startswith(EXCLUDE_BOARDS):
            continue
        if f >= 22.0:
            per_day[d].append((code, f, ext))

    idx_cache = {}

    def pick_day(d, mode):
        """实装语义：先过滤候选，再低扩展排序取 Top3（后位替补）。
        C 条件 = 恐慌闸门(mkt<0) + dev5<=-2%；regime 版仅 cold/cool 日启用。"""
        cands = per_day[d]
        gate_on = (mode == "always"
                   or (mode == "regime" and regime.get(d) in ("cold", "cool")))
        if gate_on:
            if (mkt1.get(d) or 0) >= 0:
                return []
            cands = [x for x in cands
                     if (dev5_cache.get(x[0], {}).get(d) or 0) <= -2.0]
        return sorted(cands, key=lambda x: (x[2], -x[1], x[0]))[:3]

    variants = [("基准 无过滤", "base"),
                ("C 常开", "always"),
                ("R regime切换(cold/cool)", "regime")]
    out = []
    for label, mode in variants:
        st = {"train": Stats(), "test": Stats(), "w6m": Stats(),
              "recon": Stats()}
        t1s = {"train": [], "test": [], "w6m": [], "recon": []}
        n_days_with_pick = n_days_filtered = n_days_total = 0
        for d in sorted(per_day.keys()):
            n_days_total += 1
            picks = pick_day(d, mode)
            if picks:
                n_days_with_pick += 1
            if mode == "regime" and regime.get(d) in ("cold", "cool"):
                n_days_filtered += 1
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
                    t1s["train" if wk_train else "test"].append(t1)
                    if d >= W6M:
                        t1s["w6m"].append(t1)
                    if d >= RECON:
                        t1s["recon"].append(t1)
                if out_sim is None:
                    continue
                st["train" if wk_train else "test"].add(*out_sim)
                if d >= W6M:
                    st["w6m"].add(*out_sim)
                if d >= RECON:
                    st["recon"].add(*out_sim)
        out.append((label, st, t1s, n_days_with_pick, n_days_total,
                    n_days_filtered))

    def t1stat(vs):
        if not vs:
            return "    -    "
        win = sum(1 for v in vs if v > 0) / len(vs) * 100
        return f"{win:>5.1f}%/{sum(vs)/len(vs)*100:>+6.2f}%"

    print("=" * 130)
    print("regime 自动切换实装语义复核 · 方案A退出（T1胜率/T1均值）")
    print("=" * 130)
    for label, st, t1s, ndp, ndt, ndf in out:
        print(f"\n[{label}] 出票日 {ndp}/{ndt}"
              + (f"，过滤启用日 {ndf}" if label.startswith("R") else ""))
        for wk in ("train", "test", "w6m", "recon"):
            r = st[wk].row()
            full = f"{r['mean']:>+6.2f}% PF{r['pf']:>4.2f}" if r else "   n/a  "
            print(f"  {wk:<6} T1 {t1stat(t1s[wk]):<18} 全周期 {full}")


if __name__ == "__main__":
    main()
