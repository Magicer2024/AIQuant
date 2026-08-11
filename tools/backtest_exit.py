"""
backtest_exit.py - simulate REALIZED returns under different exit rules for v2 Top8.

The v2 signal has positive drift: T+1 OC mean +0.100% but T+5 OC mean +0.225%.
The live system caps hold at 1 day (short_max_hold_days=1) with stop -5% / take +8%,
which throws away the drift. This script simulates the ACTUAL exit logic:

  entry = T+1 open
  for off in 1..maxhold:
      if day_high >= entry*(1+take):  exit at +take   (take-profit hit)
      elif day_low <= entry*(1-stop): exit at -stop   (stop hit)
      elif off == maxhold:            exit at day_close
  realized = exit/entry - 1 ; win = realized > 0

Grid: maxhold in {1,3,5}; (stop,take) in {(5,8),(4,6),(6,10),(5,12)} (percent).
Reports realized win rate, mean, median, take-hit%, stop-hit%. Read-only.
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
TOPN = 8

conn = sqlite3.connect(DB)
df_all = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, amount, pct_change "
    "FROM daily_price", conn)
df_all["trade_date"] = pd.to_datetime(df_all["trade_date"])
groups = {c: g.sort_values("trade_date").reset_index(drop=True)
          for c, g in df_all.groupby("code")}
ts = {r[0]: (r[1] if r[1] and r[1] > 0 else None)
      for r in conn.execute("SELECT code, total_shares FROM stock_info").fetchall()}
conn.close()


def fusion_from_buyscore(bs):
    return bs * (10.0 / 3.0) * 5.0


def rsi14(close):
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = gain.rolling(14).mean()
    al = loss.rolling(14).mean()
    rs = ag / al.clip(lower=1e-9)
    return 100.0 - 100.0 / (1.0 + rs)


def top8_recs(mask, fusion, g):
    if not mask.any():
        return []
    sub = fusion[mask]
    recs = []
    for _, grp in sub.groupby(level=0):
        recs.extend(grp.sort_values(ascending=False).head(TOPN).index.tolist())
    return recs


def simulate(g, idx, maxhold, stop, take):
    """return realized return (frac) or None"""
    if idx + maxhold + 1 >= len(g):
        return None
    entry = g["open"].iloc[idx + 1]
    if not entry or entry <= 0:
        return None
    for off in range(1, maxhold + 1):
        hi = g["high"].iloc[idx + off]
        lo = g["low"].iloc[idx + off]
        cl = g["close"].iloc[idx + off]
        if hi >= entry * (1 + take):
            return take
        if lo <= entry * (1 - stop):
            return -stop
        if off == maxhold:
            return cl / entry - 1.0
    return None


print("running exit simulation (v2 Top8, with/without E3 RSI sweet-spot) ...")
# accumulate per (e3flag, maxhold, stop, take)
cells = {}
n_stocks = 0
for code, g in groups.items():
    g = g.copy(); g.index = g["trade_date"]
    if len(g) < 30:
        continue
    n_stocks += 1
    b2 = strategy_bottom_fishing_v2(g)["BUY_SCORE"]
    f2 = fusion_from_buyscore(b2)
    q = quality_series(code, g, ts.get(code))
    gs = trend_gate_series(g)
    ch = chase_filter_series(g)
    ex = extension_filter_series(g)
    rsi = rsi14(g["close"].astype(float))
    e3 = (rsi >= 40) & (rsi <= 65)
    base_mask = (f2 >= SIG) & q & gs & ch & ex
    for e3flag, mask in ((0, base_mask), (1, base_mask & e3)):
        recs = top8_recs(mask, f2, g)
        for di in recs:
            idx = g.index.get_loc(di)
            for maxhold in (1, 3, 5):
                for (sp, tk) in ((0.05, 0.08), (0.04, 0.06), (0.06, 0.10), (0.05, 0.12)):
                    r = simulate(g, idx, maxhold, sp, tk)
                    if r is None:
                        continue
                    key = (e3flag, maxhold, sp, tk)
                    c = cells.setdefault(key, {"sum": 0.0, "n": 0, "pos": 0,
                                               "take": 0, "stop": 0})
                    c["sum"] += r; c["n"] += 1; c["pos"] += (1 if r > 0 else 0)
                    if r >= tk - 1e-9:
                        c["take"] += 1
                    if r <= -sp + 1e-9:
                        c["stop"] += 1

print("  processed stocks = %d\n" % n_stocks)

print("=" * 100)
print("REALIZED exit simulation (v2 Top8): entry=T+1 open, stop/take intraday, else exit at maxhold close")
print("  columns: win% = P(realized>0); take%/stop% = share of trades exited by that rule")
print("=" * 100)
print("  %-4s %-10s %-10s %9s %9s %9s %9s %9s" %
      ("E3", "maxhold", "stop/take", "trades", "win%", "mean%", "take%", "stop%"))
for e3flag in (0, 1):
    for maxhold in (1, 3, 5):
        for (sp, tk) in ((0.05, 0.08), (0.04, 0.06), (0.06, 0.10), (0.05, 0.12)):
            c = cells.get((e3flag, maxhold, sp, tk))
            if not c or not c["n"]:
                continue
            win = c["pos"] / c["n"] * 100
            mean = c["sum"] / c["n"] * 100
            takep = c["take"] / c["n"] * 100
            stopp = c["stop"] / c["n"] * 100
            print("  %-4s %-10d %-10s %9d %8.1f%% %8.3f%% %8.1f%% %8.1f%%" %
                  ("Y" if e3flag else "N", maxhold, "%d/%d" % (int(sp * 100), int(tk * 100)),
                   c["n"], win, mean, takep, stopp))

print("\ndone.")
