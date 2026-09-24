"""探针：筹码优先排序 vs 基线排序，**选票重叠度**分布（只读）。

为什么必须先做这个
------------------
影子模式要跑 3~4 周才有结论。若两种排序在候选池上**系统性选出同一批票**
（重叠 100%），那么前向 Δ 恒为 0 —— 不是"因子无效"，而是"实验没做"，
等于白等一个月。故在启动前先用历史池做重叠度体检。

方法
----
对最近 N 个交易日，取 stock_signal 的 short 记录、套上与线上近似的过滤
（fusion 门槛 / ST 剔除 / 板块限制 / 观察线阈值），用**回测缓存**里的 conc
（诊断用途，不追求与生产 1500 行 warmup 逐位一致），分别算出：

    基线 top3 = ORDER BY ext ASC,  fusion DESC
    影子 top3 = ORDER BY conc ASC, ext ASC, fusion DESC

再统计重叠数与"被换掉的票"。

用法
----
    python tests/_probe_chip_shadow_overlap.py
    python tests/_probe_chip_shadow_overlap.py --days 60 --top 3
"""
from __future__ import annotations

import argparse
import io
import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import get_conn                    # noqa: E402
from config.strategy_params import get_param    # noqa: E402

OUT = os.path.join(ROOT, "logs", "_chip_shadow_overlap.txt")
CACHE = os.path.join(ROOT, "logs", "_chip_factors_d0.65f0.003.pkl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回看交易日数")
    ap.add_argument("--top", type=int, default=3, help="每日名额（short_top_n）")
    args = ap.parse_args()

    buf = io.StringIO()

    def w(s=""):
        print(s)
        buf.write(s + "\n")

    w("=" * 76)
    w(f"筹码优先 vs 基线 · 选票重叠度体检（近 {args.days} 交易日，top{args.top}）")
    w("=" * 76)

    if not os.path.exists(CACHE):
        w(f"!! 缺缓存 {CACHE}（先跑 tools/eval_chip_factor.py）")
        io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        return
    with open(CACHE, "rb") as f:
        chip = pickle.load(f)
    if isinstance(chip, pd.DataFrame):
        conc = chip["conc"]
    else:
        conc = chip
    w(f"筹码缓存 {os.path.basename(CACHE)}：{len(conc):,} 行（诊断口径，非生产 1500 行 warmup）")

    gate = float(get_param("short_conf_gate"))
    min_ext = float(get_param("short_observe_bottom_min_ext") or 0)

    with get_conn() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal "
            "WHERE COALESCE(horizon,'short')='short' "
            "ORDER BY scan_date DESC LIMIT ?", (args.days,)).fetchall()]
        if not dates:
            w("!! 无 short 记录")
            io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
            return
        d0 = min(dates)
        df = pd.read_sql_query(f"""
            SELECT scan_date, code, name, strategy, fusion_score,
                   COALESCE(pct_above_ma20, 0) AS ext
            FROM stock_signal
            WHERE COALESCE(horizon,'short')='short' AND scan_date >= ?
              AND buy_price IS NOT NULL AND buy_price > 0
              AND name NOT LIKE '%ST%' AND name NOT LIKE '%退%'
              AND COALESCE(strategy,'') != '强势突破'
              AND COALESCE(strategy,'') != '缩量回踩'
              AND code NOT LIKE '30%' AND code NOT LIKE '68%'
              AND fusion_score >= ?
              AND (COALESCE(strategy,'') != '短线融合' OR
                   COALESCE(pct_above_ma20, 0) >= ?)
        """, conn, params=(d0, gate, min_ext))

    w(f"过滤后候选池 {len(df):,} 条 / {df['scan_date'].nunique()} 日")
    df["conc"] = [conc.get((c, d), np.nan)
                  for c, d in zip(df["code"], df["scan_date"])]
    miss = df["conc"].isna().mean() * 100
    w(f"conc 缺失率 {miss:.2f}%")
    df = df.dropna(subset=["conc"])
    if df.empty:
        w("!! 池内无 conc")
        io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        return

    rows = []
    for d, g in df.groupby("scan_date"):
        if len(g) < args.top:
            continue
        base = g.sort_values(["ext", "fusion_score"],
                             ascending=[True, False]).head(args.top)
        chipg = g.sort_values(["conc", "ext", "fusion_score"],
                              ascending=[True, True, False]).head(args.top)
        bs, cs = set(base["code"]), set(chipg["code"])
        rows.append({
            "date": d, "n_pool": len(g),
            "overlap": len(bs & cs), "n": len(cs),
            "swapped_in": sorted(cs - bs), "swapped_out": sorted(bs - cs),
            "conc_base": base["conc"].mean(), "conc_chip": chipg["conc"].mean(),
            "ext_base": base["ext"].mean(), "ext_chip": chipg["ext"].mean(),
            "pool_conc_std": g["conc"].std(),
        })
    r = pd.DataFrame(rows)
    if r.empty:
        w("!! 无可评估交易日")
        io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        return

    w("")
    w("【重叠度分布】每日两种排序选出的票有多少重合")
    w(f"{'重叠数':>8} {'天数':>7} {'占比':>8}   含义")
    for ov in sorted(r["overlap"].unique(), reverse=True):
        n = int((r["overlap"] == ov).sum())
        note = ("完全同票 → 该日实验无信息" if ov == args.top else
                "完全换票" if ov == 0 else "部分换票")
        w(f"{ov:>8} {n:>7} {n/len(r)*100:>7.1f}%   {note}")

    full = float((r["overlap"] == args.top).mean() * 100)
    zero = float((r["overlap"] == 0).mean() * 100)
    avg_ov = float(r["overlap"].mean())

    w("")
    w("【关键读数】")
    w(f"  平均每日常与基线重合 {avg_ov:.2f}/{args.top} 只")
    w(f"  完全同票的交易日占比 **{full:.1f}%**")
    w(f"  完全换票的交易日占比 **{zero:.1f}%**")
    w(f"  每日候选池中位 {int(r['n_pool'].median())} 只 → "
      f"top{args.top} 只占池子 {args.top/max(r['n_pool'].median(),1)*100:.1f}%")

    w("")
    w("【池内区分度】若池内 conc 几乎没有差异，排序自然换不动票")
    w(f"  池内 conc 标准差中位 {r['pool_conc_std'].median():.4f}"
      f"（conc 量级 0.1~0.5）")
    w(f"  基线组 conc 均值中位 {r['conc_base'].median():.4f} / "
      f"筹码组 {r['conc_chip'].median():.4f}"
      f"  → 改善 {r['conc_base'].median()-r['conc_chip'].median():+.4f}")
    w(f"  基线组 ext 均值中位 {r['ext_base'].median():.4f} / "
      f"筹码组 {r['ext_chip'].median():.4f}")

    w("")
    w("【判定】")
    if full >= 90:
        verdict = ("⚠ 系统性同票 ⇒ 该池上筹码排序几乎没有选择权，"
                   "前向记录**大概率测不出差异**。应先查候选池构成（名额是否已被"
                   "独立信号线占满 / 过滤链是否把池子压得极窄），"
                   "而不是等 3~4 周。")
    elif full >= 50:
        verdict = ("△ 过半交易日同票 ⇒ 有效对照天数偏少，"
                   "建议延长观察期或放宽候选池后再评估。")
    else:
        verdict = ("✅ 多数交易日发生换票 ⇒ 两种排序在池上有真实选择权，"
                   "前向记录能产生有效对照。")
    w(f"  {verdict}")

    w("")
    w("【样例】最近 10 个交易日的换票情况")
    w(f"{'date':>12} {'池':>5} {'重合':>5} {'换入':>28} {'换出':>28}")
    for _, x in r.head(10).iterrows():
        w(f"{x['date']:>12} {x['n_pool']:>5} {x['overlap']:>5} "
          f"{','.join(x['swapped_in'])[:28]:>28} {','.join(x['swapped_out'])[:28]:>28}")

    io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
    print(f"\n[写出] {OUT}")


if __name__ == "__main__":
    main()
