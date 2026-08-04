"""
tools/eval_fusion_mode.py —— 融合方式对比（只读评估，不写库）
==============================================================
对比三组短线入场口径在**完全相同**的过滤链（趋势闸门 + 质量过滤 + 主板）
和**完全相同**的收益口径（OC / CC 双口径）下的表现：

  A  平均+自适应   weighted_avg × 逐日 BULL/RANGE/BEAR 权重（= 现网行为）
  A2 平均+均衡     weighted_avg × 固定 DEFAULT_WEIGHTS（关掉自适应的对照组）
  B  取最大+自适应 max × 逐日自适应权重（本次改造候选）
  B2 取最大+均衡   max × 固定 DEFAULT_WEIGHTS
  C  纯抄底        bottom_score × 5（2026-04 回测占优的基准）

为什么能直接从库里算：daily_price 已存全市场全历史的 5 个子分
（vol/ma/diverge/bottom/whale_score，0-10 量纲，与 fuse_signals 内部映射一致，
且与 recalc_all_scores 逐股重算的结果同源），故三种融合方式都是这 5 列的
纯代数变换，无需重跑策略函数。

两个报告口径：
  1) 全信号池：各自过自适应阈值（低波动15 / 高波动18）的全部信号
  2) 每日 Top8：与「今日推荐」实际展示口径一致，天然按条数对齐，
     排除"某组信号多所以胜率被摊薄"的干扰，是决策相关口径

收益口径（与 tools/mine_next_day.py 完全一致）：
  nd_cc = 次日 pct_change（信号日收盘买 → 次日收盘）
  nd_oc = 次日 open→close（次日开盘才买得到的现实口径）

用法：
    python tools/eval_fusion_mode.py                      # 全市场窗口（2026-04 起池子才完整）
    python tools/eval_fusion_mode.py --since 2026-04-01
    python tools/eval_fusion_mode.py --since 2024-02-01 --min-codes 0   # 600只长窗口对照
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.db import get_conn, get_market_cap_map
from config.strategy_params import (
    QUALITY_FILTER, SHORT_TREND_GATE,
    DEFAULT_WEIGHTS, BULL_WEIGHTS, RANGE_WEIGHTS, BEAR_WEIGHTS,
    DEFAULT_SIG_THRESHOLD, HIGH_VOL_THRESHOLD, HIGH_VOL_SIG_THRESHOLD,
)
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

pd.set_option("display.width", 200)

SUB_COLS = ["vol_score", "ma_score", "diverge_score", "bottom_score", "whale_score"]


# ─────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────

def load_daily(since: str) -> pd.DataFrame:
    """全市场行情 + 已落库的 5 个子分（剔 ST/退、剔异常涨跌幅）"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            f"""
            SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close,
                   p.volume, COALESCE(p.amount, 0) AS amount, p.pct_change,
                   {', '.join('p.' + c for c in SUB_COLS)},
                   i.name
            FROM daily_price p
            LEFT JOIN stock_info i ON i.code = p.code
            WHERE p.trade_date >= ? AND p.bottom_score IS NOT NULL
            ORDER BY p.code, p.trade_date
            """,
            conn, params=[since],
        )
    df = df[~df["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
    df = df[df["pct_change"].abs() <= 21].copy()
    return df


def load_index_regime() -> pd.DataFrame:
    """逐日复刻 adaptive_weights.detect_market_state 的 regime / 阈值判定。

    口径与线上一致：MA20 近5日变化率 > +0.5% 且站上 MA20 → bull；
    < -0.5% 且跌破 MA20 → bear；否则 range。20日收益率标准差 > 2% → 阈值 18。
    """
    with get_conn() as conn:
        idx = pd.read_sql_query(
            "SELECT trade_date, close, pct_change FROM index_daily "
            "WHERE code='000001' ORDER BY trade_date", conn)
    if idx.empty:
        return pd.DataFrame()
    close = idx["close"].astype(float)
    ma20 = close.rolling(20).mean()
    ma20_prev = ma20.shift(5)
    idx["ma20_slope"] = (ma20 - ma20_prev) / ma20_prev.replace(0, np.nan)
    idx["close_vs_ma20"] = (close - ma20) / ma20.replace(0, np.nan)
    pct = idx["pct_change"].astype(float) if "pct_change" in idx else close.pct_change() * 100
    # detect_market_state 用的是 pct_change 列（百分数）的 std，此处保持一致
    idx["volatility"] = pct.rolling(20).std()

    regime = np.where(
        (idx["ma20_slope"] > 0.005) & (idx["close_vs_ma20"] > 0), "bull",
        np.where((idx["ma20_slope"] < -0.005) & (idx["close_vs_ma20"] < 0), "bear", "range"))
    idx["regime"] = pd.Series(regime).fillna("range")
    idx["threshold"] = np.where(idx["volatility"] > HIGH_VOL_THRESHOLD,
                                HIGH_VOL_SIG_THRESHOLD, DEFAULT_SIG_THRESHOLD)
    return idx[["trade_date", "regime", "threshold", "volatility"]]


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """质量过滤 / 趋势闸门 / 隔日收益，全部 groupby 向量化"""
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

    # 趋势闸门（与 rec_filters.trend_gate_series 同口径）
    ma_n = int(SHORT_TREND_GATE.get("ma", 20))
    lb = int(SHORT_TREND_GATE.get("slope_lookback", 5))
    ma = g["close"].transform(lambda s: s.rolling(ma_n).mean())
    ma_prev = ma.groupby(df["code"]).shift(lb)
    df["gate"] = ((df["close"] > ma) & (ma >= ma_prev)).fillna(False)

    # 主板过滤（小资金无创业板/科创板权限）
    if MAIN_BOARD_ONLY:
        df["main_board"] = ~df["code"].str.startswith(EXCLUDED_BOARD_PREFIXES)
    else:
        df["main_board"] = True

    # 隔日收益双口径
    df["nd_cc"] = g["pct_change"].shift(-1)
    next_open = g["open"].shift(-1)
    next_close = g["close"].shift(-1)
    df["nd_oc"] = (next_close / next_open.replace(0, np.nan) - 1) * 100
    df.loc[df["nd_cc"].abs() > 21, ["nd_cc", "nd_oc"]] = np.nan
    return df


# ─────────────────────────────────────────────
# 三组融合分（纯代数变换，逐行权重）
# ─────────────────────────────────────────────

_REGIME_WEIGHTS = {"bull": BULL_WEIGHTS, "range": RANGE_WEIGHTS, "bear": BEAR_WEIGHTS}


def _weight_matrix(df: pd.DataFrame, adaptive: bool) -> np.ndarray:
    """返回 (n_rows, 5) 的逐行权重矩阵"""
    if not adaptive:
        return np.tile(np.array(DEFAULT_WEIGHTS, dtype=float), (len(df), 1))
    w = np.empty((len(df), 5), dtype=float)
    reg = df["regime"].fillna("range").to_numpy()
    for name, wts in _REGIME_WEIGHTS.items():
        w[reg == name] = np.array(wts, dtype=float)
    return w


def fusion_scores(df: pd.DataFrame, mode: str, adaptive: bool) -> pd.Series:
    """与 strategy.strategies.fuse_signals 完全一致的两种融合公式"""
    s = df[SUB_COLS].fillna(0).to_numpy(dtype=float)      # (n, 5)，0-10 量纲
    w = _weight_matrix(df, adaptive)                       # (n, 5)
    if mode == "weighted_avg":
        total = w.sum(axis=1)
        raw = (s * w).sum(axis=1) / np.where(total > 0, total, np.nan) * s.shape[1]
    elif mode == "max":
        max_w = w.max(axis=1, keepdims=True)
        raw = (s * (w / np.where(max_w > 0, max_w, np.nan)) * 5.0).max(axis=1)
    elif mode == "pure_bottom":
        raw = df["bottom_score"].fillna(0).to_numpy(dtype=float) * 5.0
    else:
        raise ValueError(mode)
    return pd.Series(np.clip(raw, 0, 50), index=df.index)


GROUPS = [
    ("A  平均+自适应(现网)", "weighted_avg", True),
    ("A2 平均+均衡",         "weighted_avg", False),
    ("B  取最大+自适应",     "max",          True),
    ("B2 取最大+均衡",       "max",          False),
    ("C  纯抄底(基准)",      "pure_bottom",  False),
]


# ─────────────────────────────────────────────
# 统计
# ─────────────────────────────────────────────

def stat(sub: pd.DataFrame, days: int) -> dict:
    cc = sub.dropna(subset=["nd_cc"])
    oc = sub.dropna(subset=["nd_oc"])
    n = len(cc)
    if n == 0:
        return None
    return {
        "n": n,
        "per_day": n / max(days, 1),
        "win_cc": (cc["nd_cc"] > 0).mean() * 100,
        "avg_cc": cc["nd_cc"].mean(),
        "win_oc": (oc["nd_oc"] > 0).mean() * 100 if len(oc) else float("nan"),
        "avg_oc": oc["nd_oc"].mean() if len(oc) else float("nan"),
        # 盈亏比：平均盈利 / 平均亏损（OC 口径）
        "pf_oc": (oc.loc[oc["nd_oc"] > 0, "nd_oc"].mean()
                  / abs(oc.loc[oc["nd_oc"] < 0, "nd_oc"].mean())
                  if len(oc) and (oc["nd_oc"] < 0).any() else float("nan")),
    }


def _print_table(title: str, rows: list):
    print("\n" + "=" * 104)
    print(f"  {title}")
    print("=" * 104)
    print(f"  {'组别':<22}{'样本':>8}{'日均':>7}"
          f"{'OC胜率':>9}{'OC均值':>9}{'OC盈亏比':>10}"
          f"{'CC胜率':>9}{'CC均值':>9}")
    print("-" * 104)
    for label, r in rows:
        if r is None:
            print(f"  {label:<22}{'—— 无样本 ——':>20}")
            continue
        print(f"  {label:<22}{r['n']:>8}{r['per_day']:>7.1f}"
              f"{r['win_oc']:>8.2f}%{r['avg_oc']:>+8.3f}%{r['pf_oc']:>10.3f}"
              f"{r['win_cc']:>8.2f}%{r['avg_cc']:>+8.3f}%")
    print("-" * 104)


# ─────────────────────────────────────────────
# 组合口径（项目规范要求的第二重验证：PF / 回撤 / 年化）
# ─────────────────────────────────────────────

_PF_ROWS = [
    ("win_rate",      "胜率",       "pct"),
    ("profit_factor", "盈亏比(PF)", "num"),
    ("avg_win_pct",   "平均盈利",   "pct"),
    ("avg_loss_pct",  "平均亏损",   "pct"),
    ("max_drawdown",  "最大回撤",   "pct"),
    ("annual_return", "年化收益",   "pct"),
    ("total_return",  "总收益",     "pct"),
    ("total_trades",  "交易笔数",   "int"),
]


def _fmt(v, kind: str) -> str:
    if kind == "pct":
        return f"{(v or 0) * 100:+.2f}%"
    if kind == "int":
        return f"{int(v or 0)}"
    return f"{(v or 0):.3f}"


def run_portfolio(df: pd.DataFrame, base: pd.Series,
                  start: str, end: str,
                  take_profit: float, max_hold: int) -> dict:
    """把各组的融合分喂给同一个 VisualBacktestEngine._run_portfolio。

    交易口径（止损/止盈/持仓上限/单日买入/成本/次日开盘成交）各组完全一致，
    唯一变量是入场信号与 fusion_score 排序键，做 apples-to-apples 对比。
    参数与 tools/eval_short_engine.py 保持一致，便于跨脚本横向比较。

    ⚠ 价格面板必须补齐停牌日：_run_portfolio 的收盘快照对「当日无行情」的持仓
    直接 continue，该仓位市值会凭空消失，导致权益曲线出现 20~60% 的瞬时假坑，
    max_drawdown 完全不可用（实测同一组总收益 -31.9% 却报回撤 -71.6%）。
    故这里把每只股票在其自身数据区间内 reindex 到全交易日历并 ffill 价格，
    信号列在补出来的日子填 False（停牌日不可能买入）。
    """
    from backtest.engine import BacktestParams, VisualBacktestEngine, _add_indicators
    from config.strategy_params import OVERSOLD_REBOUND_V4

    d = df.copy()
    d["_dt"] = pd.to_datetime(d["trade_date"])
    d["_base"] = base.to_numpy()

    calendar = pd.DatetimeIndex(sorted(d["_dt"].unique()))
    price_cols = ["open", "high", "low", "close", "volume"]
    keep = price_cols + ["_base", "threshold"] + [f"fs::{lbl}" for lbl, _, _ in GROUPS]

    stock_data, per_code, info_map = {}, {}, {}
    filled = 0
    for code, g in d.groupby("code", sort=False):
        g = g.set_index("_dt").sort_index()
        g = g[~g.index.duplicated(keep="last")]
        info_map[code] = {"code": code, "name": str(g["name"].iloc[-1] or code)}
        span = calendar[(calendar >= g.index[0]) & (calendar <= g.index[-1])]
        gk = g[keep].reindex(span)
        filled += len(span) - len(g)
        gk[price_cols] = gk[price_cols].ffill()
        gk["_base"] = gk["_base"].eq(True)               # 补出来的停牌日不给信号（NaN→False）
        gk["threshold"] = gk["threshold"].ffill()
        per_code[code] = gk
        stock_data[code] = _add_indicators(gk[price_cols])
    print(f"      指标计算完成：{len(stock_data)} 只（补齐停牌/缺口 {filled} 行，"
          f"避免权益曲线假坑）")

    params = BacktestParams(
        start_date=start, end_date=end,
        initial_cash=1_000_000.0,
        max_holdings=5, max_buy_per_day=3,
        buy_timing="next_day_open",
        take_profit_pct=take_profit,
        stop_loss_pct=OVERSOLD_REBOUND_V4["stop_loss"],
        max_hold_days=max_hold,
        exclude_st=True, exclude_kcb=True, exclude_cyb=False,
    )

    out = {}
    for label, _, _ in GROUPS:
        col = f"fs::{label}"
        signals = {}
        for code, g in per_code.items():
            score = g[col]
            signals[code] = (g["_base"].astype(bool) & (score >= g["threshold"])).fillna(False)
            # 引擎按 fusion_score 降序挑候选，故排序键也要换成本组的分
            stock_data[code]["fusion_score"] = score
        engine = VisualBacktestEngine(params)
        r = engine._run_portfolio(stock_data, signals, info_map)
        out[label] = engine._compute_metrics(
            r["equity_curve"], r["trades"], r["final_assets"])
        print(f"      {label} 完成：{out[label]['total_trades']} 笔")
    return out, params


def print_portfolio(metrics: dict, params, window_tag: str = "样本外验证窗") -> None:
    labels = [lbl for lbl, _, _ in GROUPS]
    w = 20
    print("\n" + "=" * (16 + w * len(labels)))
    print(f"  组合口径二次确认（{params.start_date} ~ {params.end_date}，{window_tag}）")
    print("=" * (16 + w * len(labels)))
    print(f"  出场: 固定止损 {params.stop_loss_pct:+.0%} / 止盈 {params.take_profit_pct:+.0%}"
          f" / 最长持仓 {params.max_hold_days}日 / 持仓上限 {params.max_holdings}"
          f" / 单日买入 {params.max_buy_per_day}")
    print("-" * (16 + w * len(labels)))
    header = f"  {'指标':<12}" + "".join(f"{lbl.split()[0]:>{w}}" for lbl in labels)
    print(header)
    print("-" * (16 + w * len(labels)))
    for key, label, kind in _PF_ROWS:
        line = f"  {label:<12}"
        for lbl in labels:
            line += f"{_fmt(metrics[lbl].get(key, 0), kind):>{w}}"
        print(line)
    print("-" * (16 + w * len(labels)))

    a = metrics[labels[0]]
    print("  上线门槛（基准=A 现网；胜率不降 + PF 不降 + 回撤容忍多亏 3pct）:")
    for lbl in labels[1:]:
        r = metrics[lbl]
        wr_ok = r["win_rate"] >= a["win_rate"]
        pf_ok = r["profit_factor"] >= a["profit_factor"]
        # max_drawdown 是负数（drawdown.min()），"不恶化"= 不比基准更负
        # 注意 tools/eval_short_engine.py 里写成 new <= old + 0.03，方向是反的
        dd_ok = r["max_drawdown"] >= a["max_drawdown"] - 0.03
        verdict = "GO" if (wr_ok and pf_ok and dd_ok) else "NO-GO"
        print(f"    {lbl:<22} 胜率 {a['win_rate']*100:.2f}%->{r['win_rate']*100:.2f}% "
              f"{'[PASS]' if wr_ok else '[FAIL]'} | "
              f"PF {a['profit_factor']:.3f}->{r['profit_factor']:.3f} "
              f"{'[PASS]' if pf_ok else '[FAIL]'} | "
              f"回撤 {a['max_drawdown']*100:.2f}%->{r['max_drawdown']*100:.2f}% "
              f"{'[PASS]' if dd_ok else '[FAIL]'}  =>  {verdict}")
    print("=" * (16 + w * len(labels)))


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="融合方式 OC/CC 双口径对比（只读）")
    ap.add_argument("--since", default="2026-04-01",
                    help="起始日期；全市场池子 2026-04 才补全，早于此只有约600只")
    ap.add_argument("--split", default=None,
                    help="训验分割日，默认取窗口后 30%% 为验证窗")
    ap.add_argument("--topn", type=int, default=8, help="每日 TopN 口径（默认8，与今日推荐一致）")
    ap.add_argument("--portfolio", action="store_true",
                    help="额外跑组合口径（PF/回撤/年化）二次确认，只在验证窗内交易")
    ap.add_argument("--pf-all", action="store_true",
                    help="组合口径改为在全窗口交易（笔数更多、统计更稳；本项目权重是"
                         "写死的配置而非拟合参数，故全窗不构成过拟合）")
    ap.add_argument("--take-profit", type=float, default=0.12, help="组合口径固定止盈（各组一致）")
    ap.add_argument("--max-hold", type=int, default=10, help="组合口径最长持仓天数")
    args = ap.parse_args()

    print(f"[1/4] 加载 daily_price（含子分）since={args.since} ...")
    df = load_daily(args.since)
    if df.empty:
        print("[ERR] 无数据")
        return
    print(f"      {len(df)} 行 / {df['code'].nunique()} 只 / "
          f"{df['trade_date'].nunique()} 个交易日")

    print("[2/4] 计算过滤链与隔日收益 ...")
    df = add_features(df)

    print("[3/4] 合并大盘 regime 与自适应阈值 ...")
    reg = load_index_regime()
    if reg.empty:
        print("[WARN] index_daily 无 000001，全部按 range / 阈值15 处理")
        df["regime"] = "range"
        df["threshold"] = DEFAULT_SIG_THRESHOLD
    else:
        df = df.merge(reg, on="trade_date", how="left")
        df["regime"] = df["regime"].fillna("range")
        df["threshold"] = df["threshold"].fillna(DEFAULT_SIG_THRESHOLD)
    rd = df.drop_duplicates("trade_date")[["trade_date", "regime", "threshold"]]
    print("      regime 分布(交易日):", rd["regime"].value_counts().to_dict())
    print("      阈值分布(交易日):", rd["threshold"].value_counts().to_dict())

    # 训验分窗
    dates = sorted(df["trade_date"].unique())
    split = args.split or dates[int(len(dates) * 0.7)]
    print(f"[4/4] 训练窗 {dates[0]}~{split} / 验证窗 {split}~{dates[-1]}")

    # 公共过滤链（三组完全一致）
    base = df["qual"] & df["gate"] & df["main_board"]
    print(f"      过滤链保留 {int(base.sum())}/{len(df)} 行"
          f"（质量+闸门+主板，各组共用）")

    for mode_label, fs_mode, adaptive in GROUPS:
        df[f"fs::{mode_label}"] = fusion_scores(df, fs_mode, adaptive)

    for window, mask in [("训练窗", df["trade_date"] < split),
                         ("验证窗(样本外)", df["trade_date"] >= split)]:
        wdf = df[mask]
        days = wdf["trade_date"].nunique()

        # 口径1：全信号池（各自过自适应阈值）
        rows = []
        for mode_label, _, _ in GROUPS:
            sel = wdf[base[mask] & (wdf[f"fs::{mode_label}"] >= wdf["threshold"])]
            rows.append((mode_label, stat(sel, days)))
        _print_table(f"{window} · 口径1 全信号池（阈值=自适应 15/18，{days} 个交易日）", rows)

        # 口径2：每日 TopN（与今日推荐一致，条数对齐）
        rows = []
        for mode_label, _, _ in GROUPS:
            col = f"fs::{mode_label}"
            pool = wdf[base[mask] & (wdf[col] >= wdf["threshold"])]
            top = (pool.sort_values(col, ascending=False)
                       .groupby("trade_date", sort=False).head(args.topn))
            rows.append((mode_label, stat(top, days)))
        _print_table(f"{window} · 口径2 每日Top{args.topn}（决策相关，条数对齐）", rows)

    # go/no-go：验证窗 Top8 口径，B 组对 A 组
    vdf = df[df["trade_date"] >= split]
    vdays = vdf["trade_date"].nunique()
    res = {}
    for mode_label, _, _ in GROUPS:
        col = f"fs::{mode_label}"
        pool = vdf[base[df["trade_date"] >= split] & (vdf[col] >= vdf["threshold"])]
        top = (pool.sort_values(col, ascending=False)
                   .groupby("trade_date", sort=False).head(args.topn))
        res[mode_label] = stat(top, vdays)

    a = res[GROUPS[0][0]]
    print("\n" + "=" * 104)
    print(f"  go/no-go（验证窗 Top{args.topn} 口径，基准=A 现网；"
          f"门槛：OC胜率 ≥基准+1pp 且 OC均值不降）")
    print("=" * 104)
    if a is None:
        print("  基准组无样本，无法判定")
    else:
        for label, r in res.items():
            if label == GROUPS[0][0] or r is None:
                continue
            wr_ok = r["win_oc"] >= a["win_oc"] + 1.0
            avg_ok = r["avg_oc"] >= a["avg_oc"]
            verdict = "GO" if (wr_ok and avg_ok) else "NO-GO"
            print(f"  {label:<22} OC胜率 {a['win_oc']:.2f}% -> {r['win_oc']:.2f}% "
                  f"({r['win_oc']-a['win_oc']:+.2f}pp) {'[PASS]' if wr_ok else '[FAIL]'}  |  "
                  f"OC均值 {a['avg_oc']:+.3f}% -> {r['avg_oc']:+.3f}% "
                  f"{'[PASS]' if avg_ok else '[FAIL]'}  =>  {verdict}")
    print("=" * 104)
    if not args.portfolio:
        print("  注：本脚本只做隔日 OC/CC 口径。按项目规范，选定候选后还需加 --portfolio"
              " 跑组合口径（PF/回撤/年化）二次确认。")
        return

    pf_start = dates[0] if args.pf_all else split
    print(f"\n[5/5] 组合口径二次确认（交易窗 {pf_start} ~ {dates[-1]}）...")
    metrics, params = run_portfolio(df, base, pf_start, dates[-1],
                                   args.take_profit, args.max_hold)
    print_portfolio(metrics, params, "全窗口" if args.pf_all else "样本外验证窗")


if __name__ == "__main__":
    main()
