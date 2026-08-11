# -*- coding: utf-8 -*-
"""一次性实验：v2 引擎原始候选（fs>=15 + gate&chase&ext，全市场）top-N 组合的
T+1 OC 与周/月收益，对照 stock_signal 写库信号口径。只读。
"""
import os
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd

from strategy.rec_filters import trend_gate_series, chase_filter_series, extension_filter_series
from strategy.strategies import strategy_bottom_fishing_v2

sys.stdout.reconfigure(encoding="utf-8")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SIG = 15.0
SLIPPAGE = 0.001
COMMISSION = 0.0003
STAMP = 0.001
MAINBOARD = ("300", "301", "688", "689")  # 创业板+科创板前缀


def find_idx(rows, date):
    for i, r in enumerate(rows):
        if r["trade_date"] >= date:
            return i
    return -1


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("加载全市场日线…")
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close, volume "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))
    conn.close()
    print(f"股票 {len(data)} 只")

    cand = {}
    fs_by = {}
    n = 0
    for code, rows in data.items():
        if len(rows) < 30:
            continue
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["trade_date"])
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        v2 = strategy_bottom_fishing_v2(df)["BUY_SCORE"]
        ok = trend_gate_series(df) & chase_filter_series(df) & extension_filter_series(df)
        fs = v2 * (10.0 / 3.0) * 5.0
        cand[code] = (fs >= SIG) & ok
        fs_by[code] = fs
        n += 1
    print(f"v2 候选计算完成 {n} 只")

    per_day = defaultdict(list)
    for code, s in cand.items():
        fss = fs_by[code]
        for ts, val in s.items():
            if val:
                d = ts.strftime("%Y-%m-%d")
                if d >= "2024-01-01":
                    per_day[d].append((code, float(fss.loc[ts])))

    def run(topn, mainboard_only, label, start="2024-01-01"):
        per_trade = []
        oc1_all = []
        n_days = 0
        for d in sorted(per_day.keys()):
            if d < start:
                continue
            lst = sorted(per_day[d], key=lambda x: (-x[1], x[0]))
            if mainboard_only:
                lst = [x for x in lst if not x[0].startswith(MAINBOARD)]
            picks = lst[:topn] if topn else lst
            if not picks:
                continue
            n_days += 1
            for code, _fs in picks:
                rows = data.get(code)
                if not rows:
                    continue
                idx = find_idx(rows, d)
                if idx < 0 or idx + 1 >= len(rows):
                    continue
                base = rows[idx + 1]["open"] * (1 + SLIPPAGE)
                if not base or base <= 0:
                    continue
                # T+1 收盘（纯持有）
                c1 = rows[idx + 1]["close"]
                if c1 and c1 > 0:
                    r1 = c1 * (1 - SLIPPAGE) / base - 1
                    oc1_all.append(r1 * 100)
                # 管理出场：止损 -6% / 止盈 +10% / 3天收盘（收盘确认）
                ret = None
                for off in range(1, 4):
                    j = idx + off
                    if j >= len(rows):
                        break
                    cl = rows[j]["close"]
                    if not cl or cl <= 0:
                        continue
                    r = cl * (1 - SLIPPAGE) / base - 1
                    if r <= -0.06:
                        ret = r * 100
                        break
                    if r >= 0.10:
                        ret = r * 100
                        break
                    if off == 3:
                        ret = r * 100
                if ret is not None:
                    per_trade.append({"date": d, "ret": ret - (COMMISSION * 2 + STAMP) * 100})
        wk, mo = defaultdict(list), defaultdict(list)
        for t in per_trade:
            dt = datetime.strptime(t["date"], "%Y-%m-%d")
            iso = dt.isocalendar()
            wk[(iso[0], iso[1])].append(t["ret"])
            mo[(dt.year, dt.month)].append(t["ret"])
        wv = [sum(v) / len(v) for v in wk.values()]
        mv = [sum(v) / len(v) for v in mo.values()]
        wok = sum(1 for v in wv if v >= 2.0) / len(wv) * 100 if wv else 0
        mok = sum(1 for v in mv if v >= 10.0) / len(mv) * 100 if mv else 0
        w1 = sum(1 for v in oc1_all if v > 0) / len(oc1_all) * 100 if oc1_all else 0
        print(f"\n[{label}] 推荐日 {n_days} 个 | T+1 OC 胜率 {w1:.1f}% "
              f"T+1 均值 {sum(oc1_all)/len(oc1_all):+.3f}% (n={len(oc1_all)})")
        print(f"  管理出场净收益: 周均值 {sum(wv)/len(wv):+.2f}% 达标率(≥2%) {wok:.0f}%"
              f" (n={len(wv)}) | 月均值 {sum(mv)/len(mv):+.2f}% 达标率(≥10%) {mok:.0f}%"
              f" (n={len(mv)})")

    run(None, False, "v2全候选 全市场(全部买入)")
    run(8, False, "v2 top-8 全市场")
    run(4, False, "v2 top-4 全市场")
    run(4, True, "v2 top-4 主板only")
    run(8, True, "v2 top-8 主板only")
    print("\n===== 近期窗口（2026-03-01 起，当前时点 edge 参考）=====")
    run(4, True, "v2 top-4 主板only", start="2026-03-01")
    run(8, True, "v2 top-8 主板only", start="2026-03-01")
    run(None, False, "v2全候选 全市场", start="2026-03-01")


if __name__ == "__main__":
    main()
