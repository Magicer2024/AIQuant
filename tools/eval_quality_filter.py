"""
tools/eval_quality_filter.py —— 质量过滤边界敏感性扫描（只读，不写库）
========================================================================================
目标：回答"QUALITY_FILTER 的 min_amt20 / min_mktcap / max_mktcap 三个边界值
（8000万 / 30亿 / 800亿）是否有优化空间"。这三个值是人工经验值（commit a772130
引入，无回测依据，见 docs/short-reco-anti-chase-methods.md 5.x）。

方法：单变量扫描（每次只动一个参数，其余保持现网值），口径与
tools/eval_fusion_mode.py 完全对齐（过滤链=质量+趋势闸门+追高+主板；
收益=OC/CC；报告=全信号池 + 每日Top8；训/验分窗）。打分用库内 bottom_score×5
（= 现网纯抄底 fusion，与线上增量链路同源）。

注意：扫描天然有数据窥探风险，73 个验证交易日 × Top8 = 576 笔，
均值标准误约 ±0.15pp。只看"稳健的单调趋势/明显拐点"，不追单点最优。

用法：
    python tools/eval_quality_filter.py                    # 近1年，单变量扫描三参数
    python tools/eval_quality_filter.py --since 2024-02-01 # 长窗对照
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from tools.eval_fusion_mode import load_daily, add_features, load_index_regime, stat
from tools.eval_bottom_reentry import chase_mask
from core.db import get_market_cap_map
from config.strategy_params import (
    QUALITY_FILTER, DEFAULT_SIG_THRESHOLD, HIGH_VOL_THRESHOLD, HIGH_VOL_SIG_THRESHOLD,
)

pd.set_option("display.width", 220)

CUR = QUALITY_FILTER


def prep(df: pd.DataFrame) -> pd.DataFrame:
    """一次性算好扫描共用列：amt20 / mktcap（阈值组合只做布尔运算，极快）"""
    g = df.groupby("code", sort=False)
    df["amt20"] = g["amount"].transform(lambda s: s.rolling(20).mean())
    mktcap_map = get_market_cap_map()
    ts = df["code"].map(lambda c: (mktcap_map.get(c) or {}).get("total_shares"))
    df["_ts"] = ts
    df["mktcap"] = ts * df["close"]
    df["fs"] = pd.to_numeric(df.get("bottom_score"), errors="coerce").fillna(0).clip(0, 10) * 5.0
    return df


def qual_mask(df: pd.DataFrame, min_amt20, min_mktcap, max_mktcap) -> pd.Series:
    """与 rec_filters.quality_series 同口径；缺 total_shares 跳过市值项"""
    ok = df["amt20"] >= (min_amt20 or 0)
    has_ts = df["_ts"].notna() & (df["_ts"] > 0)
    mc_ok = pd.Series(True, index=df.index)
    if min_mktcap:
        mc_ok &= (df["mktcap"] >= min_mktcap)
    if max_mktcap:
        mc_ok &= (df["mktcap"] <= max_mktcap)
    ok &= (~has_ts) | mc_ok
    return ok.fillna(False)


def eval_group(df: pd.DataFrame, base: pd.Series, label: str) -> dict:
    """返回验证窗（后 30%）的：池子行数、全池 OC、Top8 OC"""
    dates = sorted(df["trade_date"].unique())
    split = dates[int(len(dates) * 0.7)]
    vmask = df["trade_date"] >= split
    vdf = df[vmask]
    vdays = vdf["trade_date"].nunique()
    pool = vdf[base[vmask] & (vdf["fs"] >= vdf["threshold"])]
    top = (pool.sort_values("fs", ascending=False)
               .groupby("trade_date", sort=False).head(8))
    s_pool = stat(pool, vdays)
    s_top = stat(top, vdays)
    if s_pool is None or s_top is None:
        return None
    return {
        "label": label,
        "pool_n": s_pool["n"], "pool_oc_w": s_pool["win_oc"], "pool_oc_a": s_pool["avg_oc"],
        "top_n": s_top["n"], "top_oc_w": s_top["win_oc"], "top_oc_a": s_top["avg_oc"],
    }


def scan(df: pd.DataFrame, base_fixed: pd.Series, param: str, values: list, cur_val) -> None:
    """单变量扫描：param 取 values 列表，其余取现网值"""
    print(f"\n{'='*110}\n  扫描参数: {param}   （现网值 = {cur_val}）")
    print(f"  {'参数值':>14} | {'池子行数':>8} {'全池OC胜率':>9} {'全池OC均值':>10} | "
          f"{'Top8笔数':>8} {'Top8OC胜率':>9} {'Top8OC均值':>10}")
    print("-" * 110)
    rows = []
    for v in values:
        kw = {"min_amt20": CUR["min_amt20"], "min_mktcap": CUR["min_mktcap"],
              "max_mktcap": CUR["max_mktcap"]}
        kw[param] = v
        qual = qual_mask(df, kw["min_amt20"], kw["min_mktcap"], kw["max_mktcap"])
        base = base_fixed & qual
        r = eval_group(df, base, str(v))
        if r is None:
            print(f"  {str(v):>14} | —— 无样本 ——")
            continue
        rows.append(r)
        mark = "  ← 现网" if v == cur_val or (param == "max_mktcap" and v == CUR["max_mktcap"]) else ""
        print(f"  {str(v):>14} | {r['pool_n']:>8} {r['pool_oc_w']:>8.2f}% {r['pool_oc_a']:>+9.3f}% | "
              f"{r['top_n']:>8} {r['top_oc_w']:>8.2f}% {r['top_oc_a']:>+9.3f}%{mark}")
    if rows:
        best = max(rows, key=lambda r: r["top_oc_a"])
        print(f"  → Top8 OC均值最优: {best['label']} "
              f"(胜率 {best['top_oc_w']:.2f}% / 均值 {best['top_oc_a']:+.3f}%)")


def main():
    ap = argparse.ArgumentParser(description="质量过滤边界敏感性扫描（只读）")
    ap.add_argument("--since", default="2025-08-01")
    args = ap.parse_args()

    print(f"[1/3] 加载 daily_price since={args.since} ...")
    df = load_daily(args.since)
    if df.empty:
        print("[ERR] 无数据"); return
    print(f"      {len(df)} 行 / {df['code'].nunique()} 只 / {df['trade_date'].nunique()} 个交易日")

    print("[2/3] 过滤链与扫描列 ...")
    df = add_features(df)
    df = prep(df)
    df["chase"] = chase_mask(df)
    reg = load_index_regime()
    if reg.empty:
        df["threshold"] = DEFAULT_SIG_THRESHOLD
    else:
        df = df.merge(reg, on="trade_date", how="left")
        df["threshold"] = df["threshold"].fillna(DEFAULT_SIG_THRESHOLD)

    dates = sorted(df["trade_date"].unique())
    split = dates[int(len(dates) * 0.7)]
    print(f"      训练窗 {dates[0]}~{split} / 验证窗 {split}~{dates[-1]}")

    # 固定部分（与质量过滤无关）：趋势闸门 + 追高 + 主板
    base_fixed = df["gate"] & df["chase"] & df["main_board"]
    print(f"      固定过滤保留 {int(base_fixed.sum())}/{len(df)} 行")

    print("[3/3] 单变量扫描（验证窗口径）...")
    print("      现网 QUALITY_FILTER =", {k: CUR[k] for k in ("min_amt20", "min_mktcap", "max_mktcap")})

    scan(df, base_fixed, "min_amt20",
         [0, 30_000_000, 50_000_000, 80_000_000, 150_000_000, 300_000_000, 500_000_000],
         CUR["min_amt20"])
    scan(df, base_fixed, "min_mktcap",
         [0, 1_000_000_000, 2_000_000_000, 3_000_000_000, 5_000_000_000, 8_000_000_000, 12_000_000_000],
         CUR["min_mktcap"])
    scan(df, base_fixed, "max_mktcap",
         [20_000_000_000, 40_000_000_000, 80_000_000_000, 150_000_000_000, 300_000_000_000, None],
         CUR["max_mktcap"])

    print("\n  说明：验证窗 Top8≈576 笔，均值标准误约 ±0.15pp；"
          "多组比较有数据窥探风险，结论只看单调趋势，不追单点最优。")


if __name__ == "__main__":
    main()
