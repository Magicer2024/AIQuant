# -*- coding: utf-8 -*-
"""
eval_short_top3.py —— 短线 Top3 化 + 选股侧调参网格（近6个月回测，只读）
=====================================================================
背景：用户要求每日短线推荐从 Top4 收缩为 Top3，并通过选股侧参数调整
提升收益率。退出侧已落地方案 A（止损-5% / 启动+8% / 回撤3% / 持10天，
2026-08-20），本实验只动选股侧。

口径（与 tools/eval_short_return_boost.py 完全一致）：
  - 候选池复用 .cache/boost_cand.pkl（v2 全过滤链重建，主板 only）
  - 入场：T+1 开盘 ×1.001 滑点；出场：收盘判定 + 双边费用
  - 主退出 = 方案A；附旧口径 BASE 对照（固定止损-6/止盈+10/持10）
  - 窗口：6个月(2026-02-20起) / 近3个月(2026-05-20起) / 对账窗(2026-07-01起) / 全窗

网格：TopN{3,4} × conf_gate{22,24,26,28} × rank{ext,fs} × pool{base,norsi,ext18}
输出：.cache/top3_results.txt
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from tools.eval_short_return_boost import simulate, Stats, EXCLUDE_BOARDS
import sqlite3
import pickle
from collections import defaultdict

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".cache", "boost_cand.pkl")

SLIP = 0.001
COST = 0.0003 * 2 + 0.001

W6M = "2026-02-20"        # 6个月窗
W3M = "2026-05-20"        # 近3个月窗
WRECENT = "2026-07-01"    # 对账窗（与实盘复盘对齐）
FULL = "2024-01-01"

# 方案A 主口径 + 旧口径对照
CFG_A = dict(mode="trail", stop=0.05, launch=0.08, trail=0.03, hold=10)
CFG_BASE = dict(mode="fixed", stop=0.06, tp=0.10, hold=10)

GATES = (22.0, 24.0, 26.0, 28.0)
TOPNS = (3, 4)


def fmt(r):
    return (f"n={r['n']:>4} 胜率{r['win']:>5.1f}% 均值{r['mean']:>+6.2f}% "
            f"PF{r['pf']:>5.2f} 均盈{r['aw']:>+5.2f} 均亏{-r['al']:>+5.2f} "
            f"年化{r['cap_day']:>+6.1f}%")


def main():
    with open(CACHE, "rb") as f:
        cand, cand_norsi, cand_ext18 = pickle.load(f)
    print(f"候选池: base={len(cand)} norsi={len(cand_norsi)} ext18={len(cand_ext18)}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    conn.close()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))
    idx_cache = {}

    def get_idx(code, d):
        imap = idx_cache.get(code)
        if imap is None:
            rows = data.get(code)
            if not rows:
                return None, None
            imap = ({r["trade_date"]: i for i, r in enumerate(rows)}, rows)
            idx_cache[code] = imap
        m, rows = imap
        return m.get(d), rows

    REPORT = []

    def emit(s=""):
        print(s)
        REPORT.append(s)

    def run_pool(cand_list, topn, gate, rank_by, pool_name):
        per_day = defaultdict(list)
        for d, code, f, ext in cand_list:
            if code.startswith(EXCLUDE_BOARDS):
                continue
            if f >= gate:
                per_day[d].append((code, f, ext))
        st = {wk: {"A": Stats(), "BASE": Stats()}
              for wk in ("w6m", "w3m", "recent", "full")}
        for d in sorted(per_day.keys()):
            if rank_by == "ext":
                picks = sorted(per_day[d], key=lambda x: (x[2], -x[1], x[0]))[:topn]
            else:
                picks = sorted(per_day[d], key=lambda x: (-x[1], x[0]))[:topn]
            for code, _f, _e in picks:
                idx, rows = get_idx(code, d)
                if idx is None or idx + 1 >= len(rows):
                    continue
                entry = rows[idx + 1]["open"] * (1 + SLIP)
                if not entry or entry <= 0:
                    continue
                for tag, cfg in (("A", CFG_A), ("BASE", CFG_BASE)):
                    out = simulate(rows, idx, cfg, entry)
                    if out is None:
                        continue
                    ret, reason, hd = out
                    st["full"][tag].add(ret, reason, hd)
                    if d >= W6M:
                        st["w6m"][tag].add(ret, reason, hd)
                    if d >= W3M:
                        st["w3m"][tag].add(ret, reason, hd)
                    if d >= WRECENT:
                        st["recent"][tag].add(ret, reason, hd)
        return st

    # ── 网格 ──
    pools = {"base": cand, "norsi": cand_norsi, "ext18": cand_ext18}
    all_rows = []     # (label, topn, gate, rank, pool, st)
    for pool_name, cand_list in pools.items():
        for topn in TOPNS:
            for gate in GATES:
                for rank in ("ext", "fs"):
                    label = (f"Top{topn} gate{gate:.0f} "
                             f"{'低扩展' if rank == 'ext' else '融合分'} [{pool_name}]")
                    print(f"运行 {label} …", flush=True)
                    st = run_pool(cand_list, topn, gate, rank, pool_name)
                    all_rows.append((label, topn, gate, rank, pool_name, st))

    emit("=" * 100)
    emit("短线 Top3 化 + 选股侧调参网格 · 退出=方案A(sl-5/启动+8/回撤3/持10)")
    emit("=" * 100)

    # ── 表1：6个月窗（2026-02-20起），按净均值排序 ──
    emit(f"\n[表1] 6个月窗 ({W6M} 起) 方案A口径，按净均值排序：")
    rows6 = [(l, st["w6m"]["A"].row(), st["w6m"]["BASE"].row())
             for l, *_x, st in all_rows]
    rows6 = [(l, a, b) for l, a, b in rows6 if a and a["n"] >= 30]
    rows6.sort(key=lambda x: -x[1]["mean"])
    for l, a, b in rows6:
        bs = f"{b['mean']:>+6.2f}%" if b else "   n/a"
        emit(f"  {l:<42} {fmt(a)} | 旧口径均值{bs}")

    # ── 表2：Top3 vs Top4 同门控对照（base池，方案A，6个月窗）──
    emit(f"\n[表2] Top3 vs Top4 对照（base池 方案A 6个月窗）：")
    emit(f"  {'gate':>5} {'rank':>5} | {'Top4 均值':>10} {'Top3 均值':>10} {'Δ均值':>8} | {'Top3 胜率':>9} {'Top3 PF':>8}")
    by_key = {(t, g, r, p): st for l, t, g, r, p, st in all_rows}
    for gate in GATES:
        for rank in ("ext", "fs"):
            s4 = by_key[(4, gate, rank, "base")]["w6m"]["A"].row()
            s3 = by_key[(3, gate, rank, "base")]["w6m"]["A"].row()
            if s4 and s3:
                emit(f"  {gate:>5.0f} {rank:>5} | {s4['mean']:>+9.2f}% {s3['mean']:>+9.2f}% "
                     f"{s3['mean']-s4['mean']:>+7.2f}% | {s3['win']:>8.1f}% {s3['pf']:>8.2f}")

    # ── 表3：最优5组在其余窗口的稳健性 ──
    emit(f"\n[表3] 6个月窗 Top5 配置的多窗口稳健性（方案A）：")
    top5 = rows6[:5]
    for l, a6, _b in top5:
        st = next(s for ll, *_x, s in all_rows if ll == l)
        emit(f"  ◆ {l}")
        for wk, wkname in (("w3m", "近3个月"), ("recent", "对账窗"), ("full", "全窗")):
            r = st[wk]["A"].row()
            if r:
                emit(f"      {wkname:<6} {fmt(r)}")

    # ── 表4：6个月窗 Top5 的 exit_reason 分布（方案A）──
    emit(f"\n[表4] Top5 配置 exit_reason 分布（6个月窗 方案A）：")
    for l, a6, _b in top5:
        st = next(s for ll, *_x, s in all_rows if ll == l)
        reasons = st["w6m"]["A"].reasons
        total = sum(reasons.values()) or 1
        parts = " ".join(f"{k}:{v}({v/total*100:.0f}%)" for k, v in
                         sorted(reasons.items(), key=lambda x: -x[1]))
        emit(f"  {l:<42} {parts}")

    OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".cache", "top3_results.txt")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    print(f"\n结果已写入 {OUT}")


if __name__ == "__main__":
    main()
