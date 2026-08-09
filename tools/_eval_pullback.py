# -*- coding: utf-8 -*-
"""P1-2.1 回测对比（只读，不写库）：已反弹版 vs 买回踩 v2 平滑权重版。

口径与 tools/diag_short_reco.py 完全一致（苹果对苹果）：
  - 候选 = fs >= sig_threshold(15) 且 trend_gate & chase & extension 通过
    （纯抄底权重下 fs = BUY_SCORE × (10/3) × 5 = BUY_SCORE × 16.67）
  - T+N OC = 信号日后第 N 个交易日收盘 / 次日开盘 - 1
  - 聚合最近 ~35 个有后续行情的 short scan_date，避免单日偏差
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from strategy.rec_filters import trend_gate_series, chase_filter_series, extension_filter_series
from strategy.strategies import strategy_bottom_fishing, strategy_bottom_fishing_v2

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SIG_THRESHOLD = 15.0


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT scan_date AS d FROM stock_signal WHERE horizon='short' "
        "AND strategy IN ('短线融合','超跌反弹v3') ORDER BY scan_date DESC"
    ).fetchall()]
    if not dates:
        print("无短线信号数据")
        return

    # 全市场日线（一次加载）
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close, volume "
        "FROM daily_price ORDER BY code, trade_date"
    ).fetchall()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(r)
    conn.close()

    def find_idx(rows, date):
        for i, r in enumerate(rows):
            if r["trade_date"] >= date:
                return i
        return -1

    # 每只股票：OLD / v2 的 BUY_SCORE 全历史 + 共用过滤链
    cand_old, cand_v2 = {}, {}
    dev_old, dev_v2 = {}, {}
    for code, rows in data.items():
        if len(rows) < 30:
            continue
        df = pd.DataFrame([dict(r) for r in rows])
        df.index = pd.to_datetime(df["trade_date"])
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        old = strategy_bottom_fishing(df)["BUY_SCORE"]
        v2 = strategy_bottom_fishing_v2(df)["BUY_SCORE"]
        gate = trend_gate_series(df)
        chase = chase_filter_series(df)
        ext = extension_filter_series(df)
        ok = gate & chase & ext
        fs_old = old * (10.0 / 3.0) * 5.0
        fs_v2 = v2 * (10.0 / 3.0) * 5.0
        cand_old[code] = (fs_old >= SIG_THRESHOLD) & ok
        cand_v2[code] = (fs_v2 >= SIG_THRESHOLD) & ok
        # 候选日的 MA20 偏离（机制验证：v2 是否真把候选拉向低扩展度）
        ma20 = df["close"].rolling(20).mean()
        dev_old[code] = ((df["close"] / ma20 - 1) * 100).where(cand_old[code])
        dev_v2[code] = ((df["close"] / ma20 - 1) * 100).where(cand_v2[code])

    # 聚合 cohort：最近 35 个有后续行情的 short scan_date
    def pool(cands, devs):
        S = {k: [] for k in ["oc1", "oc3", "pa"]}
        pooled = 0
        for d in dates[1:40]:
            ts = pd.Timestamp(d)
            # 该日有候选的股票
            hits = [c for c, s in cands.items() if bool(s.get(ts, False))]
            if not hits:
                continue
            sample = data.get(hits[0])
            if not sample or find_idx(sample, d) < 0 or find_idx(sample, d) + 6 >= len(sample):
                continue
            pooled += 1
            for code in hits:
                rows = data.get(code)
                if not rows:
                    continue
                idx = find_idx(rows, d)
                if idx < 0 or idx + 6 >= len(rows):
                    continue
                base = rows[idx + 1]["open"]
                if not base or base <= 0:
                    continue
                S["oc1"].append(rows[idx + 1]["close"] / base - 1)
                S["oc3"].append(rows[idx + 3]["close"] / base - 1)
                dv = devs.get(code)
                if dv is not None:
                    v = dv.get(ts)
                    if v is not None and pd.notna(v):
                        S["pa"].append(v)
        return pooled, S

    def stats(S):
        return (sum(1 for x in S if x > 0) / len(S) * 100, sum(S) / len(S) * 100, len(S))

    p_old, S_old = pool(cand_old, dev_old)
    p_v2, S_v2 = pool(cand_v2, dev_v2)
    print("=" * 72)
    print("P1-2.1 回测对比：已反弹版(OLD) vs 买回踩平滑版(v2)，diag 同口径 OC")
    print("=" * 72)
    for tag, p, S in [("OLD 已反弹", p_old, S_old), ("v2  买回踩", p_v2, S_v2)]:
        if not S["oc1"]:
            print(f"\n{tag}: 无候选样本")
            continue
        w1, m1, n1 = stats(S["oc1"])
        w3, m3, n3 = stats(S["oc3"])
        pa = sum(S["pa"]) / len(S["pa"]) if S["pa"] else float("nan")
        print(f"\n{tag}: cohort={p}  候选日样本 n(oc1)={n1} / n(oc3)={n3}")
        print(f"  T+1 OC: 胜率 {w1:.1f}%  均值 {m1:+.3f}%")
        print(f"  T+3 OC: 胜率 {w3:.1f}%  均值 {m3:+.3f}%")
        print(f"  候选日 MA20 偏离均值: {pa:+.2f}%")


if __name__ == "__main__":
    main()
