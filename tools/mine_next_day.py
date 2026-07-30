"""
tools/mine_next_day.py —— 隔日动量信号挖掘（只读研究脚本，不写库）
================================================================
目标：为「隔日动量」信号线寻找隔日胜率 >=60% 的入场信号。

候选信号族（每族独立统计隔日胜率/均值/样本量）：
  1. 首板次日溢价：当日涨停（主板>=9.8 / 创业板20cm>=19.8）、收盘=最高（排除炸板）、
     非一字板（high>low）、非连板（前一日未涨停）
  2. 放量新高突破：收盘创 20/60 日新高 + 量比>=2 + 收盘位于当日振幅上 30%
  3. 强势回踩反包：10 日内出现过大阳（>=7%），回踩 MA5~MA10 后当日再收阳
  4. 龙虎榜净买入：stock_lhb_detail 自带 perf_1d 标签，按净买占比/上榜原因分层
  5. 以上 × 大盘 regime（hot/warm/neutral/cool/cold）交叉分层

样本与口径：
  - daily_price 近 2 年全市场；剔 ST/退、剔 |pct_change|>21 脏数据
  - 质量过滤与现网一致：近20日均成交额 >= QUALITY_FILTER.min_amt20，
    市值落在 [min_mktcap, max_mktcap]（缺 total_shares 跳过市值项）
  - 隔日收益两个口径：
      nd_cc = 次日 pct_change（信号日收盘买 → 次日收盘，与「第二天涨没涨」口径一致）
      nd_oc = 次日 open→close（次日开盘才买得到的现实口径）
  - 训练窗（前段 ~12 个月）挖参数，验证窗（近 6 个月）样本外检验

go/no-go 门槛（验证窗）：
  隔日胜率(nd_cc) >= 60%  且  次日均值 > 0  且  日均候选 >= 2 只

用法：
    python tools/mine_next_day.py
    python tools/mine_next_day.py --since 2024-07-01 --split 2026-01-25
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.db import get_conn, get_market_cap_map
from config.strategy_params import QUALITY_FILTER

pd.set_option("display.width", 160)


# ─────────────────────────────────────────────
# 数据加载与特征计算
# ─────────────────────────────────────────────

def load_daily(since: str) -> pd.DataFrame:
    """近 N 年全市场行情 + 股票名（剔 ST/退）"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close,
                   p.volume, COALESCE(p.amount, 0) AS amount, p.pct_change,
                   i.name
            FROM daily_price p
            LEFT JOIN stock_info i ON i.code = p.code
            WHERE p.trade_date >= ?
            ORDER BY p.code, p.trade_date
            """,
            conn, params=[since],
        )
    # ST/退市整只剔除；异常涨跌幅单日剔除
    df = df[~df["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
    df = df[df["pct_change"].abs() <= 21].copy()
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """按 code 分组向量化计算特征列（避免逐只 Python 循环）"""
    g = df.groupby("code", sort=False)

    # 质量过滤：近20日均成交额 + 市值区间（与 rec_filters.quality_series 同口径）
    df["amt20"] = g["amount"].transform(lambda s: s.rolling(20).mean())
    mktcap_map = get_market_cap_map()
    ts = df["code"].map(lambda c: (mktcap_map.get(c) or {}).get("total_shares"))
    mktcap = ts * df["close"]
    qual = df["amt20"] >= (QUALITY_FILTER.get("min_amt20") or 0)
    has_ts = ts.notna() & (ts > 0)
    qual &= (~has_ts) | (
        (mktcap >= QUALITY_FILTER.get("min_mktcap", 0))
        & (mktcap <= QUALITY_FILTER.get("max_mktcap", float("inf")))
    )
    df["qual"] = qual.fillna(False)

    # 均线 / 量比 / 新高 / 收盘位置
    df["ma5"] = g["close"].transform(lambda s: s.rolling(5).mean())
    df["ma10"] = g["close"].transform(lambda s: s.rolling(10).mean())
    df["vol_ma5_prev"] = g["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    df["vol_ratio"] = df["volume"] / df["vol_ma5_prev"].clip(lower=1e-9)
    df["high20_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(20).max())
    df["high60_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(60).max())
    rng = (df["high"] - df["low"]).clip(lower=1e-9)
    df["close_pos"] = (df["close"] - df["low"]) / rng

    # 涨停判定（20cm：创业板 300xxx / 科创板 688xxx）
    is_20cm = df["code"].str.startswith(("300", "301", "688", "689"))
    limit_thr = np.where(is_20cm, 19.8, 9.8)
    df["is_limit"] = (df["pct_change"] >= limit_thr) & (df["close"] >= df["high"] * 0.999)
    df["prev_limit"] = g["is_limit"].shift(1).fillna(False)
    df["prev_pct"] = g["pct_change"].shift(1)
    df["prev_vol_ratio"] = g["vol_ratio"].shift(1)

    # 10 日内（不含今日）是否出现过大阳 >=7%
    df["big_up_10d"] = g["pct_change"].transform(
        lambda s: s.shift(1).rolling(10).max()) >= 7.0

    # 隔日收益两口径
    df["nd_cc"] = g["pct_change"].shift(-1)                       # 次日 close/close
    next_open = g["open"].shift(-1)
    next_close = g["close"].shift(-1)
    df["nd_oc"] = (next_close / next_open.replace(0, np.nan) - 1) * 100  # 次日 open→close
    # 次日脏数据剔除
    df.loc[df["nd_cc"].abs() > 21, ["nd_cc", "nd_oc"]] = np.nan
    return df


def add_regime(df: pd.DataFrame) -> pd.DataFrame:
    """大盘 regime：当日全市场平均涨跌幅"""
    avg = df.groupby("trade_date")["pct_change"].mean()
    bins = pd.cut(avg, [-np.inf, -1, -0.3, 0.3, 1, np.inf],
                  labels=["cold", "cool", "neutral", "warm", "hot"])
    df["regime"] = df["trade_date"].map(bins)
    return df


# ─────────────────────────────────────────────
# 信号族定义
# ─────────────────────────────────────────────

def build_signals(df: pd.DataFrame) -> dict:
    """返回 {信号名: 布尔掩码}，均已叠加质量过滤"""
    q = df["qual"]
    sig = {}

    # 1. 首板次日溢价：涨停封死 + 非一字 + 非连板
    sig["首板(封死,非一字,非连板)"] = (
        q & df["is_limit"] & (df["high"] > df["low"]) & (~df["prev_limit"])
    )
    # 1b. 首板细分：涨停且当日量比温和（非天量出货）
    sig["首板+量比<8"] = sig["首板(封死,非一字,非连板)"] & (df["vol_ratio"] < 8)

    # 2. 放量新高突破（非涨停，排除与首板重叠）
    base_break = q & (~df["is_limit"]) & (df["vol_ratio"] >= 2) & (df["close_pos"] >= 0.7)
    sig["放量创20日新高"] = base_break & (df["close"] > df["high20_prev"])
    sig["放量创60日新高"] = base_break & (df["close"] > df["high60_prev"])

    # 3. 强势回踩反包：10日内有大阳，昨日回踩缩量，今日收阳站上MA5
    sig["强势回踩反包"] = (
        q & df["big_up_10d"]
        & (df["prev_pct"] < 0) & (df["prev_vol_ratio"] < 0.9)
        & (df["pct_change"] > 0) & (df["close"] > df["ma5"])
        & (~df["is_limit"])
    )
    return sig


# ─────────────────────────────────────────────
# 统计与报告
# ─────────────────────────────────────────────

def stat_block(name: str, sub: pd.DataFrame, min_n: int = 30) -> dict | None:
    sub = sub.dropna(subset=["nd_cc"])
    n = len(sub)
    if n < min_n:
        print(f"  {name:<38} 样本 {n:>6}  (不足{min_n}，跳过)")
        return None
    days = sub["trade_date"].nunique()
    win_cc = (sub["nd_cc"] > 0).mean() * 100
    avg_cc = sub["nd_cc"].mean()
    oc = sub.dropna(subset=["nd_oc"])
    win_oc = (oc["nd_oc"] > 0).mean() * 100 if len(oc) else float("nan")
    avg_oc = oc["nd_oc"].mean() if len(oc) else float("nan")
    per_day = n / max(days, 1)
    print(f"  {name:<38} 样本 {n:>6}  日均 {per_day:4.1f}只 | "
          f"次日CC 胜率 {win_cc:5.1f}% 均值 {avg_cc:+.2f}% | "
          f"次日OC 胜率 {win_oc:5.1f}% 均值 {avg_oc:+.2f}%")
    return {"n": n, "per_day": per_day, "win_cc": win_cc, "avg_cc": avg_cc,
            "win_oc": win_oc, "avg_oc": avg_oc}


def run_lhb(df: pd.DataFrame, since: str, split: str):
    """龙虎榜族：用自带 perf_1d 标签（CC 口径）分层，
    并从 daily_price 特征表 merge 出现实可交易的次日 open→close 口径。

    关键口径区别：龙虎榜在收盘后才公布，真实交易只能次日（T+1）开盘买入。
    所以 perf_1d（上榜后1日=T收→T+1收，CC）含一个买不到的隔夜跳空；
    nd_oc（T+1 open→close）才是可落地的现实收益。
    """
    with get_conn() as conn:
        lhb = pd.read_sql_query(
            """
            SELECT trade_date, code, name, pct_change, net_buy, net_buy_ratio,
                   reason, perf_1d, turnover_rate
            FROM stock_lhb_detail WHERE trade_date >= ?
            """,
            conn, params=[since],
        )
    if lhb.empty or lhb["perf_1d"].notna().sum() < 100:
        print("\n[龙虎榜族] 数据不足（近2年记录过少或 perf_1d 缺失），跳过")
        return {}
    lhb = lhb[~lhb["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
    lhb = lhb.dropna(subset=["perf_1d"])
    lhb = lhb[lhb["perf_1d"].abs() <= 21]
    lhb["nd_cc"] = lhb["perf_1d"]

    # 从行情特征表 merge 出现实口径 nd_oc（T+1 open→close）与校验用 nd_cc
    px = df[["code", "trade_date", "nd_oc", "nd_cc"]].rename(
        columns={"nd_oc": "nd_oc_px", "nd_cc": "nd_cc_px"})
    lhb = lhb.merge(px, on=["code", "trade_date"], how="left")
    lhb["nd_oc"] = lhb["nd_oc_px"]

    print(f"\n【信号族4：龙虎榜净买入】（样本 {len(lhb)}，含 perf_1d 标签，"
          f"OC 口径已关联 daily_price）")
    layers = [
        ("全部上榜", pd.Series(True, index=lhb.index)),
        ("净买入>0", lhb["net_buy"] > 0),
        ("净买占比>=10%", lhb["net_buy_ratio"] >= 10),
        ("净买占比>=20%", lhb["net_buy_ratio"] >= 20),
        ("净买占比>=10%且非涨停", (lhb["net_buy_ratio"] >= 10) & (lhb["pct_change"] < 9.8)),
        ("净买占比>=20%且非涨停", (lhb["net_buy_ratio"] >= 20) & (lhb["pct_change"] < 9.8)),
    ]
    results = {}
    for label, mask in layers:
        for win_name, wmask in [("训练窗", lhb["trade_date"] < split),
                                ("验证窗", lhb["trade_date"] >= split)]:
            r = stat_block(f"{label} [{win_name}]", lhb[mask & wmask])
            if win_name == "验证窗":
                results[f"龙虎榜-{label}"] = r
    # 上榜原因分层（验证窗，净买占比>=10%）
    print("  — 按上榜原因（验证窗，净买占比>=10%）—")
    v = lhb[(lhb["trade_date"] >= split) & (lhb["net_buy_ratio"] >= 10)]
    for reason, grp in v.groupby("reason"):
        if len(grp) >= 40:
            stat_block(f"  {str(reason)[:24]}", grp, min_n=40)
    return results


def run_firstboard_focus(df: pd.DataFrame, split: str):
    """对唯一正期望的首板族做聚焦细分：确认是否存在隔日胜率 >=60% 的可交易子集。

    维度：收盘位置、量比区间、流通市值分档、大盘 regime、次日高开与否。
    仅在验证窗（样本外）统计，min_n 放宽到 20 以便看清子集分布。
    """
    print("\n" + "=" * 70)
    print("  [聚焦细分] 首板族（唯一正期望）验证窗子集扫描")
    print("=" * 70)

    fb = build_signals(df)["首板(封死,非一字,非连板)"]
    v = df[fb & (df["trade_date"] >= split)].copy()
    v = v.dropna(subset=["nd_cc"])
    print(f"  验证窗首板样本 {len(v)} 条\n")

    # 市值分档（缺市值的归入 unknown）
    mktcap_map = get_market_cap_map()
    ts = v["code"].map(lambda c: (mktcap_map.get(c) or {}).get("total_shares"))
    v["mktcap"] = ts * v["close"]

    print("  — 按收盘位置（当日振幅内）—")
    stat_block("close_pos>=0.99(封板)", v[v["close_pos"] >= 0.99], min_n=20)
    stat_block("close_pos 0.9~0.99", v[(v["close_pos"] >= 0.9) & (v["close_pos"] < 0.99)], min_n=20)

    print("  — 按量比区间 —")
    stat_block("量比<1.5(缩量涨停)", v[v["vol_ratio"] < 1.5], min_n=20)
    stat_block("量比 1.5~3", v[(v["vol_ratio"] >= 1.5) & (v["vol_ratio"] < 3)], min_n=20)
    stat_block("量比 3~6", v[(v["vol_ratio"] >= 3) & (v["vol_ratio"] < 6)], min_n=20)
    stat_block("量比>=6(天量)", v[v["vol_ratio"] >= 6], min_n=20)

    print("  — 按流通市值分档 —")
    stat_block("市值<50亿", v[v["mktcap"] < 50e8], min_n=20)
    stat_block("市值 50~150亿", v[(v["mktcap"] >= 50e8) & (v["mktcap"] < 150e8)], min_n=20)
    stat_block("市值>=150亿", v[v["mktcap"] >= 150e8], min_n=20)

    print("  — 组合：缩量涨停(量比<3) × regime —")
    calm = v[v["vol_ratio"] < 3]
    for rg in ("hot", "warm", "neutral", "cool", "cold"):
        stat_block(f"量比<3 × {rg}", calm[calm["regime"] == rg], min_n=20)

    print("  — 组合：缩量涨停(量比<3) × 市值<150亿 —")
    stat_block("量比<3 且 市值<150亿",
               calm[calm["mktcap"] < 150e8], min_n=20)

    # 找出验证窗内表现最好的子集摘要
    best = None
    for lbl, sub in [
        ("close_pos>=0.99", v[v["close_pos"] >= 0.99]),
        ("量比<1.5", v[v["vol_ratio"] < 1.5]),
        ("量比<3", calm),
        ("量比<3 且 市值<150亿", calm[calm["mktcap"] < 150e8]),
    ]:
        sub = sub.dropna(subset=["nd_cc"])
        if len(sub) >= 20:
            w = (sub["nd_cc"] > 0).mean() * 100
            if best is None or w > best[1]:
                best = (lbl, w, sub["nd_cc"].mean(), len(sub))
    if best:
        print(f"\n  最优子集: {best[0]}  胜率 {best[1]:.1f}%  "
              f"均值 {best[2]:+.2f}%  样本 {best[3]}")


def main():
    ap = argparse.ArgumentParser(description="隔日动量信号挖掘（只读）")
    ap.add_argument("--since", default="2024-07-01", help="样本起点")
    ap.add_argument("--split", default="2026-01-25",
                    help="训练/验证分割日（之前为训练窗，之后为验证窗）")
    args = ap.parse_args()

    print(f"[1/4] 加载行情 {args.since} 起 ...")
    df = load_daily(args.since)
    print(f"      {df['code'].nunique()} 只 / {len(df)} 行")

    print("[2/4] 计算特征 ...")
    df = add_features(df)
    df = add_regime(df)

    print("[3/4] 生成信号族并统计 ...")
    signals = build_signals(df)
    results = {}
    for name, mask in signals.items():
        print(f"\n【{name}】")
        for win_name, wmask in [("训练窗", df["trade_date"] < args.split),
                                ("验证窗", df["trade_date"] >= args.split)]:
            r = stat_block(f"{win_name}", df[mask & wmask])
            if win_name == "验证窗":
                results[name] = r
        # regime 交叉（验证窗）
        for rg in ("hot", "warm", "neutral", "cool", "cold"):
            stat_block(f"  验证窗 × {rg}", df[mask & (df["trade_date"] >= args.split)
                                            & (df["regime"] == rg)], min_n=30)

    print("[4/4] 龙虎榜族 ...")
    lhb_results = run_lhb(df, args.since, args.split)
    if lhb_results:
        results.update({k: v for k, v in lhb_results.items() if v is not None})

    # 首板族聚焦细分（唯一正期望族，确认有无 >=60% 子集）
    run_firstboard_focus(df, args.split)

    # ── go/no-go 判定（验证窗 CC 口径）──
    print("\n" + "=" * 70)
    print("  go/no-go 门槛：验证窗 隔日胜率(CC)>=60% 且 均值>0 且 日均候选>=2 只")
    print("=" * 70)
    passed = []
    for name, r in results.items():
        if r is None:
            continue
        ok = r["win_cc"] >= 60 and r["avg_cc"] > 0 and r["per_day"] >= 2
        mark = "[GO]  " if ok else "[NO]  "
        print(f"  {mark}{name:<32} 胜率 {r['win_cc']:5.1f}%  均值 {r['avg_cc']:+.2f}%  日均 {r['per_day']:.1f}只")
        if ok:
            passed.append(name)
    print("-" * 70)
    if passed:
        print(f"  结论: GO —— 达标信号族: {', '.join(passed)}")
    else:
        print("  结论: NO-GO —— 无信号族达标，不进入 Phase 2（龙虎榜族见上方独立报告）")


if __name__ == "__main__":
    main()
