"""短线风控规则回测：① ATR 自适应止损  ② 连续止损熔断（只读，不改任何生产数据）

背景
----
2026-09-11 诊断「出场跟踪·短线 累计胜率 16.2%」发现：33 笔已止损单里 **22 笔（67%）
在卖出后 5 个交易日内反弹超 +3%**。止损宽度 -5%~-8%，而中小盘题材股日波动率 3~5%
—— 1.5~2 倍日波动就能打掉。故本工具评估两条退出侧改造：
  1. **ATR 自适应止损**：把固定 -5% 换成 max(5%, k×ATR14%)，波动大则止损放宽；
  2. **连续止损熔断**：同一周内连续 N 笔止损 → 暂停建仓至下一周（事件驱动口径）。

口径保证
--------
- 候选池 / 行情加载 **直接复用** `tools/eval_short_observe_rule.py`（同一生产链路：
  基础过滤 + fusion≥short_conf_gate + T1 过滤 + 弱市闸门 − 观察线排除）。
- 出场状态机仍 **直接 import** `core.outcome_tracker._short_exit_sim`，本工具只替换
  `stop` 入参，不改任何判定数学。
- 入场 = 线上「回踩确认」分支（T+1 起 N 个交易日内 low 触及买点才成交，
  成交价 = min(买点, 当日开盘)）。
- ATR 只用 **信号日及之前** 的行情计算（`d <= scan_date`），无未来函数。

用法
----
    python tools/eval_short_risk_rules.py --rebuild     # 重建缓存（首次/改规则后）
    python tools/eval_short_risk_rules.py               # 完整报告
    python tools/eval_short_risk_rules.py --atr-grid    # 只跑 ATR 网格
    python tools/eval_short_risk_rules.py --cb-grid     # 只跑熔断网格
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.db import get_conn                                          # noqa: E402
from core.outcome_tracker import _short_exit_sim                      # noqa: E402
from config.strategy_params import get_param                          # noqa: E402
from strategy.exit_advisor import get_max_hold                        # noqa: E402
import eval_short_observe_rule as base                                # noqa: E402

CACHE_FILE = os.path.join(_ROOT, "logs", "_short_risk_sim.pkl")
PRICE_CACHE = os.path.join(_ROOT, "logs", "_short_risk_prices.pkl")

# 生产参数（只读，用于对照说明）
STOP_DEFAULT = float(get_param("short_stop_loss") or -0.05)           # -0.05
ATR_N = 14


# ───────────────────────── 1. 行情 + ATR（信号日可得） ─────────────────────────

def load_prices_atr(start: str) -> dict:
    """code -> {d, open, close, high, low, atr_pct}（按日期升序 numpy，ATR 为 Wilder ATR14）。"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT code, trade_date, open, close, high, low FROM daily_price "
            "WHERE trade_date >= ? ORDER BY code, trade_date",
            conn, params=(start,))
    if df.empty:
        return {}
    for c in ("open", "close", "high", "low"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    prev_close = df.groupby("code", sort=False)["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["tr"] = tr
    # Wilder ATR：alpha = 1/n 的递推均值（min_periods=n 保证前 n 行不给值）
    df["atr"] = df.groupby("code", sort=False)["tr"].transform(
        lambda s: s.ewm(alpha=1.0 / ATR_N, adjust=False, min_periods=ATR_N).mean())
    df["atr_pct"] = df["atr"] / df["close"].replace(0, np.nan)
    df["d"] = pd.to_datetime(df["trade_date"]).values.astype("datetime64[D]").astype(np.int64)

    out: dict[str, dict[str, np.ndarray]] = {}
    for code, g in df.groupby("code", sort=False):
        out[code] = {
            "d": g["d"].to_numpy(),
            "open": g["open"].to_numpy(dtype=float),
            "close": g["close"].to_numpy(dtype=float),
            "high": g["high"].to_numpy(dtype=float),
            "low": g["low"].to_numpy(dtype=float),
            "atr_pct": g["atr_pct"].to_numpy(dtype=float),
        }
    return out


# ───────────────────────── 2. 逐信号回放（可替换止损） ─────────────────────────

def simulate(signals: list[dict], prices: dict, stop_mode: str = "base",
             k: float = 2.0, floor: float = 0.05, cap: float = 0.15,
             stop_anchor: str = "entry") -> pd.DataFrame:
    """回踩确认入场 + 线上出场状态机；stop_mode='atr' 时用 max(floor, k×ATR) 替换止损。

    :param stop_anchor: ATR 止损的锚点。**这是与生产口径唯一的差别，必须对齐**：
        - "entry"  = 实际成交价 exec_entry（本工具初始口径）；
        - "signal" = 信号日收盘价（= 候选池 buy_price = 买点）。生产侧只能在信号日
          写库时就定好止损价（入场日 T+1..T+N 未知），故 **线上可实现的是 signal**。
          由于 exec_entry = min(买点, 当日开盘) <= 买点，entry 口径的实际止损距离
          恒 <= signal 口径 → 两者不等价，需实测差异。

    额外产出 `exit_date`（熔断需要按出场事件排序）与 `atr_pct`/`stop_pct_used`（诊断用）。
    """
    trail_pct = get_param("short_trailing_pct")
    max_hold = get_max_hold("short") or 10
    entry_window = max(1, int(get_param("short_entry_window_days")))
    need = entry_window + max_hold + 2

    recs = []
    for s in signals:
        code = s["code"]
        p = prices.get(code)
        entry = s.get("buy_price")
        stop_sig, tp = s.get("stop_loss"), s.get("take_profit")
        rec = {
            "id": s["id"], "scan_date": s["scan_date"], "code": code, "name": s.get("name"),
            "fusion_score": s.get("fusion_score"), "bottom_score": s.get("bottom_score"),
            "diverge_score": s.get("diverge_score"), "vol_score": s.get("vol_score"),
            "ma_score": s.get("ma_score"), "whale_score": s.get("whale_score"),
            "pct_above_ma20": s.get("pct_above_ma20"),
            "filled": 0, "final": 0, "ret": None, "exit_reason": None, "exit_date": None,
            "atr_pct": None, "stop_pct_used": None, "entry_used": None,
        }
        rec["rr_ratio"] = (round((tp - entry) / (entry - stop_sig), 3)
                           if entry and stop_sig and tp and entry > stop_sig > 0 else None)
        if p is None or not entry or entry <= 0:
            recs.append(rec)
            continue

        d0 = np.datetime64(s["scan_date"]).astype("datetime64[D]").astype(np.int64)
        i0 = int(np.searchsorted(p["d"], d0, side="right"))       # 首个 > scan_date
        if i0 >= len(p["d"]):
            recs.append(rec)
            continue
        # 信号日当行（含）用于取 ATR —— 严格无未来函数
        j0 = i0 - 1
        if j0 >= 0 and p["d"][j0] == d0:
            a = p["atr_pct"][j0]
            rec["atr_pct"] = None if (a is None or np.isnan(a)) else round(float(a), 5)

        sl = slice(i0, min(i0 + need, len(p["d"])))
        dates, op, cl, hi, lo = (p["d"][sl], p["open"][sl], p["close"][sl],
                                 p["high"][sl], p["low"][sl])

        # ── 回踩确认入场（与线上 pullback 分支同逻辑）──
        fill_idx, exec_entry = None, None
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
        rec["entry_used"] = exec_entry

        # ── 止损价：base = 信号自带（线上）；atr = max(floor, k×ATR) 封顶 cap ──
        stop_use = stop_sig
        if stop_mode == "atr":
            a = rec["atr_pct"]
            pct = max(floor, (k * a) if a else floor)
            pct = min(pct, cap)
            _anchor = exec_entry if stop_anchor == "entry" else float(entry)
            stop_use = round(_anchor * (1 - pct), 2)
            rec["stop_pct_used"] = round(pct, 4)
        elif stop_sig and exec_entry > 0:
            rec["stop_pct_used"] = round((exec_entry - stop_sig) / exec_entry, 4)

        hp = []
        end = min(fill_idx + max_hold, len(dates))
        for kk in range(fill_idx, end):
            hp.append({
                "trade_date": str(np.datetime64(int(dates[kk]), "D")),
                "close": None if np.isnan(cl[kk]) else float(cl[kk]),
                "high": None if np.isnan(hi[kk]) else float(hi[kk]),
            })
        reason, ed, ret, _hs, _lc, done = _short_exit_sim(
            hp, exec_entry, stop_use, tp, trail_pct, max_hold)
        rec["exit_reason"] = reason
        rec["exit_date"] = ed
        rec["final"] = 1 if done else 0
        rec["ret"] = ret
        recs.append(rec)

    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["half"] = df["scan_date"].str.slice(0, 4) + "H" + np.where(
        df["scan_date"].str.slice(5, 7).astype(int) <= 6, "1", "2")
    return df


# ───────────────────────── 3. 组合级 + 熔断（事件驱动） ─────────────────────────

def _pick_daily(df: pd.DataFrame, top_n: int, asc: bool) -> pd.DataFrame:
    """每日 top_n 候选（线上排序：扩展度方向 asc → fusion DESC），含未成交行。"""
    p = df[df["final"] == 1].copy()
    if p.empty:
        return p
    p["_ext"] = pd.to_numeric(p["pct_above_ma20"], errors="coerce").fillna(0)
    p = p.sort_values(["scan_date", "_ext", "fusion_score"], ascending=[True, asc, False])
    return p.groupby("scan_date", sort=True).head(top_n)


def portfolio_trades(df: pd.DataFrame, top_n: int = 3, asc: bool = True,
                     n_consec: int = 0, pause_weeks: int = 0,
                     week_scope: bool = True) -> pd.DataFrame:
    """组合级成交明细。

    n_consec<=0 → 无熔断。
    n_consec>0 → **事件驱动**熔断：按出场日推进，维护「连续止损」计数；
      计数达 n_consec 时暂停建仓，直到当周结束（pause_weeks=0 下周恢复 /
      pause_weeks=1 连下周也跳过）。week_scope=True 表示连败计数只在同一周内累计。
      口径说明：出场事件在**建仓决策之前**处理（当日收盘确认止损 → 次日不再建仓），
      只依赖已发生的历史，无未来函数。
    """
    if n_consec <= 0:
        return _pick_daily(df, top_n, asc)

    pool = df[df["final"] == 1].copy()
    if pool.empty:
        return pool
    pool["_ext"] = pd.to_numeric(pool["pct_above_ma20"], errors="coerce").fillna(0)
    pool = pool.sort_values(["scan_date", "_ext", "fusion_score"], ascending=[True, asc, False])
    by_day = {d: g for d, g in pool.groupby("scan_date", sort=True)}

    # 出场事件：只统计已成交（filled）的单子，按出场日排序
    filled = pool[pool["filled"] == 1].copy()
    filled["_exit"] = filled["exit_date"].fillna("9999-12-31")
    exit_events = filled.sort_values("_exit")[["_exit", "exit_reason"]].to_records(index=False)

    all_days = sorted(pool["scan_date"].unique())
    kept: list = []
    streak, last_week, pause_until = 0, None, None
    ei = 0
    for d in all_days:
        # ① 处理当日（不含）之前已发生的出场事件
        while ei < len(exit_events) and exit_events[ei][0] < d:
            ed, reason = exit_events[ei][0], exit_events[ei][1]
            wk = pd.Timestamp(ed).isocalendar()
            wk = (wk.year, wk.week)
            if week_scope and last_week is not None and wk != last_week:
                streak = 0
            last_week = wk
            streak = streak + 1 if reason == "stop_loss" else 0
            if streak >= n_consec:
                ts = pd.Timestamp(ed)
                next_mon = ts + pd.Timedelta(days=(7 - ts.weekday()))
                pause_until = (next_mon + pd.Timedelta(days=7 * pause_weeks)
                               - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                streak = 0
            ei += 1
        # ② 熔断期内不建仓
        if pause_until is not None and d <= pause_until:
            continue
        g = by_day[d]
        kept.append(g.head(top_n))
    return (pd.concat(kept) if kept else pool.iloc[0:0])


def _stats(picked: pd.DataFrame, label: str, n_sig: int) -> dict:
    """与 base.metrics 同键，供 base.show / base.show_portfolio 直接打印。"""
    ev = picked[picked["filled"] == 1]
    rets = pd.to_numeric(ev["ret"], errors="coerce").dropna()
    n_fill = len(rets)
    if n_fill == 0:
        return {"label": label, "n_sig": n_sig, "n_fill": 0, "fill_rate": 0.0,
                "avg_ret": None, "median_ret": None, "win_rate": None, "pf": None,
                "avg_per_sig": None, "stop_rate": None, "days": 0, "per_day": 0,
                "cum_ret": None, "avg_loss": None}
    wins, losses = rets[rets > 0], rets[rets <= 0]
    days = int(picked["scan_date"].nunique())
    return {
        "label": label, "n_sig": n_sig, "n_fill": n_fill,
        "fill_rate": round(n_fill / n_sig * 100, 1) if n_sig else None,
        "avg_ret": round(float(rets.mean()), 2),
        "median_ret": round(float(rets.median()), 2),
        "win_rate": round(float(len(wins) / len(rets) * 100), 1),
        "pf": round(float(wins.sum()) / abs(float(losses.sum())), 2) if len(losses) else None,
        "avg_per_sig": round(float(rets.sum()) / n_sig, 2) if n_sig else None,
        "stop_rate": round(float((ev["exit_reason"] == "stop_loss").sum()) / n_fill * 100, 1),
        "days": days,
        "per_day": round(n_fill / days, 2) if days else 0,
        "cum_ret": round(float(rets.sum()), 1),
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        # 尾部风险：止损放宽后必须看最差单笔，不能只看均值
        "worst": round(float(rets.min()), 2),
        "p05": round(float(rets.quantile(0.05)), 2),
        "mix": {k: int(v) for k, v in ev["exit_reason"].value_counts().items()},
    }


def portfolio(df: pd.DataFrame, top_n: int = 3, asc: bool = True, label: str = "",
              n_consec: int = 0, pause_weeks: int = 0, week_scope: bool = True) -> dict:
    picked = portfolio_trades(df, top_n, asc, n_consec, pause_weeks, week_scope)
    n_sig = int((df["final"] == 1).sum())
    return _stats(picked, label, n_sig)


# ───────────────────────── 4. 主流程 ─────────────────────────

ATR_GRID = [(1.5, 0.05, 0.15), (2.0, 0.05, 0.15), (2.5, 0.05, 0.15),
            (3.0, 0.05, 0.15), (2.0, 0.05, 0.10)]
# (连续止损笔数, 额外停几周, 连败是否只在同一周内累计)
CB_GRID = [(2, 0, True), (3, 0, True), (3, 1, True), (4, 0, True),
           (3, 0, False), (4, 0, False), (5, 0, False)]


def _variants() -> list[tuple[str, dict]]:
    """变体名必须唯一——曾用 `atr{k}` 命名，k=2 出现 3 次 → 字典键冲突互相覆盖。"""
    out = [("base", {})]
    for k, fl, cp in ATR_GRID:
        out.append((f"atr{k:g}_f{int(fl*100)}_c{int(cp*100)}",
                    {"stop_mode": "atr", "k": k, "floor": fl, "cap": cp}))
    return out


def _vlabel(name: str, kw: dict) -> str:
    if name == "base":
        return "基线·信号自带止损"
    s = f"ATR {kw['k']:g}×"
    if kw.get("cap", 0.15) != 0.15:
        s += f" 上限{kw['cap']:.0%}"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--atr-start", default="2022-06-01", help="ATR 预热起点（早于信号起点）")
    ap.add_argument("--end", default="2026-09-10")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--rebuild-prices", action="store_true", help="强制重新加载行情（ATR）")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--asc", type=int, default=1, choices=(0, 1),
                    help="1=扩展度升序（线上现行）/ 0=降序")
    ap.add_argument("--win-start", default="2026-07-20", help="同窗口起点（线上跟踪起始日）")
    ap.add_argument("--atr-grid", action="store_true")
    ap.add_argument("--cb-grid", action="store_true")
    ap.add_argument("--combo", action="store_true", help="ATR 止损 × 扩展度过滤 的叠加检验")
    args = ap.parse_args()

    variants = _variants()

    if args.rebuild or not os.path.exists(CACHE_FILE):
        print(f"[1/3] 候选池 {args.start} ~ {args.end}（生产链路 − 观察线排除）…")
        sigs = base.build_population(args.start, args.end)
        print(f"      候选信号 {len(sigs):,} 条")
        if args.rebuild_prices or not os.path.exists(PRICE_CACHE):
            print(f"[2/3] 加载行情 {args.atr_start} 起（含 ATR 预热）… 首次较慢，结果缓存")
            prices = load_prices_atr(args.atr_start)
            with open(PRICE_CACHE, "wb") as fh:
                pickle.dump(prices, fh)
            print(f"      覆盖 {len(prices):,} 只股票 → 缓存 {PRICE_CACHE}")
        else:
            with open(PRICE_CACHE, "rb") as fh:
                prices = pickle.load(fh)
            print(f"[2/3] 读取行情缓存（{len(prices):,} 只股票）—— 强制重载请加 --rebuild-prices")
        cache = {}
        for name, kw in variants:
            print(f"      · 变体 {name} {kw or '(基线：信号自带止损)'}")
            cache[name] = simulate(sigs, prices, **kw)
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "wb") as fh:
            pickle.dump(cache, fh)
        print(f"[3/3] 缓存写入 {CACHE_FILE}")
    else:
        with open(CACHE_FILE, "rb") as fh:
            cache = pickle.load(fh)
        print(f"读取缓存 {CACHE_FILE}（{len(cache)} 个变体）—— 需要重建请加 --rebuild")

    asc = bool(args.asc)
    sort_tag = "扩展度升序(线上现行)" if asc else "扩展度降序"
    top_n = args.top_n
    base_df = cache["base"]
    ev = base_df[base_df["final"] == 1]
    print(f"\n终态样本 {len(ev):,} / {len(base_df):,}"
          f"（{ev['scan_date'].min()} ~ {ev['scan_date'].max()}，"
          f"{ev['scan_date'].nunique()} 个有效交易日）")
    atr_ok = pd.to_numeric(ev["atr_pct"], errors="coerce").notna()
    print(f"ATR 覆盖率 {atr_ok.mean()*100:.1f}%   中位 ATR14% = "
          f"{pd.to_numeric(ev['atr_pct'], errors='coerce').median()*100:.2f}%")

    def _win(d: pd.DataFrame, lo: str, hi: str) -> pd.DataFrame:
        """按信号日切片——每个变体都必须用同一时间窗，否则"同窗口"栏会显示全期数字。"""
        return d[(d["scan_date"] >= lo) & (d["scan_date"] <= hi)]

    def scope(lo: str, hi: str, tag: str):
        """在给定时间范围内打印 ATR 网格：逐信号 + 组合级。"""
        w = _win(base_df, lo, hi)
        w = w[w["final"] == 1]
        if w.empty:
            print(f"\n### {tag}：无样本"); return
        print(f"\n{'='*100}\n### {tag}（终态 {len(w):,} 条，{w['scan_date'].nunique()} 个交易日）\n{'='*100}")

        print(f"\n--- ① 逐信号（全池，未成交不计）---")
        rows = []
        for name, kw in variants:
            d = _win(cache[name], lo, hi)
            d = d[d["final"] == 1]
            rows.append(base.metrics(d, _vlabel(name, kw)))
        base.show(rows, f"逐信号 · {tag}")

        print(f"\n--- ② 组合级 · 每日top{top_n} · {sort_tag} ---")
        rows, losses = [], []
        for name, kw in variants:
            m = portfolio(_win(cache[name], lo, hi), top_n, asc, _vlabel(name, kw))
            rows.append(m)
            losses.append(m["avg_loss"])
        base.show_portfolio(rows, f"组合级 · {tag}")
        # 补充三行风险诊断：止损放宽 → 单笔损失变大、出场结构改变，必须一起看
        f2 = lambda v: ("--" if v is None else f"{v:+.2f}%")  # noqa: E731
        print(f"  {'平均亏损':<30}" + "".join(f"{f2(v):>13}" for v in losses))
        print(f"  {'最差单笔':<30}" + "".join(f"{f2(m['worst']):>13}" for m in rows))
        print(f"  {'P05(最差5%分位)':<30}" + "".join(f"{f2(m['p05']):>13}" for m in rows))
        keys = ("stop_loss", "trailing_stop", "max_hold_days")
        print(f"  {'出场结构 止损/移止/到期':<30}" + "".join(
            f"{'/'.join(str(m['mix'].get(k, 0)) for k in keys):>13}" for m in rows))

    def scope_cb(lo: str, hi: str, tag: str):
        w = _win(base_df, lo, hi)
        if w[w["final"] == 1].empty:
            print(f"\n### 熔断 · {tag}：无样本"); return
        print(f"\n{'='*100}\n### ② 连续止损熔断 · {tag}（每日top{top_n} · {sort_tag}）\n{'='*100}")
        rows = []
        for name, kw in variants:
            rows.append(portfolio(_win(cache[name], lo, hi), top_n, asc,
                                  "基线·无熔断" if name == "base" else _vlabel(name, kw) + " 无熔断"))
        for n_c, pw, ws in CB_GRID:
            lb = (f"熔断 {n_c}连败[{'同周' if ws else '不重置'}] → "
                  f"停{('当周' if pw == 0 else '含下周')}")
            rows.append(portfolio(_win(cache["base"], lo, hi), top_n, asc, lb,
                                  n_consec=n_c, pause_weeks=pw, week_scope=ws))
        base.show_portfolio(rows, f"熔断 · {tag}")

    def scope_combo(lo: str, hi: str, tag: str):
        """ATR 止损 × 扩展度过滤：两者都在同一方向上有效，需确认是否叠加还是互斥。"""
        w0 = _win(base_df, lo, hi)
        if w0[w0["final"] == 1].empty:
            print(f"\n### 组合 · {tag}：无样本"); return
        print(f"\n{'='*100}\n### ③ ATR 止损 × 扩展度过滤 · {tag}（每日top{top_n} · {sort_tag}）\n{'='*100}")
        rows = []
        for fname, fexpr in (("不过滤", ""), ("ext≥5.5%", "pct_above_ma20 >= 0.055"),
                             ("ext≥7.0%", "pct_above_ma20 >= 0.070")):
            for name, kw in variants:
                if name != "base" and kw.get("k") not in (2.0, 2.5):
                    continue                                   # 只跑 base / 2× / 2.5×
                d = _win(cache[name], lo, hi)
                if fexpr:
                    d = d.query(fexpr)
                rows.append(portfolio(d, top_n, asc, f"{_vlabel(name, kw)} + {fname}"))
        base.show_portfolio(rows, f"组合 · {tag}")

    scopes = [("0000-01-01", "9999-12-31",
               f"全期 {ev['scan_date'].min()} ~ {ev['scan_date'].max()}"),
              (args.win_start, args.end, f"同窗口 {args.win_start} ~ {args.end}"),
              ("0000-01-01", "2025-06-30", "样本内 ≤2025-06-30"),
              ("2025-07-01", "9999-12-31", "样本外 >2025-06-30")]

    if args.atr_grid or args.cb_grid:
        scopes = [scopes[0], scopes[1]]

    for lo, hi, tag in scopes:
        if args.combo:
            scope_combo(lo, hi, tag)
        elif args.cb_grid and not args.atr_grid:
            scope_cb(lo, hi, tag)
        elif args.atr_grid and not args.cb_grid:
            scope(lo, hi, tag)
        else:
            scope(lo, hi, tag)
            scope_cb(lo, hi, tag)


if __name__ == "__main__":
    main()
