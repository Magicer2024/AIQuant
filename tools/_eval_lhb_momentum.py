# -*- coding: utf-8 -*-
"""一次性验证：龙虎榜隔日动量信号全期（2024-07 ~ 2026-08）表现。
条件：net_buy_ratio >= 阈值 且 当日非涨停；次日开盘买入（OC 现实口径）。
输出：阈值扫描的 T+1/T+3 OC 胜率均值、管理出场（止损-4%/止盈+6.5%/3天）
周/月收益与 2%/10% 达标率、主板对照。只读不写库。
"""
import os
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SLIPPAGE = 0.001
COMMISSION = 0.0003
STAMP = 0.001
MAINBOARD = ("300", "301", "688", "689")


def find_idx(rows, date):
    for i, r in enumerate(rows):
        if r["trade_date"] >= date:
            return i
    return -1


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("加载龙虎榜 + 日线…")
    lhb = conn.execute(
        "SELECT trade_date, code, net_buy_ratio, pct_change FROM stock_lhb_detail"
    ).fetchall()
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    conn.close()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))
    print(f"龙虎榜 {len(lhb)} 条，日线 {len(data)} 只")

    # 按 (code, date) 建索引
    by_day = defaultdict(list)
    for r in lhb:
        by_day[r["trade_date"]].append(dict(r))

    def run(thr, mainboard_only, label):
        per_trade = []
        oc1, oc3 = [], []
        n_days = 0
        for d in sorted(by_day.keys()):
            lst = [x for x in by_day[d]
                   if x["net_buy_ratio"] >= thr and (x["pct_change"] or 0) < 9.8]
            if mainboard_only:
                lst = [x for x in lst if not x["code"].startswith(MAINBOARD)]
            if not lst:
                continue
            n_days += 1
            for x in lst:
                rows = data.get(x["code"])
                if not rows:
                    continue
                idx = find_idx(rows, d)
                if idx < 0 or idx + 1 >= len(rows):
                    continue
                base = rows[idx + 1]["open"] * (1 + SLIPPAGE)
                if not base or base <= 0:
                    continue
                c1 = rows[idx + 1]["close"]
                if c1 and c1 > 0:
                    oc1.append((c1 * (1 - SLIPPAGE) / base - 1) * 100)
                if idx + 3 < len(rows):
                    c3 = rows[idx + 3]["close"]
                    if c3 and c3 > 0:
                        oc3.append((c3 * (1 - SLIPPAGE) / base - 1) * 100)
                # 管理出场：止损 -4% / 止盈 +6.5% / 3 天收盘（NEXT_DAY_MOMENTUM 参数）
                ret = None
                for off in range(1, 4):
                    j = idx + off
                    if j >= len(rows):
                        break
                    cl = rows[j]["close"]
                    if not cl or cl <= 0:
                        continue
                    r = cl * (1 - SLIPPAGE) / base - 1
                    if r <= -0.04:
                        ret = r * 100
                        break
                    if r >= 0.065:
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
        w1 = sum(1 for v in oc1 if v > 0) / len(oc1) * 100 if oc1 else 0
        w3 = sum(1 for v in oc3 if v > 0) / len(oc3) * 100 if oc3 else 0
        wok = sum(1 for v in wv if v >= 2.0) / len(wv) * 100 if wv else 0
        mok = sum(1 for v in mv if v >= 10.0) / len(mv) * 100 if mv else 0
        print(f"\n[{label}] 净买≥{thr}% 非涨停: {n_days} 个上榜日 / {len(oc1)} 笔")
        print(f"  T+1 OC: 胜率 {w1:.1f}% 均值 {sum(oc1)/len(oc1):+.3f}%"
              f" (n={len(oc1)}) | T+3 OC: 胜率 {w3:.1f}% 均值 {sum(oc3)/len(oc3):+.3f}%"
              f" (n={len(oc3)})")
        print(f"  管理出场净: 周均值 {sum(wv)/len(wv):+.2f}% 达标(≥2%) {wok:.0f}%"
              f" | 月均值 {sum(mv)/len(mv):+.2f}% 达标(≥10%) {mok:.0f}%"
              f" | 累计 {(1+sum(wv)/100)**len(wv)*100-100:+.1f}%")

    for thr in (5, 10, 15):
        run(thr, False, f"全市场 thr={thr}")
        run(thr, True, f"主板only thr={thr}")


if __name__ == "__main__":
    main()
