# -*- coding: utf-8 -*-
"""backtest/walk_forward.py —— 引擎级 walk-forward（滚动 IS/OOS）驱动（入场端 alpha 立项·阶段0）

与 optimizer.py 的 70/30 单切分不同：这里按自然月滚动生成折叠，
每折 = [IS 月窗] 选参（网格内按 IS 指标冻结一个 cfg）→ [紧邻 OOS 月窗] 用冻结 cfg 重放，
所有折的 OOS 成交拼接后统一给聚合期望 / 逐月拆解 / 配对 bootstrap。

纪律内置（不可配置绕过）：
  · OOS 窗口之间互不重叠、且不与任一 IS 选参所用窗口重叠执行（滚动前推）；
  · 每折独立选参，选参只看 IS；OOS 结果只用于判定，不回灌选参；
  · warmup_days 默认 90：指标（MA/RSI/ATR）在窗口起点前已完成预热。

行情加载缓存：同一 (start,end,codes) 只查库一次（跨折叠大量复用），
仅在本模块内替换 engine._load_all_daily，不修改引擎源码行为。
"""
from __future__ import annotations

import copy
import functools
from dataclasses import asdict, replace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest import engine as E
from backtest.engine import BacktestParams, VisualBacktestEngine

_DEFAULT_WARMUP = 90
_BOOT_N = 10000


# ─────────────── 月份窗口工具 ───────────────

def month_list(start: str, end: str) -> List[str]:
    months = pd.period_range(pd.Timestamp(start).strftime("%Y-%m"),
                             pd.Timestamp(end).strftime("%Y-%m"), freq="M")
    return [str(m) for m in months]


def make_folds(start: str, end: str, is_months: int, oos_months: int,
               step_months: Optional[int] = None) -> List[Dict[str, str]]:
    """滚动折叠：IS [i, i+is) → OOS [i+is, i+is+oos)。step 默认 = oos_months。"""
    step = step_months or oos_months
    months = month_list(start, end)
    folds = []
    i = 0
    while i + is_months + oos_months <= len(months):
        is_m = months[i:i + is_months]
        oos_m = months[i + is_months:i + is_months + oos_months]
        folds.append({
            "is_start": f"{is_m[0]}-01",
            "is_end": str(pd.Period(is_m[-1]).end_time.date()),
            "oos_start": f"{oos_m[0]}-01",
            "oos_end": str(pd.Period(oos_m[-1]).end_time.date()),
            "label": f"{is_m[0]}~{is_m[-1]}|OOS {oos_m[0]}~{oos_m[-1]}",
        })
        i += step
    return folds


# ─────────────── 加载缓存（模块内替换，不落盘副作用） ───────────────

def _cached_loader():
    cache: Dict[Tuple, pd.DataFrame] = {}

    @functools.lru_cache(maxsize=None)
    def _key(start: str, end: str, codes: Tuple[str, ...]) -> int:
        return len(cache)

    def loader(start: str, end: str, codes: List[str]) -> pd.DataFrame:
        k = (start, end, tuple(codes))
        if k not in cache:
            cache[k] = E._baseline_load(start, end, codes)
        # 返回副本防止引擎就地修改污染缓存（groupby 后 set_index 不改动原对象，保守起见）
        return cache[k]

    return loader, cache


# ─────────────── 成本还原（研究口径：逐笔毛/净期望） ───────────────

def trade_pct(trades: List[Dict[str, Any]], p: BacktestParams) -> List[float]:
    """净% = pnl / 买入名义额；毛% = 净% + (佣金in+佣金out+印花)/买入名义额（滑点已含在成交价）。"""
    out = []
    for t in trades:
        basis = t["buy_price"] * t["shares"]
        if basis <= 0:
            continue
        net = t["pnl"] / basis
        fee_in = max(basis * p.commission_rate, E.DEFAULT_MIN_COMMISSION)
        proceeds = t["sell_price"] * t["shares"]
        fee_out = max(proceeds * p.commission_rate, E.DEFAULT_MIN_COMMISSION)
        stamp = proceeds * p.stamp_tax_rate
        gross = net + (fee_in + fee_out + stamp) / basis
        out.append((gross, net))
    return out


# ─────────────── 主驱动 ───────────────

def run_engine_window(base: BacktestParams, cfg: Dict[str, Any],
                      start: str, end: str,
                      conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
    p = replace(base, start_date=start, end_date=end,
                warmup_days=base.warmup_days or _DEFAULT_WARMUP, **cfg)
    eng = VisualBacktestEngine(p)
    return eng.run(conditions), p


def run_walk_forward(base_params: BacktestParams,
                     conditions: List[Dict[str, Any]],
                     grid: List[Dict[str, Any]],
                     folds: List[Dict[str, str]],
                     select: str = "avg_gross_pct",
                     min_is_trades: int = 20,
                     seed: int = 20261110,
                     verbose: bool = True) -> Dict[str, Any]:
    """每折 IS 选参（select 最大且 IS 成交≥min_is_trades）→ OOS 重放；聚合 OOS 判定。

    select: avg_gross_pct | avg_net_pct | total_return —— IS 内部选择用，不影响 OOS 计算。
    """
    E._baseline_load = E._load_all_daily
    loader, cache = _cached_loader()
    E._load_all_daily = loader
    try:
        fold_results = []
        all_oos: List[Tuple[str, Tuple[float, float]]] = []  # (oos_month, (gross,net))
        for f in folds:
            best, best_score, is_diag = None, None, []
            for cfg in grid:
                res, p = run_engine_window(base_params, cfg, f["is_start"], f["is_end"], conditions)
                pn = trade_pct(res.get("trades", []), p)
                n = len(pn)
                if n < min_is_trades:
                    is_diag.append({"cfg": cfg, "is_n": n, "skipped": True})
                    continue
                arr = np.array(pn)
                score = {"avg_gross_pct": arr[:, 0].mean(),
                         "avg_net_pct": arr[:, 1].mean(),
                         "total_return": res.get("total_return", -9.9)}[select]
                is_diag.append({"cfg": cfg, "is_n": n, "score": float(score)})
                if best_score is None or score > best_score:
                    best, best_score = cfg, score
            rec = {"fold": f["label"], "is_diag": is_diag, "selected": best,
                   "selected_score": best_score}
            if best is None:
                rec["oos"] = {"skipped": "IS 无满足最小成交数的 cfg"}
                fold_results.append(rec)
                continue
            res, p = run_engine_window(base_params, best, f["oos_start"], f["oos_end"], conditions)
            pn = trade_pct(res.get("trades", []), p)
            rec["oos"] = {"n": len(pn),
                          "avg_gross_pct": float(np.mean([x[0] for x in pn])) * 100 if pn else None,
                          "avg_net_pct": float(np.mean([x[1] for x in pn])) * 100 if pn else None,
                          "total_return": res.get("total_return"),
                          "exit_mix": _exit_mix(res.get("trades", []))}
            for (g, nr) in pn:
                all_oos.append((pd.Timestamp(f["oos_start"]).strftime("%Y-%m"), (g, nr)))
            fold_results.append(rec)
            if verbose:
                print(f"[wf] {f['label']} sel={best} oos_n={rec['oos']['n']} "
                      f"gross={rec['oos']['avg_gross_pct']}")
        # 聚合判定
        rows = {"bootstrap_ci95": None, "monthly": None}
        if all_oos:
            arr = np.array([[g, nr] for (_m, (g, nr)) in all_oos])
            rng = np.random.default_rng(seed)
            boot = np.array([rng.choice(arr[:, 1], len(arr), replace=True).mean()
                             for _ in range(_BOOT_N)])
            lo, hi = np.percentile(boot, [2.5, 97.5])
            bym: Dict[str, List[float]] = {}
            for m, (g, nr) in all_oos:
                bym.setdefault(m, []).append(g)
            rows = {
                "oos_trades": len(arr),
                "avg_gross_pct": float(arr[:, 0].mean()) * 100,
                "avg_net_pct": float(arr[:, 1].mean()) * 100,
                "win_pct_gross": float((arr[:, 0] > 0).mean()) * 100,
                "bootstrap_net_mean_95ci": [float(lo) * 100, float(hi) * 100],
                "bootstrap_p_mean_le_0": float((boot <= 0).mean()),
                "monthly_gross": {m: float(np.mean(v)) * 100 for m, v in sorted(bym.items())},
            }
        return {"folds": fold_results, "aggregate": rows,
                "seed": seed, "select": select,
                "base_params": asdict(base_params)}
    finally:
        E._load_all_daily = E._baseline_load
        del E._baseline_load


def _exit_mix(trades: List[Dict[str, Any]]) -> Dict[str, int]:
    mix: Dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "?")
        mix[r] = mix.get(r, 0) + 1
    return mix
