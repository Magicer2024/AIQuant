# -*- coding: utf-8 -*-
"""大样本验证：入场过滤(fus24/宽度>70跳过) × 出场修正(lc/stopfill) 在 3 年候选池的表现。

候选池/行情复用 tools/eval_short_observe_rule（生产链路口径），入场=回踩确认，
出场状态机为 _short_exit_sim 的变体实现（仅加 lc/stopfill 两个开关）。
用法：python tools/_eval_short_package_big.py [start] [end]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config.strategy_params import get_param  # noqa: E402
from core.db import get_conn  # noqa: E402
from strategy.exit_advisor import get_max_hold  # noqa: E402
import eval_short_observe_rule as base  # noqa: E402


def vsim(cl, hi, entry, stop, tp, opts):
    trail = opts.get("trail", float(get_param("short_trailing_pct")))
    partial = opts.get("launch", float(get_param("short_take_profit")))
    max_hold = opts.get("mh", int(get_param("short_max_hold_days")))
    lc = opts.get("lc", False)
    sf = opts.get("sf", False)
    ratchet = opts.get("ratchet")
    stall = opts.get("stall")
    if stop is not None and stop <= 0:
        stop = None
    if tp is not None and tp <= 0:
        tp = None
    if stop is not None and tp is not None and tp <= stop:
        tp = None
    launch_line = entry * (1 + partial) if partial and partial < 1 else tp
    ten = trail is not None and 0.01 <= trail < 1.0 and launch_line is not None
    if ten and partial and trail >= partial:
        trail = max(0.01, round(partial * 0.8, 4))
    highest = None
    launched = False
    tline = None
    n = min(max_hold, len(cl))
    for j in range(1, n + 1):
        close = cl[j - 1]
        if close is None or close <= 0:
            break
        high = hi[j - 1] or close
        if highest is None or high > highest:
            highest = high
        if ratchet and highest >= entry * (1 + ratchet[0]):
            flo = entry * (1 + ratchet[1])
            stop = flo if stop is None else max(stop, flo)
        if ten and not launched:
            if (close >= launch_line) if lc else (highest >= launch_line):
                launched = True
        if ten and launched:
            line = highest * (1 - trail)
            if tline is None or line > tline:
                tline = line
        if j == 1:
            continue
        if stop is not None and close <= stop:
            px = stop if (sf and stop <= entry) else close
            return round((px - entry) / entry * 100, 2), True
        if ten and launched and tline is not None and close <= tline:
            return round((close - entry) / entry * 100, 2), True
        if stall and j >= stall and not launched and close < entry:
            return round((close - entry) / entry * 100, 2), True
        if j == max_hold:
            return round((close - entry) / entry * 100, 2), True
    return None, False


def run(signals, prices, brmap, opts, ffilter):
    trail = float(get_param("short_trailing_pct"))
    max_hold = get_max_hold("short") or 10
    ew = max(1, int(get_param("short_entry_window_days")))
    need = ew + max_hold + 2
    rets = []
    by = {}
    for s in signals:
        if not ffilter(s, brmap):
            continue
        p = prices.get(s["code"])
        entry = s.get("buy_price")
        if p is None or not entry or entry <= 0:
            continue
        d0 = np.datetime64(s["scan_date"]).astype("datetime64[D]").astype(np.int64)
        i0 = int(np.searchsorted(p["d"], d0, side="right"))
        if i0 >= len(p["d"]):
            continue
        sl = slice(i0, min(i0 + need, len(p["d"])))
        op, cl, hi, lo = p["open"][sl], p["close"][sl], p["high"][sl], p["low"][sl]
        fill = None
        ee = None
        for idx in range(min(ew, len(lo))):
            if np.isnan(lo[idx]):
                continue
            if lo[idx] <= entry:
                o = op[idx] if not np.isnan(op[idx]) else entry
                ee = round(min(float(entry), float(o)), 2)
                fill = idx
                break
        if fill is None:
            continue
        clh = [None if np.isnan(v) else float(v) for v in cl[fill:fill + max_hold]]
        hih = [None if np.isnan(v) else float(v) for v in hi[fill:fill + max_hold]]
        ret, done = vsim(clh, hih, ee, s.get("stop_loss"), s.get("take_profit"), opts)
        if done and ret is not None:
            rets.append(ret)
            by.setdefault(s["scan_date"][:4], []).append(ret)
    return rets, by


def stat(rets):
    if not rets:
        return None
    return (len(rets), round(sum(rets) / len(rets), 3),
            round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1))


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2023-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-10-09"
    signals = base.build_population(start, end)
    prices = base.load_prices(start)
    with get_conn() as conn:
        br = pd.read_sql_query(
            "SELECT trade_date, AVG(CASE WHEN pct_change>0 THEN 1.0 ELSE 0.0 END) AS b "
            "FROM daily_price WHERE trade_date >= ? GROUP BY trade_date", conn,
            params=(start,))
    brmap = {r["trade_date"]: r["b"] * 100 for _, r in br.iterrows()}
    print(f"候选池 {len(signals)} 信号 | {start}~{end}")

    always = lambda s, b: True
    f24 = lambda s, b: (s.get("fusion_score") or 0) >= 24
    fbr = lambda s, b: b.get(s["scan_date"]) is None or b.get(s["scan_date"]) <= 70
    f24br = lambda s, b: f24(s, b) and fbr(s, b)
    packs = {
        "base": ({}, always),
        "lc+sf": ({"lc": True, "sf": True}, always),
        "fus24": ({}, f24),
        "fus24+lc+sf": ({"lc": True, "sf": True}, f24),
        "fus24+br70": ({}, f24br),
        "fus24+br70+lc+sf": ({"lc": True, "sf": True}, f24br),
    }
    print(f"\n{'package':<20}{'n':>7}{'均笔%':>9}{'胜率%':>8}   分年均笔")
    for name, (opts, ff) in packs.items():
        rets, by = run(signals, prices, brmap, opts, ff)
        st = stat(rets)
        yrs = " ".join(f"{y}:{round(sum(v)/len(v),2)}" for y, v in sorted(by.items()))
        print(f"{name:<20}{st[0]:>7}{st[1]:>9}{st[2]:>8}   {yrs}")


if __name__ == "__main__":
    main()
