# -*- coding: utf-8 -*-
"""eval_short_t1_filter.py —— Top3 精选位的 T1 辅助过滤挖掘（只读）
=================================================================
背景：每日短线 Top3 精选目前只有 fusion≥22 门控 + 低扩展度排序，
无针对 T1（次日）表现的辅助过滤。本脚本在现网口径样本上扫描信号日
特征，寻找能稳定提升 T1 胜率/收益且全周期不劣化的过滤条件。

样本：每日 Top3（g22 + 低扩展排序）= 现网精选位，2024-01 起。
目标：
  T1 收益 = T+1 收盘 / 入场价(T+1 开盘×1.001) - 1
  全周期收益 = 方案A 移动止盈出场（与 simulate 同口径）
窗口：train 2024~2025 / test 2026+ / 全窗 / 6个月(2026-02-20+)
门槛（go）：T1 胜率提升 ≥ 2pp 且全周期均值不劣化，test 窗不反向。
"""
import os
import sys
import sqlite3
import pickle
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
import numpy as np
from tools.eval_short_return_boost import simulate, EXCLUDE_BOARDS

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".cache", "boost_cand.pkl")
SLIP = 0.001
CFG_A = dict(mode="trail", stop=0.05, launch=0.08, trail=0.03, hold=10)
TRAIN_END = "2025-12-31"
W6M = "2026-02-20"


def bucket_stats(feats, key):
    """按特征 key 分桶，输出各桶 T1/全周期统计。"""
    groups = defaultdict(list)
    for f in feats:
        b = f.get(key)
        if b is not None:
            groups[b].append(f)
    return groups


def stat_line(fs_):
    n = len(fs_)
    if n == 0:
        return None
    t1 = [f["ret_t1"] for f in fs_ if f["ret_t1"] is not None]
    win = sum(1 for v in t1 if v > 0) / len(t1) * 100 if t1 else 0
    t1m = sum(t1) / len(t1) * 100 if t1 else 0
    full = [f["ret_full"] for f in fs_ if f["ret_full"] is not None]
    fullm = sum(full) / len(full) * 100 if full else 0
    gw = sum(v for v in full if v > 0)
    gl = -sum(v for v in full if v <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    return dict(n=len(t1), win=win, t1m=t1m, fullm=fullm, pf=pf)


def main():
    with open(CACHE, "rb") as f:
        cand, _n, _e = pickle.load(f)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("加载行情…")
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close, volume "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    print("计算市场宽度…")
    mkt1 = {}
    for r in conn.execute(
            "SELECT trade_date, AVG(pct_change) m FROM daily_price "
            "WHERE trade_date >= '2024-01-01' AND pct_change IS NOT NULL "
            "GROUP BY trade_date"):
        mkt1[r["trade_date"]] = r["m"]
    conn.close()
    mkt_dates = sorted(mkt1)
    mkt5 = {}
    vals = [mkt1[d] for d in mkt_dates]
    for i, d in enumerate(mkt_dates):
        if i >= 5:
            mkt5[d] = sum(vals[i - 5:i]) / 5

    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))

    # ── 逐股预计算特征序列（pandas 向量化）──
    print("预计算个股特征序列…")
    feat_cache = {}   # code -> dict(trade_date -> feat dict)
    n_done = 0
    for code, rows in data.items():
        if len(rows) < 60:
            continue
        try:
            df = pd.DataFrame(rows)
            close = df["close"].astype(float)
            vol = df["volume"].astype(float)
            hi = df["high"].astype(float)
            lo = df["low"].astype(float)
            op = df["open"].astype(float)
            pct = close.pct_change()
            ma5 = close.rolling(5).mean()
            delta = close.diff()
            ag = delta.clip(lower=0).rolling(14).mean()
            al = (-delta).clip(lower=0).rolling(14).mean()
            rsi = 100 - 100 / (1 + ag / al.clip(lower=1e-9))
            vr = vol / vol.shift(1).rolling(5).mean()
            body_top = np.maximum(close.values, op.values)
            body_bot = np.minimum(close.values, op.values)
            upper = (hi.values - body_top) / close.values
            lower = (body_bot - lo.values) / close.values
            # 连跌天数（信号日之前的连续下跌天数，含信号日自身若跌）
            down = (close.diff() < 0).astype(int).values
            streak = np.zeros(len(rows), dtype=int)
            for i in range(1, len(rows)):
                if down[i]:
                    streak[i] = streak[i - 1] + 1
            fc = {}
            dates = df["trade_date"].tolist()
            for i, d in enumerate(dates):
                fc[d] = {
                    "pct": None if pd.isna(pct.iloc[i]) else float(pct.iloc[i]) * 100,
                    "dev5": None if pd.isna(ma5.iloc[i]) else
                            (close.iloc[i] / ma5.iloc[i] - 1) * 100,
                    "rsi": None if pd.isna(rsi.iloc[i]) else float(rsi.iloc[i]),
                    "vr": None if pd.isna(vr.iloc[i]) else float(vr.iloc[i]),
                    "upper": float(upper[i]) * 100,
                    "lower": float(lower[i]) * 100,
                    "bear": bool(close.iloc[i] < op.iloc[i]),
                    "streak": int(streak[i]),
                }
            feat_cache[code] = fc
        except Exception:
            continue
        n_done += 1
        if n_done % 800 == 0:
            print(f"  {n_done} 只…")
    print(f"特征序列 {len(feat_cache)} 只")

    # ── 每日 Top3（现网口径）样本构建 ──
    per_day = defaultdict(list)
    for d, code, f, ext in cand:
        if code.startswith(EXCLUDE_BOARDS):
            continue
        if f >= 22.0:
            per_day[d].append((code, f, ext))

    idx_cache = {}
    feats = []
    for d in sorted(per_day.keys()):
        picks = sorted(per_day[d], key=lambda x: (x[2], -x[1], x[0]))[:3]
        for code, f, ext in picks:
            rows = data.get(code)
            if not rows:
                continue
            imap = idx_cache.get(code)
            if imap is None:
                imap = {r["trade_date"]: i for i, r in enumerate(rows)}
                idx_cache[code] = imap
            idx = imap.get(d)
            if idx is None or idx + 1 >= len(rows):
                continue
            entry = rows[idx + 1]["open"] * (1 + SLIP)
            if not entry or entry <= 0:
                continue
            c1 = rows[idx + 1]["close"]
            ret_t1 = (c1 / entry - 1) if c1 else None
            out = simulate(rows, idx, CFG_A, entry)
            ret_full = out[0] if out else None
            sf = feat_cache.get(code, {}).get(d, {})
            feats.append({
                "date": d, "code": code, "fs": f, "ext": ext,
                "ret_t1": ret_t1, "ret_full": ret_full,
                "pct": sf.get("pct"), "dev5": sf.get("dev5"),
                "rsi": sf.get("rsi"), "vr": sf.get("vr"),
                "upper": sf.get("upper"), "lower": sf.get("lower"),
                "bear": sf.get("bear"), "streak": sf.get("streak"),
                "mkt1": mkt1.get(d), "mkt5": mkt5.get(d),
            })
    print(f"Top3 样本 {len(feats)} 条")

    REPORT = []

    def emit(s=""):
        print(s)
        REPORT.append(s)

    def split(fs_):
        return ([f for f in fs_ if f["date"] <= TRAIN_END],
                [f for f in fs_ if f["date"] > TRAIN_END],
                [f for f in fs_ if f["date"] >= W6M])

    base = stat_line(feats)
    emit("=" * 100)
    emit(f"基准 Top3 g22 低扩展：n={base['n']} T1胜率 {base['win']:.1f}% "
         f"T1均值 {base['t1m']:+.2f}% | 全周期均值 {base['fullm']:+.2f}% PF {base['pf']:.2f}")
    emit("=" * 100)

    def bucket_report(title, key, order=None):
        emit(f"\n[特征] {title}")
        emit(f"  {'桶':<14} {'n':>5} {'T1胜率':>7} {'Δ胜率':>7} {'T1均值':>8} "
             f"{'全周期均值':>9} {'PF':>5}")
        groups = bucket_stats(feats, key)
        keys = order if order else sorted(groups.keys(),
                                           key=lambda k: (isinstance(k, str), k))
        for k in keys:
            if k not in groups:
                continue
            s = stat_line(groups[k])
            if not s or s["n"] < 20:
                continue
            emit(f"  {str(k):<14} {s['n']:>5} {s['win']:>6.1f}% "
                 f"{s['win']-base['win']:>+6.1f}p {s['t1m']:>+7.2f}% "
                 f"{s['fullm']:>+8.2f}% {s['pf']:>5.2f}")

    # ── 特征分桶扫描 ──
    def assign(key, fn):
        for f in feats:
            f[key] = fn(f)

    assign("b_pct", lambda f: None if f["pct"] is None else (
        "<-7" if f["pct"] < -7 else "-7~-5" if f["pct"] < -5 else
        "-5~-3" if f["pct"] < -3 else "-3~-1" if f["pct"] < -1 else
        "-1~0" if f["pct"] < 0 else ">=0"))
    bucket_report("信号日涨跌幅%", "b_pct",
                  ["<-7", "-7~-5", "-5~-3", "-3~-1", "-1~0", ">=0"])

    assign("b_vr", lambda f: None if f["vr"] is None else (
        "<0.6" if f["vr"] < 0.6 else "0.6~1" if f["vr"] < 1.0 else
        "1~1.5" if f["vr"] < 1.5 else "1.5~2.5" if f["vr"] < 2.5 else ">=2.5"))
    bucket_report("信号日量比(vs前5日均量)", "b_vr",
                  ["<0.6", "0.6~1", "1~1.5", "1.5~2.5", ">=2.5"])

    assign("b_lower", lambda f: None if f["lower"] is None else (
        "<1" if f["lower"] < 1 else "1~2" if f["lower"] < 2 else
        "2~4" if f["lower"] < 4 else ">=4"))
    bucket_report("信号日下影线%", "b_lower", ["<1", "1~2", "2~4", ">=4"])

    assign("b_upper", lambda f: None if f["upper"] is None else (
        "<1" if f["upper"] < 1 else "1~2" if f["upper"] < 2 else
        "2~4" if f["upper"] < 4 else ">=4"))
    bucket_report("信号日上影线%", "b_upper", ["<1", "1~2", "2~4", ">=4"])

    assign("b_bear", lambda f: None if f["bear"] is None else
           ("阴线" if f["bear"] else "阳线/平"))
    bucket_report("信号日K线方向", "b_bear", ["阴线", "阳线/平"])

    assign("b_streak", lambda f: None if f["streak"] is None else (
        "0" if f["streak"] == 0 else "1" if f["streak"] == 1 else
        "2" if f["streak"] == 2 else ">=3"))
    bucket_report("信号日连跌天数", "b_streak", ["0", "1", "2", ">=3"])

    assign("b_rsi", lambda f: None if f["rsi"] is None else (
        "<30" if f["rsi"] < 30 else "30~40" if f["rsi"] < 40 else
        "40~50" if f["rsi"] < 50 else "50~60" if f["rsi"] < 60 else ">=60"))
    bucket_report("信号日RSI14", "b_rsi",
                  ["<30", "30~40", "40~50", "50~60", ">=60"])

    assign("b_dev5", lambda f: None if f["dev5"] is None else (
        "<-8" if f["dev5"] < -8 else "-8~-5" if f["dev5"] < -5 else
        "-5~-2" if f["dev5"] < -2 else "-2~0" if f["dev5"] < 0 else ">=0"))
    bucket_report("距MA5偏离%", "b_dev5", ["<-8", "-8~-5", "-5~-2", "-2~0", ">=0"])

    assign("b_ext", lambda f: (
        "<-8" if f["ext"] < -8 else "-8~-5" if f["ext"] < -5 else
        "-5~-2" if f["ext"] < -2 else "-2~0" if f["ext"] < 0 else ">=0"))
    bucket_report("距MA20偏离%(ext)", "b_ext", ["<-8", "-8~-5", "-5~-2", "-2~0", ">=0"])

    assign("b_fs", lambda f: (
        "22~25" if f["fs"] < 25 else "25~28" if f["fs"] < 28 else
        "28~32" if f["fs"] < 32 else ">=32"))
    bucket_report("融合分fs", "b_fs", ["22~25", "25~28", "28~32", ">=32"])

    assign("b_mkt1", lambda f: None if f["mkt1"] is None else (
        "<-1" if f["mkt1"] < -1 else "-1~-0.5" if f["mkt1"] < -0.5 else
        "-0.5~0" if f["mkt1"] < 0 else "0~0.5" if f["mkt1"] < 0.5 else
        "0.5~1" if f["mkt1"] < 1 else ">=1"))
    bucket_report("市场宽度(当日全市场均涨跌%)", "b_mkt1",
                  ["<-1", "-1~-0.5", "-0.5~0", "0~0.5", "0.5~1", ">=1"])

    assign("b_mkt5", lambda f: None if f["mkt5"] is None else (
        "<-0.5" if f["mkt5"] < -0.5 else "-0.5~0" if f["mkt5"] < 0 else
        "0~0.5" if f["mkt5"] < 0.5 else ">=0.5"))
    bucket_report("市场宽度5日均值%", "b_mkt5", ["<-0.5", "-0.5~0", "0~0.5", ">=0.5"])

    # ── 候选条件组合：train/test/6个月三窗验证 ──
    emit("\n" + "=" * 100)
    emit("候选过滤条件三窗验证（go 门槛：全窗 T1胜率 +2pp 且全周期不劣化，test 不反向）")
    emit("=" * 100)
    emit(f"  {'条件':<38} | {'train T1胜率/全均值':^26} | {'test T1胜率/全均值':^26} | {'6个月 T1胜率/全均值':^24}")

    def cond_line(name, fn):
        sub = [f for f in feats if fn(f)]
        tr, te, w6 = split(sub)
        parts = []
        for grp in (tr, te, w6):
            s = stat_line(grp)
            if s and s["n"] >= 20:
                parts.append(f"{s['n']:>4}条 {s['win']:>5.1f}%/{s['fullm']:>+6.2f}%")
            else:
                parts.append(f"{'n<20':^24}")
        emit(f"  {name:<38} | {parts[0]:^26} | {parts[1]:^26} | {parts[2]:^24}")

    b_tr, b_te, b_w6 = split(feats)
    bs = []
    for grp in (b_tr, b_te, b_w6):
        s = stat_line(grp)
        bs.append(f"{s['n']:>4}条 {s['win']:>5.1f}%/{s['fullm']:>+6.2f}%")
    emit(f"  {'基准(无过滤)':<38} | {bs[0]:^26} | {bs[1]:^26} | {bs[2]:^24}")

    cond_line("阴线 only", lambda f: f["bear"] is True)
    cond_line("阳线/平 only", lambda f: f["bear"] is False)
    cond_line("信号日跌幅 < -3%", lambda f: f["pct"] is not None and f["pct"] < -3)
    cond_line("信号日跌幅 < -5%", lambda f: f["pct"] is not None and f["pct"] < -5)
    cond_line("信号日非大跌 pct >= -3%", lambda f: f["pct"] is not None and f["pct"] >= -3)
    cond_line("量比 >= 1.0", lambda f: f["vr"] is not None and f["vr"] >= 1.0)
    cond_line("量比 >= 1.5", lambda f: f["vr"] is not None and f["vr"] >= 1.5)
    cond_line("量比 < 1.0（缩量）", lambda f: f["vr"] is not None and f["vr"] < 1.0)
    cond_line("下影线 >= 2%", lambda f: f["lower"] is not None and f["lower"] >= 2)
    cond_line("上影线 < 2%", lambda f: f["upper"] is not None and f["upper"] < 2)
    cond_line("连跌 >= 2 天", lambda f: f["streak"] is not None and f["streak"] >= 2)
    cond_line("连跌 <= 1 天", lambda f: f["streak"] is not None and f["streak"] <= 1)
    cond_line("RSI 30~50", lambda f: f["rsi"] is not None and 30 <= f["rsi"] <= 50)
    cond_line("RSI < 40", lambda f: f["rsi"] is not None and f["rsi"] < 40)
    cond_line("RSI >= 40", lambda f: f["rsi"] is not None and f["rsi"] >= 40)
    cond_line("距MA5 <= -2%", lambda f: f["dev5"] is not None and f["dev5"] <= -2)
    cond_line("距MA5 > -2%（贴线）", lambda f: f["dev5"] is not None and f["dev5"] > -2)
    cond_line("市场宽度当日 >= 0", lambda f: f["mkt1"] is not None and f["mkt1"] >= 0)
    cond_line("市场宽度当日 < 0", lambda f: f["mkt1"] is not None and f["mkt1"] < 0)
    cond_line("市场宽度5日 >= 0", lambda f: f["mkt5"] is not None and f["mkt5"] >= 0)
    cond_line("市场宽度5日 < 0", lambda f: f["mkt5"] is not None and f["mkt5"] < 0)
    cond_line("组合: 阴线+量比>=1", lambda f: f["bear"] is True and
              f["vr"] is not None and f["vr"] >= 1.0)
    cond_line("组合: 阴线+下影>=2%", lambda f: f["bear"] is True and
              f["lower"] is not None and f["lower"] >= 2)
    cond_line("组合: 非大跌+量比>=1", lambda f: f["pct"] is not None and
              f["pct"] >= -3 and f["vr"] is not None and f["vr"] >= 1.0)
    cond_line("组合: mkt5>=0 + 阴线", lambda f: f["mkt5"] is not None and
              f["mkt5"] >= 0 and f["bear"] is True)

    OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".cache", "t1_filter_results.txt")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    print(f"\n结果已写入 {OUT}")


if __name__ == "__main__":
    main()
