"""long 选股：历史重放 + 排序键挖掘（解决「排序键只有 3 个离散值」问题）。

背景（2026-09-19 确证）
----------------------
strategy/mid_long.py::scan_long_term 的 fusion_score = score/3*50，而 score 只能取
2.0 / 2.5 / 3.0 ⇒ 排序键只有 3 个离散值（33.33 / 41.67 / 50.0）。实测每日 long
票池中位 372 只，**与第 4 名同分的票平均 28.2 只** ⇒ `ORDER BY fusion_score DESC
LIMIT 4` 等价于从 28 只满分票里按 rowid 随机抽 4 只，排序键**零信息量**。

更糟的是样本：long 信号 2026-07-17 才有，到 09-18 仅 46 个交易日，而长线持有期
60+ 交易日 ⇒ **没有任何一笔走完过完整周期**，46 天数据根本不足以评估长线选股。

方案
----
用 daily_price（1990 至今、4511 只）**重放** scan_long_term 的打分逻辑（纯 close
可算，无未来函数），在跨牛熊的历史区间上：
  1. 复现当前排序键（3 档 fusion）对 T+60 收益的区分度；
  2. 挖掘**连续**排序键：对每个候选特征做五分位分桶，看 T+60 收益单调性；
  3. 对比「随机取 4 只」与「最优排序键取 4 只」的组合口径表现。

用法
----
    python tools/_diag_long_rescore.py --start 2015-01-01 [--step 5] [--hold 60]

输出：特征分桶表 + 组合口径对比。主判据是**桶间单调性 + 桶内增量 Δ**，
不是绝对收益（见项目铁律 6）。
"""

import argparse
import math
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")


# ─────────────────────────────────────────────
# 1. 重放 scan_long_term 打分（与 strategy/mid_long.py 逐行对齐）
# ─────────────────────────────────────────────

def score_long(close):
    """返回与 scan_long_term 同构的 (score, feats) 序列。close: pd.Series。

    打分项（满分 3.0，>=2.0 命中）：
      1) MA60 > MA120 且 close > MA120                    +1.0
      2) MA120 斜率向上（> 21 个交易日前）                 +1.0
      3) 60 日年化波动 < 35%                              +0.5
      4) 距 250 日高点回撤 < 40%                          +0.5
    """
    import pandas as pd
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    ma20 = close.rolling(20).mean()
    ret = close.pct_change()
    vol60 = ret.rolling(60).std() * math.sqrt(252)
    high250 = close.rolling(250).max()

    s1 = ((ma60 > ma120) & (close > ma120)).astype(float) * 1.0
    s2 = (ma120 > ma120.shift(21)).astype(float) * 1.0
    s3 = (vol60 < 0.35).astype(float) * 0.5
    dd = 1.0 - close / high250
    s4 = (dd < 0.40).astype(float) * 0.5
    score = (s1 + s2 + s3 + s4).fillna(0.0)

    feats = dict(
        ma60_120_gap=(ma60 / ma120 - 1.0),        # 中期均线与长期均线间距
        ma120_slope=(ma120 / ma120.shift(21) - 1.0),
        ext_ma120=(close / ma120 - 1.0),          # 相对 MA120 扩展度
        ext_ma20=(close / ma20 - 1.0),
        vol60=vol60,
        dd250=dd,
        mom20=(close / close.shift(20) - 1.0),
        mom60=(close / close.shift(60) - 1.0),
        mom120=(close / close.shift(120) - 1.0),
        vol_ratio=(vol60 / (ret.rolling(250).std() * math.sqrt(252))),
    )
    return score, feats


# ─────────────────────────────────────────────
# 2. 数据装载
# ─────────────────────────────────────────────

def load(start_date, end_date=None, min_rows=300):
    import pandas as pd
    conn = sqlite3.connect(DB)
    q = """SELECT code, trade_date, close FROM daily_price
           WHERE trade_date >= ? AND close IS NOT NULL AND close > 0
           ORDER BY code, trade_date ASC"""
    df = pd.read_sql_query(q, conn, params=(start_date,))
    conn.close()
    df["close"] = df["close"].astype("float32")
    return df


def build_signals(df, step=5, hold=60, score_threshold=2.0, verbose=True):
    """对每只股票逐只计算，在采样日落地 (date, code, score, feats, fwd_ret)。"""
    import pandas as pd
    out = []
    # ⚠ 性能：绝不能用 df[df["code"]==code] 逐只过滤——4511 只 × 全表扫描 ≈
    # 450 亿次比较（初版 16 分钟未跑完）。必须 groupby 一次切分。
    n = df["code"].nunique()
    for i, (code, g) in enumerate(df.groupby("code", sort=False)):
        if len(g) < 300:
            continue
        close = pd.Series(g["close"].values,
                          index=pd.DatetimeIndex(g["trade_date"].values))
        score, feats = score_long(close)
        # 前向收益：hold 个交易日后的收盘
        fwd = close.shift(-hold) / close - 1.0
        idx = close.index
        # 采样：每 step 个交易日取一次（避免逐日重叠导致的伪样本膨胀）
        for pos in range(300, len(idx) - hold, step):
            sc = score.iloc[pos]
            if sc < score_threshold:
                continue
            fr = fwd.iloc[pos]
            if fr is None or not math.isfinite(fr):
                continue
            d = idx[pos]
            rec = dict(code=code, date=d.strftime("%Y-%m-%d"),
                       score=float(sc), fwd=float(fr) * 100)
            ok = True
            for k, v in feats.items():
                x = v.iloc[pos]
                if x is None or not math.isfinite(x):
                    ok = False
                    break
                rec[k] = float(x)
            if ok:
                out.append(rec)
        if verbose and (i + 1) % 500 == 0:
            print(f"  ...已处理 {i+1}/{n} 只，累积信号 {len(out)}",
                  flush=True)
    return out


# ─────────────────────────────────────────────
# 3. 分桶分析
# ─────────────────────────────────────────────

def bucket_report(rows, keys, nbin=5):
    print()
    print("=" * 104)
    print(f"候选排序键分桶分析（{nbin} 分位，按信号日在全市场内横截面分桶）"
          f"   样本 {len(rows)}")
    print("  单调性 = 桶序与收益的 Spearman 相关；Δ = 最高桶 − 最低桶")
    print("=" * 104)
    # 先按日期分组，做横截面分位（避免时间趋势污染）
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    for key in keys:
        vals = defaultdict(list)
        for d, rs in byday.items():
            if len(rs) < nbin * 2:
                continue
            rs2 = sorted(rs, key=lambda x: x[key])
            m = len(rs2)
            for b in range(nbin):
                lo, hi = int(b * m / nbin), int((b + 1) * m / nbin)
                for x in rs2[lo:hi]:
                    vals[b].append(x["fwd"])
        if not vals or len(vals) < nbin:
            print(f"{key:<16} 样本不足")
            continue
        means = [st.mean(vals[b]) for b in range(nbin)]
        # Spearman（桶序 vs 桶均值序）
        ranks = list(range(nbin))
        mr = sorted(range(nbin), key=lambda i: means[i])
        rank_of = {b: k for k, b in enumerate(mr)}
        sp = spearman(ranks, [rank_of[b] for b in range(nbin)])
        delta = means[-1] - means[0]
        cells = "  ".join(f"{m:+6.2f}" for m in means)
        print(f"{key:<16} {cells}   Δ={delta:+6.2f}%  rho={sp:+.2f}"
              f"   {'✅单调' if abs(sp) >= 0.8 and abs(delta) > 0.5 else '  —'}")


def spearman(a, b):
    n = len(a)
    if n < 3:
        return 0.0
    ra = rank(a)
    rb = rank(b)
    ma, mb = st.mean(ra), st.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def rank(a):
    order = sorted(range(len(a)), key=lambda i: a[i])
    r = [0] * len(a)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def combo_report(rows, key, topn=4, hold=60, baseline=None):
    """组合口径：每个采样日按 key 取 top-n，等权持有 hold 日。

    ⚠ 必须**双向**测（升序 + 降序）：分桶表只给单调方向，直接固定降序取 topN
    会把方向搞反（初版 vol60 降序 top4 +6.29% 看着很好，但分桶明明是低波更好）。
    另同时报**中位数与胜率**——长线收益右偏，均值会被少数暴涨票拉高，
    只报均值会选中「靠右尾吃饭」的高波动组合。
    """
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    import random
    random.seed(42)

    def curve(pick):
        out = []
        for d in sorted(byday):
            rs = byday[d]
            if len(rs) < topn:
                continue
            out.append(st.mean(x["fwd"] for x in pick(rs)))
        return out

    asc = curve(lambda rs: sorted(rs, key=lambda x: x[key])[:topn])
    desc = curve(lambda rs: sorted(rs, key=lambda x: x[key], reverse=True)[:topn])
    if not asc:
        print("  组合口径样本不足")
        return

    def fmt(v):
        return (f"均值 {st.mean(v):+6.2f}%  中位 {st.median(v):+6.2f}%  "
                f"胜率 {sum(1 for x in v if x > 0)/len(v)*100:5.1f}%")

    d_asc = st.mean(asc) - baseline[0]
    d_desc = st.mean(desc) - baseline[0]
    print(f"{key:<14} ASC  {fmt(asc)}  Δ均值={d_asc:+6.2f}%")
    print(f"{'':<14} DESC {fmt(desc)}  Δ均值={d_desc:+6.2f}%")


def baseline_curve(rows, topn=4, nrep=20):
    """随机取 top-n 的基线（多次重复取均值，降低抽样噪声），返回 (均值,中位,胜率)。"""
    import random
    random.seed(42)
    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    means, meds, wins = [], [], []
    for _ in range(nrep):
        v = []
        for d in sorted(byday):
            rs = byday[d]
            if len(rs) < topn:
                continue
            v.append(st.mean(x["fwd"] for x in random.sample(rs, topn)))
        means.append(st.mean(v))
        meds.append(st.median(v))
        wins.append(sum(1 for x in v if x > 0) / len(v) * 100)
    return st.mean(means), st.mean(meds), st.mean(wins)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--step", type=int, default=5,
                    help="采样间隔（交易日），避免逐日重叠")
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--topn", type=int, default=4)
    ap.add_argument("--cache", default=None,
                    help="把重放出的信号缓存到 pickle，供后续分析秒级复用")
    ap.add_argument("--from-cache", default=None,
                    help="从已有 pickle 直接加载信号，跳过重放")
    args = ap.parse_args()

    print("=" * 104)
    print(f"long 选股历史重放   起点 {args.start}   采样间隔 {args.step} 日   "
          f"持有 {args.hold} 交易日")
    print("=" * 104)
    if args.from_cache:
        import pickle
        with open(args.from_cache, "rb") as f:
            rows = pickle.load(f)
        print(f"从缓存载入 {len(rows):,} 条信号")
    else:
        df = load(args.start)
        print(f"载入 {len(df):,} 行 / {df['code'].nunique()} 只")
        rows = build_signals(df, step=args.step, hold=args.hold)
    print(f"重放出 {len(rows):,} 个 long 信号，"
          f"覆盖 {len(set(r['date'] for r in rows))} 个采样日")

    # 当前排序键的区分度
    print()
    print("=" * 104)
    print("【现状诊断】当前排序键 fusion_score 只有 3 档，逐档看 T+%d 收益" % args.hold)
    print("=" * 104)
    byscore = defaultdict(list)
    for r in rows:
        byscore[round(r["score"], 2)].append(r["fwd"])
    for k in sorted(byscore):
        v = byscore[k]
        print(f"  score={k:<5} fusion={k/3*50:5.2f}  n={len(v):>7}  "
              f"均值 {st.mean(v):+6.2f}%  胜率 "
              f"{sum(1 for x in v if x > 0)/len(v)*100:5.1f}%")
    print(f"  ⇒ 满分档（3.0）占 {len(byscore.get(3.0, []))/len(rows)*100:.1f}%，"
          f"top4 在这批同分票里等价随机")

    keys = ["ma60_120_gap", "ma120_slope", "ext_ma120", "ext_ma20", "vol60",
            "dd250", "mom20", "mom60", "mom120", "vol_ratio"]
    bucket_report(rows, keys)

    print()
    print("=" * 104)
    print(f"【组合口径】每日按该键取 top{args.topn} 等权持有 {args.hold} 日"
          f"（最终判据，见项目铁律 7）｜ASC=取最小 / DESC=取最大｜基线=随机抽签")
    print("=" * 104)
    base = baseline_curve(rows, topn=args.topn)
    print(f"{'随机基线':<14}      均值 {base[0]:+6.2f}%  中位 {base[1]:+6.2f}%  "
          f"胜率 {base[2]:5.1f}%")
    print("-" * 104)
    for key in keys:
        combo_report(rows, key, topn=args.topn, hold=args.hold, baseline=base)

    if args.cache:
        import pickle
        with open(args.cache, "wb") as f:
            pickle.dump(rows, f)
        print(f"\n信号缓存已写入 {args.cache}（{len(rows)} 条）")


if __name__ == "__main__":
    main()
