# -*- coding: utf-8 -*-
"""eval_short_fusion_cap.py —— 短线选股排序方向全窗评估（只读）

背景（2026-08-22 诊断）：
  1. fusion 高分段方向反向（28+ 的 T5 转负、深亏 25~35%），但实装语义下加
     fusion 上限（cap26/28/30）无增益——低扩展排序已避开大部分高分毒票。
  2. 真正的毒段是 ext(pct_above_ma20) 0~0.5%：T1 胜率 45.8%、T5 -0.43%、
     5日深亏(dd<=-8%) 31.3%；而 ext>=0.5%（站稳 MA20、趋势确认）深亏仅
     10~12%、T5 +0.4~0.7%。现网 ext 升序排序恰优先选中毒段。

变体（fusion>=22 + regime T1 过滤为共同基线）：
  asc        : ext 升序（现网）
  asc05      : 切掉 ext<0.5% 后升序
  desc       : ext 降序（趋势确认优先）← 全窗最优
  regime_desc: 冷市日升序(超跌反弹)/其余降序
窗口：train 2024~2025 / test 2026+ / 6个月(2026-02-20+) / 对账窗(2026-07-20+)
退出：方案A（simulate, CFG_A）。
结论：desc 全周期均值全窗翻正（train +0.85/test +0.54/w6m +0.45%，PF 1.12-1.28，
胜率 47.5-48.7%），train/test 方向一致非过拟合；regime_desc 在 test/w6m 弱于 desc。
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
    mkt1 = {}
    for r in conn.execute(
            "SELECT trade_date, AVG(pct_change) m FROM daily_price "
            "WHERE trade_date >= '2024-01-01' AND pct_change IS NOT NULL "
            "GROUP BY trade_date"):
        mkt1[r["trade_date"]] = r["m"]
    regime = compute_regime_series(conn, "2024-01-01")
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    conn.close()

    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))

    dev5_cache = {}
    for code, rows in data.items():
        if len(rows) < 30:
            continue
        closes = pd.Series([r["close"] for r in rows], dtype=float)
        dates = [r["trade_date"] for r in rows]
        dev = (closes / closes.rolling(5).mean() - 1) * 100
        dev5_cache[code] = {d: (None if pd.isna(dev.iloc[i]) else float(dev.iloc[i]))
                            for i, d in enumerate(dates)}

    per_day = defaultdict(list)   # 不做 fusion 下限过滤，交给变体
    for d, code, f, ext in cand:
        if code.startswith(EXCLUDE_BOARDS):
            continue
        per_day[d].append((code, f, ext))

    idx_cache = {}

    def pick_day(d, sort_mode):
        """实装语义：fusion>=22 + regime T1 过滤(cold/cool 日) + 排序取 Top3。
        sort_mode: asc=ext升序(现网) / asc05=切掉ext<0.5%毒段后升序 / desc=ext降序"""
        cands = [x for x in per_day[d] if x[1] >= 22]
        if regime.get(d) in ("cold", "cool"):
            if (mkt1.get(d) or 0) >= 0:
                return []
            cands = [x for x in cands
                     if (dev5_cache.get(x[0], {}).get(d) or 0) <= -2.0]
        if sort_mode == "asc05":
            cands = [x for x in cands if (x[2] or 0) >= 0.005]
        if sort_mode == "desc":
            return sorted(cands, key=lambda x: (-x[2], -x[1], x[0]))[:3]
        if sort_mode == "regime_desc":
            # 冷市日超跌反弹逻辑(ext升序)，其余日趋势确认(ext降序)
            if regime.get(d) in ("cold", "cool"):
                return sorted(cands, key=lambda x: (x[2], -x[1], x[0]))[:3]
            return sorted(cands, key=lambda x: (-x[2], -x[1], x[0]))[:3]
        return sorted(cands, key=lambda x: (x[2], -x[1], x[0]))[:3]

    variants = [("基准 ext升序(现网)", "asc"),
                ("切毒段 ext>=0.5%升序", "asc05"),
                ("ext降序(趋势确认优先)", "desc"),
                ("regime条件排序(冷升/其余降)", "regime_desc")]
    out = []
    for label, sm in variants:
        st = {w: Stats() for w in ("train", "test", "w6m", "recon")}
        t1s = {w: [] for w in ("train", "test", "w6m", "recon")}
        ndp = ndt = 0
        for d in sorted(per_day.keys()):
            ndt += 1
            picks = pick_day(d, sm)
            if picks:
                ndp += 1
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
                wk = "train" if d <= TRAIN_END else "test"
                if c1:
                    t1 = c1 / entry - 1
                    t1s[wk].append(t1)
                    if d >= W6M:
                        t1s["w6m"].append(t1)
                    if d >= RECON:
                        t1s["recon"].append(t1)
                out_sim = simulate(rows, idx, CFG_A, entry)
                if out_sim is None:
                    continue
                st[wk].add(*out_sim)
                if d >= W6M:
                    st["w6m"].add(*out_sim)
                if d >= RECON:
                    st["recon"].add(*out_sim)
        out.append((label, st, t1s, ndp, ndt))

    def t1stat(vs):
        if not vs:
            return "    -    "
        win = sum(1 for v in vs if v > 0) / len(vs) * 100
        return f"{win:>5.1f}%/{sum(vs)/len(vs)*100:>+6.2f}%"

    print("=" * 120)
    print("fusion 上限门控实装语义复核 · 方案A退出（T1胜率/T1均值 | 全周期 均值/PF/胜率）")
    print("=" * 120)
    for label, st, t1s, ndp, ndt in out:
        print(f"\n[{label}] 出票日 {ndp}/{ndt}")
        for wk in ("train", "test", "w6m", "recon"):
            s = st[wk]
            full = (f"{s.sum/s.n*100:>+6.2f}% PF{s.gross_win/-s.gross_loss if s.gross_loss else 99:.2f} "
                    f"胜{s.pos/s.n*100:.1f}% n={s.n}") if s.n else "   n/a  "
            print(f"  {wk:<6} T1 {t1stat(t1s[wk]):<18} 全周期 {full}")


if __name__ == "__main__":
    main()
