"""long 选股——**16 种条件组合（mask）逐一归因**，定位负 alpha 的确切来源。

上游结论（tools/_diag_long_pool.py）
------------------------------------
- score>=2.0 的票池，长期超额 **-0.92%，t=-7.30**（11 年里 8 年为负）⇒ 负 alpha 确证
- 但 threshold=3.0（四项全满足）超额 **+0.53%**、日胜率 59.9%，票池 66 只/日
- leave-one-out：剔除任一单项 Δ 仅 ±0.13pp ⇒ 价值不在单项，在组合方式

⇒ 必须把 16 种 mask 拆开看，才知道哪些组合该留、哪些该剔除。
    bit0=趋势(MA60>MA120且站上MA120) bit1=MA120斜率↑ bit2=低波<35% bit3=回撤<40%

用法
----
    python tools/_diag_long_mask.py [--cache logs/_long_pool_cache.pkl]
"""

import argparse
import math
import os
import pickle
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "_long_pool_cache.pkl")

BIT_NAME = {1: "趋势", 2: "斜率", 4: "低波", 8: "回撤"}


def mask_label(m):
    return "".join(BIT_NAME[b] for b in (8, 4, 2, 1) if m & b) or "(无)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--min-pool", type=int, default=20,
                    help="票池少于该只数的采样日不计入（避免小样本噪声）")
    args = ap.parse_args()

    with open(args.cache, "rb") as f:
        rows = pickle.load(f)
    print(f"载入 {len(rows):,} 条，覆盖 {len(set(r['date'] for r in rows))} 个采样日")

    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    days = sorted(byday)

    # 每个采样日的全市场等权
    mkt = {d: st.mean(r["fwd"] for r in byday[d]) for d in days}

    print()
    print("=" * 108)
    print("【D】16 种条件组合（mask）逐一归因 —— 相对全市场等权的超额")
    print("   n日 = 票池 >= min_pool 的采样日数；可用率 = 票池 >= 4 只的天数占比")
    print("=" * 108)
    print(f"{'mask':<6}{'组合':<18}{'n日':>6}{'可用率':>8}{'日均只数':>9}"
          f"{'票池均值':>10}{'超额':>9}{'日胜率':>8}{'超额t':>8}")

    results = []
    for m in range(16):
        diffs, pool_v, sizes = [], [], []
        n_ok4 = 0
        for d in days:
            rs = byday[d]
            pool = [r["fwd"] for r in rs if r["mask"] == m]
            sizes.append(len(pool))
            if len(pool) >= 4:
                n_ok4 += 1
            if len(pool) < args.min_pool:
                continue
            pm = st.mean(pool)
            diffs.append(pm - mkt[d])
            pool_v.append(pm)
        if len(diffs) < 30:
            print(f"{m:<6}{mask_label(m):<18}{len(diffs):>6}  样本不足")
            continue
        mu = st.mean(diffs)
        se = st.stdev(diffs) / math.sqrt(len(diffs))
        t = mu / se if se else 0.0
        results.append((m, mu, t, len(diffs), st.mean(sizes), n_ok4 / len(days) * 100))
        print(f"{m:<6}{mask_label(m):<18}{len(diffs):>6}{n_ok4/len(days)*100:>7.1f}%"
              f"{st.mean(sizes):>9.0f}{st.mean(pool_v):>+9.2f}%{mu:>+8.2f}%"
              f"{sum(1 for v in diffs if v>0)/len(diffs)*100:>7.1f}%{t:>+8.2f}")

    print()
    print("=" * 108)
    print("按超额排序（只列超额 t 显著的）")
    print("=" * 108)
    for m, mu, t, nd, sz, ok in sorted(results, key=lambda x: -x[1]):
        flag = "✅" if t > 2 else ("⚠" if t < -2 else "  ")
        print(f"{flag} mask={m:<3}{mask_label(m):<18} 超额 {mu:+6.2f}%  t={t:+6.2f}  "
              f"日均 {sz:>5.0f} 只  可用率 {ok:5.1f}%")

    # 现在推荐的组合方式：哪些 mask 之和构成「正 alpha 且规模够用」的票池
    print()
    print("=" * 108)
    print("【E】候选票池定义对比（按 score 门槛 vs 按 mask 白名单）")
    print("=" * 108)
    cands = {
        "现状 score>=2.0": lambda m: ((1 if m & 1 else 0) + (1 if m & 2 else 0)
                                      + (0.5 if m & 4 else 0) + (0.5 if m & 8 else 0)) >= 2.0,
        "score>=2.5": lambda m: ((1 if m & 1 else 0) + (1 if m & 2 else 0)
                                 + (0.5 if m & 4 else 0) + (0.5 if m & 8 else 0)) >= 2.5,
        "四项全满足(mask=15)": lambda m: m == 15,
        "趋势+斜率+低波(7)": lambda m: (m & 7) == 7,
        "趋势+斜率+回撤(11)": lambda m: (m & 11) == 11,
        "趋势+斜率(3)": lambda m: (m & 3) == 3,
        "去低波去回撤后各项": lambda m: m in (15, 7, 11, 3),
    }
    print(f"{'票池定义':<24}{'n日':>6}{'可用率':>8}{'日均只数':>9}"
          f"{'票池均值':>10}{'超额':>9}{'日胜率':>8}{'超额t':>8}")
    for lbl, fn in cands.items():
        diffs, pool_v, sizes = [], [], []
        n_ok4 = 0
        for d in days:
            rs = byday[d]
            pool = [r["fwd"] for r in rs if fn(r["mask"])]
            sizes.append(len(pool))
            if len(pool) >= 4:
                n_ok4 += 1
            if len(pool) < args.min_pool:
                continue
            pm = st.mean(pool)
            diffs.append(pm - mkt[d])
            pool_v.append(pm)
        if len(diffs) < 30:
            print(f"{lbl:<24} 样本不足")
            continue
        mu = st.mean(diffs)
        se = st.stdev(diffs) / math.sqrt(len(diffs))
        t = mu / se if se else 0.0
        flag = "✅" if t > 2 else ("⚠" if t < -2 else "  ")
        print(f"{flag}{lbl:<22}{len(diffs):>6}{n_ok4/len(days)*100:>7.1f}%"
              f"{st.mean(sizes):>9.0f}{st.mean(pool_v):>+9.2f}%{mu:>+8.2f}%"
              f"{sum(1 for v in diffs if v>0)/len(diffs)*100:>7.1f}%{t:>+8.2f}")


if __name__ == "__main__":
    main()
