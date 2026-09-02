# -*- coding: utf-8 -*-
"""只读诊断（不写库）：个股深度「买点是否落在局部高点」

问题背景（用户反馈）：stock_deep 面板的买点（level=buy/add）常在阶段高位触发，买进就回调。

本脚本把 stock_deep.py 的 _signal_at + _rhythm_adjust + _classify 全历史逐日跑一遍，
对每个 buy/add 信号日统计：
  - 扩展度 pct_above_ma20（相对 MA20 偏离）
  - 相对近 20 日区间的位置 frac20 = (close-low20)/(high20-low20)
  - 当日是否创近 20 日收盘新高（nr20_new_high）
  - 前瞻收益 OC（次日开盘买 → T+N 收盘卖）N∈{1,2,3,5}，另有 CC(T+N 收盘/信号日收盘-1)

然后按上述维度分桶，看是否「越靠近高点，前瞻收益越差」，据此定位改进方向。

口径说明：
  - 样本：全市场活跃股确定性抽样（保证不同板块/量级都覆盖），每只 ≤250 个交易日。
  - 所有信号用 _signal_at（含追高/扩展度守卫）+ _rhythm_adjust（顺势加分/追高减分）。
  - OC 依赖次日开盘，故信号日需是该股最后一个交易日之前的第 N+1 天。
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategy.stock_deep import (
    _compute, _signal_at, _rhythm_adjust, _rhythm_at, _classify, detect_rhythm,
    _range_pos,
)

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SAMPLE = int(os.environ.get("SAMPLE", "240"))
LOOKBACK = 250


def load(conn, code, lookback=LOOKBACK):
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, volume, pct_change FROM daily_price "
        "WHERE code=? AND close IS NOT NULL ORDER BY trade_date ASC",
        (code,),
    ).fetchall()
    if len(rows) < 70:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.drop_duplicates(subset="trade_date", keep="last").set_index("trade_date")
    for c in ("open", "high", "low", "close", "volume", "pct_change"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_index().iloc[-lookback:]
    return df


def segment(sig_rows):
    """sig_rows: list of dict with keys ext, frac20, new_high, oc, cc, n. 返回分桶统计。"""
    def oc_stats(g, key):
        vals = [r[key] for r in g if r.get(key) is not None]
        if not vals:
            return None
        arr = np.array(vals, dtype=float)
        return (float((arr > 0).mean() * 100), float(arr.mean() * 100), int(len(arr)))

    def bucket_rows(rows, keyfn, labels):
        buckets = {L: [] for L in labels}
        for r in rows:
            b = keyfn(r)
            if b in buckets:
                buckets[b].append(r)
        return buckets

    print("\n=== 【A】买点 vs 扩展度 pct_above_ma20 ===")
    eb = bucket_rows(sig_rows, lambda r: r["ext_b"], ["<0", "0~3", "3~6", "6~10", "10~15", ">15"])
    for L, g in eb.items():
        if not g:
            continue
        o1, o3 = oc_stats(g, "oc1"), oc_stats(g, "oc3")
        o5, cc1 = oc_stats(g, "oc5"), oc_stats(g, "cc1")
        if o1:
            print(f"  ext {L:>5}: n={o1[2]:5d}  T+1 OC {o1[0]:5.1f}%/{o1[1]:+.3f}%  "
                  f"T+3 {o3[0]:5.1f}%/{o3[1]:+.3f}%  T+5 {o5[0]:5.1f}%/{o5[1]:+.3f}%  "
                  f"CC1 {cc1[0]:5.1f}%/{cc1[1]:+.3f}%")

    print("\n=== 【B】买点 vs 近20日区间位置 frac20 = (close-low20)/(high20-low20) ===")
    fb = bucket_rows(sig_rows, lambda r: r["frac_b"], ["<0.25", "0.25~0.5", "0.5~0.75", "0.75~0.9", ">=0.9"])
    for L, g in fb.items():
        if not g:
            continue
        o1, o3 = oc_stats(g, "oc1"), oc_stats(g, "oc3")
        if o1:
            print(f"  frac {L:>6}: n={o1[2]:5d}  T+1 OC {o1[0]:5.1f}%/{o1[1]:+.3f}%  "
                  f"T+3 {o3[0]:5.1f}%/{o3[1]:+.3f}%")

    print("\n=== 【C】买点当日是否创近20日收盘新高 (nr20_high) ===")
    nh = [r for r in sig_rows if r["new_high"]]
    no = [r for r in sig_rows if not r["new_high"]]
    for tag, g in (("创近20日新高", nh), ("未创新高", no)):
        o1, o3 = oc_stats(g, "oc1"), oc_stats(g, "oc3")
        if o1:
            print(f"  {tag}: n={o1[2]:5d}  T+1 OC {o1[0]:5.1f}%/{o1[1]:+.3f}%  "
                  f"T+3 {o3[0]:5.1f}%/{o3[1]:+.3f}%")

    print("\n=== 【D】整体买点池 ===")
    o1, o3 = oc_stats(sig_rows, "oc1"), oc_stats(sig_rows, "oc3")
    o5, cc1 = oc_stats(sig_rows, "oc5"), oc_stats(sig_rows, "cc1")
    if o1:
        print(f"  buy/add 全样本: n={o1[2]:5d}")
        print(f"    T+1 OC {o1[0]:5.1f}%/{o1[1]:+.3f}%   T+3 {o3[0]:5.1f}%/{o3[1]:+.3f}%")
        print(f"    T+5 OC {o5[0]:5.1f}%/{o5[1]:+.3f}%   CC1 {cc1[0]:5.1f}%/{cc1[1]:+.3f}%")
        exts = [r["ext"] for r in sig_rows if r["ext"] is not None]
        if exts:
            print(f"    买点扩展度均值 {np.mean(exts):+.2f}%  中位 {np.median(exts):+.2f}%  "
                  f">8% 占比 {(np.array(exts) > 8).mean()*100:.1f}%")
        fr = [r["frac20"] for r in sig_rows if r["frac20"] is not None]
        if fr:
            print(f"    买点区间位置均值 {np.mean(fr):.2f}  med {np.median(fr):.2f}  "
                  f">=0.75 占比 {(np.array(fr) >= 0.75).mean()*100:.1f}%")


    print("\n=== 【E】候选过滤对比（每个规则作用于全部 buy/add）===")
    filters = [
        ("基线(全部 buy/add)", lambda r: True),
        ("剔除“当日创近20日新高”", lambda r: not r["new_high"]),
        ("剔除 frac20>=0.9(顶部十分位)", lambda r: r["frac20"] is None or r["frac20"] < 0.9),
        ("保留 frac20<0.75(低位3/4)", lambda r: r["frac20"] is None or r["frac20"] < 0.75),
        ("保留 frac20<0.5(下半区)", lambda r: r["frac20"] is None or r["frac20"] < 0.5),
        ("剔除 ext>8(高扩展)", lambda r: r["ext"] is None or r["ext"] <= 8),
        ("组合: 剔新高 且 frac<0.75 且 ext<=8", lambda r: (not r["new_high"]) and (r["frac20"] is None or r["frac20"] < 0.75) and (r["ext"] is None or r["ext"] <= 8)),
    ]
    for tag, f in filters:
        g = [r for r in sig_rows if f(r)]
        o1, o3, o5 = oc_stats(g, "oc1"), oc_stats(g, "oc3"), oc_stats(g, "oc5")
        cc1 = oc_stats(g, "cc1")
        if o1:
            print(f"  {tag:38s}: n={o1[2]:5d}(砍{(1-o1[2]/len(sig_rows))*100:4.1f}%)  "
                  f"T+1 {o1[0]:5.1f}%/{o1[1]:+.3f}%  T+3 {o3[0]:5.1f}%/{o3[1]:+.3f}%  "
                  f"T+5 {o5[0]:5.1f}%/{o5[1]:+.3f}%  CC1 {cc1[0]:5.1f}%/{cc1[1]:+.3f}%")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    codes = [r["code"] for r in conn.execute(
        "SELECT d.code FROM daily_price d JOIN stock_info i ON i.code=d.code "
        "WHERE i.is_active=1 GROUP BY d.code ORDER BY d.code"
    ).fetchall()]
    # 确定性抽样，跨板块
    codes = codes[:: max(1, len(codes) // SAMPLE)][:SAMPLE]
    print(f"样本股票: {len(codes)} 只  每只 ≤{LOOKBACK} 日")

    sig_rows = []
    for ci, code in enumerate(codes):
        df = load(conn, code)
        if df is None:
            continue
        try:
            df = _compute(df)
            rhythm = detect_rhythm(df) if ("ATR" in df.columns) else {}
        except Exception:
            continue
        close = df["close"].to_numpy(dtype=float)
        ma20 = df["MA20"].to_numpy(dtype=float) if "MA20" in df.columns else np.full(len(df), np.nan)
        n = len(df)
        for i in range(30, n):  # 暖机后
            if i + 5 >= n:      # 需要至少 T+5 的后续行情
                break
            c0 = close[i]
            rng = _range_pos(df, i)
            frac20 = rng["frac20"]
            new_high = rng["new_high"]
            ext = float(np.nan) if np.isnan(ma20[i]) or ma20[i] <= 0 else (close[i] / ma20[i] - 1) * 100

            try:
                score, _, _ = _signal_at(df, i)
                score = _rhythm_adjust(score, _rhythm_at(rhythm, i), rng)
                lvl = _classify(score)
            except Exception:
                continue
            if lvl not in ("buy", "add"):
                continue

            base = df.iloc[i + 1]["open"]  # 次日开盘
            if not base or base <= 0 or np.isnan(base):
                continue
            rec = {"ext": ext, "frac20": frac20, "new_high": new_high,
                   "oc1": None, "oc3": None, "oc5": None, "cc1": None,
                   "ext_b": None, "frac_b": None}
            if np.isfinite(ext):
                rec["ext_b"] = ("<0" if ext < 0 else "0~3" if ext < 3 else
                                "3~6" if ext < 6 else "6~10" if ext < 10 else
                                "10~15" if ext < 15 else ">15")
            if frac20 is not None and np.isfinite(frac20):
                rec["frac_b"] = ("<0.25" if frac20 < 0.25 else
                                 "0.25~0.5" if frac20 < 0.5 else
                                 "0.5~0.75" if frac20 < 0.75 else
                                 "0.75~0.9" if frac20 < 0.9 else ">=0.9")
            for k, N in (("oc1", 1), ("oc3", 3), ("oc5", 5)):
                if i + N + 1 < n:
                    rec[k] = df.iloc[i + N]["close"] / base - 1  # 次日开盘买 → T+N 收盘卖
            rec["cc1"] = df.iloc[i + 1]["close"] / close[i] - 1 if i + 1 < n else None
            sig_rows.append(rec)
        if (ci + 1) % 60 == 0:
            print(f"  已处理 {ci+1}/{len(codes)}  累计买点 {len(sig_rows)}")
    conn.close()

    print(f"\n累计 buy/add 信号: {len(sig_rows)}")
    segment(sig_rows)


if __name__ == "__main__":
    main()
