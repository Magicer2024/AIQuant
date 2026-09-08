# -*- coding: utf-8 -*-
"""只读实验（不写库）：个股深度「出场侧」多臂头对头 —— 到底哪套卖点规则值得转正。

背景（docs/stock-deep-perstock-calibration-report.md 实验二已证）：
  面板/K线计划价这条链用的是「固定止盈 2.5×风险 + 持10到期」，84.7% 的交易被时间常数
  了结、止盈 10 天内只有 4.7% 触达 —— 出场规则事实上只有一个时间常数。
  但报告漏了一个已被验证的候选：deep_track 生产跟踪单早已用「移动止盈（启动+8%/回撤3%）」，
  且短线推荐链 2026-08 的同池对比里移动止盈明确优于固定止盈。
  本实验把报告方向 A1/A2/A3 与移动止盈候选放进**同一批交易、同一比较域**头对头。

设计约束：
  · 单一变量：所有臂共用同一批 buy/add 信号与同一入场价（次日开盘），只换出场规则。
  · 比较域限定：只取信号后至少还有 MAX_SCAN_HOLD(20)+1 个交易日的信号 ——
    保证每一臂的每一笔都能走完（无"持仓中"歧义），臂间样本完全同构。
  · 含成本：净值口径统一扣往返成本 COST_RT（默认 0.4%：1万本金单笔约3千，
    买卖佣金各按最低5元≈0.167%/边 + 印花税0.05% + 过户费，四舍五入 0.4%）。
  · 已知遗留（各臂相同，不影响 A/B）：detect_rhythm/up_amp_avg 用全历史统计，含轻微前视。

臂定义（出场引擎全部走生产函数 evaluate_exit_by_prices，不另写模拟器）：
  A0  baseline        固定止盈 2.5rr + 持10（现行面板口径）
  A1a/b/c 持有期扫描   固定止盈不动，持 5 / 15 / 20（报告方向 A1）
  A4  无止盈           只有止损+到期（持10）——检验固定止盈本身有没有贡献
  T1  移动止盈(生产)   启动 +8% / 回撤 3% / 持10（= deep_track 现行口径）
  T2  移动止盈         启动 +8% / 回撤 5% / 持10
  T3  移动止盈         启动 +10% / 回撤 8% / 持10（2026-08-20 之前的旧口径）
  A2a/b 本股可达止盈   take = entry×(1 + k×up_amp_avg)，k=0.33 / 0.5，持10（报告方向 A2）
  A3  止损上限         baseline + stop_cap_pct=10%（报告方向 A3）
  C1  组合             T1 + stop_cap 10%

环境变量：SAMPLE=300 抽样只数  LOOKBACK=250  COST_RT=0.4（往返成本 %）
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategy.stock_deep import _compute, detect_rhythm, recent_advice
from strategy.exit_advisor import evaluate_exit_by_prices

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SAMPLE = int(os.environ.get("SAMPLE", "300"))
LOOKBACK = int(os.environ.get("LOOKBACK", "250"))
COST_RT = float(os.environ.get("COST_RT", "0.4"))
MAX_SCAN_HOLD = 20   # 比较域：信号后至少要有这么多交易日，保证持20臂也能走完

ARMS = [
    # (key, label, kwargs-builder 说明见 run_arm)
    ("A0",  "baseline 固定止盈2.5rr 持10", dict(hold=10, mode="fixed")),
    ("A1a", "固定止盈 持5",                dict(hold=5,  mode="fixed")),
    ("A1b", "固定止盈 持15",               dict(hold=15, mode="fixed")),
    ("A1c", "固定止盈 持20",               dict(hold=20, mode="fixed")),
    ("A4",  "无止盈 只止损+到期 持10",      dict(hold=10, mode="notake")),
    ("T1",  "移动止盈 启动8%/回撤3% 持10",  dict(hold=10, mode="trail", launch=0.08, trail=0.03)),
    ("T2",  "移动止盈 启动8%/回撤5% 持10",  dict(hold=10, mode="trail", launch=0.08, trail=0.05)),
    ("T3",  "移动止盈 启动10%/回撤8% 持10", dict(hold=10, mode="trail", launch=0.10, trail=0.08)),
    ("A2a", "本股可达止盈 0.33×up_amp 持10", dict(hold=10, mode="peramp", k=0.33)),
    ("A2b", "本股可达止盈 0.5×up_amp 持10",  dict(hold=10, mode="peramp", k=0.50)),
    ("A3",  "baseline + 止损上限10%",       dict(hold=10, mode="fixed", cap=0.10)),
    ("C1",  "T1 + 止损上限10%",             dict(hold=10, mode="trail", launch=0.08, trail=0.03, cap=0.10)),
]


def load(conn, code):
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, volume, pct_change FROM daily_price "
        "WHERE code=? AND close IS NOT NULL ORDER BY trade_date ASC",
        (code,),
    ).fetchall()
    if len(rows) < 90:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.drop_duplicates(subset="trade_date", keep="last").set_index("trade_date")
    for c in ("open", "high", "low", "close", "volume", "pct_change"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index().iloc[-LOOKBACK:]


def run_arm(spec, sig, df):
    """按臂定义对单笔信号跑生产出场引擎，返回 (ret_pct, reason, hold_days)。"""
    entry = sig["entry"]
    kw = dict(entry_price=entry, entry_date=sig["date"], df=df,
              stop_loss=sig["stop"], max_hold_days=spec["hold"])
    mode = spec["mode"]
    if mode == "fixed":
        kw["take_profit"] = sig["take"]
    elif mode == "notake":
        kw["take_profit"] = None
    elif mode == "trail":
        kw["take_profit"] = None
        kw["trailing_pct"] = spec["trail"]
        kw["partial_tp"] = spec["launch"]
    elif mode == "peramp":
        if sig["up_amp"] is None:
            return None
        kw["take_profit"] = entry * (1 + spec["k"] * sig["up_amp"] / 100.0)
    if spec.get("cap"):
        kw["stop_cap_pct"] = spec["cap"]
    res = evaluate_exit_by_prices(**kw)
    d = res.get("detail") or {}
    if res.get("status") != "clear" or d.get("current_pnl_pct") is None:
        return None   # 比较域已保证可走完，这里只兜异常
    r = d.get("exit_reason") or ""
    reason = ("止损" if "止损" in r else
              "移动止盈" if "移动止盈" in r else
              "止盈" if "止盈" in r else
              "到期" if "到期" in r else "其他")
    return (float(d["current_pnl_pct"]), reason, int(d.get("hold_days") or 0))


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    codes = [r["code"] for r in conn.execute(
        "SELECT d.code FROM daily_price d JOIN stock_info i ON i.code=d.code "
        "WHERE i.is_active=1 AND d.code NOT LIKE '30%' AND d.code NOT LIKE '68%' "
        "  AND COALESCE(i.name,'') NOT LIKE '%ST%' "
        "GROUP BY d.code HAVING COUNT(*) >= ? ORDER BY d.code",
        (LOOKBACK,),
    ).fetchall()]
    if os.environ.get("DEEP_TRACK") == "1":
        dt = {r["code"] for r in conn.execute("SELECT DISTINCT code FROM deep_track")}
        codes = [c for c in codes if c in dt]
        print(f"样本：deep_track 已跟踪票 {len(codes)} 只（部署队列口径），"
              f"比较域=信号后≥{MAX_SCAN_HOLD}+1 交易日，往返成本 {COST_RT}%")
    else:
        codes = codes[:: max(1, len(codes) // SAMPLE)][:SAMPLE]
        print(f"样本：主板活跃非ST {len(codes)} 只，比较域=信号后≥{MAX_SCAN_HOLD}+1 交易日，"
              f"往返成本 {COST_RT}%")

    sigs = []          # [{date, entry, stop, take, up_amp, code, df_id}]
    dfs = {}
    for ci, code in enumerate(codes):
        df = load(conn, code)
        if df is None:
            continue
        try:
            df = _compute(df)
            rhythm = detect_rhythm(df)
            adv = recent_advice(df, rhythm, days=len(df) - 30)
        except Exception:
            continue
        up_amp = rhythm.get("up_amp_avg")
        date_idx = {d: i for i, d in enumerate(df.index)}
        n = len(df)
        kept = False
        for a in adv:
            if a.get("level") not in ("buy", "add") or a.get("take_profit") is None:
                continue
            i = date_idx.get(a["date"])
            # 比较域：次日入场 + 最长臂(持20)也要能走完 → 需要 i+1+MAX_SCAN_HOLD <= n-1
            if i is None or i + 1 + MAX_SCAN_HOLD > n - 1:
                continue
            entry = float(df.iloc[i + 1]["open"])
            if not np.isfinite(entry) or entry <= 0:
                continue
            sigs.append({"code": code, "date": a["date"], "entry": entry,
                         "stop": a.get("stop_loss"), "take": a.get("take_profit"),
                         "up_amp": up_amp})
            kept = True
        if kept:
            dfs[code] = df
        if (ci + 1) % 60 == 0:
            print(f"  已处理 {ci+1}/{len(codes)}  累计信号 {len(sigs)}")
    conn.close()
    if not sigs:
        print("无可测笔")
        return
    print(f"\n比较域内信号 {len(sigs)} 笔（{len(dfs)} 只票），所有臂同一批交易\n")

    # 每臂对每笔信号只跑一次生产出场引擎，结果按 sigs 顺序对齐缓存（None=该臂不适用）
    results = {}   # key -> (label, aligned list[(ret, reason, hold) | None])
    for key, label, spec in ARMS:
        aligned = [run_arm(spec, s, dfs[s["code"]]) for s in sigs]
        results[key] = (label, aligned)

    hdr = (f"{'臂':4s} {'规则':28s} {'n':>5s} {'胜率':>6s} {'均值':>8s} {'中位':>7s} "
           f"{'P5':>7s} {'净均值':>8s} {'净胜率':>6s} {'持天':>5s} {'日均':>7s} "
           f"{'止损%':>5s} {'止盈%':>5s} {'移止%':>5s} {'到期%':>5s}")
    print(hdr)
    base_rows = results["A0"][1]
    for key, label, spec in ARMS:
        label, aligned = results[key]
        rows = [x for x in aligned if x is not None]
        if not rows:
            print(f"{key:4s} {label:28s}     n=0")
            continue
        r = np.asarray([x[0] for x in rows], dtype=float)
        hd = np.asarray([x[2] for x in rows], dtype=float)
        cnt = {}
        for _, reason, _ in rows:
            cnt[reason] = cnt.get(reason, 0) + 1
        n = len(rows)
        pct = lambda k: cnt.get(k, 0) / n * 100
        net = r - COST_RT
        avg_hold = hd.mean()
        print(f"{key:4s} {label:28s} {n:5d} {float((r > 0).mean()*100):5.1f}% "
              f"{r.mean():+7.3f}% {np.median(r):+6.2f}% {np.percentile(r, 5):+6.2f}% "
              f"{net.mean():+7.3f}% {float((net > 0).mean()*100):5.1f}% "
              f"{avg_hold:5.1f} {net.mean()/max(avg_hold,1e-9):+6.3f}% "
              f"{pct('止损'):4.1f}% {pct('止盈'):4.1f}% {pct('移动止盈'):4.1f}% {pct('到期'):4.1f}%")

    # 逐笔配对差（相对 baseline，同一批交易 → 可做配对比较）
    print("\n=== 逐笔配对：各臂收益 − baseline 收益（同一笔交易，毛口径）===")
    for key, label, spec in ARMS:
        if key == "A0":
            continue
        aligned = results[key][1]
        diffs = [a[0] - b[0] for a, b in zip(aligned, base_rows)
                 if a is not None and b is not None]
        if not diffs:
            continue
        d = np.asarray(diffs, dtype=float)
        nz = d[d != 0]
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 and d.std(ddof=1) > 0 else 0.0
        print(f"  {key:4s} {results[key][0]:28s} 均差 {d.mean():+6.3f}%  "
              f"改动笔占比 {len(nz)/len(d)*100:4.1f}%  改动笔均差 "
              f"{(nz.mean() if len(nz) else 0):+6.3f}%  t={t:+.2f}")

    # A2 专项：止盈触达率有没有从 4.7% 提上来（报告 A2 的成功信号 ≥20%）
    print("\n=== A2 专项：止盈触达率 ===")
    for key in ("A0", "A2a", "A2b"):
        label, aligned = results[key]
        rows = [x for x in aligned if x is not None]
        if rows:
            n = len(rows)
            tp = sum(1 for _, reason, _ in rows if reason == "止盈") / n * 100
            print(f"  {key:4s} {label:28s} 止盈触达 {tp:4.1f}%")

    # A3 专项：被收窄止损扫出的那批，若按 baseline 拿完会怎样（防止止损扫在最低点）
    print("\n=== A3 专项：cap=10% 新增扫出的笔，baseline 口径下的表现 ===")
    hurt, saved = [], []
    for a, b in zip(results["A3"][1], base_rows):
        if a is None or b is None:
            continue
        if a[1] == "止损" and a[0] != b[0]:      # cap 扫出且结果与 baseline 不同
            (saved if b[0] < a[0] else hurt).append((a[0], b[0]))
    for tag, g in (("cap救回(baseline更差)", saved), ("cap误伤(baseline更好)", hurt)):
        if g:
            arr = np.asarray(g, dtype=float)
            print(f"  {tag}: n={len(g)}  cap后均值 {arr[:,0].mean():+.2f}%  baseline均值 {arr[:,1].mean():+.2f}%")
        else:
            print(f"  {tag}: n=0")


if __name__ == "__main__":
    main()
