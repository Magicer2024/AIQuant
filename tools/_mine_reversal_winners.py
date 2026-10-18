"""
tools/_mine_reversal_winners.py —— 反转首日观察池：「推荐后 10 日涨幅 > 10%」赢家共性挖掘
==========================================================================================

目标（用户 2026-10-15 提出）：
  1) 找出「反转首日观察」推荐票中，推荐后 10 个交易日内累计涨幅 > 10% 的那批，看它们有什么共性；
  2) 把共性归纳成可执行的筛选策略，并配止盈止损；
  3) 跑 1 个月 / 3 个月 / 6 个月 / 1 年 四个窗口回测，评估这套归纳出来的策略。

═══ 为什么不能直接用落库信号 ═══════════════════════════════════════════════
落库的反转首日信号只有 2026-07-20 起（first_reversal_hist 951 行 / 48 日 +
stock_signal 126 行），连 3 个月都覆盖不满，做不了 1 年回测。
但 scan_first_reversal 是纯函数、daily_price 有 1990→今全量 ⇒ 本脚本**向量化重放**
历史信号，条件与 strategy/first_reversal.py 的主条件 A3 逐条对齐：
    close > MA20  &  prev_close <= prev_MA20  &  自算涨幅 >= 5%
    &  成交额量比(对前 5 日均额) >= 1.5  &  非涨停
（量比用 amount 而非 volume：与 backfill_first_reversal.py / 线上同源；
  早期 _diag_reversal_v5.py 用的是 volume，两者结论方向一致但数值不可直接对比。）

═══ 口径纪律（沿用项目既有铁律）═══════════════════════════════════════════
  · daily_price.pct_change **不可用**（63.3% 的行恒为 0）⇒ 一律 close/close.shift(1) 自算。
  · 入场 = T+1 开盘价（信号日收盘后才产出，当天买不到）；剔除 T+1 一字板（买不进）。
  · 收益评估一律配「同日配对」市场中性超额：信号组 − 当日全市场同持有期基线。
  · **训练/测试时间切分**：赢家共性只在 TRAIN（<= 2025-09-30）上挖，
    规则定稿后在 TEST（2025-10-01 ~ 2026-10-15）上**只评估一次**，防止数据窥探。
  · 结论要求分年一致（2023/2024/2025/2026 同号），单年驱动的一律不采信。

═══ 已知偏差（诚实披露）═══════════════════════════════════════════════════
  · stock_info.industry 全表 NULL ⇒ 行业维度挖不了。
  · circ_shares / name 取的是**当前**快照，不是 point-in-time：
      - 流通市值、换手率含轻微未来信息（股本变动）；
      - ST/退 过滤用的是今天的名字（当年正常、今天 ST 的会被误剔）。
    ⇒ 若某特征只在市值/换手上显著，需另行用 point-in-time 数据复核后才可上线。
  · 已退市股票的历史行情在 daily_price 里自然终止 ⇒ 其 10 日前向收益为 NaN 被剔除，
    存在幸存者偏差（会**高估**策略收益）。

用法：
  python tools/_mine_reversal_winners.py build      # 建特征表 → data_cache/_reversal_mine.parquet
  python tools/_mine_reversal_winners.py mine       # 训练期赢家共性挖掘
  python tools/_mine_reversal_winners.py exit       # 止盈止损/持有期网格（训练期定参）
  python tools/_mine_reversal_winners.py backtest   # 1M/3M/6M/1Y 四窗口回测（含测试期）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd
from scipy import stats

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "core", "quant.db")
CACHE = os.path.join(PROJECT_ROOT, "data_cache", "_reversal_mine.parquet")

LOAD_START = "2022-01-01"      # 铺垫：250 日高低点 / ret120 需要长历史
SIG_START = "2023-07-01"       # 信号窗口起
SIG_END = "2026-10-15"         # 信号窗口止（= daily_price 最新日）
TRAIN_END = "2025-09-30"       # 训练期上界（含）；之后为测试期
HOLD_WIN = 10                  # 「推荐后 N 个交易日内」
WIN_THR = 10.0                 # 「累计涨幅超过 N%」
MIN_PCT = 5.0                  # A3：大阳下限
VOL_RATIO_MIN = 1.5            # A3：量比下限
MAIN_BOARD = ("60", "00")      # 用户仅能交易主板（config.personal_config.MAIN_BOARD_ONLY=True）
COST_PP = 0.10                 # A 股往返成本（佣金+印花税+滑点），单位 pp


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 · 建特征表
# ══════════════════════════════════════════════════════════════════════════
def build() -> pd.DataFrame:
    t0 = time.time()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    print(f"[1/6] 载入 daily_price {LOAD_START} ~ {SIG_END} ...")
    df = pd.read_sql(
        "SELECT code, trade_date, open, high, low, close, volume, amount "
        "FROM daily_price WHERE trade_date >= ? AND trade_date <= ? "
        "ORDER BY code, trade_date", con, params=(LOAD_START, SIG_END))
    si = pd.read_sql("SELECT code, name, circ_shares, total_shares FROM stock_info", con)
    con.close()
    print(f"      {len(df):,} 行 / {df['code'].nunique()} 只   {time.time()-t0:.0f}s")

    # 剔除科创板(688/689)、北交所(8/9)；保留主板+创业板（市场基线要用全市场口径）
    df = df[~df["code"].str.startswith(("688", "689", "8", "9"))].copy()
    df = df[(df["close"] > 0) & (df["open"] > 0)
            & (df["high"] > 0) & (df["low"] > 0)].reset_index(drop=True)
    df = df.merge(si, on="code", how="left")
    print(f"[2/6] 过滤后 {len(df):,} 行 / {df['code'].nunique()} 只")

    G = lambda c: df.groupby("code", sort=False)[c]          # noqa: E731
    rm = lambda c, n, sh=0, mp=None: (                        # noqa: E731
        (G(c).shift(sh) if sh else df[c])
        .groupby(df["code"], sort=False)
        .transform(lambda s: s.rolling(n, min_periods=mp or n).mean()))

    print("[3/6] 计算特征 ...")
    t = time.time()
    df["ret"] = (df["close"] / G("close").shift(1) - 1) * 100        # 权威自算涨幅
    df["close_prev"] = G("close").shift(1)

    # 均线族
    df["ma20"] = rm("close", 20)
    df["ma20_prev"] = G("ma20").shift(1)
    df["ma60"] = rm("close", 60)
    df["ma120"] = rm("close", 120)
    df["ext20"] = (df["close"] / df["ma20"] - 1) * 100                # 偏离 MA20
    df["ma60_rel"] = (df["close"] / df["ma60"] - 1) * 100
    df["ma120_rel"] = (df["close"] / df["ma120"] - 1) * 100
    df["ma20_slope5"] = (df["ma20"] / G("ma20").shift(5) - 1) * 100   # MA20 5 日斜率

    # 量能（amount 口径，与线上一致）
    df["amt5_prev"] = rm("amount", 5, sh=1)
    df["amt60_prev"] = rm("amount", 60, sh=1, mp=40)
    df["vr"] = df["amount"] / df["amt5_prev"].replace(0, np.nan)      # 量比
    df["vshrink"] = df["amt5_prev"] / df["amt60_prev"].replace(0, np.nan)  # 信号前缩量度
    df["amt_yi"] = df["amount"] / 1e8

    # ATR 波动率
    pc = G("close").shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr_pct"] = (tr.groupby(df["code"], sort=False)
                     .transform(lambda s: s.rolling(14, min_periods=14).mean())
                     / df["close"] * 100)

    # 250 日位置（用 shift(1) 的窗口，避免信号日大阳自指）
    hi = G("high").shift(1).groupby(df["code"], sort=False).rolling(250, min_periods=120).max()
    lo = G("low").shift(1).groupby(df["code"], sort=False).rolling(250, min_periods=120).min()
    df["hi250"] = hi.droplevel(0)
    df["lo250"] = lo.droplevel(0)
    df["pos250"] = (df["close"] - df["lo250"]) / (df["hi250"] - df["lo250"]).replace(0, np.nan)
    df["dd_hi250"] = (df["close"] / df["hi250"] - 1) * 100

    # 前期涨跌幅（截至 T-1）
    for n in (5, 20, 60, 120):
        df[f"ret{n}_p"] = (G("close").shift(1) / G("close").shift(1 + n) - 1) * 100

    # 前期形态计数（截至 T-1）
    for nm, col, win in (("down10", (df["ret"] < 0).astype(float), 10),
                         ("big20", (df["ret"] >= 5).astype(float), 20),
                         ("lim20", (df["ret"] >= 9.7).astype(float), 20)):
        df[nm] = (col.groupby(df["code"], sort=False).shift(1)
                  .groupby(df["code"], sort=False)
                  .transform(lambda s: s.rolling(win, min_periods=win).sum()))

    # 信号日 K 线形态
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["body"] = (df["close"] - df["open"]) / rng                     # 实体占比
    df["close_pos"] = (df["close"] - df["low"]) / rng                 # 收盘强度
    df["up_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / rng
    df["gap_open"] = (df["open"] / df["close_prev"] - 1) * 100

    # KDJ（与 strategy/first_reversal._kdj_last 同参数：n=9, ewm alpha=1/3）
    l9 = G("low").transform(lambda s: s.rolling(9, min_periods=9).min())
    h9 = G("high").transform(lambda s: s.rolling(9, min_periods=9).max())
    rsv = (df["close"] - l9) / (h9 - l9 + 1e-12) * 100
    df["kdj_k"] = rsv.groupby(df["code"], sort=False).transform(
        lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
    df["kdj_d"] = df["kdj_k"].groupby(df["code"], sort=False).transform(
        lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())

    # 市值/换手（⚠ circ_shares/total_shares 是当前快照，非 point-in-time）
    df["circ_mv"] = df["circ_shares"] * df["close"] / 1e8             # 亿元
    df["total_mv"] = df["total_shares"] * df["close"] / 1e8           # 亿元
    df["to_calc"] = df["volume"] / df["circ_shares"].replace(0, np.nan) * 100
    df["amt20"] = rm("amount", 20)                                    # 近20日均成交额（元）

    # 市场状态（全市场口径：主板+创业板）
    df["is_up"] = (df["ret"] > 0).astype(float)
    df["above_ma20"] = (df["close"] > df["ma20"]).astype(float)
    print(f"      个股特征完成  {time.time()-t:.0f}s")

    print("[4/6] 市场状态聚合 ...")
    mkt = df[df["trade_date"] >= LOAD_START].groupby("trade_date").agg(
        mkt_pct=("ret", "mean"),
        mkt_up=("is_up", lambda s: s.mean() * 100),
        mkt_above_ma20=("above_ma20", lambda s: s.mean() * 100),
    )
    # 市场 20 日累计涨跌（用等权全市场日均涨幅累加，作 regime 代理）
    mkt["mkt_ret20"] = mkt["mkt_pct"].rolling(20, min_periods=20).sum()
    df = df.merge(mkt, left_on="trade_date", right_index=True, how="left")

    # ── 信号掩码（A3 / 反转首日主条件）──────────────────────────────────
    print("[5/6] 生成反转首日信号 ...")
    sig = (
        (df["trade_date"] >= SIG_START) & (df["trade_date"] <= SIG_END)
        & df["ma20"].notna() & df["ret"].notna() & df["vr"].notna()
        & (df["close"] > df["ma20"]) & (df["close_prev"] <= df["ma20_prev"])
        & (df["ret"] >= MIN_PCT) & (df["vr"] >= VOL_RATIO_MIN)
        & (df["ret"] < np.where(df["code"].str.startswith(("300", "301")), 19.8, 9.8))
    )
    # 与线上一致：剔 ST/退（⚠ 用今天的名字，含轻微偏差）
    nm = df["name"].fillna("")
    sig &= ~(nm.str.contains("ST") | nm.str.contains("退"))
    # 与线上一致：passes_quality（QUALITY_FILTER）——近20日均成交额 + 总市值区间
    # ⚠ 与 quality_series 一致：total_shares 缺失时**跳过市值项**（优雅降级，不误杀）
    _mc = df["total_mv"] * 1e8
    df["passes_q"] = (
        (df["amt20"] >= 80_000_000)
        & (_mc.isna() | ((_mc >= 3_000_000_000) & (_mc <= 300_000_000_000)))
    )
    n_before = int(sig.sum())
    sig &= df["passes_q"].fillna(False)
    print(f"      质量过滤（amt20>=0.8亿 & 总市值30~3000亿）：{n_before:,} → {int(sig.sum()):,}")
    idx = df.index[sig]
    print(f"      全板信号 {len(idx):,} 条；其中主板 "
          f"{df.loc[idx, 'code'].str.startswith(MAIN_BOARD).sum():,} 条")

    # 当日信号拥挤度
    nsig = df.loc[idx].groupby("trade_date").size().rename("n_signal")
    nsig_mb = (df.loc[idx].assign(
        mb=df.loc[idx, "code"].str.startswith(MAIN_BOARD))
        .groupby("trade_date")["mb"].sum().rename("n_signal_mb"))

    # ── 前向 10 日行情（只为信号行保留，避免 40 列全表爆内存）──────────────
    print("[6/6] 拼接前向行情 + 打标签 ...")
    fwd = {}
    for k in range(1, HOLD_WIN + 1):
        for col, pre in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
            fwd[f"{pre}{k}"] = df.groupby("code", sort=False)[col].shift(-k).loc[idx]
    S = pd.DataFrame(fwd, index=idx)

    FEATS = ["code", "name", "trade_date", "open", "high", "low", "close",
             "ret", "vr", "ext20", "vshrink", "amt_yi", "atr_pct",
             "pos250", "dd_hi250", "ret5_p", "ret20_p", "ret60_p", "ret120_p",
             "down10", "big20", "lim20", "body", "close_pos", "up_shadow",
             "gap_open", "kdj_k", "kdj_d", "circ_mv", "to_calc",
             "ma60_rel", "ma120_rel", "ma20_slope5", "total_mv", "amt20", "passes_q",
             "mkt_pct", "mkt_up", "mkt_above_ma20", "mkt_ret20"]
    out = df.loc[idx, FEATS].copy()
    out = out.join(S)
    out["main_board"] = out["code"].str.startswith(MAIN_BOARD)
    out = out.merge(nsig, left_on="trade_date", right_index=True, how="left")
    out = out.merge(nsig_mb, left_on="trade_date", right_index=True, how="left")

    c0 = out["close"].to_numpy()
    C = np.column_stack([out[f"c{k}"].to_numpy() for k in range(1, HOLD_WIN + 1)])
    H = np.column_stack([out[f"h{k}"].to_numpy() for k in range(1, HOLD_WIN + 1)])
    L = np.column_stack([out[f"l{k}"].to_numpy() for k in range(1, HOLD_WIN + 1)])
    # 「10 日内累计涨幅」主口径 = 收盘可达（T+1..T+10 收盘价相对信号日收盘）
    out["maxg10c"] = (np.fmax.reduce(C, axis=1) / c0 - 1) * 100
    # 盘中口径（含最高价，衡量「有没有摸到过」）
    out["maxg10h"] = (np.fmax.reduce(H, axis=1) / c0 - 1) * 100
    out["ming10l"] = (np.fmin.reduce(L, axis=1) / c0 - 1) * 100
    out["c10ret"] = (out[f"c{HOLD_WIN}"] / c0 - 1) * 100
    out["c5ret"] = (out["c5"] / c0 - 1) * 100
    out["maxg5c"] = (np.fmax.reduce(
        np.column_stack([out[f"c{k}"].to_numpy() for k in range(1, 6)]), axis=1) / c0 - 1) * 100
    out["win10"] = out["maxg10c"] > WIN_THR                 # 主标签
    out["win10h"] = out["maxg10h"] > WIN_THR                # 盘中口径标签
    # 完整 10 日前向（c10 缺失＝退市/数据末端，np.fmax 会静默用不足 10 天的最大值 ⇒ 必须显式剔除）
    out["valid10"] = out[f"c{HOLD_WIN}"].notna()
    # T+1 可买（非一字板）
    out["buyable"] = out["o1"].notna() & ~((out["o1"] == out["h1"]) & (out["o1"] == out["l1"]))
    out["entry"] = out["o1"]                                # T+1 开盘建仓
    out["year"] = out["trade_date"].str[:4]
    out["period"] = np.where(out["trade_date"] <= TRAIN_END, "train", "test")

    out = out.reset_index(drop=True)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    out.to_parquet(CACHE, index=False)
    print(f"      ✅ 落盘 {CACHE}  ({len(out):,} 行 × {out.shape[1]} 列)  "
          f"总耗时 {time.time()-t0:.0f}s")
    _describe(out)
    return out


def _describe(out: pd.DataFrame) -> None:
    mb = out[out["main_board"]]
    print("\n" + "=" * 78)
    print("样本概览")
    print("=" * 78)
    for nm, d in (("全板(主板+创业板)", out), ("仅主板", mb)):
        ok = d["maxg10c"].notna()
        print(f"  {nm:<20} n={len(d):>6,}  有 10 日前向={ok.sum():>6,}  "
              f"信号日 {d['trade_date'].min()} ~ {d['trade_date'].max()}  "
              f"{d['trade_date'].nunique()} 个信号日  日均 {len(d)/max(1,d['trade_date'].nunique()):.1f} 只")
    for nm, d in (("train", mb[mb["period"] == "train"]), ("test", mb[mb["period"] == "test"])):
        if len(d) == 0:
            continue
        print(f"    {nm:<6} n={len(d):>6,}  日期 {d['trade_date'].min()} ~ {d['trade_date'].max()}"
              f"  win10(收盘>{WIN_THR:.0f}%) 命中率 {d['win10'].mean()*100:.1f}%"
              f"  win10h(盘中) {d['win10h'].mean()*100:.1f}%")
    print(f"  可买(剔 T+1 一字板)占比：主板 {mb['buyable'].mean()*100:.1f}%")


def load() -> pd.DataFrame:
    if not os.path.exists(CACHE):
        print("特征表不存在，先跑 build")
        sys.exit(1)
    return pd.read_parquet(CACHE)


# ══════════════════════════════════════════════════════════════════════════
# 统计工具
# ══════════════════════════════════════════════════════════════════════════
def paired_daily(sub: pd.DataFrame, val: str, base: pd.DataFrame) -> tuple:
    """同日配对：子集当日均值 − 当日全部信号均值，返回 (超额 pp, t 值, 有效日数)。

    这是项目一贯的市场中性口径——绝对收益会被当日大盘涨跌污染，
    只有「同一天里，过了滤的票 vs 全部票」的差才归因于过滤器本身。
    """
    a = sub.groupby("trade_date")[val].mean()
    b = base.groupby("trade_date")[val].mean()
    d = (a - b).dropna()
    if len(d) < 5:
        return np.nan, np.nan, len(d)
    t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if d.std(ddof=1) > 0 else np.nan
    return d.mean(), t, len(d)


def by_year(sub: pd.DataFrame, base: pd.DataFrame, val: str) -> str:
    parts = []
    for y in sorted(sub["year"].unique()):
        e, t, n = paired_daily(sub[sub["year"] == y], val, base[base["year"] == y])
        parts.append(f"{y}:{e:+.2f}(t{t:+.1f},n{n})" if np.isfinite(e) else f"{y}:—")
    return "  ".join(parts)


FEATURE_LABELS = {
    "ret": "信号日涨幅%", "vr": "量比(amount/前5日)", "ext20": "偏离MA20%",
    "vshrink": "前5日均额/前60日", "amt_yi": "成交额(亿)", "atr_pct": "ATR14/收盘%",
    "pos250": "250日区间位置", "dd_hi250": "距250日高%", "ret5_p": "前5日涨幅%",
    "ret20_p": "前20日涨幅%", "ret60_p": "前60日涨幅%", "ret120_p": "前120日涨幅%",
    "down10": "前10日下跌天数", "big20": "前20日大阳天数", "lim20": "前20日涨停天数",
    "body": "实体占比", "close_pos": "收盘强度", "up_shadow": "上影占比",
    "gap_open": "当日跳空%", "kdj_k": "KDJ K", "kdj_d": "KDJ D",
    "circ_mv": "流通市值(亿)*", "to_calc": "换手率%*", "total_mv": "总市值(亿)*",
    "ma60_rel": "偏离MA60%", "ma120_rel": "偏离MA120%", "ma20_slope5": "MA20五日斜率%",
    "mkt_pct": "当日全市场均涨幅%", "mkt_up": "当日上涨家数占比%",
    "mkt_above_ma20": "站上MA20占比%", "mkt_ret20": "市场20日累计%",
    "n_signal": "当日信号总数", "n_signal_mb": "当日主板信号数",
}


def mine() -> None:
    d = load()
    d = d[d["main_board"] & d["valid10"] & d["buyable"]].copy()
    tr = d[d["period"] == "train"]
    print("=" * 100)
    print(f"赢家共性挖掘 · 训练期 {tr['trade_date'].min()} ~ {tr['trade_date'].max()}"
          f"  n={len(tr):,}（主板 / 可买 / 有完整 10 日前向）")
    print(f"目标标签 win10 = 「T+1..T+10 收盘价相对信号日收盘的最大累计涨幅 > {WIN_THR:.0f}%」")
    print("=" * 100)
    print(f"  命中率(收盘口径) {tr['win10'].mean()*100:.1f}%   "
          f"命中率(盘中口径) {tr['win10h'].mean()*100:.1f}%   "
          f"maxg10c 中位数 {tr['maxg10c'].median():+.2f}%  均值 {tr['maxg10c'].mean():+.2f}%")
    print(f"  测试期(留出) n={len(d[d['period']=='test']):,}  "
          f"命中率 {d[d['period']=='test']['win10'].mean()*100:.1f}%  ← 本轮不参与选特征")

    # ── 1. 赢家 vs 输家 特征均值对比 ─────────────────────────────────────
    print("\n【A】赢家(win10=True) vs 输家 特征对比（训练期，标准化均值差 = Cohen's d）")
    print(f"  {'特征':<26}{'赢家均值':>11}{'输家均值':>11}{'差':>10}{'d':>8}{'t':>8}   方向")
    rows = []
    w, l = tr[tr["win10"]], tr[~tr["win10"]]
    for f, lab in FEATURE_LABELS.items():
        if f not in tr.columns:
            continue
        a, b = w[f].dropna(), l[f].dropna()
        if len(a) < 100 or len(b) < 100:
            continue
        sp = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1))
                     / (len(a) + len(b) - 2))
        dd = (a.mean() - b.mean()) / sp if sp > 0 else np.nan
        tt = stats.ttest_ind(a, b, equal_var=False).statistic
        rows.append((abs(dd), f, lab, a.mean(), b.mean(), a.mean() - b.mean(), dd, tt))
    for _, f, lab, am, bm, diff, dd, tt in sorted(rows, reverse=True):
        arrow = "赢家更高" if diff > 0 else "赢家更低"
        print(f"  {lab:<26}{am:>11.3f}{bm:>11.3f}{diff:>+10.3f}{dd:>+8.3f}{tt:>+8.1f}   {arrow}")

    # ── 2. 分位数分档：命中率是否单调 ───────────────────────────────────
    print("\n【B】按特征五分位分档 · win10 命中率 & maxg10c 均值 & 同日配对超额（训练期）")
    print("    超额 = 该档当日 maxg10c 均值 − 当日全部信号均值（市场中性，pp）")
    mono = {}
    for f, lab in FEATURE_LABELS.items():
        if f not in tr.columns or tr[f].notna().sum() < 500:
            continue
        try:
            q = pd.qcut(tr[f], 5, duplicates="drop")
        except ValueError:
            continue
        if q.nunique() < 4:
            continue
        g = tr.groupby(q, observed=True).agg(
            n=("win10", "size"), hit=("win10", "mean"),
            mg=("maxg10c", "mean"), c10=("c10ret", "mean"))
        hits = g["hit"].to_numpy() * 100
        # 单调性：Spearman(档位序, 命中率)
        rho = stats.spearmanr(np.arange(len(hits)), hits).statistic
        # 最高档 / 最低档各自相对「当日全部信号」的市场中性超额
        hi_sub = tr.loc[q == g.index[-1]]
        lo_sub = tr.loc[q == g.index[0]]
        ex, tv, nd = paired_daily(hi_sub, "maxg10c", tr)
        ex0, tv0, nd0 = paired_daily(lo_sub, "maxg10c", tr)
        mono[f] = (lab, rho, hits, g["n"].to_numpy())
        flag = "★单调" if abs(rho) >= 0.9 else ("  半单调" if abs(rho) >= 0.7 else "")
        print(f"\n  ▸ {lab}  [{f}]  Spearman ρ={rho:+.2f} {flag}")
        print(f"    {'档':<26}{'n':>7}{'命中率%':>9}{'maxg10c%':>10}{'c10ret%':>9}")
        for iv, r in g.iterrows():
            print(f"    {str(iv):<26}{int(r['n']):>7}{r['hit']*100:>8.1f}%"
                  f"{r['mg']:>9.2f}%{r['c10']:>8.2f}%")
        print(f"    最高档 vs 全体：超额 {ex:+.2f}pp (t={tv:+.1f}, {nd}日)   "
              f"最低档 vs 全体：超额 {ex0:+.2f}pp (t={tv0:+.1f})")

    # ── 3. ρ 排序汇总 ───────────────────────────────────────────────────
    print("\n【C】单调性排序（|Spearman ρ| 越大＝该特征对「10日内涨超10%」越有区分度）")
    print(f"  {'特征':<30}{'ρ':>8}   命中率(低档→高档)")
    for f, (lab, rho, hits, ns) in sorted(mono.items(), key=lambda kv: -abs(kv[1][1])):
        print(f"  {lab:<30}{rho:>+8.2f}   " + " → ".join(f"{h:.1f}" for h in hits))


# ══════════════════════════════════════════════════════════════════════════
# Stage 3/4 占位（mine 之后按需实现）
# ══════════════════════════════════════════════════════════════════════════
def exit_grid() -> None:
    raise SystemExit("exit 阶段见 tools/_mine_reversal_exit.py（下一步实现）")


def backtest() -> None:
    raise SystemExit("backtest 阶段见 tools/_mine_reversal_exit.py（下一步实现）")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "build"
    {"build": build, "mine": mine, "exit": exit_grid, "backtest": backtest}[stage]()
