"""换手率口径缺陷对筹码峰结论的影响 —— `circ_shares == total_shares` 的证伪检验

问题来源
--------
`stock_info.total_shares` 与 `circ_shares` 在库中 4419/4419 **完全相等**（比值恒 1.000）
→ 库里没有真正的流通股本。而 `strategy/chip.py::turnover_series()` 在 `daily_price.turnover`
为 0/空（约 68%）时回退 `volume / circ_shares`，于是那 68% 的换手率被**系统性低估**
（分母用了总股本，大于真实流通股）。而换手率直接决定筹码衰减速度 → 直接影响 `conc`。

本工具要回答的唯一问题：
    **两轮筹码峰结论（conc 排序有效 / <300亿 更有效）是不是这个缺陷造出来的？**

三条独立判据
------------
A. 偏差量化：`volume/total_shares` 与库里真实 `turnover` 的相对误差（只在两者都 >0 处比）
B. 子样本复现：只用**真实 turnover**（非回退）的行重跑 2×2 分解，看 Δ 是否仍在
C. 因子漂移：回退行 vs 真实行 的 `conc` 分布是否系统性错位

⚠ 若 B 的样本按月份严重集中，则该子样本有偏，结论只能当"辅助证据"而非"定论"——
   故本工具先输出 fill rate 的月度分布，让人自己判断能不能用。

用法
----
    python tools/eval_chip_turnover_bias.py            # decay=0.65
    python tools/eval_chip_turnover_bias.py --decay 0.35
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.db import get_conn                                                    # noqa: E402

LOGS = os.path.join(_ROOT, "logs")
CAP_CUT = 300.0


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt(v, s="{:.2f}"):
    return s.format(v) if v is not None and np.isfinite(v) else "--"


def _delta(H, EF, g: pd.DataFrame, top_n: int, lb: str):
    """返回 (基线, 筹码优先, Δ, 基线止损, 筹码止损)。"""
    a = EF._portfolio_by(H, g, "", ["_p", "_f"], [True, False], top_n, lb + "-base")
    b = EF._portfolio_by(H, g, "", ["conc", "_p", "_f"], [True, True, False], top_n, lb + "-chip")
    if a["avg_ret"] is None or b["avg_ret"] is None:
        return None, None, None, None, None
    return (a["avg_ret"], b["avg_ret"], b["avg_ret"] - a["avg_ret"],
            a["stop_rate"], b["stop_rate"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decay", type=float, default=0.65)
    ap.add_argument("--decay-floor", type=float, default=None)
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--is-end", default="2025-06-30")
    args = ap.parse_args()

    EM = _load("_em", os.path.join(_ROOT, "tools", "eval_chip_mktcap.py"))
    df_floor = args.decay_floor if args.decay_floor is not None else 0.003
    H, EF, ev = EM.load_data(args.decay, df_floor)
    ev = EM._sort_keys(ev)

    # ── 取 scan_date 当日真实价量 ──
    with get_conn() as conn:
        px = pd.read_sql_query(
            "SELECT code, trade_date, volume, turnover, close FROM daily_price "
            "WHERE trade_date >= ?", conn, params=(ev["scan_date"].min(),))
    k = pd.MultiIndex.from_arrays([px["code"], px["trade_date"]])
    for c in ("volume", "turnover", "close"):
        ev["_db_" + c] = pd.Series(px[c].to_numpy(), index=k).reindex(
            pd.MultiIndex.from_arrays([ev["code"], ev["scan_date"]])).to_numpy()
    del px

    # 回退口径：volume / total_shares × 100（库中 turnover 是百分数）
    ev["_calc_to"] = ev["_db_volume"] / ev["_ts"] * 100.0
    ev["_db_to"] = pd.to_numeric(ev["_db_turnover"], errors="coerce")
    ev["_filled"] = ev["_db_to"].fillna(0) > 0

    print("=" * 84)
    print("§A 换手率偏差量化（仅在两口径都 >0 处比较）")
    print("=" * 84)
    cmp_ = ev[ev["_filled"] & (ev["_calc_to"] > 0)].copy()
    cmp_["_ratio"] = cmp_["_db_to"] / cmp_["_calc_to"]
    print(f"可比较样本 {len(cmp_):,} / {len(ev):,}（{len(cmp_) / len(ev) * 100:.1f}%）")
    if len(cmp_):
        q = cmp_["_ratio"].quantile([.05, .25, .5, .75, .95])
        print("  库中真实 turnover ÷ (volume/总股本×100) 分位："
              f"P5={q.iloc[0]:.3f} P25={q.iloc[1]:.3f} P50={q.iloc[2]:.3f} "
              f"P75={q.iloc[3]:.3f} P95={q.iloc[4]:.3f}")
        print(f"  → 中位比值 {q.iloc[2]:.3f}：>1 表示真实流通股 < 总股本，"
              f"回退口径把换手率压低了 {(1 - 1 / q.iloc[2]) * 100:.0f}%")
        print(f"  比值 >1.05 的占比 {(cmp_['_ratio'] > 1.05).mean() * 100:.1f}%"
              f"  >1.5 的占比 {(cmp_['_ratio'] > 1.5).mean() * 100:.1f}%")

    print("\n" + "=" * 84)
    print("§B 真实 turnover 填充率分布（决定子样本能不能用）")
    print("=" * 84)
    ev["_ym"] = ev["scan_date"].str.slice(0, 7)
    m = ev.groupby("_ym").agg(n=("_filled", "size"), filled=("_filled", "sum"))
    m["rate"] = (m["filled"] / m["n"] * 100).round(1)
    print("按月：")
    line = []
    for ym, r in m.iterrows():
        line.append(f"{ym}:{r['rate']:.0f}%")
    for i in range(0, len(line), 9):
        print("   " + "  ".join(line[i:i + 9]))
    print(f"\n总填充率 {ev['_filled'].mean() * 100:.1f}%"
          f"（{int(ev['_filled'].sum()):,} / {len(ev):,}）")
    b = ev.groupby(pd.cut(ev["mktcap"], [0, 100, 300, 1000, 1e9],
                          labels=["<100", "100-300", "300-1000", ">1000"]),
                   observed=True)["_filled"].agg(["size", "mean"])
    print("按市值桶：")
    for lb, r in b.iterrows():
        print(f"   {lb:<12} n={int(r['size']):>6}  填充率 {r['mean'] * 100:>5.1f}%")

    print("\n" + "=" * 84)
    print("§C 因子漂移：回退行 vs 真实行 的 conc 是否系统性错位")
    print("=" * 84)
    for c in ("conc", "pressure", "cost_ratio", "lock", "winner"):
        v = pd.to_numeric(ev[c], errors="coerce")
        a, bb = v[ev["_filled"]], v[~ev["_filled"]]
        if a.notna().sum() < 100 or bb.notna().sum() < 100:
            continue
        print(f"  {c:<12} 真实行中位 {a.median():>7.4f}  回退行中位 {bb.median():>7.4f}"
              f"  差 {bb.median() - a.median():>+7.4f}")

    print("\n" + "=" * 84)
    print("§D 子样本复现：只用真实 turnover 的行重跑 2×2 分解")
    print("=" * 84)
    is_end = args.is_end
    di = ev[ev["scan_date"] <= is_end]
    do = ev[ev["scan_date"] > is_end]
    print(f"{'子样本':<14}{'市值桶':<12}{'n':>7}{'基线':>9}{'筹码优先':>10}"
          f"{'Δ':>8}{'基线止损':>10}{'筹码止损':>10}")
    print("-" * 84)
    for sname, mask in (("全部（现口径）", ev["mktcap"] > 0),
                        ("仅真实turnover", ev["_filled"])):
        g_all = ev[mask]
        for cname, cmask in ((f"<{CAP_CUT:.0f}亿", g_all["mktcap"] < CAP_CUT),
                             (f">={CAP_CUT:.0f}亿", g_all["mktcap"] >= CAP_CUT)):
            g = g_all[cmask]
            if len(g) < 200:
                print(f"{sname:<14}{cname:<12}{len(g):>7}  样本不足，跳过")
                continue
            a, b_, d, sa, sb = _delta(H, EF, g, args.top_n, sname + cname)
            print(f"{sname:<14}{cname:<12}{len(g):>7}{_fmt(a, '{:+.2f}%'):>9}"
                  f"{_fmt(b_, '{:+.2f}%'):>10}{_fmt(d, '{:+.2f}%'):>8}"
                  f"{_fmt(sa, '{:.1f}%'):>10}{_fmt(sb, '{:.1f}%'):>10}")

    print("\n" + "=" * 84)
    print("§E 子样本的样本内外（仅 <300亿 桶）")
    print("=" * 84)
    print(f"{'子样本':<16}{'窗口':<10}{'n':>7}{'基线':>9}{'筹码优先':>10}{'Δ':>8}")
    print("-" * 84)
    for sname, mask in (("全部（现口径）", ev["mktcap"] > 0),
                        ("仅真实turnover", ev["_filled"])):
        for wname, wmask in (("全期", None),
                             (f"内(<= {is_end})", "i"),
                             (f"外(> {is_end})", "o")):
            g = ev[mask & (ev["mktcap"] < CAP_CUT)]
            if wmask == "i":
                g = g[g["scan_date"] <= is_end]
            elif wmask == "o":
                g = g[g["scan_date"] > is_end]
            if len(g) < 150:
                print(f"{sname:<16}{wname:<10}{len(g):>7}  样本不足")
                continue
            a, b_, d, _, _ = _delta(H, EF, g, args.top_n, sname + wname)
            print(f"{sname:<16}{wname:<10}{len(g):>7}{_fmt(a, '{:+.2f}%'):>9}"
                  f"{_fmt(b_, '{:+.2f}%'):>10}{_fmt(d, '{:+.2f}%'):>8}")

    print("\n" + "=" * 84)
    print("§F 回退行自身的换手率是否可信（volume 单位错排查）")
    print("=" * 84)
    print("§B 已证填充率按月/按市值桶大致均匀 → 两组可比。若回退组的『计算换手率』")
    print("分布与真实组严重错位（尤其出现 >50% 的堆），说明 volume 列存在单位错")
    print("（如部分源给的是『手』），回退换手率被放大 100 倍 → 筹码记忆塌缩成一日。")
    for gname, gmask in (("真实组（用库 turnover）", ev["_filled"]),
                         ("回退组（volume/总股本）", ~ev["_filled"])):
        v = pd.to_numeric(ev.loc[gmask, "_calc_to"], errors="coerce").dropna()
        dbt = pd.to_numeric(ev.loc[gmask, "_db_to"], errors="coerce")
        if len(v) == 0:
            continue
        q = v.quantile([.05, .25, .5, .75, .95])
        print(f"\n  {gname}  n={len(v):,}")
        print(f"    计算换手率(%)  P5={q.iloc[0]:.3f} P25={q.iloc[1]:.3f} "
              f"P50={q.iloc[2]:.3f} P75={q.iloc[3]:.3f} P95={q.iloc[4]:.3f}")
        print(f"    库 turnover(%) 中位 {dbt[dbt > 0].median() if (dbt > 0).any() else float('nan'):.3f}")
        for thr in (10, 30, 50, 80):
            print(f"    计算换手率 > {thr:>3}%  占比 {(v > thr).mean() * 100:>6.2f}%")
    a = pd.to_numeric(ev.loc[ev["_filled"], "_calc_to"], errors="coerce").dropna()
    bb = pd.to_numeric(ev.loc[~ev["_filled"], "_calc_to"], errors="coerce").dropna()
    if len(a) > 100 and len(bb) > 100:
        print(f"\n  → 回退组/真实组 计算换手率中位比 = "
              f"{bb.median() / a.median():.3f}（接近 1 = 无单位错；≈100 = 单位错）")

    print("\n" + "=" * 84)
    print("判读指引")
    print("=" * 84)
    print("  · §A 比值中位接近 1.0 → 缺陷实际影响很小（多数标的已全流通），结论无需返工")
    print("  · §B 填充率按月均匀 → §D/§E 的子样本可作为**独立复现证据**")
    print("  · §D/§E 中「仅真实turnover」的 Δ 仍为正且量级相近 → 缺陷不是结论的地基")
    print("  · 若 Δ 转负/消失 → 必须先修流通股本数据，全部筹码结论作废重跑")


if __name__ == "__main__":
    main()
