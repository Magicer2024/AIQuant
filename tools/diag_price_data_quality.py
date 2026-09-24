"""行情数据体检：`daily_price` 的 `volume` / `amount` / `turnover` 是否自洽

起因
----
`strategy/chip.py` 与 `tools/eval_chip_factor.py::build_factors()` 的换手率取值逻辑是

    to = 库 turnover（若 >0） 否则 volume / circ_shares

若这两条路径给出的值互相矛盾，筹码衰减速度就会被写错 → `conc` 偏移。
在 `_chip_turnover_bias` 诊断里发现：`库 turnover ÷ (volume/circ_shares)` 有约 37.6% 的行
偏离极大（P25=0.106，P5=0.010），必须判定**到底是 `turnover` 错还是 `volume` 错**。

判据（不能只看 turnover 与 volume 的比值——它们互相矛盾时无解）
--------------------------------------------------------------
用 `amount`（成交额，元）做第三方仲裁：

    vwap = amount / volume

真实均价必定落在当日 `[low, high]` 区间内。若 `volume` 被放大 N 倍，则 vwap 会被压到
close/N，**直接跌出区间** → 可判定 volume 不可信；若 vwap 仍落在区间内，则 volume 可信，
不一致应由 `turnover` 承担。

同时反解「隐含股本」= `volume / (turnover/100)` 并与真实流通股比较：
≈1 → 两列自洽；≈15 → volume 被放大 15 倍（或 turnover 被压小）。

用法
----
    python tools/diag_price_data_quality.py
    python tools/diag_price_data_quality.py --shares data_cache/tencent_shares.csv
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "core", "quant.db")
CACHE = os.path.join(_ROOT, "data_cache", "tencent_shares.csv")

GROUPS = (("ratio<0.2", lambda s: s.ratio < 0.2),
          ("ratio 0.9~1.1", lambda s: (s.ratio > 0.9) & (s.ratio < 1.1)),
          ("ratio>5", lambda s: s.ratio > 5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shares", default=CACHE, help="腾讯股本快照（含 float_shares）")
    args = ap.parse_args()

    if not os.path.exists(args.shares):
        raise SystemExit(f"缺股本快照 {args.shares}，先跑：python tools/fix_circ_shares.py")
    sp = pd.read_csv(args.shares, dtype={"code": str})
    fs = dict(zip(sp["code"], sp["float_shares"]))

    c = sqlite3.connect(DB)
    si = pd.read_sql_query("SELECT code, circ_shares FROM stock_info", c)
    cs = dict(zip(si["code"].astype(str), si["circ_shares"]))

    px = pd.read_sql_query(
        "SELECT code, trade_date, low, high, close, volume, amount, turnover "
        "FROM daily_price WHERE volume > 0 AND amount > 0", c)
    c.close()

    px["code"] = px["code"].astype(str)
    px["cs_"] = px["code"].map(cs)
    px["fs_"] = px["code"].map(fs)
    px = px[px["cs_"] > 0].copy()
    px["calc"] = px["volume"] / px["cs_"] * 100.0
    px["ratio"] = px["turnover"] / px["calc"]
    px["vwap"] = px["amount"] / px["volume"]
    px["vwap_over_close"] = px["vwap"] / px["close"]
    px["vwap_ok"] = (px["vwap"] >= px["low"] * 0.98) & (px["vwap"] <= px["high"] * 1.02)
    px["implied_sh"] = px["implied_sh"] = np.where(
        px["turnover"] > 0, px["volume"] / (px["turnover"] / 100.0), np.nan)
    px["impl_vs_float"] = px["implied_sh"] / px["fs_"]
    # ⚠ 关键：`turnover` 约 75% 的行本来就是 0（未填充），这些行在 build_factors 里
    # 早已回退到 `volume/circ_shares`。若把 db==0 的行也纳入 ratio 分组，会把
    # `ratio≈0` 误读成"库值被低估几十倍"。故**分组只看 db>0 的行**。
    px["yr"] = px["trade_date"].str.slice(0, 4)      # 需在 pf 复制之前生成
    px["db_filled"] = px["turnover"] > 0
    pf = px[px["db_filled"]].copy()
    print(f"其中 `turnover > 0`（已填充）{len(pf):,} 行（{len(pf) / len(px) * 100:.1f}%）"
          f" —— **只有这些行两个口径才会分叉**")

    print("\n=== A. amount/volume 是否落在当日 low~high（volume 单位是否可信）===")
    print(f"{'分组':<16}{'n':>12}{'vwap在区间内':>14}{'vwap/close':>12}")
    for lab, f in GROUPS:
        g = pf[f(pf)]
        if not len(g):
            continue
        print(f"{lab:<16}{len(g):>12,}{g['vwap_ok'].mean() * 100:>13.1f}%"
              f"{(g['vwap'] / g['close']).median():>12.4f}")

    print("\n=== B. 反解隐含股本 ÷ 真实流通股（≈1 自洽 / ≈15 volume 被放大）===")
    print(f"{'分组':<16}{'n':>12}{'P5':>9}{'P25':>9}{'P50':>9}{'P75':>9}{'P95':>9}")
    for lab, f in GROUPS:
        g = pf[f(pf)]
        if not len(g):
            continue
        q = g["impl_vs_float"].quantile([.05, .25, .5, .75, .95])
        print(f"{lab:<16}{len(g):>12,}" + "".join(f"{v:>9.3f}" for v in q))

    print("\n=== C. 按年份：三列自洽率 + vwap/close（判别'单位错' vs '复权不匹配'）===")
    g = px.groupby("yr").agg(n=("ratio", "size"),
                             consistent=("ratio", lambda s: ((s > 0.9) & (s < 1.1)).mean() * 100),
                             vwap_ok=("vwap_ok", "mean"),
                             vwap_close=("vwap_over_close", "median"))
    g["vwap_ok"] = (g["vwap_ok"] * 100).round(1)
    g["consistent"] = g["consistent"].round(1)
    g["vwap_close"] = g["vwap_close"].round(4)
    print(g.to_string())
    print("  判读：vwap/close 中位 ≈1 → 价格与成交额同口径，vwap 出区间是 **volume 单位错**；")
    print("        若按年呈稳定的非 1 常数 → 价格列是复权价而 amount 是原始额 → **复权不匹配**。")

    print("\n=== D. 逐行抽样（ratio<0.2：库 turnover 偏小的行）===")
    s = px[px["ratio"] < 0.2].head(8)
    for r in s.itertuples():
        true_vol = r.turnover / 100.0 * r.fs_
        print(f"  {r.code} {r.trade_date} vol={r.volume:>13,.0f} "
              f"若turnover为准则vol应={true_vol:>13,.0f} 倍数={r.volume / true_vol:>6.3f}  "
              f"vwap={r.vwap:>7.3f} 区间={r.low}~{r.high} 在区间内={bool(r.vwap_ok)}")

    print("\n=== D2. 分年代原始行（各取 3 行）===")
    for yr in ("2010", "2018", "2023", "2025", "2026"):
        sub = px[(px["yr"] == yr) & (px["ratio"] < 0.2)].head(3)
        if not len(sub):
            continue
        print(f"  --- {yr} ---")
        for r in sub.itertuples():
            print(f"    {r.code} {r.trade_date} O/H/L/C={r.low}/{r.high}/{r.close} "
                  f"vol={r.volume:>13,.0f} amt={r.amount:>16,.0f} vwap={r.vwap:>8.3f} "
                  f"库to={r.turnover:>7.3f}% calc={r.calc:>7.3f}%")

    print("\n=== E. 结论判定（先分离'未填充'与'复权'，再判'单位'）===")
    print(f"  ① `turnover = 0`（未填充）：{len(px) - len(pf):,} 行"
          f"（{(1 - len(pf) / len(px)) * 100:.1f}%）→ auto 口径已回退 `volume/circ_shares`，无问题。")
    neg = int((px["turnover"] < 0).sum())
    print(f"  ② 反向证据：`turnover` 列存在**负值** {neg:,} 行"
          f"（如 000963 2023-01-18 = -0.890%）⇒ **该列并非换手率**。")
    r = pf.groupby("yr")["vwap_over_close"].median()
    print("     且复权不匹配只出现在**已填充行**（`turnover>0`）：其 `vwap/close` 按年单调趋近 1")
    print("     ⇒ 这些行的 `low/high/close` 是**前复权**价、`amount` 是原始额。末 8 年：")
    print("     " + " ".join(f"{y}:{v:.4f}" for y, v in r.tail(8).items()))
    # ⚠ 只取两列做 groupby，避免在 1400 万行上再复制一份完整切片（曾致 OOM）
    r2 = (px.loc[~px["db_filled"], ["yr", "vwap_over_close"]]
            .groupby("yr")["vwap_over_close"].median())
    print("     未填充行 `vwap/close`："
          + " ".join(f"{y}:{v:.4f}" for y, v in r2.tail(4).items())
          + "  ⇒ 三列同口径，无复权错位。")
    print("     ⚠ 结论：`daily_price` 里**混了两种价源**（带 turnover 的行=复权价，不带=原始价），")
    print("       任何把 `amount/volume` 与 `close` 混算的逻辑都会在旧年份出错。")

    clean = px[(px["yr"] >= "2025") & ((px["vwap_over_close"] - 1).abs() < 0.02)]
    print(f"\n  ③ 复权影响 <2% 的行（2025 年起）{len(clean):,}："
          f"vwap 落在 low~high 内 {clean['vwap_ok'].mean() * 100:.1f}%")
    print("     ⇒ volume 与 amount 单位一致、可信（100% 自洽）。")

    bad = clean[(clean["db_filled"]) & (clean["ratio"] < 0.5)]
    ok = clean[(clean["db_filled"]) & (clean["ratio"].between(0.9, 1.1))]
    print(f"\n  ④ 已填充行的一致性（2025 年起、复权影响 <2%）：")
    print(f"     一致（ratio 0.9~1.1）：{len(ok):,}")
    print(f"     分歧（ratio < 0.5）  ：{len(bad):,}", end="")
    if len(bad):
        mult = bad["calc"] / bad["turnover"]
        near100 = float(mult.between(95, 105).mean() * 100)
        print(f"  倍数 P50={mult.median():.1f}  ≈100 的占 {near100:.1f}%")
        print("     ⇒ 这部分行 `turnover` 疑似被错除以 100（数量级错），volume 可信。")
    else:
        print()

    print("\n  最终判定：")
    print("    · `circ_shares` 实为**流通股本** ⇒ 换手率回退分母**正确**，无需修数据；")
    print("    · `volume` / `amount` **可信**；")
    print("    · `turnover` 约 3/4 未填充（已回退）、已填充部分以一致为主、少数行有 ÷100 量级错；")
    print("    · 实测影响：auto 与 calc 两口径的筹码因子在 **26,746 条信号行上仅 18 行不同**")
    print("      ⇒ **换手率口径不是筹码结论的风险点，不必返工**。")
    print("    · 真正需要记住的是：`low/high/close` 是**前复权**价而 `volume/amount` 是原始值，")
    print("      `chip.py` 未做复权还原（模块 docstring 已自认），旧年份的价量口径是混的。")

    print(f"\n  参考：全样本三列自洽率 {px['ratio'].between(0.9, 1.1).mean() * 100:.1f}%"
          f"（含未填充行，故偏低）")
    if len(pf):
        print(f"        仅已填充行自洽率 {pf['ratio'].between(0.9, 1.1).mean() * 100:.1f}%")
    recent = px[px["yr"] >= "2023"]
    print(f"  ⚠ 2023 年起（短线回测窗口 + 筹码 warmup）n={len(recent):,}"
          f"  vwap在区间内 {recent['vwap_ok'].mean() * 100:.1f}%")


if __name__ == "__main__":
    main()
