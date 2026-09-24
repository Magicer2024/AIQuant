"""long 选股——**票池 alpha 检验** + **打分项 leave-one-out 归因**。

之前所有分析（_diag_long_rescore / _diag_long_key_eval / _diag_long_exit_sim）都只在
「score>=2.0 的票池内部」换排序键，从没测过**票池本身有没有 alpha**。如果票池相对全市场
就是负超额，换排序键救不了，必须改选股条件。

本脚本一次重放，产出可复用的增强缓存：
  - mask：4 个打分条件的 bitmask（bit0 趋势 / bit1 斜率 / bit2 低波 / bit3 回撤受控）
    ⇒ 之后可任意组合条件做 threshold 网格与 leave-one-out，无需重算
  - fwd：T+hold 裸持有收益
  - feats：10 个连续特征（供排序键挖掘复用）
  - **全市场样本也记录**（mask 任意，含不满足条件的票）⇒ 可算「票池 vs 全市场」超额

用法
----
    python tools/_diag_long_pool.py --start 2015-01-01 --step 5 --hold 60 \
        --cache logs/_long_pool_cache.pkl          # 重放并缓存
    python tools/_diag_long_pool.py --from-cache logs/_long_pool_cache.pkl   # 只分析
"""

import argparse
import math
import os
import pickle
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")

FEAT_KEYS = ["ma60_120_gap", "ma120_slope", "ext_ma120", "ext_ma20", "vol60",
             "dd250", "mom20", "mom60", "mom120", "vol_ratio"]


# ─────────────────────────────────────────────
# 1. 重放（与 strategy/mid_long.py::scan_long_term 逐行对齐）
# ─────────────────────────────────────────────

def load(start_date):
    import pandas as pd
    conn = sqlite3.connect(DB)
    q = """SELECT code, trade_date, close FROM daily_price
           WHERE trade_date >= ? AND close IS NOT NULL AND close > 0
           ORDER BY code, trade_date ASC"""
    df = pd.read_sql_query(q, conn, params=(start_date,))
    conn.close()
    df["close"] = df["close"].astype("float32")
    return df


def build(df, step=5, hold=60, verbose=True):
    """逐只计算，采样点落地 (code, date, mask, fwd, feats)。

    mask 位定义（与 scan_long_term 的四项一一对应）：
      bit0 = MA60>MA120 且 close>MA120        （长趋势向上）
      bit1 = MA120 斜率向上（>21 交易日前）
      bit2 = 60 日年化波动 < 35%
      bit3 = 距 250 日高点回撤 < 40%
    """
    import pandas as pd
    out = []
    n = df["code"].nunique()
    for i, (code, g) in enumerate(df.groupby("code", sort=False)):
        if len(g) < 300:
            continue
        close = pd.Series(g["close"].values.astype("float64"),
                          index=pd.DatetimeIndex(g["trade_date"].values))
        ma60 = close.rolling(60).mean()
        ma120 = close.rolling(120).mean()
        ma20 = close.rolling(20).mean()
        ret = close.pct_change()
        vol60 = ret.rolling(60).std() * math.sqrt(252)
        vol250 = ret.rolling(250).std() * math.sqrt(252)
        high250 = close.rolling(250).max()

        b0 = ((ma60 > ma120) & (close > ma120)).values
        b1 = (ma120 > ma120.shift(21)).values
        b2 = (vol60 < 0.35).values
        dd = (1.0 - close / high250).values
        b3 = (dd < 0.40)
        b3 = b3.values if hasattr(b3, "values") else b3

        fwd = (close.shift(-hold) / close - 1.0).values
        idx = close.index
        feats = dict(
            ma60_120_gap=(ma60 / ma120 - 1.0).values,
            ma120_slope=(ma120 / ma120.shift(21) - 1.0).values,
            ext_ma120=(close / ma120 - 1.0).values,
            ext_ma20=(close / ma20 - 1.0).values,
            vol60=vol60.values,
            dd250=dd,
            mom20=(close / close.shift(20) - 1.0).values,
            mom60=(close / close.shift(60) - 1.0).values,
            mom120=(close / close.shift(120) - 1.0).values,
            vol_ratio=(vol60 / vol250).values,
        )
        for pos in range(300, len(idx) - hold, step):
            fr = fwd[pos]
            if fr is None or not math.isfinite(fr):
                continue
            mask = ((1 if b0[pos] else 0) | (2 if b1[pos] else 0)
                    | (4 if b2[pos] else 0) | (8 if b3[pos] else 0))
            rec = {"code": code, "date": idx[pos].strftime("%Y-%m-%d"),
                   "mask": mask, "fwd": float(fr) * 100}
            if not (rec["mask"] & 3):        # 两个主条件都不满足 → 不算趋势票
                pass                          # 仍记录（全市场基线需要）
            for k in FEAT_KEYS:
                v = feats[k][pos]
                rec[k] = float(v) if (v is not None and math.isfinite(v)) else None
            out.append(rec)
        if verbose and (i + 1) % 500 == 0:
            print(f"  ...已处理 {i+1}/{n} 只，累积 {len(out):,}", flush=True)
    return out


# ─────────────────────────────────────────────
# 2. 票池定义
# ─────────────────────────────────────────────

def score_of(mask):
    """与 scan_long_term 同构：bit0 +1.0, bit1 +1.0, bit2 +0.5, bit3 +0.5。"""
    return ((1.0 if mask & 1 else 0) + (1.0 if mask & 2 else 0)
            + (0.5 if mask & 4 else 0) + (0.5 if mask & 8 else 0))


def mask_of_score_and_bits(mask, keep_bits, threshold):
    """在 keep_bits 限制下重算得分，判断是否 >= threshold。

    keep_bits：保留哪些条件参与打分（用于 leave-one-out）。
    """
    s = 0.0
    if keep_bits & 1 and (mask & 1):
        s += 1.0
    if keep_bits & 2 and (mask & 2):
        s += 1.0
    if keep_bits & 4 and (mask & 4):
        s += 0.5
    if keep_bits & 8 and (mask & 8):
        s += 0.5
    return s >= threshold


# ─────────────────────────────────────────────
# 3. 票池 vs 全市场（分年超额）
# ─────────────────────────────────────────────

def pool_vs_market(rows, keep_bits=15, threshold=2.0, min_pool=20):
    """每个采样日：票池等权 fwd vs 全市场等权 fwd，返回按日差值序列。"""
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    diffs, pool_v, mkt_v = [], [], []
    for d in sorted(byday):
        rs = byday[d]
        pool = [r["fwd"] for r in rs
                if mask_of_score_and_bits(r["mask"], keep_bits, threshold)]
        if len(pool) < min_pool or len(rs) < 50:
            continue
        pm, mm = st.mean(pool), st.mean(r["fwd"] for r in rs)
        diffs.append(pm - mm)
        pool_v.append(pm)
        mkt_v.append(mm)
    return diffs, pool_v, mkt_v


def yearly(rows, pairs):
    """pairs: list of (date, value) —— 按年聚合。"""
    by = defaultdict(list)
    for d, v in pairs:
        by[d[:4]].append(v)
    return {y: (st.mean(v), len(v)) for y, v in sorted(by.items())}


# ─────────────────────────────────────────────
# 4. 报告
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--from-cache", default=None)
    args = ap.parse_args()

    if args.from_cache:
        with open(args.from_cache, "rb") as f:
            rows = pickle.load(f)
        print(f"从缓存载入 {len(rows):,} 条")
    else:
        print("=" * 100)
        print(f"long 票池重放   起点 {args.start}   step={args.step}   hold={args.hold}")
        print("=" * 100)
        df = load(args.start)
        print(f"载入 {len(df):,} 行 / {df['code'].nunique()} 只", flush=True)
        rows = build(df, step=args.step, hold=args.hold)
        print(f"重放 {len(rows):,} 条，覆盖 {len(set(r['date'] for r in rows))} 个采样日")
        if args.cache:
            with open(args.cache, "wb") as f:
                pickle.dump(rows, f)
            print(f"缓存写入 {args.cache}")

    # ── A. 票池 alpha：票池等权 vs 全市场等权 ──
    print()
    print("=" * 100)
    print("【A】票池 alpha —— score>=2.0 的票池 vs 全市场等权（同日配对，分年）")
    print("=" * 100)
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    pairs_all, pairs_pool, pairs_mkt = [], [], []
    for d in sorted(byday):
        rs = byday[d]
        pool = [r["fwd"] for r in rs
                if mask_of_score_and_bits(r["mask"], 15, 2.0)]
        if len(pool) < 20 or len(rs) < 50:
            continue
        pm, mm = st.mean(pool), st.mean(r["fwd"] for r in rs)
        pairs_all.append((d, pm - mm))
        pairs_pool.append((d, pm))
        pairs_mkt.append((d, mm))
    ya, yp, ym = yearly(rows, pairs_all), yearly(rows, pairs_pool), yearly(rows, pairs_mkt)
    print(f"{'年份':<8}{'采样日':>7}{'票池均值':>11}{'全市场':>11}{'超额':>10}{'日胜率':>9}")
    allp = defaultdict(list)
    for d, v in pairs_all:
        allp[d[:4]].append(v)
    for y in sorted(yp):
        pm, mm = yp[y][0], ym[y][0]
        wr = sum(1 for v in allp[y] if v > 0) / len(allp[y]) * 100
        print(f"{y:<8}{yp[y][1]:>7}{pm:>+10.2f}%{mm:>+10.2f}%{ya[y][0]:>+9.2f}%"
              f"{wr:>8.1f}%")
    dv = [v for _, v in pairs_all]
    print(f"{'全期':<8}{len(dv):>7}{st.mean([v for _, v in pairs_pool]):>+10.2f}%"
          f"{st.mean([v for _, v in pairs_mkt]):>+10.2f}%{st.mean(dv):>+9.2f}%"
          f"{sum(1 for v in dv if v > 0)/len(dv)*100:>8.1f}%")
    print(f"  票池日均占比 "
          f"{st.mean([len([r for r in byday[d] if mask_of_score_and_bits(r['mask'],15,2.0)])/len(byday[d]) for d in byday])*100:.1f}%")
    if dv:
        se = st.stdev(dv) / math.sqrt(len(dv))
        print(f"  超额 t = {st.mean(dv)/se:+.2f}（|t|>2 视为显著）")

    # ── B. 打分项 leave-one-out 归因 ──
    print()
    print("=" * 100)
    print("【B】打分项 leave-one-out —— 逐项剔除后，票池超额如何变化")
    print("   （keep=15 表示四项全保留；剔除 bitX 即 keep=15-X）")
    print("=" * 100)
    print(f"{'组合':<26}{'采样日':>7}{'票池均值':>11}{'超额':>10}"
          f"{'日胜率':>9}{'Δ超额':>10}")
    base_ex = st.mean(dv)
    for lbl, keep, thr in (
            ("四项全保留(现状)", 15, 2.0),
            ("剔除 趋势MA60>MA120", 14, 1.5),
            ("剔除 MA120斜率", 13, 1.5),
            ("剔除 低波<35%", 11, 2.0),
            ("剔除 回撤<40%", 7, 2.0),
            ("只保留 趋势+斜率", 3, 1.5),
            ("四项全满足(mask=15)", 15, 3.0),
    ):
        diffs, pv, mv = [], [], []
        for d in sorted(byday):
            rs = byday[d]
            pool = [r["fwd"] for r in rs
                    if mask_of_score_and_bits(r["mask"], keep, thr)]
            if len(pool) < 20 or len(rs) < 50:
                continue
            pm, mm = st.mean(pool), st.mean(r["fwd"] for r in rs)
            diffs.append(pm - mm); pv.append(pm); mv.append(mm)
        if not diffs:
            print(f"{lbl:<26} 样本不足")
            continue
        print(f"{lbl:<26}{len(diffs):>7}{st.mean(pv):>+10.2f}%"
              f"{st.mean(diffs):>+9.2f}%"
              f"{sum(1 for v in diffs if v>0)/len(diffs)*100:>8.1f}%"
              f"{st.mean(diffs)-base_ex:>+9.2f}%")

    # ── C. threshold 网格 ──
    print()
    print("=" * 100)
    print("【C】threshold 网格（收紧票池是否有用）")
    print("=" * 100)
    print(f"{'threshold':<12}{'采样日':>7}{'票池均值':>11}{'超额':>10}{'日胜率':>9}"
          f"{'票池日均规模':>13}")
    for thr in (2.0, 2.5, 3.0):
        diffs, pv, sizes = [], [], []
        for d in sorted(byday):
            rs = byday[d]
            pool = [r["fwd"] for r in rs
                    if mask_of_score_and_bits(r["mask"], 15, thr)]
            if len(pool) < 20 or len(rs) < 50:
                continue
            pm, mm = st.mean(pool), st.mean(r["fwd"] for r in rs)
            diffs.append(pm - mm); pv.append(pm); sizes.append(len(pool))
        if not diffs:
            print(f"{thr:<12.1f} 样本不足")
            continue
        print(f"{thr:<12.1f}{len(diffs):>7}{st.mean(pv):>+10.2f}%"
              f"{st.mean(diffs):>+9.2f}%"
              f"{sum(1 for v in diffs if v>0)/len(diffs)*100:>8.1f}%"
              f"{st.mean(sizes):>12.0f}只")


if __name__ == "__main__":
    main()
