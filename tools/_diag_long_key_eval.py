"""long 排序键稳健性检验 + 组合键评估（消费 _diag_long_rescore.py 的缓存）。

为什么单独一个脚本
------------------
long 重放一次 ~10 分钟（62 万信号 / 1000 万行 daily_price）。把信号缓存成 pickle
后，所有后续分析（分年、组合键、剔除检验）都是秒级，避免每次重复重放。

主判据（按项目铁律）
--------------------
- 铁律 3：**分年压力测试**——只看全期均值会被某一年绑架，必须逐年都成立。
- 铁律 6：桶内增量 Δ 才是判据，不是绝对收益（组合口径用「vs 随机基线」的 Δ）。
- 铁律 7：组合口径（每日 top-N）才是最终判据，不是池子平均。

用法
----
    python tools/_diag_long_key_eval.py [--cache logs/_long_rescore_cache.pkl] [--topn 4]
"""

import argparse
import os
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "_long_rescore_cache.pkl")


def load(cache):
    import pickle
    with open(cache, "rb") as f:
        return pickle.load(f)


def group_by_day(rows):
    d = defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    return d


def pick_curve(byday, pick_fn, topn):
    """返回 [(date, 当日 top-n 等权收益)]。"""
    out = []
    for d in sorted(byday):
        rs = byday[d]
        if len(rs) < topn:
            continue
        out.append((d, st.mean(x["fwd"] for x in pick_fn(rs))))
    return out


def rand_curve(byday, topn, seed=42, nrep=30):
    import random
    rng = random.Random(seed)
    acc = defaultdict(list)
    for _ in range(nrep):
        for d in sorted(byday):
            rs = byday[d]
            if len(rs) < topn:
                continue
            acc[d].append(st.mean(x["fwd"] for x in rng.sample(rs, topn)))
    return {d: st.mean(v) for d, v in acc.items()}


def summarize(curve, base=None):
    v = [x[1] for x in curve]
    if not v:
        return None
    s = dict(n=len(v), mean=st.mean(v), median=st.median(v),
             win=sum(1 for x in v if x > 0) / len(v) * 100)
    if base:
        diffs = [x[1] - base.get(x[0], 0) for x in curve]
        s["delta"] = st.mean(diffs)
        s["win_rate_vs_base"] = sum(1 for x in diffs if x > 0) / len(diffs) * 100
    return s


def year_split(curve):
    d = defaultdict(list)
    for date, v in curve:
        d[date[:4]].append(v)
    return d


def _composite_rank(w_vol, w_ext, topn=4):
    """合成键：当日横截面百分位排名加权求和（升序取 topn）。

    ⚠ 为什么需要它：两层结构（先按 vol_ratio 取池、再按 ext_ma20 取 topN）在 SQL
    里要两层窗口函数，而**字典序** `ORDER BY vol_ratio ASC, ext_ma20 ASC` 会退化成
    纯 vol_ratio（vol_ratio 是连续值，同分几乎不存在）⇒ 拿不到两层结构的稳健性。
    合成百分位排名是**单个连续键**，SQL 里一次 ORDER BY 就能实现，且天然等价于
    「两个条件都靠前」的折中。
    """
    def fn(rs):
        n = len(rs)
        by_vol = sorted(rs, key=lambda x: x["vol_ratio"])
        by_ext = sorted(rs, key=lambda x: x["ext_ma20"])
        rv = {id(x): i / max(1, n - 1) for i, x in enumerate(by_vol)}
        re_ = {id(x): i / max(1, n - 1) for i, x in enumerate(by_ext)}
        return sorted(rs, key=lambda x: w_vol * rv[id(x)] + w_ext * re_[id(x)])[:topn]
    return fn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=DB_CACHE)
    ap.add_argument("--topn", type=int, default=4)
    args = ap.parse_args()

    rows = load(args.cache)
    byday = group_by_day(rows)
    print(f"信号 {len(rows):,} 条 / {len(byday)} 个采样日   top_n={args.topn}")

    base = rand_curve(byday, args.topn)
    base_curve = sorted(base.items())
    bs = summarize(base_curve)
    print(f"随机基线：均值 {bs['mean']:+.2f}%  中位 {bs['median']:+.2f}%  "
          f"胜率 {bs['win']:.1f}%   n={bs['n']}")

    def key_fn(key, desc=False):
        return lambda rs: sorted(rs, key=lambda x: x[key],
                                 reverse=desc)[:args.topn]

    candidates = [
        ("vol_ratio ASC",  key_fn("vol_ratio", desc=False)),
        ("vol60 ASC",      key_fn("vol60", desc=False)),
        ("mom120 ASC",     key_fn("mom120", desc=False)),
        ("dd250 DESC",     key_fn("dd250", desc=True)),
        ("ma120_slope DESC", key_fn("ma120_slope", desc=True)),
        # 组合键：波动收敛 + 长期反转（先按 vol_ratio 升序取 3 倍池，再按 mom120 升序）
        ("vol_ratio×mom120", lambda rs: sorted(
            sorted(rs, key=lambda x: x["vol_ratio"])[:args.topn * 5],
            key=lambda x: x["mom120"])[:args.topn]),
        # 波动收敛 + 深度回撤（回撤大的优先）
        ("vol_ratio×dd250", lambda rs: sorted(
            sorted(rs, key=lambda x: x["vol_ratio"])[:args.topn * 5],
            key=lambda x: x["dd250"], reverse=True)[:args.topn]),
        # 波动收敛 + 不追高（ext_ma20 低的优先）
        ("vol_ratio×ext20", lambda rs: sorted(
            sorted(rs, key=lambda x: x["vol_ratio"])[:args.topn * 5],
            key=lambda x: x["ext_ma20"])[:args.topn]),
        # 同上但池子更宽（10×），看池宽敏感性
        ("vol_ratio×ext20(10x)", lambda rs: sorted(
            sorted(rs, key=lambda x: x["vol_ratio"])[:args.topn * 10],
            key=lambda x: x["ext_ma20"])[:args.topn]),
        # 同上但池子更窄（2×）
        ("vol_ratio×ext20(2x)", lambda rs: sorted(
            sorted(rs, key=lambda x: x["vol_ratio"])[:args.topn * 2],
            key=lambda x: x["ext_ma20"])[:args.topn]),
        # 合成键：横截面百分位排名求和（可写成单个连续键 ⇒ SQL 字典序友好）
        ("合成rank(7:3)", _composite_rank(0.7, 0.3, args.topn)),
        ("合成rank(5:5)", _composite_rank(0.5, 0.5, args.topn)),
        ("合成rank(3:7)", _composite_rank(0.3, 0.7, args.topn)),
    ]

    print()
    print("=" * 100)
    print("【全期组合口径】vs 随机基线")
    print("=" * 100)
    print(f"{'排序键':<20}{'均值':>9}{'中位':>9}{'胜率':>8}{'Δ均值':>9}{'跑赢基线的天数占比':>12}")
    curves = {}
    for name, fn in candidates:
        c = pick_curve(byday, fn, args.topn)
        curves[name] = c
        s = summarize(c, base)
        print(f"{name:<20}{s['mean']:>+8.2f}%{s['median']:>+8.2f}%{s['win']:>7.1f}%"
              f"{s['delta']:>+8.2f}%{s['win_rate_vs_base']:>11.1f}%")

    # ── 分年压力测试（铁律 3）──────────────────────
    print()
    print("=" * 100)
    print("【分年压力测试】逐年 Δ（该键当日收益 − 随机基线当日收益）的均值")
    print("  ⚠ 只看全期会被某一年绑架：必须**每一年** Δ 都为正才算稳健")
    print("=" * 100)
    years = sorted({d[:4] for d in byday})
    base_by_year = year_split(base_curve)
    print(f"{'排序键':<20}" + "".join(f"{y:>8}" for y in years) + f"{'最差年':>10}")
    for name, _fn in candidates:
        cy = year_split(curves[name])
        cells, worst = [], None
        for y in years:
            if y not in cy or y not in base_by_year:
                cells.append(f"{'--':>8}")
                continue
            # 按日期对齐后求差
            bd = {d: v for d, v in curves[name] if d[:4] == y}
            diffs = [v - base.get(d, 0) for d, v in bd.items()]
            m = st.mean(diffs)
            cells.append(f"{m:>+7.2f}%")
            worst = m if worst is None else min(worst, m)
        print(f"{name:<20}" + "".join(cells) +
              (f"{worst:>+9.2f}%" if worst is not None else f"{'--':>10}"))

    # ── 剔除单年（leave-one-year-out）─────────────
    print()
    print("=" * 100)
    print("【剔除单年】逐个剔除某年后，剩余年份的 Δ 均值（看是否被单一年份绑架）")
    print("=" * 100)
    for name, _fn in candidates:
        allv = []
        per = {}
        for d, v in curves[name]:
            per.setdefault(d[:4], []).append(v - base.get(d, 0))
        for y in years:
            rest = [x for yy, vs in per.items() if yy != y for x in vs]
            if rest:
                allv.append((y, st.mean(rest)))
        if not allv:
            continue
        lo = min(allv, key=lambda t: t[1])
        hi = max(allv, key=lambda t: t[1])
        print(f"{name:<20} 剔除后 Δ 区间 [{lo[1]:+.2f}% ~ {hi[1]:+.2f}%]  "
              f"最差=剔除{lo[0]}  最好=剔除{hi[0]}  "
              f"{'✅ 全正' if lo[1] > 0 else '⚠ 有负值'}")


if __name__ == "__main__":
    main()
