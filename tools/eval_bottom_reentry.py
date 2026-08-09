"""
tools/eval_bottom_reentry.py —— 方法B离线回放：抄底触发「买回踩+重燃」 vs 现网「买反弹」
========================================================================================
背景：docs/short-reco-anti-chase-methods.md 方法B（P0，治本）建议把
strategy_bottom_fishing 的 score_rebound（距10日低反弹5%即满分，strategies.py L204-205）
替换为「回踩深度 + 重燃确认」：买上行趋势中从近期高位温和回踩、今日出现重启迹象的票，
而非"已经涨了5%"的反弹尾巴。

本脚本不改任何生产代码 / 不写库，只对全市场历史行情向量化重算两组 bottom 打分，
在完全一致的过滤链（质量 + 趋势闸门 + 追高否决 + 主板）与收益口径（OC/CC）下对比：

  O  现网抄底公式（基准）：score_drop + score_rebound(弹5%满分) + score_vol
  N  回踩+重燃公式：       score_drop + [回踩深度分 + 重燃分] + score_vol
  N+A  可选：N 公式再叠加方法A「扩展度/超买硬否决」（--with-extension）

两组共用同一 BUY_SCORE 0~3 → 0-10（×10/3）→ ×5 → 0-50 量纲映射，与 fuse_signals
及库内 bottom_score 列同口径。报告两个口径（全信号池 / 每日TopN），
统计与 tools/eval_fusion_mode.py 完全对齐（win_oc/avg_oc/pf_oc + win_cc/avg_cc）。

先跑本脚本拿到 OC/CC 证据，再决定是否动 strategy_bottom_fishing（动策略=全量重算）。

用法：
    python tools/eval_bottom_reentry.py                          # 近1年，自动训验分窗
    python tools/eval_bottom_reentry.py --since 2024-02-01       # 长窗（全市场池 2024-02 起才完整）
    python tools/eval_bottom_reentry.py --pullback-lo 0.03 --pullback-hi 0.08 --pullback-decay 0.15
    python tools/eval_bottom_reentry.py --with-extension         # 叠加方法A扩展否决组
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from tools.eval_fusion_mode import load_daily, add_features, load_index_regime, stat, _print_table
from config.strategy_params import (
    QUALITY_FILTER, SHORT_TREND_GATE, CHASE_FILTER,
    DEFAULT_SIG_THRESHOLD, HIGH_VOL_THRESHOLD, HIGH_VOL_SIG_THRESHOLD,
)

pd.set_option("display.width", 200)


# ─────────────────────────────────────────────
# 两组 bottom 打分（向量化重算，与 strategies.strategy_bottom_fishing 同公式）
# ─────────────────────────────────────────────

def bottom_scores(df: pd.DataFrame,
                  pullback_lo: float, pullback_hi: float, pullback_decay: float) -> tuple:
    """
    返回 (old_buy10, new_buy10)：
      old_buy10 = 现网公式 BUY_SCORE(0~3) × 10/3 → 0-10（= 库内 bottom_score 口径）
      new_buy10 = 方法B公式同量纲。
    df 必须含 code/close/high/volume，按 code 分组滚动计算（向量化）。
    """
    g = df.groupby("code", sort=False)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    volume = df["volume"].astype(float)

    # ── 现网公式（strategies.py L198-211 逐行照搬）──
    low_10d = g["close"].transform(lambda s: s.rolling(10).min())
    high_10d_before = g["close"].transform(lambda s: s.shift(10).rolling(10).max())
    drop_depth = ((high_10d_before - low_10d) / high_10d_before.clip(lower=1e-9)).clip(lower=0)
    score_drop = drop_depth.clip(upper=1.0)
    rebound_ratio = ((close - low_10d) / low_10d.clip(lower=1e-9)).clip(lower=0)
    score_rebound_old = (rebound_ratio / 0.05).clip(lower=0, upper=1.0)
    vol5 = g["volume"].transform(lambda s: s.rolling(5).mean())
    vol_ratio = volume / vol5.clip(lower=1e-9)
    score_vol = (vol_ratio / 2.0).clip(lower=0, upper=1.0)
    old_buy = (score_drop + score_rebound_old + score_vol).fillna(0)

    # ── 方法B公式：score_rebound 替换为「回踩深度 + 重燃」──
    # 回踩深度：距20日高 (high_20d-close)/high_20d，在 [pullback_lo, pullback_hi] 区间满分，
    #           小于 lo 线性升至满分（刚摸顶/贴近高点=不给分），大于 hi 线性衰减至 decay 归零
    #           （回踩过深=趋势破坏，不给分）。
    high_20d = g["close"].transform(lambda s: s.rolling(20).max())
    depth = (high_20d - close) / high_20d.clip(lower=1e-9)
    pullback = np.where(
        depth < pullback_lo, depth / max(pullback_lo, 1e-9),
        np.where(depth > pullback_hi,
                 (pullback_decay - depth) / max(pullback_decay - pullback_hi, 1e-9), 1.0))
    pullback = np.clip(np.nan_to_num(pullback, nan=0.0), 0.0, 1.0)

    # 重燃确认：收盘重新站上 MA5（0.5）+ 量能重燃（前段缩量后当日重新放量，0.5）
    ma5 = g["close"].transform(lambda s: s.rolling(5).mean())
    vol20 = g["volume"].transform(lambda s: s.rolling(20).mean())
    rekindle = ((close > ma5).astype(float) * 0.5
                + ((vol5 < vol20) & (volume > vol5)).astype(float) * 0.5)
    score_rebound_new = (pullback + rekindle).clip(lower=0, upper=1.0)

    new_buy = (score_drop + score_rebound_new + score_vol).fillna(0)

    return (old_buy * 10.0 / 3.0).clip(0, 10), (new_buy * 10.0 / 3.0).clip(0, 10)


# ─────────────────────────────────────────────
# 追高否决（与 rec_filters.chase_filter_series 同口径，groupby 向量化）
# ─────────────────────────────────────────────

def chase_mask(df: pd.DataFrame) -> pd.Series:
    cfg = CHASE_FILTER
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)

    def _chase(g: pd.DataFrame) -> pd.Series:
        close = g["close"].astype(float)
        pct = g["pct_change"].astype(float)
        high = g["high"].astype(float) if "high" in g.columns else close
        limit_pct = float(cfg.get("limit_pct", 9.8))
        is_limit = (pct >= limit_pct).astype(int)
        seg = (is_limit != is_limit.shift(1)).cumsum()
        streak = is_limit.groupby(seg).cumsum()

        ok = pd.Series(True, index=g.index)
        max_consec = int(cfg.get("max_consecutive_limit", 3))
        if max_consec >= 1:
            ok &= streak < max_consec
        cooldown_consec = int(cfg.get("cooldown_consecutive", 3))
        cooldown_days = int(cfg.get("cooldown_days", 0))
        if cooldown_consec >= 1 and cooldown_days >= 1:
            ok &= streak.rolling(cooldown_days, min_periods=1).max() < cooldown_consec
        if cfg.get("reject_limit_open", True):
            prev_close = close.shift(1)
            touched = high >= prev_close * (1 + (limit_pct - 0.3) / 100.0)
            sealed = pct >= limit_pct
            limit_open = touched & (~sealed) & prev_close.notna()
            ok &= ~limit_open.fillna(False)
        max_ret3 = float(cfg.get("max_ret_3d", 0.25))
        if max_ret3 > 0:
            ret3 = close / close.shift(3) - 1.0
            ok &= ret3.fillna(0.0) < max_ret3
        return ok.reindex(g.index).fillna(False)

    # 逐组循环拼接（groupby.apply 会返回 MultiIndex，reindex 到原索引易出 dtype 崩溃）
    cols = ["close", "pct_change", "high"]
    parts = [_chase(g[cols]) for _, g in df.groupby("code", sort=False)]
    return pd.concat(parts).reindex(df.index).fillna(False)


# ─────────────────────────────────────────────
# 方法A扩展否决（可选叠加组，阈值即文档建议值）
# ─────────────────────────────────────────────

def rsi_series(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.clip(lower=1e-9)
    return 100 - (100 / (1 + rs))


def extension_mask(df: pd.DataFrame, rsi_th: float = 68.0, ma20_x: float = 1.10,
                   hi20_x: float = 0.97, pct_th: float = 5.0) -> pd.Series:
    """方法A扩展否决（True=可推荐）。任一阈值 <= 0 即禁用该条。
    rsi_th  RSI 上限（默认 68）
    ma20_x  距 MA20 上限倍数（默认 1.10，即 close > MA20×1.10 否决）
    hi20_x  距 20 日高上限（默认 0.97，即 close > 20d_high×0.97 否决）
    pct_th  当日涨幅上限（默认 5.0，即 pct_change > 5% 否决）
    """
    g = df.groupby("code", sort=False)
    close = df["close"].astype(float)
    ok = pd.Series(True, index=df.index)
    if ma20_x and ma20_x > 0:
        ma20 = g["close"].transform(lambda s: s.rolling(20).mean())
        ok &= ~(close > ma20 * ma20_x)                    # 距 MA20 过远
    if rsi_th and rsi_th > 0:
        rsi = g["close"].transform(rsi_series)
        ok &= ~(rsi > rsi_th)                             # 超买
    if hi20_x and hi20_x > 0:
        high_20d = g["close"].transform(lambda s: s.rolling(20).max())
        ok &= ~(close > high_20d * hi20_x)                # 刚创 20 日高
    if pct_th and pct_th > 0:
        pct = df["pct_change"].astype(float)
        ok &= ~(pct > pct_th)                             # 当日急拉
    return ok.fillna(True)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="方法B离线回放：买回踩+重燃 vs 买反弹（只读）")
    ap.add_argument("--since", default="2025-08-01",
                    help="起始日期（默认近1年；全市场池子 2024-02 起才完整）")
    ap.add_argument("--split", default=None, help="训验分割日，默认取窗口后 30% 为验证窗")
    ap.add_argument("--topn", type=int, default=8, help="每日 TopN 口径（默认8，与今日推荐一致）")
    ap.add_argument("--pullback-lo", type=float, default=0.03, help="回踩深度满分下限")
    ap.add_argument("--pullback-hi", type=float, default=0.08, help="回踩深度满分上限")
    ap.add_argument("--pullback-decay", type=float, default=0.15, help="回踩深度衰减归零点")
    ap.add_argument("--with-extension", action="store_true", help="叠加方法A扩展否决组")
    ap.add_argument("--ext-rsi", type=float, default=68.0, help="扩展否决 RSI 上限（<=0 禁用）")
    ap.add_argument("--ext-ma20", type=float, default=1.10, help="扩展否决 距MA20 上限倍数（<=0 禁用）")
    ap.add_argument("--ext-hi20", type=float, default=0.97, help="扩展否决 距20日高上限（<=0 禁用）")
    ap.add_argument("--ext-pct", type=float, default=5.0, help="扩展否决 当日涨幅上限（<=0 禁用）")
    args = ap.parse_args()

    print(f"[1/5] 加载 daily_price since={args.since} ...")
    df = load_daily(args.since)
    if df.empty:
        print("[ERR] 无数据")
        return
    print(f"      {len(df)} 行 / {df['code'].nunique()} 只 / "
          f"{df['trade_date'].nunique()} 个交易日")

    print("[2/5] 过滤链（质量+趋势闸门+主板，与 eval_fusion_mode 同口径）...")
    df = add_features(df)
    df["chase"] = chase_mask(df)
    print(f"      追高否决保留 {int(df['chase'].sum())}/{len(df)} 行")
    if args.with_extension:
        df["ext"] = extension_mask(df, rsi_th=args.ext_rsi, ma20_x=args.ext_ma20,
                                   hi20_x=args.ext_hi20, pct_th=args.ext_pct)
        print(f"      扩展否决保留 {int(df['ext'].sum())}/{len(df)} 行")

    print("[3/5] 重算两组 bottom 打分（旧=库内线上列 / 新=回踩+重燃）...")
    old10, new10 = bottom_scores(df, args.pullback_lo, args.pullback_hi, args.pullback_decay)
    # O 组直接用库内 bottom_score 列（0-10 量纲）：线上增量链路（reuse_scores）就是读它，
    # 这才是用户实际看到的推荐口径；重算值仅用于校验公式还原度。
    df["bottom_old"] = pd.to_numeric(df.get("bottom_score"), errors="coerce").fillna(0).clip(0, 10)
    df["bottom_new"] = new10

    # 一致性校验：重算的旧公式 vs 库内 bottom_score 列（0-10 量纲）
    lib = pd.to_numeric(df.get("bottom_score"), errors="coerce")
    if lib is not None and lib.notna().any():
        mae = (old10 - lib).abs().dropna().mean()
        print(f"      [校验] 重算现码公式 vs 库内 bottom_score MAE = {mae:.4f} "
              f"（≈0 说明公式还原正确；O 组已直接用库内列，不受此差影响）")

    print("[4/5] 大盘 regime / 自适应阈值 ...")
    reg = load_index_regime()
    if reg.empty:
        print("[WARN] index_daily 无 000001，全部按 range / 阈值15 处理")
        df["regime"] = "range"
        df["threshold"] = DEFAULT_SIG_THRESHOLD
    else:
        df = df.merge(reg, on="trade_date", how="left")
        df["regime"] = df["regime"].fillna("range")
        df["threshold"] = df["threshold"].fillna(DEFAULT_SIG_THRESHOLD)

    dates = sorted(df["trade_date"].unique())
    split = args.split or dates[int(len(dates) * 0.7)]
    print(f"[5/5] 训练窗 {dates[0]}~{split} / 验证窗 {split}~{dates[-1]}")

    # 公共过滤链：质量 + 闸门 + 追高 + 主板（生产写库前四道全齐）
    base = df["qual"] & df["gate"] & df["chase"] & df["main_board"]
    if args.with_extension:
        base &= df["ext"]
    print(f"      过滤链保留 {int(base.sum())}/{len(df)} 行")

    # 两组融合分：bottom(0-10) × 5 → 0-50（纯抄底，PURE_BOTTOM_WEIGHTS 口径）
    df["fs::O 现网买反弹"] = df["bottom_old"] * 5.0
    df["fs::N 回踩+重燃"] = df["bottom_new"] * 5.0
    groups = ["O 现网买反弹", "N 回踩+重燃"]

    for window, mask in [("训练窗", df["trade_date"] < split),
                         ("验证窗(样本外)", df["trade_date"] >= split)]:
        wdf = df[mask]
        days = wdf["trade_date"].nunique()

        # 口径1：全信号池（各自过自适应阈值）
        rows = []
        for lbl in groups:
            sel = wdf[base[mask] & (wdf[f"fs::{lbl}"] >= wdf["threshold"])]
            rows.append((lbl, stat(sel, days)))
        _print_table(f"{window} · 口径1 全信号池（阈值=自适应 15/18，{days} 个交易日）", rows)

        # 口径2：每日 TopN（与今日推荐一致，条数对齐）
        rows = []
        for lbl in groups:
            col = f"fs::{lbl}"
            pool = wdf[base[mask] & (wdf[col] >= wdf["threshold"])]
            top = (pool.sort_values(col, ascending=False)
                       .groupby("trade_date", sort=False).head(args.topn))
            rows.append((lbl, stat(top, days)))
        _print_table(f"{window} · 口径2 每日Top{args.topn}（决策相关，条数对齐）", rows)

    # go/no-go：验证窗 TopN 口径，N 对 O（门槛：OC胜率 ≥ 基准+1pp 且 OC均值不降）
    vdf = df[df["trade_date"] >= split]
    vdays = vdf["trade_date"].nunique()
    res = {}
    for lbl in groups:
        col = f"fs::{lbl}"
        pool = vdf[base[df["trade_date"] >= split] & (vdf[col] >= vdf["threshold"])]
        top = (pool.sort_values(col, ascending=False)
                   .groupby("trade_date", sort=False).head(args.topn))
        res[lbl] = stat(top, vdays)

    a = res[groups[0]]
    print("\n" + "=" * 104)
    print(f"  go/no-go（验证窗 Top{args.topn} 口径，基准={groups[0]}；"
          f"门槛：OC胜率 ≥基准+1pp 且 OC均值不降）")
    print("=" * 104)
    if a is None:
        print("  基准组无样本，无法判定")
    else:
        for label, r in res.items():
            if label == groups[0] or r is None:
                continue
            wr_ok = r["win_oc"] >= a["win_oc"] + 1.0
            avg_ok = r["avg_oc"] >= a["avg_oc"]
            verdict = "GO" if (wr_ok and avg_ok) else "NO-GO"
            print(f"  {label:<22} OC胜率 {a['win_oc']:.2f}% -> {r['win_oc']:.2f}% "
                  f"({r['win_oc']-a['win_oc']:+.2f}pp) {'[PASS]' if wr_ok else '[FAIL]'}  |  "
                  f"OC均值 {a['avg_oc']:+.3f}% -> {r['avg_oc']:+.3f}% "
                  f"{'[PASS]' if avg_ok else '[FAIL]'}  =>  {verdict}")
    print("=" * 104)
    print("  注：本脚本只做隔日 OC/CC 口径。按项目规范，若结论为 GO，还需跑组合口径"
          "（PF/回撤/年化）二次确认后再落地策略函数 + 全量重算。")


if __name__ == "__main__":
    main()
