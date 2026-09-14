"""短线「抄底融合线」收窄排除条件评估工具（只读回测，不改任何生产数据）

背景
----
`short_observe_bottom=1` 让 `short_observe_bottom_sql()` 返回 `strategy != '短线融合'`，
在「今日推荐 / 出场跟踪 / 复盘入库」三处统一排除。而短线信号 100% 是 `strategy='短线融合'`
（SHORT_ENGINE=pure_bottom），于是整条短线链路被排空（2026-09-10 实测 short=0）。

本工具回答：**把"排除全部"收窄成"排除最差的那一档"时，阈值应该定在哪里**——
以平均收益为第一目标（用户明确：收益率优先于胜率）。

口径保证
--------
出场状态机**直接 import 线上实现** `core.outcome_tracker._short_exit_sim`，
入场用线上「回踩确认」分支的同款逻辑（T+1 起 5 个交易日内 low 触及买点才成交，
成交价 = min(买点, 当日开盘)；窗口内未回踩 → no_fill）。不重写、不复制数学，
避免"回测与线上不一致"的经典陷阱。

候选池 = 生产链路（基础过滤 + fusion≥short_conf_gate + T1 过滤 + 弱市闸门）
**但去掉 observation-bottom 排除**，即"如果恢复短线，会被评估的全部信号"。

用法
----
    python tools/eval_short_observe_rule.py --start 2023-01-01 --rebuild   # 重建缓存
    python tools/eval_short_observe_rule.py --report                       # 出报告
    python tools/eval_short_observe_rule.py --rule "bottom_score>=7" --oos # 单规则样本外
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn                                    # noqa: E402
from core.outcome_tracker import _short_exit_sim, short_t1_filter_sql, short_market_gate_sql  # noqa: E402
from config.strategy_params import get_param                    # noqa: E402
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES  # noqa: E402
from strategy.exit_advisor import get_max_hold                  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
CACHE_FILE = os.path.join(CACHE_DIR, "_short_observe_sim.pkl")

# 参与网格搜索的特征列（全部是信号日当日可得，无未来函数）
FEATURES = ["fusion_score", "bottom_score", "diverge_score", "vol_score",
            "ma_score", "whale_score", "pct_above_ma20", "rr_ratio"]


# ────────────────────────────── 1. 候选池 ──────────────────────────────

def build_population(start: str, end: str) -> list[dict]:
    """生产链路候选池（基础 + fusion 门控 + T1 + 弱市闸门），但**不含**观察线排除。"""
    board_filter = ""
    if MAIN_BOARD_ONLY:
        board_filter = "".join(
            f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
    gate = float(get_param("short_conf_gate"))
    with get_conn() as conn:
        t1_cond, t1_params = short_t1_filter_sql(conn, start)
        mk_cond, mk_params = short_market_gate_sql(conn, start)
        rows = conn.execute(f"""
            SELECT s.id, s.scan_date, s.code, s.name,
                   s.buy_price, s.stop_loss, s.take_profit, s.fusion_score,
                   s.bottom_score, s.diverge_score, s.vol_score,
                   s.ma_score, s.whale_score, s.pct_above_ma20
            FROM stock_signal s
            WHERE s.scan_date >= ? AND s.scan_date <= ?
              AND s.strategy = '短线融合'
              AND s.buy_price IS NOT NULL AND s.buy_price > 0
              AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
              {board_filter}
              AND s.fusion_score >= ?
              AND {t1_cond} AND {mk_cond}
            ORDER BY s.scan_date, s.code
        """, (start, end, gate, *t1_params, *mk_params)).fetchall()
    return [dict(r) for r in rows]


# ────────────────────────────── 2. 行情加载 ──────────────────────────────

def load_prices(start: str) -> dict[str, dict[str, np.ndarray]]:
    """code -> {dates(int), open, close, high, low}，均为 numpy 数组（按日期升序）。"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT code, trade_date, open, close, high, low FROM daily_price "
            "WHERE trade_date >= ? ORDER BY code, trade_date",
            conn, params=(start,))
    if df.empty:
        return {}
    df["d"] = pd.to_datetime(df["trade_date"]).values.astype("datetime64[D]").astype(np.int64)
    out: dict[str, dict[str, np.ndarray]] = {}
    for code, g in df.groupby("code", sort=False):
        out[code] = {
            "d": g["d"].to_numpy(),
            "open": g["open"].to_numpy(dtype=float),
            "close": g["close"].to_numpy(dtype=float),
            "high": g["high"].to_numpy(dtype=float),
            "low": g["low"].to_numpy(dtype=float),
        }
    return out


# ────────────────────────────── 3. 逐信号回放 ──────────────────────────────

def simulate(signals: list[dict], prices: dict) -> pd.DataFrame:
    """按线上口径回放每个信号，返回带 ret / filled / exit_reason 的 DataFrame。"""
    trail_pct = get_param("short_trailing_pct")
    max_hold = get_max_hold("short") or 10
    entry_window = max(1, int(get_param("short_entry_window_days")))
    # 需要的前向行数：回踩窗口 + 持仓上限 + 缓冲
    need = entry_window + max_hold + 2
    to_days = np.datetime64

    recs = []
    for s in signals:
        code = s["code"]
        p = prices.get(code)
        entry = s.get("buy_price")
        rec = {
            "id": s["id"], "scan_date": s["scan_date"], "code": code, "name": s.get("name"),
            "fusion_score": s.get("fusion_score"), "bottom_score": s.get("bottom_score"),
            "diverge_score": s.get("diverge_score"), "vol_score": s.get("vol_score"),
            "ma_score": s.get("ma_score"), "whale_score": s.get("whale_score"),
            "pct_above_ma20": s.get("pct_above_ma20"),
            "filled": 0, "final": 0, "ret": None, "exit_reason": None,
        }
        # 风险收益比（信号日可得，无未来函数）
        stop, tp = s.get("stop_loss"), s.get("take_profit")
        rec["rr_ratio"] = (round((tp - entry) / (entry - stop), 3)
                           if entry and stop and tp and entry > stop > 0 else None)
        if p is None or not entry or entry <= 0:
            recs.append(rec)
            continue
        d0 = np.datetime64(s["scan_date"]).astype("datetime64[D]").astype(np.int64)
        i0 = int(np.searchsorted(p["d"], d0, side="right"))   # 首个 > scan_date 的行
        if i0 >= len(p["d"]):
            recs.append(rec)
            continue
        sl = slice(i0, min(i0 + need, len(p["d"])))
        dates, op, cl, hi, lo = (p["d"][sl], p["open"][sl], p["close"][sl],
                                 p["high"][sl], p["low"][sl])

        # ── 回踩确认入场（与 _evaluate_short 的 pullback 分支同逻辑）──
        fill_idx = None
        exec_entry = None
        for idx in range(min(entry_window, len(dates))):
            if lo[idx] is None or np.isnan(lo[idx]) or entry <= 0:
                continue
            if lo[idx] <= entry:
                open_ = op[idx] if op[idx] and not np.isnan(op[idx]) else entry
                exec_entry = round(min(float(entry), float(open_)), 2)
                fill_idx = idx
                break
        if fill_idx is None:
            if len(dates) >= entry_window:
                rec["exit_reason"] = "no_fill"
                rec["final"] = 1
            recs.append(rec)
            continue
        rec["filled"] = 1

        # ── 出场状态机（线上同一函数）──
        hp = []
        end = min(fill_idx + max_hold, len(dates))
        for k in range(fill_idx, end):
            hp.append({
                "trade_date": str(np.datetime64(int(dates[k]), "D")),
                "close": None if np.isnan(cl[k]) else float(cl[k]),
                "high": None if np.isnan(hi[k]) else float(hi[k]),
            })
        reason, _ed, ret, _hs, _lc, done = _short_exit_sim(
            hp, exec_entry, stop, tp, trail_pct, max_hold)
        rec["exit_reason"] = reason
        rec["final"] = 1 if done else 0
        rec["ret"] = ret

        # ── 对照口径：旧「次日开盘无条件建仓」（用于判断入场改造是否改变了最优排序方向）──
        open0 = op[0] if op[0] and not np.isnan(op[0]) else cl[0]
        rec["filled_open"] = 1
        if open0 and not np.isnan(open0) and open0 > 0:
            hp2 = []
            for k in range(min(max_hold, len(dates))):
                hp2.append({
                    "trade_date": str(np.datetime64(int(dates[k]), "D")),
                    "close": None if np.isnan(cl[k]) else float(cl[k]),
                    "high": None if np.isnan(hi[k]) else float(hi[k]),
                })
            r2, _d2, ret2, _h2, _l2, done2 = _short_exit_sim(
                hp2, float(open0), stop, tp, trail_pct, max_hold)
            rec["exit_reason_open"] = r2
            rec["final_open"] = 1 if done2 else 0
            rec["ret_open"] = ret2
        else:
            rec["exit_reason_open"] = None
            rec["final_open"] = 0
            rec["ret_open"] = None
        recs.append(rec)

    df = pd.DataFrame(recs)
    df["year"] = df["scan_date"].str.slice(0, 4)
    df["half"] = df["scan_date"].str.slice(0, 4) + "H" + np.where(
        df["scan_date"].str.slice(5, 7).astype(int) <= 6, "1", "2")
    return df


# ────────────────────────────── 4. 指标 ──────────────────────────────

def metrics(sub: pd.DataFrame, label: str = "") -> dict:
    """收益率优先的指标组。

    avg_ret      : 入场笔的平均收益（未成交不计）—— 单笔质量
    avg_per_sig  : 每个信号的平均贡献（未成交记 0）—— 资金效率（占名额但空仓）
    pf           : 盈亏比 = 总盈利 / |总亏损|
    """
    filled = sub[sub["filled"] == 1]
    ev = filled[filled["final"] == 1]
    rets = ev["ret"].dropna()
    n_sig = int(len(sub))
    n_fill = int(len(ev))
    if n_fill == 0:
        return {"label": label, "n_sig": n_sig, "n_fill": n_fill, "fill_rate": 0.0,
                "avg_ret": None, "median_ret": None, "win_rate": None, "pf": None,
                "avg_per_sig": None, "stop_rate": None}
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    gross_win = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    return {
        "label": label,
        "n_sig": n_sig,
        "n_fill": n_fill,
        "fill_rate": round(n_fill / n_sig * 100, 1),
        "avg_ret": round(float(rets.mean()), 2),
        "median_ret": round(float(rets.median()), 2),
        "win_rate": round(float(len(wins) / len(rets) * 100), 1),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_per_sig": round(float(rets.sum()) / n_sig, 2),
        "stop_rate": round(float((ev["exit_reason"] == "stop_loss").sum()) / n_fill * 100, 1),
    }


def show(rows: list[dict], title: str, key: str = "avg_ret"):
    print(f"\n=== {title} ===")
    print(f"{'规则/分组':<28}{'信号':>7}{'成交':>7}{'成交率':>8}{'均收益':>9}"
          f"{'中位':>8}{'胜率':>8}{'盈亏比':>8}{'每信号':>9}{'止损率':>8}")
    for r in rows:
        f = lambda v, s="{:.2f}": (s.format(v) if v is not None else "--")  # noqa: E731
        print(f"{r['label']:<28}{r['n_sig']:>7}{r['n_fill']:>7}"
              f"{f(r['fill_rate'], '{:.1f}%'):>8}{f(r['avg_ret'], '{:+.2f}%'):>9}"
              f"{f(r['median_ret'], '{:+.2f}%'):>8}{f(r['win_rate'], '{:.1f}%'):>8}"
              f"{f(r['pf']):>8}{f(r['avg_per_sig'], '{:+.2f}%'):>9}"
              f"{f(r['stop_rate'], '{:.1f}%'):>8}")


def quantile_grid(df: pd.DataFrame, col: str, qs=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)) -> list[float]:
    v = pd.to_numeric(df[col], errors="coerce").dropna()
    if v.empty:
        return []
    return sorted({round(float(v.quantile(q)), 3) for q in qs})


def portfolio(df: pd.DataFrame, expr: str, top_n: int = 3, asc: bool = True,
              label: str = "") -> dict:
    """按**线上真实选股口径**评估：每日从（规则过滤后的）候选池取 top_n。

    排序与 `short_order_clause()` 一致：优先级（本池全为「短线融合」=2）→
    pct_above_ma20（方向由 short_ext_sort_desc 控制，默认 ASC）→ fusion_score DESC。
    这才是在替代"观察线排除"之后真正会成交的那批票。
    """
    pool = df.query(expr) if expr else df
    if pool.empty:
        return {"label": label, "n_sig": 0, "n_fill": 0, "days": 0, "per_day": 0,
                "fill_rate": None, "avg_ret": None, "median_ret": None,
                "win_rate": None, "pf": None, "avg_per_sig": None, "stop_rate": None,
                "cum_ret": None}
    pool = pool[pool["final"] == 1].copy()
    pool["_ext"] = pd.to_numeric(pool["pct_above_ma20"], errors="coerce").fillna(0)
    pool = pool.sort_values(["_ext", "fusion_score"], ascending=[asc, False])
    picked = pool.groupby("scan_date", sort=True).head(top_n)
    m = metrics(picked, label)
    days = int(picked["scan_date"].nunique())
    m.update(days=days, per_day=round(len(picked) / days, 2) if days else 0,
             cum_ret=round(float(picked.loc[picked["filled"] == 1, "ret"].dropna().sum()), 1))
    m["n_sig"] = int(len(pool))
    return m


def show_portfolio(rows: list[dict], title: str):
    print(f"\n=== {title} ===")
    print(f"{'规则':<30}{'候选':>7}{'成交':>7}{'日均':>6}"
          f"{'均收益':>9}{'中位':>8}{'胜率':>8}{'盈亏比':>8}{'累计':>9}{'止损率':>8}")
    for r in rows:
        f = lambda v, s="{:.2f}": (s.format(v) if v is not None else "--")  # noqa: E731
        print(f"{r['label']:<30}{r['n_sig']:>7}{(r.get('n_fill') or 0):>7}"
              f"{f(r.get('per_day'), '{:.1f}'):>6}"
              f"{f(r['avg_ret'], '{:+.2f}%'):>9}{f(r['median_ret'], '{:+.2f}%'):>8}"
              f"{f(r['win_rate'], '{:.1f}%'):>8}{f(r['pf']):>8}"
              f"{f(r.get('cum_ret'), '{:+.0f}%'):>9}{f(r['stop_rate'], '{:.1f}%'):>8}")


# ────────────────────────────── 5. 主流程 ──────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--is-end", default="2025-06-30", help="样本内结束日（之后为样本外）")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--rule", default=None, help='如 "bottom_score>=7 and rr_ratio>=1.5"')
    ap.add_argument("--portfolio", action="store_true",
                    help="按线上真实选股口径（每日 top-N）评估，而非全池平均")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--grid", action="store_true",
                    help="阈值细网格，同时要求样本内/样本外为正")
    ap.add_argument("--entry", choices=("pullback", "open"), default="pullback",
                    help="入场口径：pullback=回踩确认（线上）/ open=次日开盘（旧，对照）")
    ap.add_argument("--oos", action="store_true")
    args = ap.parse_args()

    if args.rebuild or not os.path.exists(CACHE_FILE):
        print(f"[1/3] 构建候选池 {args.start} ~ {args.end}（生产链路 - 观察线排除）…")
        sigs = build_population(args.start, args.end)
        print(f"      候选信号 {len(sigs):,} 条")
        print("[2/3] 加载行情并逐信号回放（线上同口径状态机）…")
        prices = load_prices(args.start)
        print(f"      覆盖 {len(prices):,} 只股票")
        df = simulate(sigs, prices)
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "wb") as fh:
            pickle.dump(df, fh)
        print(f"[3/3] 缓存写入 {CACHE_FILE}")
    else:
        with open(CACHE_FILE, "rb") as fh:
            df = pickle.load(fh)
        if "ret_open" not in df.columns:
            print("缓存缺少对照口径列（ret_open），自动重建…")
            sigs = build_population(args.start, args.end)
            prices = load_prices(args.start)
            df = simulate(sigs, prices)
            with open(CACHE_FILE, "wb") as fh:
                pickle.dump(df, fh)
        print(f"读取缓存 {CACHE_FILE}（{len(df):,} 条）—— 需要重建请加 --rebuild")

    # 口径切换：pullback=回踩确认（线上现行）；open=旧「次日开盘无条件建仓」对照
    if args.entry == "open":
        # 先丢弃现口径列，再重命名（否则产生重复列名，pandas 会报 duplicate labels）
        df = df.drop(columns=["ret", "filled", "final", "exit_reason"], errors="ignore")
        df = df.rename(columns={"ret_open": "ret", "filled_open": "filled",
                                "final_open": "final",
                                "exit_reason_open": "exit_reason"})
        print(">>> 对照口径：旧「次日开盘无条件建仓」（short_pullback_entry=0）")
    else:
        print(">>> 线上口径：回踩确认入场（short_pullback_entry=1）")

    # 仅统计终态：`final==1` 由 _short_exit_sim 保证（已触发出场，或前向窗完整走完）。
    # ⚠ 不要再额外按"今天-N 天"截断——那会把最近一段（出场已触发、结果完整）的信号
    # 整体剔除，等于按时间做有偏抽样（2026-09-10 曾因此把最差的最近一个月剔掉）。
    ev = df[df["final"] == 1].copy()
    print(f"终态样本 {len(ev):,} / {len(df):,}"
          f"（覆盖 {ev['scan_date'].min()} ~ {ev['scan_date'].max()}，"
          f"{ev['scan_date'].nunique()} 个有效交易日）")

    is_df = ev[ev["scan_date"] <= args.is_end]
    oos_df = ev[ev["scan_date"] > args.is_end]

    if args.grid:
        # 细网格：只找"样本内与样本外同时为正"的稳健阈值（避免挑到单窗口运气）
        combos = []
        for pos in [round(float(x), 3) for x in np.arange(0.015, 0.0801, 0.005)]:
            combos.append((f"ext>={pos:.3f}", {"pct_above_ma20": pos}))
            combos.append((f"ext>={pos:.3f}+div1.6",
                           {"pct_above_ma20": pos, "diverge_score": 1.6}))
        rows = []
        for label, cond in combos:
            expr = " and ".join(f"{k} >= {v}" for k, v in cond.items())
            a = portfolio(is_df, expr, args.top_n, True, label)
            b = portfolio(oos_df, expr, args.top_n, True, label)
            c = portfolio(ev, expr, args.top_n, True, label)
            rows.append({
                "label": label,
                "is": a["avg_ret"], "oos": b["avg_ret"], "all": c["avg_ret"],
                "is_wr": a["win_rate"], "oos_wr": b["win_rate"], "all_wr": c["win_rate"],
                "is_pf": a["pf"], "oos_pf": b["pf"],
                "min": None if (a["avg_ret"] is None or b["avg_ret"] is None)
                       else round(min(a["avg_ret"], b["avg_ret"]), 2),
                "n_fill": c["n_fill"], "per_day": c["per_day"],
            })
        rows.sort(key=lambda r: (r["min"] is None, -(r["min"] or -99)))
        print(f"\n=== 阈值细网格 · 每日top{args.top_n} · 现行排序(扩展度升序) ===")
        print(f"{'规则':<22}{'样本内':>9}{'样本外':>9}{'全期':>9}{'最差窗':>9}"
              f"{'全期胜率':>9}{'全期PF':>8}{'成交':>7}{'日均':>6}")
        for r in rows:
            f = lambda v, s="{:.2f}": (s.format(v) if v is not None else "--")  # noqa: E731
            print(f"{r['label']:<22}{f(r['is'],'{:+.2f}%'):>9}{f(r['oos'],'{:+.2f}%'):>9}"
                  f"{f(r['all'],'{:+.2f}%'):>9}{f(r['min'],'{:+.2f}%'):>9}"
                  f"{f(r['all_wr'],'{:.1f}%'):>9}{f(r['is_pf']):>8}"
                  f"{r['n_fill']:>7}{f(r['per_day'],'{:.1f}'):>6}")
        return

    if args.portfolio:
        rules = [
            ("", "基线·不过滤"),
            ("pct_above_ma20 >= 0.028", "ext>=2.8%"),
            ("pct_above_ma20 >= 0.037", "ext>=3.7%"),
            ("pct_above_ma20 >= 0.043", "ext>=4.3%"),
            ("pct_above_ma20 >= 0.055", "ext>=5.5%"),
            ("pct_above_ma20 >= 0.070", "ext>=7.0%"),
            ("pct_above_ma20 >= 0.080", "ext>=8.0%"),
            ("pct_above_ma20 >= 0.055 and diverge_score >= 1.6", "ext>=5.5% +diverge>=1.6"),
            ("pct_above_ma20 >= 0.070 and diverge_score >= 1.6", "ext>=7.0% +diverge>=1.6"),
        ]
        for wname, wdf in (("样本内", is_df), ("样本外", oos_df)):
            if wdf.empty:
                continue
            for asc in (True, False):
                tag = "扩展度升序(现行)" if asc else "扩展度降序"
                rows = [portfolio(wdf, e, args.top_n, asc, f"{lb}")
                        for e, lb in rules]
                show_portfolio(rows, f"组合级 · 每日top{args.top_n} · {wname} · 排序[{tag}]")
        return

    if args.rule:
        target = oos_df if args.oos else is_df
        sel = target.query(args.rule)
        tag = "样本外" if args.oos else "样本内"
        rows = [metrics(target, "全部（基线）"), metrics(sel, args.rule)]
        show(rows, f"单规则对比 · {tag}（样本 {len(target):,}）")
        return

    print("\n########## 特征分布（分位）##########")
    for c in FEATURES:
        v = pd.to_numeric(is_df[c], errors="coerce").dropna()
        if v.empty:
            print(f"  {c:<16} 全空")
            continue
        print(f"  {c:<16} n={len(v):>7} " + "  ".join(
            f"P{int(q*100)}={v.quantile(q):.2f}" for q in (0.05, 0.25, 0.5, 0.75, 0.95)))

    print("\n########## 单变量阈值扫描（样本内，目标：平均收益）##########")
    for c in FEATURES:
        grid = quantile_grid(is_df, c)
        if not grid:
            continue
        rows = [metrics(is_df, f"{c} — 基线(不过滤)")]
        for t in grid:
            rows.append(metrics(is_df[is_df[c] >= t], f"{c} >= {t}"))
        show(rows, f"特征：{c}")

    # 分半稳定性（看最优档是否只是某一段的运气）——必须与"同期基线"对比，
    # 否则无法区分"规则有效"与"这段时间本来就赚"
    print("\n########## 分半年稳定性：规则 vs 同期基线（样本内）##########")

    def stability(df_: pd.DataFrame, expr: str, label: str):
        sel = df_.query(expr)
        if sel.empty:
            return
        rows = [metrics(sel, f"{label} · 全期")]
        for h in sorted(df_["half"].unique()):
            g_all = df_[df_["half"] == h]
            g_sel = sel[sel["half"] == h]
            rows.append(metrics(g_sel, f"      {h} · 规则"))
            rows.append(metrics(g_all, f"      {h} · 基线"))
        show(rows, f"稳定性：{label}")

    for expr, label in (
        ("pct_above_ma20 >= 0.028", "ext>=2.8%"),
        ("pct_above_ma20 >= 0.055", "ext>=5.5%"),
        ("pct_above_ma20 >= 0.070", "ext>=7.0%"),
        ("pct_above_ma20 >= 0.080", "ext>=8.0%"),
    ):
        stability(is_df, expr, label)

    # ── 双变量组合：pct_above_ma20（位置/强度）是唯一单调轴，试与正交特征叠加 ──
    print("\n########## 双变量组合（样本内，目标：平均收益）##########")
    combo_rows = [metrics(is_df, "基线(不过滤)")]
    for pos in quantile_grid(is_df, "pct_above_ma20", qs=(0.5, 0.7, 0.85)):
        for extra, tag in (("", ""),
                           (" and diverge_score >= 1.6", " +diverge>=1.6"),
                           (" and whale_score >= 2.7", " +whale>=2.7"),
                           (" and vol_score >= 4.4", " +vol>=4.4")):
            expr = f"pct_above_ma20 >= {pos}{extra}"
            combo_rows.append(metrics(is_df.query(expr), f"ma20>={pos}{tag}"))
    show(combo_rows, "组合：pct_above_ma20 × 正交特征")



if __name__ == "__main__":
    main()
