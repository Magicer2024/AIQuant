# -*- coding: utf-8 -*-
"""入场过滤 × 出场修正 组合回测（只读）。

状态机输入直接由「面板条目 + daily_price + stock_signal(代码,推荐日)」重建：
  entry = 面板 detail.entry_price；entry_date 传建仓日前一交易日（j=1=建仓日）；
  stop/tp = stock_signal 该(代码,推荐日)行（连续推荐段按最新一次，与面板同口径）。
base 变体先与面板 detail.current_pnl_pct 逐笔对账，再跑过滤×修正组合。

用法：python tools/_backtest_short_combo.py
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

from config.strategy_params import get_param  # noqa: E402
from core.db import get_conn  # noqa: E402
from core.repository.price_repo import get_price_frames  # noqa: E402
from core.services.exit_tracking import build_exit_advice_payload  # noqa: E402


def sim(df, entry, entry_date, stop, tp, opts: dict):
    trail = float(get_param("short_trailing_pct"))
    partial = float(get_param("short_take_profit"))
    max_hold = int(get_param("short_max_hold_days"))
    launch_on_close = opts.get("lc", False)
    stopfill = opts.get("stopfill", False)
    launch_ratio = partial if partial < 1 else None

    if stop is not None and stop <= 0:
        stop = None
    if tp is not None and tp <= 0:
        tp = None
    if stop is not None and tp is not None and tp <= stop:
        tp = None
    launch_line = entry * (1 + launch_ratio) if launch_ratio else tp
    trailing_enabled = trail is not None and launch_line is not None
    if trailing_enabled and launch_ratio and trail >= launch_ratio:
        trail = max(0.01, round(launch_ratio * 0.8, 4))

    if hasattr(df.index, "strftime"):
        after = df[df.index > pd.Timestamp(entry_date)]
    else:
        after = df[df.index > entry_date]
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
            px = stop if (stopfill and stop <= entry) else close
            return round((px - entry) / entry * 100, 2), "stop"
        if trailing_enabled and launched and tline is not None and close <= tline:
            return round((close - entry) / entry * 100, 2), "trail"
        if max_hold is not None and i >= max_hold:
            return round((close - entry) / entry * 100, 2), "expire"
    last = float(after.iloc[-1]["close"])
    return round((last - entry) / entry * 100, 2), "hold"


def main():
    payload = build_exit_advice_payload()
    items = [r for r in payload["groups"]["short"]
             if r["status"] in ("clear", "hold")]
    with get_conn() as conn:
        sig = {}
        for r in conn.execute(
            "SELECT code, scan_date, fusion_score, stop_loss, take_profit "
            "FROM stock_signal WHERE horizon='short'"):
            sig[(r["code"], r["scan_date"])] = r
        tds = [r["trade_date"] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date")]
        br = pd.read_sql_query(
            "SELECT trade_date, AVG(CASE WHEN pct_change>0 THEN 1.0 ELSE 0.0 END) AS br "
            "FROM daily_price WHERE trade_date >= '2026-07-01' GROUP BY trade_date", conn)
        brmap = {r["trade_date"]: r["br"] * 100 for _, r in br.iterrows()}
        frames = get_price_frames([r["code"] for r in items], conn=conn)
    td_pos = {d: i for i, d in enumerate(tds)}

    segs = []
    bad = 0
    for it in items:
        d = it.get("detail") or {}
        entry = float(d["entry_price"])
        edate = str(d["entry_date"])
        prev = tds[td_pos[edate] - 1] if edate in td_pos and td_pos[edate] > 0 else None
        s = sig.get((it["code"], it["scan_date"]))
        df = frames.get(it["code"])
        if df is None or prev is None or s is None:
            bad += 1
            continue
        p, _ = sim(df, entry, prev, s["stop_loss"], s["take_profit"], {})
        if p is None or abs(p - float(d["current_pnl_pct"])) > 0.011:
            bad += 1
            print(f"  对账失败 {it['code']} {it['scan_date']} sim={p} "
                  f"panel={d['current_pnl_pct']}")
            continue
        segs.append({
            "df": df, "entry": entry, "prev": prev,
            "stop": s["stop_loss"], "tp": s["take_profit"],
            "code": it["code"], "scan": it["scan_date"],
            "strategy": it.get("strategy") or "",
            "fusion": round(s["fusion_score"], 1) if s["fusion_score"] is not None else None,
            "br": brmap.get(it["scan_date"]),
        })
    print(f"段数 {len(segs)}（对账失败/缺数据 {bad}）")

    filters = {
        "无": lambda s: True,
        "fus24": lambda s: s["strategy"] == "隔日动量" or (
            s["fusion"] is not None and s["fusion"] >= 24),
        "br<=70": lambda s: s["br"] is None or s["br"] <= 70,
        "br30-70": lambda s: s["br"] is None or 30 <= s["br"] <= 70,
        "fus24+br<=70": lambda s: (s["strategy"] == "隔日动量" or (
            s["fusion"] is not None and s["fusion"] >= 24))
            and (s["br"] is None or s["br"] <= 70),
        "fus24+br30-70": lambda s: (s["strategy"] == "隔日动量" or (
            s["fusion"] is not None and s["fusion"] >= 24))
            and (s["br"] is None or 30 <= s["br"] <= 70),
    }
    exits = {
        "base": {},
        "lc": {"lc": True},
        "sf": {"stopfill": True},
        "lc+sf": {"lc": True, "stopfill": True},
    }

    print(f"\n{'入场过滤':<15}{'出场':<7}{'n':>4}{'均笔%':>8}{'胜率%':>7}"
          f"{'8月':>7}{'9月':>7}{'10月':>7}")
    out = []
    for fname, fn in filters.items():
        kept = [s for s in segs if fn(s)]
        for ename, opts in exits.items():
            rows = [sim(s["df"], s["entry"], s["prev"], s["stop"], s["tp"], opts)
                    for s in kept]
            pnls = [p for p, _ in rows if p is not None]
            n = len(pnls)
            mean = round(sum(pnls) / n, 2) if n else None
            wr = round(sum(1 for p in pnls if p > 0) / n * 100, 1) if n else None
            bm = defaultdict(list)
            for s, (p, _) in zip(kept, rows):
                if p is not None:
                    bm[s["scan"][:7]].append(p)
            mm = {m: round(sum(v) / len(v), 2) for m, v in bm.items()}
            out.append((fname, ename, mean, wr, n))
            print(f"{fname:<15}{ename:<7}{n:>4}{mean:>8}{wr:>7}"
                  f"{str(mm.get('2026-08')):>7}{str(mm.get('2026-09')):>7}"
                  f"{str(mm.get('2026-10')):>7}")
    print("\nTop:", sorted(out, key=lambda x: -(x[2] or -99))[:8])


if __name__ == "__main__":
    main()
