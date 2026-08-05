"""
tools/analyze_confirm_gap.py —— 只读研究脚本（不写库）
================================================================
回答：「建议买入价 vs 突破确认价」价差与次日买入胜率的关系。

口径（与 routes/investor.py 的 /api/investor/today 完全一致）：
  - confirm_trigger = MAX(high) WHERE trade_date >= scan_date（推荐日以来最高价）× 1.002
  - buy_price = 信号日收盘价（写库值，即卡片上的「建议买入价」）
  - gap_pct = (confirm_trigger - buy_price) / buy_price * 100

回放口径：
  - T+1：推荐日次日开盘买入 → 当日收盘卖出（nd_oc，A 股现实可交易口径）
  - 高开/低开：T+1 开盘 vs 推荐日收盘（buy_price）
  - 突破确认路径：从推荐日次日起扫描，首个「收盘 > 此前最高价×1.002」日为确认日 X，
    X+1 开盘买入 → X+1 收盘卖出；确认窗口 = 推荐日后 10 个交易日，超时记「未确认」

用法：python tools/analyze_confirm_gap.py [--since 2024-08-01]
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.db import get_conn

pd.set_option("display.width", 200)


def load(since: str):
    with get_conn() as conn:
        sig = pd.read_sql_query(
            """
            SELECT code, scan_date, buy_price, strategy FROM stock_signal
            WHERE horizon='short' AND buy_price IS NOT NULL AND buy_price > 0
              AND scan_date >= ? AND strategy != '隔日动量'
            """,
            conn, params=[since],
        )
        px = pd.read_sql_query(
            """
            SELECT code, trade_date, open, high, low, close FROM daily_price
            WHERE trade_date >= ?
            ORDER BY code, trade_date
            """,
            conn, params=[since],
        )
    px = px.sort_values(["code", "trade_date"]).reset_index(drop=True)
    by_code = {c: g.reset_index(drop=True) for c, g in px.groupby("code", sort=False)}
    return sig, by_code


def analyze(sig: pd.DataFrame, by_code: dict, win: int = 10):
    """逐信号回放：价差、T+1 表现、突破确认路径表现"""
    rows = []
    for _, s in sig.iterrows():
        df = by_code.get(s["code"])
        if df is None:
            continue
        dates = df["trade_date"].to_numpy()
        pos = int(np.searchsorted(dates, s["scan_date"]))
        if pos >= len(dates) or dates[pos] != s["scan_date"]:
            continue
        bp = float(s["buy_price"])
        # 推荐日以来（含推荐日）最高价 ×1.002 —— 与后端 confirm_trigger 同口径
        seg = df.iloc[pos:]
        confirm_trigger = float(seg["high"].max()) * 1.002
        gap_pct = (confirm_trigger - bp) / bp * 100.0

        after = df.iloc[pos + 1: pos + 1 + win]  # 推荐日之后最多 win 个交易日
        if len(after) < 1:
            continue
        t1 = after.iloc[0]
        t1_oc = (float(t1["close"]) - float(t1["open"])) / float(t1["open"]) * 100.0
        t1_gap = (float(t1["open"]) - bp) / bp * 100.0  # 高开>0 / 低开<0

        # 突破确认路径：首个收盘 > 此前最高价×1.002 的交易日（含推荐日高点起步）
        running_high = float(seg.iloc[0]["high"])
        confirm_i = None
        for i in range(1, len(after) + 1):
            row = after.iloc[i - 1]
            if float(row["close"]) > running_high * 1.002:
                confirm_i = i - 1  # 在 after 中的位置
                break
            running_high = max(running_high, float(row["high"]))
        confirm_oc = None
        if confirm_i is not None and confirm_i + 1 < len(after):
            nx = after.iloc[confirm_i + 1]
            confirm_oc = (float(nx["close"]) - float(nx["open"])) / float(nx["open"]) * 100.0

        rows.append({
            "code": s["code"], "scan_date": s["scan_date"], "strategy": s["strategy"],
            "buy_price": bp, "confirm_trigger": round(confirm_trigger, 2),
            "gap_pct": gap_pct, "t1_oc": t1_oc, "t1_gap": t1_gap,
            "confirmed": confirm_i is not None, "confirm_oc": confirm_oc,
        })
    return pd.DataFrame(rows)


def stat_block(label: str, sub: pd.DataFrame, col: str = "t1_oc", min_n: int = 50):
    if len(sub) < min_n:
        print(f"  {label:<34} 样本不足({len(sub)}<{min_n})")
        return None
    w = (sub[col] > 0).mean() * 100
    print(f"  {label:<34} n={len(sub):6d}  胜率 {w:5.1f}%  均值 {sub[col].mean():+6.2f}%  "
          f"中位 {sub[col].median():+6.2f}%")
    return (len(sub), w, sub[col].mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-08-01")
    args = ap.parse_args()

    print(f"加载数据 since={args.since} ...")
    sig, by_code = load(args.since)
    print(f"信号 {len(sig)} 条\n回放中 ...")
    r = analyze(sig, by_code)
    print(f"有效样本 {len(r)} 条（要求 scan_date 在行情表中且有次日行情）\n")

    # ── 1. 价差分布 ──
    print("=" * 74)
    print("1) 建议买入价 → 突破确认价 的价差分布（gap_pct = 确认价相对买入价上浮 %）")
    print("=" * 74)
    bins = [-1e9, 0.5, 1, 2, 3, 5, 1e9]
    labels = ["≤0.5%", "0.5~1%", "1~2%", "2~3%", "3~5%", ">5%"]
    r["gap_bin"] = pd.cut(r["gap_pct"], bins=bins, labels=labels, right=True)
    dist = r.groupby("gap_bin", observed=True).size()
    for lb in labels:
        print(f"  价差 {lb:<8} n={int(dist.get(lb, 0)):6d}  ({dist.get(lb, 0) / len(r) * 100:5.1f}%)")

    # ── 2. T+1 开盘买入当天胜率（按价差分档）──
    print("\n" + "=" * 74)
    print("2) 次日(T+1)开盘买入→当日收盘，按价差分档（用户核心问题）")
    print("=" * 74)
    stat_block("全部样本", r, min_n=0)
    for lb in labels:
        stat_block(f"价差 {lb}", r[r["gap_bin"] == lb], min_n=30)

    # ── 3. 高开 / 低开 子分组 ──
    print("\n" + "=" * 74)
    print("3) 高开 vs 低开（T+1 开盘相对推荐日收盘），按价差分档")
    print("=" * 74)
    for side, mask in [("全部", r["t1_gap"] != 0),
                       ("高开(open>收盘)", r["t1_gap"] > 0),
                       ("低开(open<收盘)", r["t1_gap"] < 0)]:
        print(f"  -- {side} --")
        stat_block("  T+1当天(全部)", r[mask], min_n=30)
        for lb in labels:
            stat_block(f"  价差 {lb}", r[mask & (r["gap_bin"] == lb)], min_n=30)

    # ── 4. 突破确认路径（等收盘站上确认线再买）──
    print("\n" + "=" * 74)
    print(f"4) 突破确认路径（10个交易日内收盘站上推荐日以来最高价×1.002，X+1开盘买入→X+1收盘）")
    print("=" * 74)
    conf = r[r["confirm_oc"].notna()]
    stat_block("全部样本 T+1当天（对照）", r, col="t1_oc", min_n=0)
    stat_block("已确认样本 T+1当天（对照）", conf, col="t1_oc", min_n=0)
    stat_block("已确认样本 确认后隔日", conf, col="confirm_oc", min_n=0)
    print(f"  确认率: {len(conf) / len(r) * 100:.1f}%（{len(conf)}/{len(r)}）")
    for lb in labels:
        sub = conf[conf["gap_bin"] == lb]
        stat_block(f"价差 {lb} 确认后隔日", sub, col="confirm_oc", min_n=30)
    # 确认后隔日 vs 该样本 T+1 当天（同一批股票，直接对比）
    d = conf[["t1_oc", "confirm_oc"]]
    print(f"\n  已确认样本内部对比: T+1当天胜率 {(d.t1_oc>0).mean()*100:.1f}% → 确认后隔日胜率 {(d.confirm_oc>0).mean()*100:.1f}%")
    print(f"                       T+1当天均值 {d.t1_oc.mean():+.2f}% → 确认后隔日均值 {d.confirm_oc.mean():+.2f}%")

    # ── 5. 结论速查：价差小(≤1%) 时高开怎么走 ──
    print("\n" + "=" * 74)
    print("5) 聚焦：价差≤1%（确认线离买入价很近）时的 T+1 表现")
    print("=" * 74)
    near = r[r["gap_bin"].isin(["≤0.5%", "0.5~1%"])]
    stat_block("价差≤1% 全部", near, min_n=0)
    stat_block("价差≤1% × 高开", near[near["t1_gap"] > 0], min_n=30)
    stat_block("价差≤1% × 低开", near[near["t1_gap"] < 0], min_n=30)
    stat_block("价差≤1% × 高开>2%", near[near["t1_gap"] > 2], min_n=30)


if __name__ == "__main__":
    main()
