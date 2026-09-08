# -*- coding: utf-8 -*-
"""只读测量（不写库）：个股深度计划价的「止盈到底打不打得到」。

为什么要测：后续优化方向里排在第一位的假设是 —— 固定 rr_target=2.5 / stop_mult=2.5
对平均波幅很小的震荡票根本不可达，于是几乎所有交易都被"到期"或"止损"截断，
止盈线只是一条画给人看的装饰线。这个假设如果站不住，方向本身就不该做，
所以先量一遍，不要凭感觉往下推（同 [[feedback-no-invented-numbers-in-ui]] 的纪律）。

口径完全复用生产路径：信号日次日开盘买入、A股 T+1、卖出判定用当日收盘价、
出场优先级 收盘破止损 → 固定止盈 → 到期（max_hold_days 取 get_max_hold("short")，默认 10）。

分组维度用该股自己的节奏统计 detect_rhythm.up_amp_avg（平均上涨波幅 %），分位数分桶 ——
如果止盈触达率随本股波幅单调上升，说明"全局常量参数"确实和个股性格错配。

环境变量：SAMPLE=200 抽样只数  LOOKBACK=250
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategy.stock_deep import _compute, detect_rhythm, recent_advice, get_max_hold
from strategy.exit_advisor import evaluate_exit_by_prices

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SAMPLE = int(os.environ.get("SAMPLE", "200"))
LOOKBACK = int(os.environ.get("LOOKBACK", "250"))


def load(conn, code):
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, volume, pct_change FROM daily_price "
        "WHERE code=? AND close IS NOT NULL ORDER BY trade_date ASC",
        (code,),
    ).fetchall()
    if len(rows) < 90:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.drop_duplicates(subset="trade_date", keep="last").set_index("trade_date")
    for c in ("open", "high", "low", "close", "volume", "pct_change"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index().iloc[-LOOKBACK:]


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    codes = [r["code"] for r in conn.execute(
        "SELECT d.code FROM daily_price d JOIN stock_info i ON i.code=d.code "
        "WHERE i.is_active=1 AND d.code NOT LIKE '30%' AND d.code NOT LIKE '68%' "
        "  AND COALESCE(i.name,'') NOT LIKE '%ST%' "
        "GROUP BY d.code HAVING COUNT(*) >= ? ORDER BY d.code",
        (LOOKBACK,),
    ).fetchall()]
    codes = codes[:: max(1, len(codes) // SAMPLE)][:SAMPLE]
    hold_cap = get_max_hold("short") or 10
    print(f"样本：主板活跃非ST {len(codes)} 只，max_hold_days={hold_cap}，出场口径=生产同路径")

    recs = []
    for ci, code in enumerate(codes):
        df = load(conn, code)
        if df is None:
            continue
        try:
            df = _compute(df)
            rhythm = detect_rhythm(df)
            adv = recent_advice(df, rhythm, days=len(df) - 30)
        except Exception:
            continue
        up_amp = rhythm.get("up_amp_avg")
        down_amp = rhythm.get("down_amp_avg")
        date_idx = {d: i for i, d in enumerate(df.index)}
        for a in adv:
            if a.get("level") not in ("buy", "add") or a.get("take_profit") is None:
                continue
            i = date_idx.get(a["date"])
            if i is None or i + 1 >= len(df):
                continue
            entry = float(df.iloc[i + 1]["open"])
            if not np.isfinite(entry) or entry <= 0:
                continue
            res = evaluate_exit_by_prices(
                entry_price=entry, entry_date=a["date"], df=df,
                stop_loss=a.get("stop_loss"), take_profit=a.get("take_profit"),
                max_hold_days=hold_cap)
            d = res.get("detail") or {}
            reason = d.get("exit_reason") or ("持仓中" if res.get("status") == "hold" else "其他")
            # 完成度：持有期内最高收盘相对入场价的实际涨幅 ÷ 止盈要求的涨幅
            take_req = a["take_profit"] / entry - 1
            j0, j1 = i + 1, min(i + 1 + hold_cap, len(df) - 1)
            got = float(df["close"].iloc[j0:j1 + 1].max()) / entry - 1
            recs.append({"code": code, "level": a["level"], "reason": reason,
                         "ret": d.get("current_pnl_pct"),
                         "up_amp": up_amp, "down_amp": down_amp,
                         "take_req": take_req * 100,
                         "ratio": (got / take_req) if take_req and take_req > 0 else None,
                         "stop_dist": (1 - a["stop_loss"] / entry) * 100 if a.get("stop_loss") else None})
        if (ci + 1) % 50 == 0:
            print(f"  已处理 {ci+1}/{len(codes)}  累计笔数 {len(recs)}")
    conn.close()

    if not recs:
        print("无可测笔")
        return
    print(f"\n总笔数 {len(recs)}")

    def dist(g):
        n = len(g)
        r = {}
        for t in g:
            key = ("止盈" if "止盈" in t["reason"] else
                   "止损" if "止损" in t["reason"] else
                   "到期" if "到期" in t["reason"] else t["reason"])
            r[key] = r.get(key, 0) + 1
        parts = "  ".join(f"{k} {v}({v/n*100:.1f}%)" for k, v in
                          sorted(r.items(), key=lambda kv: -kv[1]))
        rets = np.asarray([t["ret"] for t in g if t["ret"] is not None], dtype=float)
        wr = float((rets > 0).mean() * 100) if rets.size else 0.0
        return f"n={n:5d}  {parts}  | 胜率 {wr:5.1f}% 均值 {rets.mean():+.2f}%" if rets.size else f"n={n:5d}  {parts}"

    print("\n=== 【A】全部 buy/add 笔的出场原因分布 ===")
    print("  " + dist(recs))

    amps = np.asarray([r["up_amp"] for r in recs if r["up_amp"] is not None], dtype=float)
    if amps.size:
        q33, q66 = np.percentile(amps, [33, 66])
        print(f"\n=== 【B】按本股平均上涨波幅分桶（up_amp_avg，单位 %）===")
        print(f"  分界点：33%={q33:.1f}  66%={q66:.1f}")
        for lo, hi, lab in ((0, q33, "小波幅"), (q33, q66, "中波幅"), (q66, 1e9, "大波幅")):
            g = [r for r in recs if r["up_amp"] is not None and lo <= r["up_amp"] < hi]
            if g:
                req = np.asarray([r["take_req"] for r in g], dtype=float)
                rat = np.asarray([r["ratio"] for r in g if r["ratio"] is not None], dtype=float)
                print(f"  {lab} up_amp∈[{lo:.1f},{hi:.1f}):  " + dist(g))
                print(f"      止盈需涨 {req.mean():.1f}%（中位 {np.median(req):.1f}%）  "
                      f"实际完成度 中位 {np.median(rat)*100:.0f}%  "
                      f"≥100% 占比 {(rat >= 1).mean()*100:.1f}%")
        print("\n=== 【C】波幅十等分：止盈触达率是否随本股性格单调 ===")
        dec = np.percentile(amps, np.arange(10, 100, 10))
        edges = [0] + list(dec) + [1e9]
        for b in range(10):
            g = [r for r in recs if r["up_amp"] is not None
                 and edges[b] <= r["up_amp"] < edges[b + 1]]
            if len(g) < 10:
                continue
            tp = sum(1 for t in g if "止盈" in t["reason"]) / len(g) * 100
            sl = sum(1 for t in g if "止损" in t["reason"]) / len(g) * 100
            ex = sum(1 for t in g if "到期" in t["reason"]) / len(g) * 100
            req = np.mean([t["take_req"] for t in g])
            print(f"  桶{b+1} up_amp<{edges[b+1]:5.1f}%: n={len(g):5d}  "
                  f"止盈 {tp:5.1f}%  止损 {sl:5.1f}%  到期 {ex:5.1f}%  止盈需涨 {req:4.1f}%")

    stops = [r["stop_dist"] for r in recs if r["stop_dist"] is not None]
    if stops:
        s = np.asarray(stops, dtype=float)
        print(f"\n=== 【D】止损宽度分布（相对入场价 %）===")
        print(f"  中位 {np.median(s):.1f}%  均值 {s.mean():.1f}%  "
              f">10% 占比 {(s > 10).mean()*100:.1f}%  >15% 占比 {(s > 15).mean()*100:.1f}%")
        print("  （1万元本金、单笔约 3 千元：止损宽度直接决定单票最大亏损额）")


if __name__ == "__main__":
    main()
