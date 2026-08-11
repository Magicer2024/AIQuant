"""
backtest_enhance.py - test entry-quality enhancements on top of v2 (pullback) engine.

Current v2 Top8 multi-cohort T+1 win rate is only ~48.6% (coin-flip). Sorting is
already correct (fixed by v2). Remaining lever: ENTRY QUALITY filtering. This script
tests three candidate entry-quality enhancements:

  E1 market breadth regime skip: on signal day, if fraction of all stocks with
     close > MA20 (breadth) is in the historical bottom quantile, skip recommendations
     (broad-market weakness raises pullback-signal failure rate; more decisive than
      the current "halve" behavior).
  E2 volume dry-up pullback: signal-day 5d avg volume <= 10d avg volume (pullback is
     low-volume consolidation, not high-volume distribution).
  E3 RSI sweet spot: RSI(14) within [lo, hi] (exclude weak <lo and overbought >hi).

Configs: base(v2) / +E1 / +E2 / +E3 / +ALL / +ALLw. Plus sweeps for E1 quantile
and E3 range to pick thresholds.

Return basis: buy at T+1 open (OC), measure T+1/T+3/T+5 close return, multi-cohort
aggregate (aligned with backtest_engine_compare.py). Read-only, no DB writes.
"""
import sys, os, sqlite3
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from strategy.strategies import strategy_bottom_fishing_v2
from strategy.rec_filters import (quality_series, trend_gate_series,
                                  chase_filter_series, extension_filter_series)

DB = os.path.join(PROJECT_ROOT, "core", "quant.db")
SIG = 15.0
MA_N = 20
SLOPE = 5
TOPN = 8

conn = sqlite3.connect(DB)
print("loading daily_price ...")
df_all = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, amount, pct_change "
    "FROM daily_price", conn)
df_all["trade_date"] = pd.to_datetime(df_all["trade_date"])
groups = {c: g.sort_values("trade_date").reset_index(drop=True)
          for c, g in df_all.groupby("code")}
print("  stocks = %d, rows = %d" % (len(groups), len(df_all)))

print("loading stock_info.total_shares ...")
ts = {r[0]: (r[1] if r[1] and r[1] > 0 else None)
      for r in conn.execute("SELECT code, total_shares FROM stock_info").fetchall()}
conn.close()


def _roll_mean(s):
    return s.rolling(MA_N).mean()


# market breadth regime: fraction of stocks with close > MA20 per date
print("computing market breadth regime ...")
df_all["ma20"] = df_all.groupby("code")["close"].transform(_roll_mean)
close_f = df_all["close"].astype(float)
df_all["above_ma20"] = (close_f > df_all["ma20"]).astype(float)
breadth = df_all.groupby("trade_date")["above_ma20"].mean()
breadth_q = {q: breadth.quantile(q) for q in (0.10, 0.20, 0.30)}
print("  breadth quantiles 10/20/30%% = %.3f/%.3f/%.3f" %
      (breadth_q[0.10], breadth_q[0.20], breadth_q[0.30]))
breadth_map = breadth.to_dict()


def fusion_from_buyscore(bs):
    return bs * (10.0 / 3.0) * 5.0


def new_acc():
    return {"sum": 0.0, "n": 0, "pos": 0}


def add(acc, v):
    acc["sum"] += v
    acc["n"] += 1
    acc["pos"] += (1 if v > 0 else 0)


def stat(acc):
    if acc["n"]:
        return (acc["pos"] / acc["n"] * 100, acc["sum"] / acc["n"])
    return (float("nan"), float("nan"))


def eval_oc(g, idx):
    if idx + 5 >= len(g):
        return None
    base = g["open"].iloc[idx + 1]
    if not base or base <= 0:
        return None
    return tuple((g["close"].iloc[idx + off] / base - 1.0) * 100.0 for off in (1, 3, 5))


def top8_recs(mask, fusion, g):
    if not mask.any():
        return []
    sub = fusion[mask]
    recs = []
    for _, grp in sub.groupby(level=0):
        recs.extend(grp.sort_values(ascending=False).head(TOPN).index.tolist())
    return recs


def rsi14(close):
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = gain.rolling(14).mean()
    al = loss.rolling(14).mean()
    rs = ag / al.clip(lower=1e-9)
    return 100.0 - 100.0 / (1.0 + rs)


CONFIGS = ["base", "E1", "E2", "E3", "ALL", "ALLw"]
acc = {c: {k: new_acc() for k in ("oc1", "oc3", "oc5")} for c in CONFIGS}
n_rec = {c: 0 for c in CONFIGS}

print("running main backtest ...")
n_stocks = 0
for code, g in groups.items():
    g = g.copy()
    g.index = g["trade_date"]
    if len(g) < 30:
        continue
    n_stocks += 1

    b2 = strategy_bottom_fishing_v2(g)["BUY_SCORE"]
    f2 = fusion_from_buyscore(b2)

    q = quality_series(code, g, ts.get(code))
    gs = trend_gate_series(g)
    ch = chase_filter_series(g)
    ex = extension_filter_series(g)
    cl = g["close"].astype(float)
    ma20 = cl.rolling(MA_N).mean()
    vol = g["volume"].astype(float)
    vol5 = vol.rolling(5).mean()
    vol10 = vol.rolling(10).mean()
    rsi = rsi14(cl)

    base_mask = (f2 >= SIG) & q & gs & ch & ex

    br = g.index.to_series().map(breadth_map).fillna(1.0)
    e1_mask = {qk: br >= breadth_q[qk] for qk in (0.10, 0.20, 0.30)}
    e2_mask = (vol5 <= vol10 * 1.0)
    e3_mask = {rng: (rsi >= rng[0]) & (rsi <= rng[1])
               for rng in ((40, 65), (35, 70), (45, 60))}

    e1_used = e1_mask[0.20]
    e3_used = e3_mask[(40, 65)]
    all_mask = base_mask & e1_used & e2_mask & e3_used
    allw_mask = base_mask & e1_used & e2_mask & e3_mask[(35, 70)]

    config_masks = {
        "base": base_mask,
        "E1": base_mask & e1_used,
        "E2": base_mask & e2_mask,
        "E3": base_mask & e3_used,
        "ALL": all_mask,
        "ALLw": allw_mask,
    }

    for tag, mask in config_masks.items():
        for di in top8_recs(mask, f2, g):
            idx = g.index.get_loc(di)
            oc = eval_oc(g, idx)
            if oc is None:
                continue
            add(acc[tag]["oc1"], oc[0])
            add(acc[tag]["oc3"], oc[1])
            add(acc[tag]["oc5"], oc[2])
            n_rec[tag] += 1

print("  processed stocks = %d\n" % n_stocks)


def show(tag, label):
    a = acc[tag]
    w1, m1 = stat(a["oc1"])
    w3, m3 = stat(a["oc3"])
    w5, m5 = stat(a["oc5"])
    print("  %-22s rec%7d  T+1 %5.1f%%/%+6.3f%%  T+3 %5.1f%%/%+6.3f%%  T+5 %5.1f%%/%+6.3f%%" %
          (label, n_rec[tag], w1, m1, w3, m3, w5, m5))


print("=" * 82)
print("PART 1: entry-quality enhancement comparison (base = v2 current)")
print("=" * 82)
print("  %-22s %7s   %16s   %16s   %16s" % ("config", "recs", "T+1 w/m", "T+3 w/m", "T+5 w/m"))
show("base", "base v2")
show("E1", "+E1 breadth skip(20%)")
show("E2", "+E2 vol dry-up")
show("E3", "+E3 RSI(40-65)")
show("ALL", "+E1+E2+E3")
show("ALLw", "+E1+E2+E3(35-70)")

# E1 quantile sweep
print("\n" + "=" * 82)
print("PART 2: E1 breadth-skip quantile sweep")
print("=" * 82)
acc_e1 = {qk: {k: new_acc() for k in ("oc1", "oc3", "oc5")} for qk in (0.10, 0.20, 0.30)}
n_e1 = {qk: 0 for qk in (0.10, 0.20, 0.30)}
for code, g in groups.items():
    g = g.copy(); g.index = g["trade_date"]
    if len(g) < 30:
        continue
    b2 = strategy_bottom_fishing_v2(g)["BUY_SCORE"]; f2 = fusion_from_buyscore(b2)
    q = quality_series(code, g, ts.get(code)); gs = trend_gate_series(g)
    ch = chase_filter_series(g); ex = extension_filter_series(g)
    base_mask = (f2 >= SIG) & q & gs & ch & ex
    br = g.index.to_series().map(breadth_map).fillna(1.0)
    for qk in (0.10, 0.20, 0.30):
        m = base_mask & (br >= breadth_q[qk])
        for di in top8_recs(m, f2, g):
            idx = g.index.get_loc(di); oc = eval_oc(g, idx)
            if oc is None:
                continue
            add(acc_e1[qk]["oc1"], oc[0]); add(acc_e1[qk]["oc3"], oc[1]); add(acc_e1[qk]["oc5"], oc[2])
            n_e1[qk] += 1
for qk in (0.10, 0.20, 0.30):
    a = acc_e1[qk]; w1, m1 = stat(a["oc1"]); w5, m5 = stat(a["oc5"])
    print("  skip bottom %d%% breadth: rec%7d  T+1 %5.1f%%/%+6.3f%%  T+5 %5.1f%%/%+6.3f%%" %
          (int(qk * 100), n_e1[qk], w1, m1, w5, m5))

# E3 range sweep
print("\n" + "=" * 82)
print("PART 3: E3 RSI sweet-spot range sweep")
print("=" * 82)
ranges = ((40, 65), (35, 70), (45, 60), (30, 75))
acc_e3 = {rng: {k: new_acc() for k in ("oc1", "oc3", "oc5")} for rng in ranges}
n_e3 = {rng: 0 for rng in ranges}
for code, g in groups.items():
    g = g.copy(); g.index = g["trade_date"]
    if len(g) < 30:
        continue
    b2 = strategy_bottom_fishing_v2(g)["BUY_SCORE"]; f2 = fusion_from_buyscore(b2)
    q = quality_series(code, g, ts.get(code)); gs = trend_gate_series(g)
    ch = chase_filter_series(g); ex = extension_filter_series(g)
    base_mask = (f2 >= SIG) & q & gs & ch & ex
    rsi = rsi14(g["close"].astype(float))
    for rng in ranges:
        m = base_mask & (rsi >= rng[0]) & (rsi <= rng[1])
        for di in top8_recs(m, f2, g):
            idx = g.index.get_loc(di); oc = eval_oc(g, idx)
            if oc is None:
                continue
            add(acc_e3[rng]["oc1"], oc[0]); add(acc_e3[rng]["oc3"], oc[1]); add(acc_e3[rng]["oc5"], oc[2])
            n_e3[rng] += 1
for rng in ranges:
    a = acc_e3[rng]; w1, m1 = stat(a["oc1"]); w5, m5 = stat(a["oc5"])
    print("  RSI[%d,%d]: rec%7d  T+1 %5.1f%%/%+6.3f%%  T+5 %5.1f%%/%+6.3f%%" %
          (rng[0], rng[1], n_e3[rng], w1, m1, w5, m5))

print("\ndone.")
