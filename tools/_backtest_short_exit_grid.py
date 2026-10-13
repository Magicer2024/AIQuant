# -*- coding: utf-8 -*-
"""短线出场规则变体网格回测（只读，不改任何生产数据）。

Part 1：monkey-patch evaluate_exit_by_prices，捕获出场跟踪面板（short 组）
       全部段的真实输入（entry_price/entry_date/日线/止损/止盈），base 变体
       逐笔复刻面板盈亏做口径校验。
Part 2：变体状态机 = 线上 evaluate_exit_by_prices 语义 + 候选规则开关：
       - stop_cap      止损宽度上限（评估期夹逼）
       - ratchet       浮盈保本棘轮：highest>=entry*(1+trigger) 后止损上移至 entry*(1+floor)
       - launch_on_close 移动止盈启动改收盘判定（修复「盘中插针启动+同日砸盘锁亏」）
       - launch_ratio  启动线覆盖（默认 short_take_profit=0.08）
       - trail_pct     回撤比例覆盖（默认 short_trailing_pct=0.03）
       - max_hold      持仓上限覆盖（默认 10）
       - stall_day     滞退出场：j>=stall_day 且未启动且收盘<entry → 收盘卖出
Part 3：按变体输出 均笔/胜率/出场原因分布/月度均值，供选型。

用法：python tools/_backtest_short_exit_grid.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

import strategy.exit_advisor as ea  # noqa: E402
from config.strategy_params import get_param  # noqa: E402
from core.services.exit_tracking import build_exit_advice_payload  # noqa: E402

CAPTURES: dict = {}


def _wrap(orig):
    def patched(entry_price, entry_date, df, *, stop_loss=None, take_profit=None,
                max_hold_days=None, trailing_pct=None, partial_tp=None,
                stop_cap_pct=None):
        key = (id(df), round(float(entry_price), 4), str(entry_date))
        CAPTURES[key] = {
            "df": df, "entry": float(entry_price), "entry_date": str(entry_date),
            "stop": stop_loss, "tp": take_profit, "max_hold": max_hold_days,
            "trail": trailing_pct, "partial": partial_tp, "cap": stop_cap_pct,
        }
        return orig(entry_price, entry_date, df, stop_loss=stop_loss,
                    take_profit=take_profit, max_hold_days=max_hold_days,
                    trailing_pct=trailing_pct, partial_tp=partial_tp,
                    stop_cap_pct=stop_cap_pct)
    return patched


def sim(c, opts: dict):
    """变体状态机：返回 (pnl_pct, reason)。语义对齐 evaluate_exit_by_prices。"""
    df = c["df"]
    entry = c["entry"]
    stop = c["stop"]
    tp = c["tp"]
    trail = opts.get("trail_pct", c["trail"])
    partial = c["partial"]
    max_hold = opts.get("max_hold", c["max_hold"])
    stop_cap = opts.get("stop_cap")
    ratchet = opts.get("ratchet")
    launch_on_close = opts.get("launch_on_close", False)
    stall_day = opts.get("stall_day")
    launch_ratio = opts.get("launch_ratio", partial if (partial or 0) < 1 else None)

    if stop_cap is not None and 0.01 <= stop_cap < 1.0:
        capped = entry * (1 - stop_cap)
        if stop is None or capped > stop:
            stop = capped
    if stop is not None and stop <= 0:
        stop = None
    if tp is not None and tp <= 0:
        tp = None
    if stop is not None and tp is not None and tp <= stop:
        tp = None
    if trail is not None and not (0.01 <= trail < 1.0):
        trail = None
    launch_line = entry * (1 + launch_ratio) if launch_ratio else tp
    trailing_enabled = trail is not None and launch_line is not None
    if trailing_enabled and launch_ratio and trail >= launch_ratio:
        trail = max(0.01, round(launch_ratio * 0.8, 4))

    if hasattr(df.index, "strftime"):
        after = df[df.index > pd.Timestamp(c["entry_date"])]
    else:
        after = df[df.index > c["entry_date"]]
    if after.empty:
        return None, "no_data"

    highest = None
    launched = False
    tline = None
    for i, (ts, row) in enumerate(after.iterrows(), start=1):
        close = float(row["close"])
        high = float(row["high"]) if pd.notna(row.get("high")) else close
        if highest is None or high > highest:
            highest = high
        # 保本棘轮：浮盈曾达 trigger → 止损上移（与移动止盈启动同用盘中 high）
        if ratchet and stop is not None or ratchet:
            if ratchet and highest >= entry * (1 + ratchet[0]):
                flo = entry * (1 + ratchet[1])
                stop = flo if stop is None else max(stop, flo)
        if trailing_enabled and not launched:
            if (close >= launch_line) if launch_on_close else (highest >= launch_line):
                launched = True
        if trailing_enabled and launched:
            line = highest * (1 - trail)
            if tline is None or line > tline:
                tline = line
        if i == 1:
            continue
        if stop is not None and close <= stop:
            return round((close - entry) / entry * 100, 2), "stop"
        if trailing_enabled and launched and tline is not None and close <= tline:
            return round((close - entry) / entry * 100, 2), "trail"
        if stall_day and i >= stall_day and not launched and close < entry:
            return round((close - entry) / entry * 100, 2), "stall"
        if max_hold is not None and i >= max_hold:
            return round((close - entry) / entry * 100, 2), "expire"
    last = float(after.iloc[-1]["close"])
    return round((last - entry) / entry * 100, 2), "hold"


def metrics(rows):
    pnls = [p for p, _ in rows if p is not None]
    n = len(pnls)
    mean = round(sum(pnls) / n, 2) if n else None
    wr = round(sum(1 for p in pnls if p > 0) / n * 100, 1) if n else None
    by = defaultdict(list)
    for p, r in rows:
        if p is not None:
            by[r].append(p)
    detail = {}
    for r in ("stop", "trail", "expire", "stall", "hold"):
        v = by.get(r, [])
        detail[r] = (len(v), round(sum(v) / len(v), 2) if v else None)
    return n, mean, wr, detail, by


def month_means(by_month):
    out = {}
    for m in sorted(by_month):
        v = by_month[m]
        out[m] = round(sum(v) / len(v), 2) if v else None
    return out


def main():
    orig = ea.evaluate_exit_by_prices
    ea.evaluate_exit_by_prices = _wrap(orig)
    try:
        payload = build_exit_advice_payload()
    finally:
        ea.evaluate_exit_by_prices = orig

    # 仅 short 组口径：trail=short_trailing_pct 且 cap=None
    short_trail = float(get_param("short_trailing_pct"))
    segs = [c for c in CAPTURES.values()
            if c["cap"] is None and c["trail"] is not None
            and abs(float(c["trail"]) - short_trail) < 1e-9]
    # 去重（split 检测与最终评估同输入）
    seen = set()
    uniq = []
    for c in segs:
        k = (id(c["df"]), c["entry"], c["entry_date"])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    print(f"捕获 short 段 {len(uniq)} 个")

    # 口径校验：base 变体 vs 面板
    panel = payload["summary"]["perf"]["short"]
    base_rows = [sim(c, {}) for c in uniq]
    n, mean, wr, detail, _ = metrics(base_rows)
    print(f"base 复刻: n={n} 均笔={mean}% 胜率={wr}% | 面板: "
          f"n={panel['segments']} 均笔={panel['avg_pct']}% 胜率={panel['win_rate']}%")

    variants = {
        "base": {},
        "cap10": {"stop_cap": 0.10},
        "cap8": {"stop_cap": 0.08},
        "r40": {"ratchet": (0.04, 0.00)},
        "r50": {"ratchet": (0.05, 0.00)},
        "r41": {"ratchet": (0.04, 0.01)},
        "r62": {"ratchet": (0.06, 0.02)},
        "lc": {"launch_on_close": True},
        "l6": {"launch_ratio": 0.06},
        "l5": {"launch_ratio": 0.05},
        "s5": {"stall_day": 5},
        "s6": {"stall_day": 6},
        "mh7": {"max_hold": 7},
        "c8r40": {"stop_cap": 0.08, "ratchet": (0.04, 0.00)},
        "c1": {"launch_on_close": True, "ratchet": (0.04, 0.00)},
        "c2": {"launch_on_close": True, "ratchet": (0.04, 0.00), "stop_cap": 0.08},
        "c3": {"launch_on_close": True, "ratchet": (0.04, 0.00), "stall_day": 5},
        "c4": {"launch_on_close": True, "ratchet": (0.04, 0.00), "stop_cap": 0.08,
               "stall_day": 5},
        "c5": {"launch_on_close": True, "ratchet": (0.05, 0.00), "stop_cap": 0.08,
               "stall_day": 6},
        "c6": {"launch_on_close": True, "launch_ratio": 0.06,
               "ratchet": (0.04, 0.00), "stop_cap": 0.08},
        "c7": {"launch_on_close": True, "ratchet": (0.04, 0.00), "stop_cap": 0.08,
               "stall_day": 5, "max_hold": 7},
    }

    print(f"\n{'variant':<8}{'n':>4}{'均笔%':>8}{'胜率%':>7}"
          f"{'止损n/均':>12}{'移止n/均':>12}{'到期n/均':>12}{'滞退n/均':>10}"
          f"{'08月':>7}{'09月':>7}{'10月':>7}")
    results = []
    for name, opts in variants.items():
        rows = [sim(c, opts) for c in uniq]
        n, mean, wr, detail, by = metrics(rows)
        bm = defaultdict(list)
        for c, (p, r) in zip(uniq, rows):
            if p is not None:
                bm[c["entry_date"][:7]].append(p)
        mm = month_means(bm)
        results.append((name, mean, wr))
        st, tr, ex, sl = detail["stop"], detail["trail"], detail["expire"], detail["stall"]
        print(f"{name:<8}{n:>4}{mean:>8}{wr:>7}"
              f"{f'{st[0]}/{st[1]}':>12}{f'{tr[0]}/{tr[1]}':>12}"
              f"{f'{ex[0]}/{ex[1]}':>12}{f'{sl[0]}/{sl[1]}':>10}"
              f"{str(mm.get('2026-08')):>7}{str(mm.get('2026-09')):>7}"
              f"{str(mm.get('2026-10')):>7}")
    print("\n按均笔排序:", sorted(results, key=lambda x: -(x[1] or -99)))


if __name__ == "__main__":
    main()
