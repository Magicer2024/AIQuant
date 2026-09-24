"""long 选股重设计 —— 候选方案对比（**带真实出场纪律**，分年 + 2026）。

背景（已确证，见 reference/long-selection.md）
----------------------------------------------
1. 排序键零信息量：fusion_score 只有 3 档，top4 等价从 28 只同分票随机抽
2. 票池负 alpha：score>=2.0 的池子长期超额 -0.92%，t=-7.30（11 年里 8 年为负）
3. 逐 mask 拆解（_diag_long_mask.py）：
     - 回撤<40% 单独 t=-14.90（最恶劣，剔除超跌票 = 剔除反转收益来源）
     - 趋势+斜率(3) -0.79% / 趋势+斜率+低波(7) **+0.56%** / 四项全满足(15) **+0.53%**
   ⇒ 低波在「趋势+斜率已满足」的前提下是**正贡献筛选器**（208只→39只）

重设计思路
----------
把「离散门槛」改成「连续排序键」：票池放宽到 mask=3（趋势+斜率，208 只/日），
再用**低波动类连续键**排序取 top4。这样池子大、每层都可解释，且避免
「门槛卡在 35% 边界上」的悬崖效应。

本脚本对比所有候选组合，**主判据 = 带出场纪律的分年超额**（铁律：不能用裸持有表选键）。

用法
----
    python tools/_diag_long_redesign.py [--every 5] [--topn 4]
"""

import argparse
import math
import os
import pickle
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")
CACHE = os.path.join(ROOT, "logs", "_long_pool_cache.pkl")

B_TREND, B_SLOPE, B_LOWVOL, B_DD = 1, 2, 4, 8


def score_of(mask):
    return ((1.0 if mask & B_TREND else 0) + (1.0 if mask & B_SLOPE else 0)
            + (0.5 if mask & B_LOWVOL else 0) + (0.5 if mask & B_DD else 0))


# ─────────────────────────────────────────────
# 候选方案：(票池过滤, 排序函数)
# ─────────────────────────────────────────────

def pool_score_ge(thr):
    return lambda r: score_of(r["mask"]) >= thr


def pool_mask_eq(m):
    return lambda r: r["mask"] == m


def pool_mask_in(ms):
    return lambda r: r["mask"] in ms


def key_none(rs, n):
    """现状语义：fusion DESC（3 档）→ 实际按 (score, vol_ratio) 排序，同分内近乎随机。

    注意：池缓存只存 mask，score 需现算。
    """
    return sorted(rs, key=lambda x: (score_of(x["mask"]), x.get("vol_ratio") or 0),
                  reverse=True)[:n]


def key_by(k, reverse=False):
    def fn(rs, n):
        rs2 = [r for r in rs if r.get(k) is not None]
        if len(rs2) < n:
            rs2 = rs
        return sorted(rs2, key=lambda x: x[k], reverse=reverse)[:n]
    return fn


def key_composite(w_vol, w_dd):
    """横截面百分位排名加权（升序取 topn）。w_dd 为正=偏好回撤大的（反转）。"""
    def fn(rs, n):
        m = len(rs)
        vol_ok = [r for r in rs if r.get("vol60") is not None]
        dd_ok = [r for r in rs if r.get("dd250") is not None]
        if not vol_ok or not dd_ok:
            return rs[:n]
        bv = {id(x): i / max(1, len(vol_ok) - 1)
              for i, x in enumerate(sorted(vol_ok, key=lambda x: x["vol60"]))}
        bd = {id(x): i / max(1, len(dd_ok) - 1)
              for i, x in enumerate(sorted(dd_ok, key=lambda x: x["dd250"]))}
        return sorted(rs, key=lambda x: w_vol * bv.get(id(x), 0.5)
                      + w_dd * bd.get(id(x), 0.5))[:n]
    return fn


def pool_bits(bits):
    """票池 = 指定 bit 全部满足（其它位随意）。bits=3 ⇒ 趋势+斜率，日均约 208 只。"""
    return lambda r: (r["mask"] & bits) == bits


def key_two_stage(k1, k2, mult):
    """两层排序（SQL 可实现版）：先按 k1 升序取 n×mult 的池，池内按 k2 升序取 n。

    ⚠ 连续值排序键**不能**靠字典序组成多键（同分几乎不存在 ⇒ 退化成第一键），
    必须走「先切池、再排序」的两层结构。
    """
    def fn(rs, n):
        pool = sorted(rs, key=lambda x: x.get(k1) if x.get(k1) is not None else 1e9)
        pool = pool[:max(n, int(n * mult))]
        return sorted(pool, key=lambda x: x.get(k2) if x.get(k2) is not None else 1e9)[:n]
    return fn


PLANS = {
    "① 现状 score>=2.0 + fusion": (pool_score_ge(2.0), key_none),
    "② mask=15 + 随机(同分)": (pool_mask_eq(15), key_none),
    "③ mask=15 + vol60 ASC": (pool_mask_eq(15), key_by("vol60")),
    "④ mask=15 + vol_ratio ASC": (pool_mask_eq(15), key_by("vol_ratio")),
    "⑤ 低波池(43) + vol60 ASC": (pool_mask_in({3, 7, 15}), key_by("vol60")),
    "⑥ 低波池(43) + vol_ratio ASC": (pool_mask_in({3, 7, 15}), key_by("vol_ratio")),
    "⑦ 低波池(43) + vol60:dd(3:1)": (
        pool_mask_in({3, 7, 15}), key_composite(0.75, 0.25)),
    "⑧ 低波池(43) + vol60:dd(1:1)": (
        pool_mask_in({3, 7, 15}), key_composite(0.5, 0.5)),
    "⑨ 低波池(43) + ext_ma120 ASC": (pool_mask_in({3, 7, 15}), key_by("ext_ma120")),
    "⑩ 低波池(43) + mom120 ASC(反转)": (pool_mask_in({3, 7, 15}), key_by("mom120")),
    "⑪ 低波池(43) + dd250 DESC": (pool_mask_in({3, 7, 15}), key_by("dd250", True)),
    # ── 核心重设计：去掉低波硬门槛，改用连续排序键（避免门槛悬崖效应）──
    "⑬ 趋势池(208) + vol60 ASC": (pool_bits(3), key_by("vol60")),
    "⑭ 趋势池(208) + vol60:dd(3:1)": (pool_bits(3), key_composite(0.75, 0.25)),
    "⑮ 趋势池 + vol60→dd250 两层5x": (pool_bits(3), key_two_stage("vol60", "dd250", 5)),
    "⑯ 趋势池 + vol60→dd250 两层3x": (pool_bits(3), key_two_stage("vol60", "dd250", 3)),
    "⑰ 趋势池 + vol60→dd250 两层8x": (pool_bits(3), key_two_stage("vol60", "dd250", 8)),
    "⑱ 趋势池 + vol60 ASC→随机(对照)": (pool_bits(3), key_two_stage("vol60", "vol60", 5)),
    # ── 低波池内的两层版（SQL 可实现，复用现有 long_second_order_clause 基础设施）──
    "⑲ 低波池 + vol60→dd250 两层2x": (
        pool_mask_in({3, 7, 15}), key_two_stage("vol60", "dd250", 2)),
    "⑳ 低波池 + vol60→dd250 两层3x": (
        pool_mask_in({3, 7, 15}), key_two_stage("vol60", "dd250", 3)),
    "㉑ 低波池 + vol60→dd250 两层5x": (
        pool_mask_in({3, 7, 15}), key_two_stage("vol60", "dd250", 5)),
    "㉒ 低波池(仅mask7|15) + vol60:dd(3:1)": (
        lambda r: (r["mask"] & 7) == 7, key_composite(0.75, 0.25)),
}


def simulate_trade(bars, entry_i, entry_price, ma120_at_entry,
                   stop_mult, tp_mult, partial_tp, trailing_pct, max_hold):
    """与 outcome_tracker._evaluate_mid_long 同构（收盘判定）。"""
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
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--topn", type=int, default=4)
    args = ap.parse_args()

    with open(args.cache, "rb") as f:
        rows = pickle.load(f)
    print(f"载入 {len(rows):,} 条 / {len(set(r['date'] for r in rows))} 采样日")

    from config.strategy_params import get_param
    from strategy.exit_advisor import get_max_hold
    stop_mult = float(get_param("long_ma_stop_mult"))
    partial_tp = float(get_param("long_partial_tp"))
    trailing_pct = float(get_param("long_trailing_pct"))
    max_hold = get_max_hold("long") or 60
    print(f"出场：止损 MA120×{stop_mult}  止盈 ×1.5  移动止盈 "
          f"{partial_tp}/{trailing_pct}  max_hold={max_hold}")

    byday = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    days_all = sorted(byday)
    days = days_all[::args.every]
    print(f"采样 {len(days)} / {len(days_all)} 日（every={args.every}）")

    # 1) 选票
    picks = {}
    needed = set()
    for name, (pf, kf) in PLANS.items():
        cur = {}
        for d in days:
            rs = [r for r in byday[d] if pf(r)]
            if len(rs) < args.topn:
                continue
            sel = kf(rs, args.topn)
            cur[d] = sel
            for x in sel:
                needed.add((x["code"], d))
        picks[name] = cur
    print(f"需加载 {len(needed):,} 个 (code,date)")

    # 2) 拉行情
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
    print(f"载入 {len(bars_by_code)} 只行情")

    def locate(code, date):
        b = bars_by_code.get(code)
        if not b:
            return None
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

    # 3) 模拟
    trades = {}
    for name, cur in picks.items():
        out = []
        for d, sel in cur.items():
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
                    b, idx, entry, ma120, stop_mult, 1.5,
                    partial_tp, trailing_pct, max_hold)
                if ret is None:
                    continue
                out.append((d, ret, reason))
        trades[name] = out

    def summarize(name, out, year=None):
        if year:
            out = [t for t in out if t[0].startswith(year)]
        if not out:
            return None
        v = [t[1] for t in out]
        sr = sorted(v)
        return dict(n=len(v), mean=st.mean(v), med=st.median(v),
                    win=sum(1 for x in v if x > 0) / len(v) * 100,
                    stop=sum(1 for t in out if t[2] == "stop_loss") / len(out) * 100,
                    p5=sr[int(len(sr) * 0.05)], tail=st.mean(sr[:max(1, int(len(sr) * .05))]))

    for year in (None, "2026"):
        print()
        print("=" * 112)
        print(f"带出场纪律  top{args.topn}  " + ("【2026 年】" if year else "【全期】"))
        print("=" * 112)
        print(f"{'方案':<38}{'笔数':>6}{'均值':>9}{'中位':>9}{'胜率':>8}"
              f"{'止损率':>8}{'P5':>9}{'左尾':>9}")
        for name in PLANS:
            s = summarize(name, trades.get(name, []), year)
            if not s:
                print(f"{name:<38} 样本不足")
                continue
            print(f"{name:<38}{s['n']:>6}{s['mean']:>+8.2f}%{s['med']:>+8.2f}%"
                  f"{s['win']:>7.1f}%{s['stop']:>7.1f}%{s['p5']:>+8.2f}%"
                  f"{s['tail']:>+8.2f}%")

    # 分年稳健性（对最优候选）
    print()
    print("=" * 112)
    print("分年稳健性（均值 / 胜率）—— 只列代表性方案")
    print("=" * 112)
    focus = ["① 现状 score>=2.0 + fusion", "③ mask=15 + vol60 ASC",
             "⑤ 低波池(43) + vol60 ASC",
             "⑦ 低波池(43) + vol60:dd(3:1)",
             "⑬ 趋势池(208) + vol60 ASC",
             "⑭ 趋势池(208) + vol60:dd(3:1)",
             "⑲ 低波池 + vol60→dd250 两层2x",
             "⑳ 低波池 + vol60→dd250 两层3x",
             "㉑ 低波池 + vol60→dd250 两层5x"]
    years = sorted({d[:4] for d in days})
    print(f"{'方案':<38}" + "".join(f"{y:>13}" for y in years))
    for name in focus:
        cells = []
        for y in years:
            s = summarize(name, trades.get(name, []), y)
            cells.append(f"{s['mean']:+6.2f}%/{s['win']:4.0f}%" if s else "     n/a    ")
        print(f"{name:<38}" + "".join(f"{c:>13}" for c in cells))


if __name__ == "__main__":
    main()
