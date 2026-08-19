"""
tools/mine_surge_next_day.py —— 「次日大涨/涨停」候选策略挖掘（只读研究脚本，不写库）
================================================================
目标：为首页新栏目「次日强势股」寻找次日（T+1）大概率涨停或大涨的筛选规则。

与 mine_next_day.py 的区别：
  - 目标变量不同：除隔日胜率外，重点看「次日大涨概率」
      big_oc   = 次日 open->close >= 5%（开盘买得到、日内大涨，现实可吃到的肉）
      big_cc   = 次日 pct_change >= 5%（含隔夜跳空，展示用）
      limit_oc = 次日日内最高触及涨停（next_high >= next_open*涨停幅度）
  - 仅主板（60/00 开头）：账户未开通科创/创业板权限
  - 沿用现实口径纪律：信号 T 日收盘后算出，只能 T+1 开盘买入 → OC 为主口径

候选信号族（均叠加质量过滤 + 主板过滤）：
  A. 首板强封：当日涨停封死 + 非一字 + 非连板（量比/市值细分）
  B. 二板/连板：当日涨停且前一日也涨停（强者恒强）
  C. 大阳突破：当日涨幅 5~9.7%（未涨停，次日才买得到）+ 创20日新高 + 放量
  D. 大阳突破细分：量比区间 × 收盘位置 × 市值

go/no-go 门槛（验证窗）：
  OC 胜率 >= 55% 且 OC 均值 > 0 且 日均候选 >= 1 只
  （大涨类博弈胜率天然低于 60%，门槛按风险偏好放宽，但均值必须显著为正）

用法：
    python tools/mine_surge_next_day.py
    python tools/mine_surge_next_day.py --since 2024-07-01 --split 2026-01-25
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

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")


# ─────────────────────────────────────────────
# 数据加载与特征计算
# ─────────────────────────────────────────────

def load_daily(since: str) -> pd.DataFrame:
    """近 N 年主板行情 + 股票名（剔 ST/退，仅 60/00 开头）"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close,
                   p.volume, COALESCE(p.amount, 0) AS amount, p.pct_change,
                   p.turnover, i.name
            FROM daily_price p
            LEFT JOIN stock_info i ON i.code = p.code
            WHERE p.trade_date >= ?
            ORDER BY p.code, p.trade_date
            """,
            conn, params=[since],
        )
    df = df[~df["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
    df = df[df["pct_change"].abs() <= 21].copy()
    # 仅主板
    df = df[df["code"].str.startswith(MAIN_BOARD_PREFIXES)].copy()
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """按 code 分组向量化计算特征列"""
    g = df.groupby("code", sort=False)

    # 质量过滤（与 rec_filters.quality_series 同口径）
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
    df["mktcap"] = mktcap

    # 均线 / 量比 / 新高 / 收盘位置
    df["ma5"] = g["close"].transform(lambda s: s.rolling(5).mean())
    df["ma10"] = g["close"].transform(lambda s: s.rolling(10).mean())
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
    df["vol_ma5_prev"] = g["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    df["vol_ratio"] = df["volume"] / df["vol_ma5_prev"].clip(lower=1e-9)
    df["high20_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(20).max())
    df["high60_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(60).max())
    rng = (df["high"] - df["low"]).clip(lower=1e-9)
    df["close_pos"] = (df["close"] - df["low"]) / rng

    # 涨停判定（主板 10cm：收盘涨幅 >=9.8 且收盘≈最高 = 封死）
    df["is_limit"] = (df["pct_change"] >= 9.8) & (df["close"] >= df["high"] * 0.999)
    df["prev_limit"] = g["is_limit"].shift(1).fillna(False)
    df["prev_pct"] = g["pct_change"].shift(1)
    df["prev_vol_ratio"] = g["vol_ratio"].shift(1)
    # 连板计数（1~4+）
    df["streak"] = g["is_limit"].transform(
        lambda s: s.groupby((s != s.shift()).cumsum()).cumcount() + 1)
    df.loc[~df["is_limit"], "streak"] = 0

    # ── 隔日收益：OC 现实口径为主 ──
    next_open = g["open"].shift(-1)
    next_close = g["close"].shift(-1)
    next_high = g["high"].shift(-1)
    df["nd_cc"] = g["pct_change"].shift(-1)
    df["nd_oc"] = (next_close / next_open.replace(0, np.nan) - 1) * 100
    df["nd_high_pct"] = (next_high / next_open.replace(0, np.nan) - 1) * 100  # 次日日内最高可触及
    # 次日脏数据剔除
    dirty = df["nd_cc"].abs() > 21
    df.loc[dirty, ["nd_cc", "nd_oc", "nd_high_pct"]] = np.nan
    # 次日是否涨停开盘（一字/巨高开，开盘买不到或风险极高，需单独标注）
    df["next_gap_pct"] = (next_open / df["close"].replace(0, np.nan) - 1) * 100
    return df


def add_regime(df: pd.DataFrame) -> pd.DataFrame:
    """大盘 regime：当日全市场（主板）平均涨跌幅"""
    avg = df.groupby("trade_date")["pct_change"].mean()
    bins = pd.cut(avg, [-np.inf, -1, -0.3, 0.3, 1, np.inf],
                  labels=["cold", "cool", "neutral", "warm", "hot"])
    df["regime"] = df["trade_date"].map(bins)
    return df


# ─────────────────────────────────────────────
# 信号族定义（目标：次日涨停或大涨）
# ─────────────────────────────────────────────

def build_signals(df: pd.DataFrame) -> dict:
    """返回 {信号名: 布尔掩码}，均已叠加质量过滤"""
    q = df["qual"]
    sig = {}

    # A. 首板强封：涨停封死 + 非一字 + 非连板
    first_board = q & df["is_limit"] & (df["high"] > df["low"]) & (~df["prev_limit"])
    sig["A1 首板(封死,非一字)"] = first_board
    sig["A2 首板+量比<3(缩量封板)"] = first_board & (df["vol_ratio"] < 3)
    sig["A3 首板+量比<3+市值<150亿"] = (
        first_board & (df["vol_ratio"] < 3) & (df["mktcap"] < 150e8))
    sig["A4 首板+量比<1.5(极度缩量)"] = first_board & (df["vol_ratio"] < 1.5)

    # B. 连板：当日涨停且前一日也涨停（二板及以上，强者恒强）
    sig["B1 二板及以上"] = q & df["is_limit"] & df["prev_limit"]
    sig["B2 恰好二板"] = q & df["is_limit"] & df["prev_limit"] & (df["streak"] == 2)

    # C. 大阳突破：当日 5~9.7%（未涨停→次日开盘买得到）+ 创 20 日新高 + 放量
    big_yang = (
        q & (df["pct_change"] >= 5) & (df["pct_change"] < 9.8)
        & (df["close"] > df["high20_prev"]) & (df["vol_ratio"] >= 1.5)
        & (df["close_pos"] >= 0.7)
    )
    sig["C1 大阳突破20日新高"] = big_yang
    sig["C2 大阳突破+量比1.5~4"] = big_yang & (df["vol_ratio"] < 4)
    sig["C3 大阳突破+市值<150亿"] = big_yang & (df["mktcap"] < 150e8)
    sig["C4 大阳突破60日新高"] = (
        q & (df["pct_change"] >= 5) & (df["pct_change"] < 9.8)
        & (df["close"] > df["high60_prev"]) & (df["vol_ratio"] >= 1.5)
        & (df["close_pos"] >= 0.7)
    )

    # D. 大阳细分：站上 MA20 的温和放量大阳（非追高天量）
    sig["D1 大阳+量比1.5~3+站上MA20"] = (
        big_yang & (df["vol_ratio"] < 3) & (df["close"] > df["ma20"]))
    return sig


# ─────────────────────────────────────────────
# 统计与报告
# ─────────────────────────────────────────────

def stat_block(name: str, sub: pd.DataFrame, min_n: int = 30) -> dict | None:
    sub = sub.dropna(subset=["nd_oc"])
    n = len(sub)
    if n < min_n:
        print(f"  {name:<40} 样本 {n:>6}  (不足{min_n}，跳过)")
        return None
    days = sub["trade_date"].nunique()
    win_oc = (sub["nd_oc"] > 0).mean() * 100
    avg_oc = sub["nd_oc"].mean()
    big_oc = (sub["nd_oc"] >= 5).mean() * 100            # 次日日内大涨（OC≥5%）
    big_cc = (sub.dropna(subset=["nd_cc"])["nd_cc"] >= 5).mean() * 100
    touch_limit = (sub["nd_high_pct"] >= 9.7).mean() * 100  # 次日盘中触及涨停
    gap_open = (sub["next_gap_pct"] >= 5).mean() * 100      # 次日高开≥5%（不好买）
    per_day = n / max(days, 1)
    print(f"  {name:<40} 样本 {n:>6} 日均 {per_day:4.1f} | "
          f"OC胜率 {win_oc:5.1f}% 均值 {avg_oc:+.2f}% | "
          f"次日大涨(OC≥5%) {big_oc:4.1f}% 触涨停 {touch_limit:4.1f}% "
          f"CC≥5% {big_cc:4.1f}% | 高开≥5% {gap_open:4.1f}%")
    return {"n": n, "per_day": per_day, "win_oc": win_oc, "avg_oc": avg_oc,
            "big_oc": big_oc, "big_cc": big_cc,
            "touch_limit": touch_limit, "gap_open": gap_open}


def main():
    ap = argparse.ArgumentParser(description="次日大涨/涨停候选策略挖掘（只读）")
    ap.add_argument("--since", default="2024-07-01", help="样本起点")
    ap.add_argument("--split", default="2026-01-25",
                    help="训练/验证分割日（之前为训练窗，之后为验证窗）")
    args = ap.parse_args()

    print(f"[1/3] 加载主板行情 {args.since} 起 ...")
    df = load_daily(args.since)
    print(f"      {df['code'].nunique()} 只 / {len(df)} 行")

    print("[2/3] 计算特征 ...")
    df = add_features(df)
    df = add_regime(df)

    print("[3/3] 生成信号族并统计（目标：次日涨停或大涨）...")
    signals = build_signals(df)
    results = {}
    for name, mask in signals.items():
        print(f"\n【{name}】")
        for win_name, wmask in [("训练窗", df["trade_date"] < args.split),
                                ("验证窗", df["trade_date"] >= args.split)]:
            r = stat_block(win_name, df[mask & wmask])
            if win_name == "验证窗":
                results[name] = r
        # regime 交叉（验证窗）
        for rg in ("hot", "warm", "neutral", "cool", "cold"):
            stat_block(f"  × {rg}",
                       df[mask & (df["trade_date"] >= args.split) & (df["regime"] == rg)],
                       min_n=20)

    # ── go/no-go 判定（验证窗 OC 口径）──
    print("\n" + "=" * 78)
    print("  go/no-go 门槛：验证窗 OC胜率>=55% 且 OC均值>0 且 日均候选>=1 只")
    print("=" * 78)
    passed = []
    for name, r in results.items():
        if r is None:
            continue
        ok = r["win_oc"] >= 55 and r["avg_oc"] > 0 and r["per_day"] >= 1
        mark = "[GO]  " if ok else "[NO]  "
        print(f"  {mark}{name:<34} OC胜率 {r['win_oc']:5.1f}%  均值 {r['avg_oc']:+.2f}%  "
              f"大涨率 {r['big_oc']:4.1f}%  触涨停 {r['touch_limit']:4.1f}%  日均 {r['per_day']:.1f}只")
        if ok:
            passed.append(name)
    print("-" * 78)
    if passed:
        print(f"  结论: GO —— 达标信号族: {', '.join(passed)}")
        # 按「大涨率 × 胜率」综合挑最优，供 Phase 2 落地参考
        best = max((results[p] for p in passed if results[p]),
                   key=lambda r: r["big_oc"] * 0.6 + r["win_oc"] * 0.4)
        for p in passed:
            if results[p] is best:
                print(f"  推荐落地: {p}（大涨率 {best['big_oc']:.1f}% / "
                      f"OC胜率 {best['win_oc']:.1f}% / 均值 {best['avg_oc']:+.2f}%）")
    else:
        print("  结论: NO-GO —— 无信号族达标，如实汇报最优组合与差距，不上线")
        best_name, best_r = None, None
        for name, r in results.items():
            if r and (best_r is None or r["win_oc"] > best_r["win_oc"]):
                best_name, best_r = name, r
        if best_r:
            print(f"  最优: {best_name}（OC胜率 {best_r['win_oc']:.1f}% / "
                  f"均值 {best_r['avg_oc']:+.2f}% / 大涨率 {best_r['big_oc']:.1f}%）")


if __name__ == "__main__":
    main()
