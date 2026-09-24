"""long 排序键 —— **带真实出场纪律**的组合口径验证（最终判据）。

为什么还要这一层
----------------
_diag_long_rescore.py / _diag_long_key_eval.py 测的是**裸持有 T+60**（信号日收盘买入、
60 个交易日后收盘卖出），没有止损、没有止盈、没有移动止盈。而线上 long 的出场纪律是
（strategy/mid_long.py + outcome_tracker._evaluate_mid_long）：
  - 止损：MA120 × long_ma_stop_mult（默认 0.95）
  - 止盈：入场价 × 1.5
  - 移动止盈：long_partial_tp 启动 + long_trailing_pct 回撤
  - 到期：get_max_hold('long')
裸持有 Δ 为正不代表加上出场纪律后仍为正——止损线可能正好把所选的票扫掉。
本脚本把出场纪律接上，重跑各排序键。

用法
----
    python tools/_diag_long_exit_sim.py [--cache logs/_long_rescore_cache.pkl]
                                        [--every 10] [--topn 4]
"""

import argparse
import math
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "logs", "_long_rescore_cache.pkl")


def _composite_rank(w_vol, w_ext):
    """统一签名 (rs, n) —— 与 CANDIDATES 里其它键一致。"""
    def fn(rs, n):
        m = len(rs)
        by_vol = sorted(rs, key=lambda x: x["vol_ratio"])
        by_ext = sorted(rs, key=lambda x: x["ext_ma20"])
        rv = {id(x): i / max(1, m - 1) for i, x in enumerate(by_vol)}
        re_ = {id(x): i / max(1, m - 1) for i, x in enumerate(by_ext)}
        return sorted(rs, key=lambda x: w_vol * rv[id(x)] + w_ext * re_[id(x)])[:n]
    return fn


CANDIDATES = {
    "当前(fusion DESC)": lambda rs, n: sorted(
        rs, key=lambda x: (x["score"], x.get("vol_ratio", 0)),
        reverse=True)[:n],
    "vol_ratio ASC": lambda rs, n: sorted(rs, key=lambda x: x["vol_ratio"])[:n],
    "vol_ratio×ext20(5x)": lambda rs, n: sorted(
        sorted(rs, key=lambda x: x["vol_ratio"])[:n * 5],
        key=lambda x: x["ext_ma20"])[:n],
    "合成rank(5:5)": _composite_rank(0.5, 0.5),
    "合成rank(3:7)": _composite_rank(0.3, 0.7),
}


def simulate_trade(bars, entry_i, entry_price, ma120_at_entry,
                   stop_mult, tp_mult, partial_tp, trailing_pct, max_hold):
    """从 entry_i 起逐日模拟出场，返回 (exit_return_pct, reason)。

    bars: list[(date, open, close, high, low)]，entry_i 为信号日在 bars 中的下标。
    与 outcome_tracker._evaluate_mid_long 同构：收盘判定口径。
    """
    stop = ma120_at_entry * stop_mult if ma120_at_entry else None
    tp = entry_price * tp_mult
    highest = None
    tline = None
    launched = False
    launch_line = entry_price * (1 + partial_tp) if partial_tp else None
    n = len(bars)
    for j in range(entry_i + 1, min(entry_i + 1 + max_hold, n)):
        _d, _o, c, h, low = bars[j]
        if not c or c <= 0:
            break
        high = h or c
        highest = high if highest is None else max(highest, high)
        if launch_line and not launched and highest >= launch_line:
            launched = True
        if launched and trailing_pct:
            line = highest * (1 - trailing_pct)
            tline = line if tline is None else max(tline, line)
        if stop and c <= stop:
            return (c - entry_price) / entry_price * 100, "stop_loss"
        if launched and tline is not None and c <= tline:
            return (c - entry_price) / entry_price * 100, "trailing_stop"
        if tp and c >= tp:
            return (c - entry_price) / entry_price * 100, "take_profit"
    last = min(entry_i + max_hold, n - 1)
    if last <= entry_i:
        return None, "no_data"
    c = bars[last][2]
    return (c - entry_price) / entry_price * 100, "max_hold"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--every", type=int, default=10,
                    help="每 N 个采样日取一个（控制回测量）")
    ap.add_argument("--topn", type=int, default=4)
    ap.add_argument("--stop-grid", default="0.95",
                    help="止损倍数网格，逗号分隔，如 0.95,0.90,0.85,0.80")
    ap.add_argument("--year", default=None, help="只统计该年份（如 2026）")
    args = ap.parse_args()

    import pickle
    with open(args.cache, "rb") as f:
        rows = pickle.load(f)

    from config.strategy_params import get_param
    from strategy.exit_advisor import get_max_hold
    stop_mult = float(get_param("long_ma_stop_mult"))
    partial_tp = float(get_param("long_partial_tp"))
    trailing_pct = float(get_param("long_trailing_pct"))
    max_hold = get_max_hold("long") or 60
    print(f"出场参数：止损 MA120×{stop_mult}  止盈 ×1.5  移动止盈 "
          f"启动 {partial_tp}/回撤 {trailing_pct}  max_hold={max_hold}")

    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    days = sorted(byday)[::args.every]
    print(f"采样日 {len(byday)} → 取 {len(days)} 个（every={args.every}）")

    cands = dict(CANDIDATES)

    # 1) 先按各键选出票，收集 (code, date)
    needed = set()
    picks_by_key = {}
    for name, fn in cands.items():
        cur = {}
        for d in days:
            rs = byday[d]
            if len(rs) < args.topn:
                continue
            sel = fn(rs, args.topn)
            cur[d] = sel
            for x in sel:
                needed.add((x["code"], d))
        picks_by_key[name] = cur
    print(f"需加载 {len(needed)} 个 (code, date) 的行情")

    # 2) 批量拉 OHLC（含 MA120 预热窗口 130 日 + 出场 70 日）
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    code_days = defaultdict(list)
    for code, d in needed:
        code_days[code].append(d)
    bars_by_code = {}
    codes = sorted(code_days)
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        ph = ",".join("?" * len(chunk))
        q = f"""SELECT code, trade_date, open, close, high, low FROM daily_price
                WHERE code IN ({ph}) ORDER BY code, trade_date ASC"""
        for r in conn.execute(q, chunk):
            bars_by_code.setdefault(r["code"], []).append(
                (r["trade_date"], r["open"], r["close"], r["high"], r["low"]))
    conn.close()

    def locate(code, date):
        b = bars_by_code.get(code)
        if not b:
            return None
        # 二分找 date 的下标
        lo, hi = 0, len(b) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if b[mid][0] == date:
                return mid
            if b[mid][0] < date:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def run_all(stop_m):
        out = {}
        for name, cur in picks_by_key.items():
            rets, stops = [], 0
            for d, sel in cur.items():
                if args.year and not d.startswith(args.year):
                    continue
                for x in sel:
                    idx = locate(x["code"], d)
                    if idx is None or idx < 130:
                        continue
                    b = bars_by_code[x["code"]]
                    entry = b[idx][2]
                    if not entry:
                        continue
                    ma120 = sum(v[2] for v in b[idx - 119:idx + 1] if v[2]) / 120.0
                    ret, reason = simulate_trade(
                        b, idx, entry, ma120, stop_m, 1.5,
                        partial_tp, trailing_pct, max_hold)
                    if ret is None:
                        continue
                    rets.append(ret)
                    if reason == "stop_loss":
                        stops += 1
            if not rets:
                continue
            sr = sorted(rets)
            p5 = sr[int(len(sr) * 0.05)]
            out[name] = dict(n=len(rets), mean=st.mean(rets),
                             median=st.median(rets),
                             win=sum(1 for v in rets if v > 0) / len(rets) * 100,
                             stop=stops / len(rets) * 100,
                             pos=sum(rets) * 0.25,
                             # 左尾：放宽止损必然肥左尾，必须同时看（short ATR 教训）
                             p5=p5, worst=sr[0],
                             tail=st.mean(sr[:int(len(sr) * 0.05)]))
        return out

    grid = [float(x) for x in args.stop_grid.split(",")]
    for sm in grid:
        print()
        print("=" * 98)
        print(f"止损 = MA120 × {sm}" + ("（线上现值）" if abs(sm - stop_mult) < 1e-9 else ""))
        print("=" * 98)
        print(f"{'排序键':<20}{'笔数':>6}{'均值':>9}{'中位':>9}{'胜率':>8}"
              f"{'止损率':>8}{'P5':>9}{'最差':>9}{'左尾均值':>10}")
        res = run_all(sm)
        for name, s in res.items():
            print(f"{name:<20}{s['n']:>6}{s['mean']:>+8.2f}%{s['median']:>+8.2f}%"
                  f"{s['win']:>7.1f}%{s['stop']:>7.1f}%{s['p5']:>+8.2f}%"
                  f"{s['worst']:>+8.2f}%{s['tail']:>+9.2f}%")


if __name__ == "__main__":
    main()
