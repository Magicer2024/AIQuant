# -*- coding: utf-8 -*-
"""个股深度（deep_track）出场侧「止损宽度规则」头对头回测（只读，不写任何生产数据）。

要回答的问题
────────────
个股深度**早已是 ATR 止损**（`stock_deep._current_signal`：`stop = 收盘 − 2.5×ATR`，
摆动低点兜底），但**没有 floor / cap 夹逼**。实测 `stock_deep_signal` 实际止损宽度：
  中位 10.63% · 均值 11.44% · P5 5.74% · P95 19.83% · 最大 52.26%
  → 19.7% 的候选宽度 >15%（单笔风险敞口远超短线线的 8.45%），2.2% <5%（易被噪音扫）。
本工具只改**这一个变量**：止损宽度规则。入场、止盈、持仓上限、排序、候选池全部不动。

口径（严格复刻生产，不另写模拟器语义）
──────────────────────────────────────
· 候选池 = `deep_tracker.sync_from_scan` 的同一 SQL（board 过滤 + `candidate_order_by`
  排序 + `daily_top_n` 截断），多档 scope 对照：top4（生产口径）/ top20 / 全 buy+add。
· 入场 = `deep_tracker._try_fill`：T+1 起 entry_window_days(5) 个交易日内**最低价触及
  买点**才成交，成交价 = min(sig_entry, 当日开盘)；窗口内未触及 → no_fill（不计入）。
· 出场 = `deep_tracker._run_exit`：建仓日序列 j=1 起（j==1 不判）；收盘 ≤ 止损 → stop_loss；
  收盘 ≥ 止盈(sig_tp) → take_profit；j ≥ max_hold(10) → max_hold_days。
· ATR% 直接用 `stock_deep_signal.atr_pct`（信号日已落库，与生产同源，无未来函数）。

臂定义（仅止损宽度不同；`take` 恒为信号自带 sig_tp）
────────────────────────────────────────────────────
  base      pct = (sig_entry − sig_stop)/sig_entry          生产现值（= 2.5×ATR，无夹逼）
  cap10/12/15   pct = min(pct, cap)                          只加上限
  floor5    pct = max(pct, 0.05)                             只加下限
  c5_15      pct = clamp(pct, 5%, 15%)                       双边夹逼（与短线线同参）
  atr_k*     pct = clamp(k × atr_pct, 5%, 15%)               从 ATR 重算（k=2.0/2.5/3.0）

用法
────
    python tools/eval_deep_atr_stop.py                 # 默认 top4 + top20
    python tools/eval_deep_atr_stop.py --scope all     # 全 buy+add 池（样本最大）
    python tools/eval_deep_atr_stop.py --scope top4
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from core.db import get_conn
from config.strategy_params import DEEP_TRACK
from strategy.deep_tracker import _entry_window, _max_hold
from strategy.stock_deep import candidate_board_filter, candidate_order_by

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

COST_RT = 0.4          # 往返成本 %（与工具 _eval_deep_exit_arms 同口径，仅净口径参考）

# (key, label, mode, 参数)
ARMS = [
    ("base",  "生产现值（2.5×ATR 无夹逼）",        "base",   {}),
    ("cap6",  "上限 6%",                          "cap",    {"cap": 0.06}),
    ("cap8",  "上限 8%",                          "cap",    {"cap": 0.08}),
    ("cap9",  "上限 9%",                          "cap",    {"cap": 0.09}),
    ("cap10", "上限 10%",                         "cap",    {"cap": 0.10}),
    ("cap12", "上限 12%",                         "cap",    {"cap": 0.12}),
    ("cap15", "上限 15%",                         "cap",    {"cap": 0.15}),
    ("floor5", "下限 5%",                         "floor",  {"floor": 0.05}),
    ("c5_10", "夹逼 5%~10%",                      "clamp",  {"floor": 0.05, "cap": 0.10}),
    ("c5_15", "夹逼 5%~15%（短线线同参）",         "clamp",  {"floor": 0.05, "cap": 0.15}),
    ("atr2_c5_15", "ATR 2.0×夹逼5~15%",           "atr",    {"k": 2.0, "floor": 0.05, "cap": 0.15}),
    ("atr2.5_c5_15", "ATR 2.5×夹逼5~15%",         "atr",    {"k": 2.5, "floor": 0.05, "cap": 0.15}),
    ("atr3_c5_15", "ATR 3.0×夹逼5~15%",           "atr",    {"k": 3.0, "floor": 0.05, "cap": 0.15}),
]


# scope → (level 过滤, 每日条数上限)。top4 是生产口径；buy / all 用于取统计功效。
SCOPES = {
    "top4":  (None, "prod"),
    "top20": (None, 20),
    "buy":   (("buy",), None),
    "all":   (None, None),
}


# ───────────────────────── 1. 候选池（复刻 sync_from_scan） ─────────────────────────

def load_population(scopes: list) -> dict:
    """返回 {scope: {(scan_date, code): signal}}；signal 含 entry_price/stop_loss/take_profit/atr_pct。"""
    out = {s: {} for s in scopes}
    top_n_prod = DEEP_TRACK.get("daily_top_n", 10)
    top_n_prod = None if top_n_prod in (None, 0, "") else int(top_n_prod)
    with get_conn() as conn:
        order_by = candidate_order_by(conn)
        board = candidate_board_filter()
        dates = [r["d"] for r in conn.execute(
            "SELECT DISTINCT scan_date AS d FROM stock_deep_signal ORDER BY scan_date").fetchall()]
        for d in dates:
            rows = conn.execute(
                f"SELECT scan_date, code, name, level, entry_price, stop_loss, take_profit, atr_pct "
                f"FROM stock_deep_signal WHERE scan_date = ? {board} ORDER BY {order_by}",
                (d,)).fetchall()
            for scope in scopes:
                levels, lim = SCOPES[scope]
                lim = top_n_prod if lim == "prod" else lim
                sel = rows
                if levels:
                    sel = [r for r in sel if r["level"] in levels]
                if lim is not None:
                    sel = sel[:lim]
                for r in sel:
                    dd = dict(r)
                    if not dd.get("entry_price") or not dd.get("stop_loss"):
                        continue
                    ep, sl = float(dd["entry_price"]), float(dd["stop_loss"])
                    if ep <= 0 or sl <= 0 or sl >= ep:
                        continue
                    dd["pct"] = (ep - sl) / ep
                    out[scope][(d, dd["code"])] = dd
        print(f"扫描日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）· 排序键 candidate_order_by（Q12）")
        for scope in scopes:
            print(f"  候选池 [{scope}] {len(out[scope]):,} 条")
    return out


# ───────────────────────── 2. 行情 ─────────────────────────

def load_prices(codes: list, start: str = "2026-05-01") -> dict:
    """code -> {d,op,hi,lo,cl} numpy（升序）。"""
    out = {}
    with get_conn() as conn:
        ph = ",".join("?" * min(len(codes), 900))
        # 分块查，避免 SQLite 变量上限
        for i in range(0, len(codes), 900):
            chunk = codes[i:i + 900]
            ph = ",".join("?" * len(chunk))
            df = pd.read_sql_query(
                f"SELECT code, trade_date, open, high, low, close FROM daily_price "
                f"WHERE code IN ({ph}) AND trade_date >= ? ORDER BY code, trade_date",
                conn, params=(*chunk, start))
            for code, g in df.groupby("code", sort=False):
                d = pd.to_datetime(g["trade_date"]).values.astype("datetime64[D]").astype(np.int64)
                out[code] = {
                    "d": d,
                    "op": g["open"].to_numpy(dtype=float),
                    "hi": g["high"].to_numpy(dtype=float),
                    "lo": g["low"].to_numpy(dtype=float),
                    "cl": g["close"].to_numpy(dtype=float),
                }
    return out


# ───────────────────────── 3. 单笔回放（复刻 _try_fill + _run_exit） ─────────────────────────

def stop_pct_for(mode: str, kw: dict, sig: dict) -> float:
    """按臂定义算出止损宽度（正数，如 0.1063）。异常宽度回退生产现值。"""
    base = float(sig["pct"])
    if mode == "base":
        return base
    if mode == "cap":
        return min(base, float(kw["cap"]))
    if mode == "floor":
        return max(base, float(kw["floor"]))
    if mode == "clamp":
        lo, hi = float(kw["floor"]), float(kw["cap"])
        return min(max(base, lo), hi)
    if mode == "atr":
        a = sig.get("atr_pct")
        if a is None or not np.isfinite(a) or a <= 0:
            return base
        lo, hi = float(kw["floor"]), float(kw["cap"])
        return min(max(float(kw["k"]) * float(a) / 100.0, lo), hi)
    return base


def sim_one(sig: dict, p: dict, mode: str, kw: dict, entry_window: int, max_hold: int):
    """返回 (ret_pct, exit_reason, hold_tdays, pct_used, filled) 或 None(no_fill/数据不足)。"""
    d0 = np.datetime64(sig["scan_date"]).astype("datetime64[D]").astype(np.int64)
    i0 = int(np.searchsorted(p["d"], d0, side="right"))     # 首个 > scan_date
    entry = float(sig["entry_price"])
    need = entry_window + max_hold + 2
    if i0 >= len(p["d"]):
        return None
    win = min(entry_window, len(p["d"]) - i0)

    # ── 回踩确认入场（_try_fill）──
    fill_idx, exec_entry = None, None
    for k in range(win):
        j = i0 + k
        lo = p["lo"][j]
        if lo is None or np.isnan(lo):
            continue
        if lo <= entry:
            op = p["op"][j] if p["op"][j] and not np.isnan(p["op"][j]) else entry
            exec_entry = round(min(entry, float(op)), 2)
            fill_idx = j
            break
    if fill_idx is None:
        return None                                          # no_fill / 窗口未走完

    pct = stop_pct_for(mode, kw, sig)
    if mode == "base":
        stop = float(sig["stop_loss"])           # 生产现值原样使用（含摆动低点兜底）
    else:
        # 其余臂锚定信号日买点（= 候选池 entry_price），与短线线的锚点口径一致
        stop = round(float(sig["entry_price"]) * (1 - pct), 2)
    take = float(sig["take_profit"]) if sig.get("take_profit") else None

    # ── 出场（_run_exit）：j 从建仓日起算，j==1 不判 ──
    end = min(fill_idx + max_hold, len(p["d"]))
    for j in range(fill_idx, end):
        close = p["cl"][j]
        if close is None or np.isnan(close) or close <= 0:
            break
        step = j - fill_idx + 1
        if step == 1:
            continue
        reason = None
        if close <= stop:
            reason = "stop_loss"
        elif take and close >= take:
            reason = "take_profit"
        elif step >= max_hold:
            reason = "max_hold_days"
        if reason:
            ret = (float(close) - exec_entry) / exec_entry * 100
            return (round(ret, 2), reason, step - 1, round(pct, 4), 1)
    return None                                              # 未走完（观察样本截断）


# ───────────────────────── 4. 统计 ─────────────────────────

def complete_domain(sig: dict, p: dict, entry_window: int, max_hold: int) -> bool:
    """该信号之后是否有足够交易日让**每一臂**都走完（含最大持仓上限）。

    ⚠ 必须做这一步，否则比较被系统性污染：止损紧的臂出场早 → 更容易"已完成"，
    止损宽的臂还在持仓 → 判为未走完被剔除 → 等于把宽止损臂里最差的浮亏样本删掉。

    与 tools/_eval_deep_exit_arms.py 的「比较域限定」同一手法。
    """
    d0 = np.datetime64(sig["scan_date"]).astype("datetime64[D]").astype(np.int64)
    i0 = int(np.searchsorted(p["d"], d0, side="right"))
    return i0 + entry_window + max_hold + 1 <= len(p["d"])


def stats(rows: list) -> dict:
    r = np.asarray([x[0] for x in rows], dtype=float)
    if not len(r):
        return {}
    reasons = {}
    for x in rows:
        reasons[x[1]] = reasons.get(x[1], 0) + 1
    n = len(r)
    wins, losses = r[r > 0], r[r <= 0]
    return {
        "n": n,
        "win": float((r > 0).mean() * 100),
        "avg": float(r.mean()),
        "med": float(np.median(r)),
        "p5": float(np.percentile(r, 5)),
        "worst": float(r.min()),
        "gross_win": float(wins.sum()),
        "gross_loss": float(abs(losses.sum())),
        "pf": float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else 99.0,
        "stop": reasons.get("stop_loss", 0) / n * 100,
        "tp": reasons.get("take_profit", 0) / n * 100,
        "hold": reasons.get("max_hold_days", 0) / n * 100,
        "avg_hold": float(np.mean([x[2] for x in rows])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="top4,top20", help="top4 / top20 / all（逗号分隔）")
    ap.add_argument("--start-price", default="2026-05-01")
    args = ap.parse_args()
    scopes = [s.strip() for s in args.scope.split(",") if s.strip()]

    ew, mh = _entry_window(), _max_hold()
    print(f"出场口径：回踩窗口 {ew} 日 · 持仓上限 {mh} 交易日 · 止盈=信号自带可达位 · 成本 {COST_RT}%\n")

    pop = load_population(scopes)
    codes = sorted({c for scope in scopes for (_, c) in pop[scope]})
    print(f"加载行情 {len(codes):,} 只（{args.start_price} 起）…")
    prices = load_prices(codes, args.start_price)
    print(f"  命中 {len(prices):,} 只\n")

    for scope in scopes:
        all_sigs = [s for s in pop[scope].values() if s["code"] in prices]
        sigs = [s for s in all_sigs if complete_domain(s, prices[s["code"]], ew, mh)]
        dropped = len(all_sigs) - len(sigs)
        print("=" * 118)
        print(f"### scope = {scope}（候选 {len(pop[scope]):,} → 有行情 {len(all_sigs):,}"
              f" → 比较域内 {len(sigs):,}，剔除末端未走完 {dropped:,}）")
        print("=" * 118)

        # 基线先跑，用于对齐逐笔配对
        res = {}
        for key, label, mode, kw in ARMS:
            res[key] = [sim_one(s, prices[s["code"]], mode, kw, ew, mh) for s in sigs]

        base_rows = res["base"]
        filled_base = [x for x in base_rows if x is not None]
        print(f"基线成交 {len(filled_base):,} / 候选 {len(sigs):,}"
              f"（成交率 {len(filled_base)/max(len(sigs),1)*100:.1f}%；未成交=回踩未触及或走势未走完）")
        print(f"\n{'臂':<14}{'n':>6}{'胜率':>8}{'均值':>9}{'中位':>8}{'PF':>7}"
              f"{'止损%':>7}{'止盈%':>7}{'到期%':>7}{'持天':>6}{'P5':>8}{'最差':>8}{'宽度中位':>9}")

        width_med = {}
        for key, label, mode, kw in ARMS:
            rows = [x for x in res[key] if x is not None]
            st = stats(rows)
            if not st:
                print(f"{key:<14}{'n=0':>6}")
                continue
            width_med[key] = float(np.median([x[3] for x in rows]))
            print(f"{key:<14}{st['n']:>6}{st['win']:>7.1f}%{st['avg']:>+8.2f}%{st['med']:>+7.2f}%"
                  f"{st['pf']:>7.2f}{st['stop']:>6.1f}%{st['tp']:>6.1f}%{st['hold']:>6.1f}%"
                  f"{st['avg_hold']:>6.1f}{st['p5']:>+7.2f}%{st['worst']:>+7.2f}%{width_med[key]*100:>8.2f}%")

        # 逐笔配对（同一批候选，只比双方都成交的笔）
        print(f"\n--- 逐笔配对（vs base，毛口径；只含两臂都成交的笔）---")
        for key, label, mode, kw in ARMS:
            if key == "base":
                continue
            pairs = [(a[0], b[0]) for a, b in zip(res[key], base_rows)
                     if a is not None and b is not None]
            if not pairs:
                continue
            d = np.asarray([a - b for a, b in pairs], dtype=float)
            nz = d[d != 0]
            t = (d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))
                 if len(d) > 1 and d.std(ddof=1) > 0 else 0.0)
            print(f"  {key:<14}{label:<24} n={len(pairs):<5} 均差 {d.mean():+6.3f}%  "
                  f"被改动笔 {len(nz)/len(d)*100:5.1f}%  改动笔均差 "
                  f"{(nz.mean() if len(nz) else 0):+6.3f}%  t={t:+.2f}")

        # 分月稳定性（只对上限臂做，看是否单月撑起）
        print(f"\n--- 分月稳定性（均值/笔，vs base）---")
        months = sorted({s["scan_date"][:7] for s in sigs})
        print(f"{'月份':<9}{'base':>9}{'cap8':>9}{'cap10':>9}{'cap15':>9}{'atr3':>9}{'样本':>6}{'谁更好':>9}")
        win_cnt = {}
        for m in months:
            sel = [i for i, s in enumerate(sigs) if s["scan_date"][:7] == m]
            if len(sel) < 8:
                continue

            def avg_of(key):
                v = [res[key][i][0] for i in sel if res[key][i] is not None]
                return float(np.mean(v)) if len(v) >= 5 else None

            vals = {k: avg_of(k) for k in ("base", "cap8", "cap10", "cap15", "atr3_c5_15")}
            b = vals["base"]
            if b is None:
                continue
            nmon = max((sum(1 for i in sel if res["base"][i] is not None)), 0)
            rivals = {k: v for k, v in vals.items() if k != "base" and v is not None}
            best = max(rivals, key=lambda k: rivals[k]) if rivals else "-"
            win_cnt[best] = win_cnt.get(best, 0) + 1

            def f(v):
                return "    --" if v is None else f"{v:+.2f}%"

            print(f"{m:<9}{b:>+8.2f}%{f(vals['cap8']):>9}{f(vals['cap10']):>9}"
                  f"{f(vals['cap15']):>9}{f(vals['atr3_c5_15']):>9}{nmon:>6}{best:>9}")
        if win_cnt:
            print(f"  分月最优计数: {win_cnt}")
        print()


if __name__ == "__main__":
    main()
