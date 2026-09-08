# -*- coding: utf-8 -*-
"""只读实验（不写库）：个股深度「买卖档位阈值」从全局常量改为按本股分数分布分位标定，胜率/收益有没有动。

背景：_classify 用的是全市场同一把绝对尺子（score>=5 buy、>=2 add、<-2 sell）。
个股节奏只以 ±2 分修正参与决策，而基础分跨度约 -8~+7，等于"描述了规律、仍按全局规则交易"。
本实验把唯一变量换成**阈值的来源**：绝对常量 vs 本股自身分数分布的分位数，其余（_signal_at、
_rhythm_adjust、指标计算、前向收益口径）完全不动。

三种口径：
  V0 baseline      —— score >= 2（= 现行 add∪buy 池），全局常量。
  V1 expanding分位 —— score >= 本股 [暖机, i) 历史分数的 q 分位。只用 i 之前的分数，无未来函数。
  V2 分位+绝对下限 —— score >= 2 且 score >= 本股 q 分位（相对/绝对取严，防低波动票放宽）。

另有两个诊断段：
  跨股公平性 —— 全局绝对阈值下 buy/add 在个股间怎么分布（多少票从不产出信号、前 10% 票占比）。
                这是"该不该按本股标定"的前提。
  信号互换分解 —— 等密度档下把日子分成「两臂都选中 / 仅 baseline 选中 / 仅分位选中」三组，
                分别看前向胜率与收益。只有"分位新增"那组明显好过"分位剔除"那组，标定才值得做。
                （注：每股按分数取前 K 名的 oracle 与 score>=2 选中同一批日子，是恒等式，测不出东西。）

信号密度必须对齐才可比：分位阈值会同时改变命中数量，所以除了整条 q 曲线，
还单独报「命中数最接近 baseline」的那一档 —— 那一档才是回答"胜率有没有动"的数字。
比较域只取分位阈值已可用的日子（每股前 MIN_HIST 天没有本股历史分数，结构上不可能命中）。

口径：次日开盘买 → T+N 收盘卖（OC），与 tools/_eval_deep_buypoints.py 一致。
样本：仅主板（排除 30/68 开头）、非 ST、活跃股，确定性跨板块抽样，每只 ≤250 个交易日。
      【A2】/【C2】用全局阈值扫描作对照臂，用来判断 V2 的增益是"按个股标定"带来的、
      还是仅仅"阈值变严"带来的 —— 只有等命中数下 V2 胜过全局 k，per-stock 才有独立价值。

已知遗留未来函数（两条臂相同，不影响 A/B）：detect_rhythm 的切腿阈值用了全历史中位数。

环境变量：SAMPLE=300 抽样只数  LOOKBACK=250  DEEP_TRACK=1 只跑 deep_track 已跟踪票
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategy.stock_deep import _compute, _signal_at, _rhythm_adjust, _rhythm_at, _range_pos

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
SAMPLE = int(os.environ.get("SAMPLE", "300"))
LOOKBACK = int(os.environ.get("LOOKBACK", "250"))

WARM = 30          # 指标暖机
MIN_HIST = 60      # 分位阈值需要多少条本股历史分数
BASE_ADD = 2       # baseline: score>=2 进 buy/add 池
BASE_BUY = 5       # baseline: score>=5 才算"建议买入"
Q_GRID = [0.60, 0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98]
Q_LOW = [0.02, 0.05, 0.10, 0.15]      # 卖点侧：本股历史分数的低分位
ALL_Q = Q_GRID + Q_LOW


def load(conn, code):
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, volume, pct_change FROM daily_price "
        "WHERE code=? AND close IS NOT NULL ORDER BY trade_date ASC",
        (code,),
    ).fetchall()
    if len(rows) < 70:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.drop_duplicates(subset="trade_date", keep="last").set_index("trade_date")
    for c in ("open", "high", "low", "close", "volume", "pct_change"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index().iloc[-LOOKBACK:]


def collect(conn, codes):
    """每只票逐日算调整后分数 + 前向收益，返回扁平记录列表。"""
    out = []
    skipped = 0
    for ci, code in enumerate(codes):
        df = load(conn, code)
        if df is None:
            skipped += 1
            continue
        try:
            df = _compute(df)
            rhythm = detect_rhythm_safe(df)
            close = df["close"].to_numpy(dtype=float)
            opne = df["open"].to_numpy(dtype=float)
            n = len(df)
        except Exception:
            skipped += 1
            continue
        # 逐日分数（含节奏修正）—— 与生产路径 _signal_plan_at 同一套调用
        scores = np.full(n, np.nan)
        for i in range(WARM, n):
            try:
                s, _, _ = _signal_at(df, i)
                scores[i] = _rhythm_adjust(s, _rhythm_at(rhythm, i), _range_pos(df, i))
            except Exception:
                continue
        # expanding 分位阈值：第 i 天只用 [WARM, i) 的分数
        hist = []
        pct = {q: np.full(n, np.nan) for q in ALL_Q}
        for i in range(WARM, n):
            if len(hist) >= MIN_HIST:
                arr = np.asarray(hist, dtype=float)
                vals = np.percentile(arr, [q * 100 for q in ALL_Q])
                for q, v in zip(ALL_Q, vals):
                    pct[q][i] = v
            if np.isfinite(scores[i]):
                hist.append(float(scores[i]))
        # 前向收益需要 i+N <= n-1
        last_ok = n - 6
        for i in range(WARM, last_ok + 1):
            if not np.isfinite(scores[i]):
                continue
            base = opne[i + 1]
            if not base or base <= 0 or not np.isfinite(base):
                continue
            rec = {"code": code, "i": i, "score": float(scores[i]),
                   "oc1": close[i + 1] / base - 1,
                   "oc3": close[i + 3] / base - 1,
                   "oc5": close[i + 5] / base - 1,
                   "cc1": close[i + 1] / close[i] - 1}
            for q in ALL_Q:
                rec[f"p{q}"] = float(pct[q][i]) if np.isfinite(pct[q][i]) else None
            out.append(rec)
        if (ci + 1) % 60 == 0:
            print(f"  已处理 {ci+1}/{len(codes)}  累计日频记录 {len(out)}")
    print(f"有效 {len(set(r['code'] for r in out))} 只（跳过 {skipped} 只）")
    return out


def detect_rhythm_safe(df):
    from strategy.stock_deep import detect_rhythm
    try:
        return detect_rhythm(df) if "ATR" in df.columns else {}
    except Exception:
        return {}


def stat(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None and np.isfinite(r[key])]
    if not vals:
        return (0.0, 0.0, 0.0, 0)
    a = np.asarray(vals, dtype=float) * 100
    return (float((a > 0).mean() * 100), float(a.mean()), float(np.median(a)), len(a))


def line(tag, rows, total_days):
    s1, s3, s5 = stat(rows, "oc1"), stat(rows, "oc3"), stat(rows, "oc5")
    c1 = stat(rows, "cc1")
    if s1[3] == 0:
        print(f"  {tag:34s}: n=0")
        return s1[3]
    print(f"  {tag:34s}: n={s1[3]:5d}({s1[3]/total_days*100:4.1f}%)  "
          f"T+1 {s1[0]:5.1f}%/{s1[1]:+.3f}%  T+3 {s3[0]:5.1f}%/{s3[1]:+.3f}%  "
          f"T+5 {s5[0]:5.1f}%/{s5[1]:+.3f}%  CC1 {c1[0]:5.1f}%/{c1[1]:+.3f}%")
    return s1[3]


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
        print(f"样本：deep_track 已跟踪票 {len(codes)} 只 × {LOOKBACK} 日（部署队列口径）")
    else:
        codes = codes[:: max(1, len(codes) // SAMPLE)][:SAMPLE]
        print(f"样本：主板活跃非ST {len(codes)} 只 × {LOOKBACK} 日，无未来函数阈值")

    rows = collect(conn, codes)
    conn.close()
    # 只在"分位阈值可用"的日子比较：否则 baseline 含一批 treatment 结构上不可能命中的日子
    raw_total = len(rows)
    q0 = Q_GRID[0]
    rows = [r for r in rows if r.get(f"p{q0}") is not None]
    total = len(rows)
    print(f"\n分位阈值可用日频样本：{total} 股·日（剔除历史不足 {raw_total-total}）")

    base = [r for r in rows if r["score"] >= BASE_ADD]
    base_buy = [r for r in rows if r["score"] >= BASE_BUY]
    hold = [r for r in rows if -2 <= r["score"] < BASE_ADD]
    sell = [r for r in rows if r["score"] < -2]

    print("\n=== 【A】现行全局阈值（baseline）===")
    nb = line(f"score>=2 (add∪buy, 现行池)", base, total)
    line(f"score>=5 (仅 buy)", base_buy, total)
    line(f"-2<=score<2 (hold)", hold, total)
    ns = line(f"score<-2 (sell, 期望前向为负)", sell, total)

    print("\n=== 【A2】全局阈值扫描（对照臂：单纯收紧/放宽绝对尺子）===")
    for k in range(2, 8):
        line(f"score>={k} (全局)", [r for r in rows if r["score"] >= k], total)

    print("\n=== 【B】V1 expanding 分位阈值（无未来函数，整条密度曲线）===")
    curve = []
    for q in Q_GRID:
        g = [r for r in rows if r.get(f"p{q}") is not None and r["score"] >= r[f"p{q}"]]
        s1, s3, s5 = stat(g, "oc1"), stat(g, "oc3"), stat(g, "oc5")
        curve.append((q, s1[3], s1[0], s1[1], s3[0], s3[1], s5[0], s5[1]))
        if s1[3]:
            print(f"  q={q:.2f}: n={s1[3]:5d}({s1[3]/total*100:4.1f}%)  "
                  f"T+1 {s1[0]:5.1f}%/{s1[1]:+.3f}%  T+3 {s3[0]:5.1f}%/{s3[1]:+.3f}%  "
                  f"T+5 {s5[0]:5.1f}%/{s5[1]:+.3f}%")
    print("\n=== 【B2】V2 分位 ∩ 绝对下限 score>=2 ===")
    for q in Q_GRID:
        g = [r for r in rows if r.get(f"p{q}") is not None
             and r["score"] >= r[f"p{q}"] and r["score"] >= BASE_ADD]
        s1, s3, s5 = stat(g, "oc1"), stat(g, "oc3"), stat(g, "oc5")
        if s1[3]:
            print(f"  q={q:.2f}: n={s1[3]:5d}({s1[3]/total*100:4.1f}%)  "
                  f"T+1 {s1[0]:5.1f}%/{s1[1]:+.3f}%  T+3 {s3[0]:5.1f}%/{s3[1]:+.3f}%  "
                  f"T+5 {s5[0]:5.1f}%/{s5[1]:+.3f}%")

    print("\n=== 【C】等密度公平对比（命中数最接近 baseline 的分位档）===")
    def pick(mode):
        best = None
        for q in Q_GRID:
            g = [r for r in rows if r.get(f"p{q}") is not None and r["score"] >= r[f"p{q}"]
                 and (mode == "v1" or r["score"] >= BASE_ADD)]
            if best is None or abs(len(g) - nb) < abs(best[1] - nb):
                best = (q, len(g), g)
        return best
    v1_q = pick("v1")[0]
    for mode, label in (("v1", "V1 纯分位"), ("v2", "V2 分位∩下限")):
        q, cnt, g = pick(mode)
        print(f"  [{label}] 命中 q={q:.2f} n={cnt} (baseline n={nb})")
        line(f"  → {label}", g, total)
        line(f"  → baseline 同池", base, total)

    print("\n=== 【C2】等命中数头对头：V2(分位∩下限) vs 全局阈值 k ===")
    def gk(cnt):
        best = None
        for k in range(2, 9):
            c = sum(1 for r in rows if r["score"] >= k)
            if best is None or abs(c - cnt) < abs(best[1] - cnt):
                best = (k, c)
        return best
    for q in (0.85, 0.92, 0.96):
        v2 = [r for r in rows if r["score"] >= r[f"p{q}"] and r["score"] >= BASE_ADD]
        k, kc = gk(len(v2))
        glob = [r for r in rows if r["score"] >= k]
        print(f"  @ q={q:.2f}：V2 n={len(v2)}  vs  全局 score>={k} n={kc}")
        line(f"    V2 分位∩下限", v2, total)
        line(f"    全局 score>={k}", glob, total)

    print("\n=== 【D1】全局绝对阈值的跨股公平性（'该不该按本股标定'的前提）===")
    per_cnt = {}
    for r in rows:
        per_cnt[r["code"]] = per_cnt.get(r["code"], 0) + (1 if r["score"] >= BASE_ADD else 0)
    stocks = sorted(per_cnt, key=lambda c: -per_cnt[c])
    cnts = np.asarray([per_cnt[c] for c in stocks], dtype=float)
    k10 = max(1, len(stocks) // 10)
    print(f"  {len(stocks)} 只票中 {int((cnts == 0).sum())} 只整个窗口一次 buy/add 都不出")
    print(f"  每股 buy/add 天数：中位 {np.median(cnts):.1f}  均值 {cnts.mean():.1f}  "
          f"最大 {cnts.max():.0f}  最小 {cnts.min():.0f}")
    print(f"  信号最多的前 10% 票（{k10} 只）贡献 {cnts[:k10].sum() / max(1, cnts.sum()) * 100:.1f}% 的买点")

    print(f"\n=== 【D2】信号互换分解 @ V1 等密度档 q={v1_q:.2f} ===")
    inb = lambda r: r["score"] >= BASE_ADD
    inv = lambda r: r["score"] >= r[f"p{v1_q}"]
    line("两臂都选中", [r for r in rows if inb(r) and inv(r)], total)
    line("仅 baseline 选中（分位要剔除）", [r for r in rows if inb(r) and not inv(r)], total)
    line("仅分位选中（分位要新增）", [r for r in rows if inv(r) and not inb(r)], total)

    print("\n=== 【E】卖点侧：全局 score<-2 vs 本股低分位（前向越负越好）===")
    line("baseline sell (score<-2)", sell, total)
    for q in Q_LOW:
        g = [r for r in rows if r.get(f"p{q}") is not None and r["score"] <= r[f"p{q}"]]
        s1, s3, s5 = stat(g, "oc1"), stat(g, "oc3"), stat(g, "oc5")
        if s1[3]:
            print(f"  q={q:.2f}: n={s1[3]:5d}({s1[3]/total*100:4.1f}%)  "
                  f"T+1 {s1[0]:5.1f}%/{s1[1]:+.3f}%  T+3 {s3[0]:5.1f}%/{s3[1]:+.3f}%  "
                  f"T+5 {s5[0]:5.1f}%/{s5[1]:+.3f}%")

    print("\n注：【B】/【B2】的 n 随 q 变化，跨档比较没有意义；结论看【C】的等密度对比与【D2】的互换分解。")


if __name__ == "__main__":
    main()
