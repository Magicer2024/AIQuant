"""强势突破（次日强势观察）历史重放：口径归因 + LHB 硬过滤有效性
=================================================================
目的：实盘成绩单只有 7~9 条（统计不可读）。用同一套条件在更长历史上重放，
      回答两件事：
        ① 信号本身在「2026-08-21 起的实盘窗口」是不是真的转负了？
        ② 龙虎榜硬过滤（lhb_required）在两窗之外还成立吗？

重放条件完全对齐 strategy/surge_breakout.py + core/sync.py 的写库链路：
  K 线：pct_change ∈ [min_pct, max_pct) + 量比>=1.5 + 收盘>前20日最高 + 收盘位置>=0.7
  质量：非 ST/退 + 近20日均成交额>=8000万 + 流通市值 ∈ [30亿, 3000亿]
  市值：流通市值 <= 150亿（SURGE_BREAKOUT.max_mktcap）
  门控：当日全市场 AVG(pct_change) < 1.0%（火热日不出信号）
  LHB ：同日上榜且 net_buy > 0（lhb_required）

口径：
  OC  = 次日 open -> 次日 close（模块原始设计目标口径）
  A   = 次日开盘买入；收盘判定 -4% 止损 / +8% 止盈 / 5 日到期（现网成绩单口径）
  C   = 盘中触价（跳空穿线按更差价成交，保守）
用法：
  python tools/_diag_surge_replay.py [start] [end]
"""
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")

MIN_PCT, MAX_PCT = 5.0, 9.8
VOL_MIN, CLOSE_POS_MIN, WINDOW = 1.5, 0.7, 20
MAX_MKTCAP_QUAL, MIN_MKTCAP_QUAL = 300e8, 30e8
SURGE_MAX_MKTCAP = 150e8
MIN_AMT20 = 80e6
MAX_MARKET_PCT = 1.0
STOP_PCT, TAKE_PCT, MAX_HOLD = -0.04, 0.08, 5
EXCLUDED_PREFIX = ("30", "68")


def is_st(name):
    n = name or ""
    return ("ST" in n.upper()) or ("退" in n)


def load(start, end, buf_days=60):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    buf_start = conn.execute(
        "SELECT MIN(trade_date) FROM (SELECT DISTINCT trade_date FROM daily_price "
        "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?)", (start, buf_days)
    ).fetchone()[0]
    info = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, total_shares FROM stock_info")}
    px = defaultdict(list)
    for r in conn.execute(
        """SELECT code, trade_date, open, high, low, close, volume, amount, pct_change
           FROM daily_price WHERE trade_date >= ? AND trade_date <= ?
           ORDER BY code, trade_date""", (buf_start, end)):
        px[r["code"]].append((r["trade_date"], r["open"], r["high"], r["low"],
                             r["close"], r["volume"], r["amount"], r["pct_change"]))
    lhb = set()
    for r in conn.execute(
            "SELECT trade_date, code FROM stock_lhb_detail WHERE net_buy > 0"):
        lhb.add((r["trade_date"], r["code"]))
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= ? "
        "AND trade_date <= ? ORDER BY trade_date", (start, end))]
    mkt = {}
    for r in conn.execute(
            """SELECT trade_date, AVG(pct_change) a FROM daily_price
               WHERE trade_date >= ? AND trade_date <= ? GROUP BY trade_date""",
            (start, end)):
        mkt[r["trade_date"]] = r["a"]
    conn.close()
    return info, px, lhb, dates, mkt


def build_index(px):
    """code -> {trade_date: 行下标}，避免 evaluate 里逐条线性扫描。"""
    return {c: {t[0]: i for i, t in enumerate(seq)} for c, seq in px.items()}


def evaluate(cands, px, idx, info, lhb, want_lhb):
    """cands: list of (date, code)。返回每条信号的各口径收益。"""
    out = []
    for d, code in cands:
        if want_lhb and (d, code) not in lhb:
            continue
        if (not want_lhb) and (d, code) in lhb:
            continue
        seq = px.get(code)
        if not seq:
            continue
        pos = (idx.get(code) or {}).get(d)
        if pos is None or pos < WINDOW + 1:
            continue
        cur = seq[pos]
        pct = cur[7]
        if pct is None or not (MIN_PCT <= pct < MAX_PCT):
            continue
        if not cur[5] or cur[5] <= 0 or not cur[1] or cur[1] <= 0:
            continue
        pv = [x[5] for x in seq[pos - 5:pos]]
        if len(pv) < 5 or any(v is None for v in pv) or sum(pv) <= 0:
            continue
        vol_ratio = cur[5] / (sum(pv) / 5.0)
        if vol_ratio < VOL_MIN:
            continue
        ph = [x[2] for x in seq[pos - WINDOW:pos]]
        if len(ph) < WINDOW or any(v is None for v in ph):
            continue
        if cur[4] <= max(ph):
            continue
        rng = max(cur[2] - cur[3], 1e-9)
        close_pos = (cur[4] - cur[3]) / rng
        if close_pos < CLOSE_POS_MIN:
            continue
        info_c = info.get(code) or {}
        ts = info_c.get("total_shares")
        if ts and ts > 0:
            mc = ts * cur[4]
            if not (MIN_MKTCAP_QUAL <= mc <= MAX_MKTCAP_QUAL):
                continue
            if mc > SURGE_MAX_MKTCAP:
                continue
        amt = [x[6] for x in seq[max(0, pos - 19):pos + 1]]
        if any(v is None for v in amt) or len(amt) < 20:
            continue
        if sum(amt) / len(amt) < MIN_AMT20:
            continue
        # 未来行情
        fut = seq[pos + 1:pos + 1 + MAX_HOLD + 1]
        if not fut:
            continue
        o0 = fut[0][1]
        if not o0 or o0 <= 0:
            continue
        stop = cur[4] * (1 + STOP_PCT)
        tp = cur[4] * (1 + TAKE_PCT)
        oc = (fut[0][4] - o0) / o0 * 100.0
        # 现网 A：收盘判定
        ra = None
        rex = "skipped"
        if o0 <= tp:
            rex = "hold"
            for j, p in enumerate(fut[:MAX_HOLD], start=1):
                cl = p[4]
                if not cl or cl <= 0:
                    break
                if j == 1:
                    continue
                if cl <= stop:
                    ra, rex = (cl - o0) / o0 * 100.0, "stop_loss"
                    break
                if cl >= tp:
                    ra, rex = (cl - o0) / o0 * 100.0, "take_profit"
                    break
                if j >= MAX_HOLD:
                    ra, rex = (cl - o0) / o0 * 100.0, "max_hold_days"
                    break
            if ra is None:
                ra = (fut[min(len(fut), MAX_HOLD) - 1][4] - o0) / o0 * 100.0
        # C：盘中触价，跳空穿线按开盘成交
        rc = None
        if o0 <= tp:
            for j, p in enumerate(fut[:MAX_HOLD], start=1):
                if j == 1:
                    continue
                if p[3] and p[3] <= stop:
                    fill = min(p[1], stop) if p[1] else stop
                    rc = (fill - o0) / o0 * 100.0
                    break
                if p[2] and p[2] >= tp:
                    fill = max(p[1], tp) if p[1] else tp
                    rc = (fill - o0) / o0 * 100.0
                    break
                if j >= MAX_HOLD:
                    rc = (p[4] - o0) / o0 * 100.0
                    break
            if rc is None:
                rc = (fut[min(len(fut), MAX_HOLD) - 1][4] - o0) / o0 * 100.0
        out.append({"date": d, "code": code, "name": info_c.get("name"),
                    "oc": oc, "a": ra, "c": rc, "pct_day": pct, "exit": rex,
                    "gap": (o0 - cur[4]) / cur[4] * 100.0,
                    "big": oc >= 5.0,
                    "tn": {n: ((fut[n - 1][4] - o0) / o0 * 100.0
                               if len(fut) >= n and fut[n - 1][4] else None)
                           for n in range(1, MAX_HOLD + 1)},
                    "limit": bool(fut[0][2] and fut[0][2] >= cur[4] * 1.095)})
    return out


def agg(tag, vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return f"    {tag:<16} n=0"
    n = len(vals)
    w = sum(1 for v in vals if v > 0)
    return (f"    {tag:<16} n={n:<4} 胜率 {w / n * 100:5.1f}%  "
            f"均值 {sum(vals) / n:+6.2f}%  最差 {min(vals):+6.2f}%")


def tstat(a, b):
    """两样本均值差的粗略 t 值（不假设方差齐性）。"""
    import math
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None, None
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se <= 0:
        return None, None
    return (ma - mb) / se, ma - mb


def exit_breakdown(sigs):
    """现网 A 口径出场类型分布。"""
    out = defaultdict(int)
    for s in sigs:
        out[s.get("exit") or "skipped"] += 1
    return dict(out)


def report(title, sigs, dates):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    if not sigs:
        print("  无信号")
        return
    print(agg("OC(次日开盘->收盘)", [s["oc"] for s in sigs]))
    print(agg("A 现网(收盘判定)", [s["a"] for s in sigs]))
    print(agg("C 盘中触价", [s["c"] for s in sigs]))
    nd = len(dates)
    print(f"    信号数 {len(sigs)}  /  交易日 {nd}  →  日均 {len(sigs) / max(nd, 1):.2f} 只")
    print(f"    A 口径出场分布: {exit_breakdown(sigs)}")
    # 模块自称的承诺指标（这才是「次日强势观察」该被考核的东西）
    print(f"    ── 模块承诺指标（次日口径）──")
    print(f"      次日开盘平均跳空 {sum(s['gap'] for s in sigs) / len(sigs):+5.2f}%   "
          f"次日收盘大涨率(OC>=5%) "
          f"{sum(1 for s in sigs if s['big']) / len(sigs) * 100:4.1f}%   "
          f"次日盘中触涨停 "
          f"{sum(1 for s in sigs if s['limit']) / len(sigs) * 100:4.1f}%")
    # 分月
    bym = defaultdict(list)
    for s in sigs:
        bym[s["date"][:7]].append(s)
    print("    分月（OC 口径）：")
    for m in sorted(bym):
        v = [s["oc"] for s in bym[m]]
        w = sum(1 for x in v if x > 0)
        av = [s["a"] for s in bym[m] if s["a"] is not None]
        print(f"      {m}  n={len(v):<3} 胜率 {w / len(v) * 100:5.1f}%  "
              f"OC均值 {sum(v) / len(v):+6.2f}%  A均值 "
              f"{(sum(av) / len(av) if av else 0):+6.2f}%")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-15"
    info, px, lhb, dates, mkt = load(start, end)
    print(f"窗口 {start} ~ {end}：{len(dates)} 个交易日，"
          f"{len(px)} 只有行情，龙虎榜净买记录 {len(lhb)} 条")

    # 构造全量候选（主板 + 全板），门控逐日
    idx = build_index(px)
    cands_main, cands_all, blocked_main, blocked_all = [], [], [], []
    for d in dates:
        hot = (mkt.get(d) or 0) >= MAX_MARKET_PCT
        for code, seq in px.items():
            i = idx[code].get(d)
            if i is None:
                continue
            p = seq[i][7]
            if p is None or not (MIN_PCT <= p < MAX_PCT):
                continue
            nm = (info.get(code) or {}).get("name")
            if is_st(nm):
                continue
            (blocked_all if hot else cands_all).append((d, code))
            if not code.startswith(EXCLUDED_PREFIX):
                (blocked_main if hot else cands_main).append((d, code))
    print(f"行情层候选（大阳未涨停、非 ST）：主板 {len(cands_main)}，"
          f"含双创 {len(cands_all)}；被火热日门控拦截 {len(blocked_main)}（主板）")

    main_dates = dates
    for lab, cs in (("【主板】", cands_main), ("【全部板块】", cands_all)):
        for tag, want in (("仅 K 线（无龙虎榜）", False),
                          ("K 线 + 龙虎榜硬过滤", True)):
            sigs = evaluate(cs, px, idx, info, lhb, want)
            report(f"{lab}{tag}", sigs, main_dates)

    # ── 龙虎榜硬过滤的“两窗验证”复核（对齐 tools/eval_lhb_surge_boost.py 的 SPLIT）──
    SPLIT = "2026-01-25"
    base = evaluate(cands_main, px, idx, info, lhb, False)   # 未上榜
    lhbs = evaluate(cands_main, px, idx, info, lhb, True)    # 上榜净买>0
    print(f"\n{'=' * 78}\n龙虎榜硬过滤复核（主板，SPLIT={SPLIT}）\n{'=' * 78}")
    for wn, lo, hi in (("训练窗", "1900-01-01", SPLIT),
                       ("验证窗", SPLIT, "9999-12-31")):
        b = [x["oc"] for x in base if lo <= x["date"] < hi]
        l = [x["oc"] for x in lhbs if lo <= x["date"] < hi]
        print(f"  【{wn}】未上榜 n={len(b)} 均值 "
              f"{(sum(b) / len(b) if b else 0):+.2f}% | "
              f"上榜净买>0 n={len(l)} 均值 "
              f"{(sum(l) / len(l) if l else 0):+.2f}%")
        t, diff = tstat(l, b)
        if t is not None:
            print(f"          交集 - 未上榜 = {diff:+.3f}pp，t={t:+.2f}"
                  f"{'（不显著）' if abs(t) < 2 else '（显著）'}")
    allb = [x["oc"] for x in base]
    alll = [x["oc"] for x in lhbs]
    t, diff = tstat(alll, allb)
    print(f"  【全期 2026】未上榜 n={len(allb)} 均值 {sum(allb) / len(allb):+.2f}% | "
          f"上榜净买>0 n={len(alll)} 均值 {sum(alll) / len(alll):+.2f}%")
    print(f"          交集 - 未上榜 = {diff:+.3f}pp，t={t:+.2f}"
          f"{'（不显著）' if abs(t) < 2 else '（显著）'}")

    # ── 大盘门控有效性（被拦截的火热日候选，隔日表现如何）──
    print(f"\n{'=' * 78}\n大盘门控（当日全市场均值 >= {MAX_MARKET_PCT}% 不出信号）复核\n{'=' * 78}")
    gp = evaluate(cands_main, px, idx, info, lhb, False)
    gb = evaluate(blocked_main, px, idx, info, lhb, False)
    for wn, lo, hi in (("2024-07~2026-01 训练窗", "1900-01-01", SPLIT),
                       ("2026-01~2026-09 验证窗", SPLIT, "9999-12-31"),
                       ("全期", "1900-01-01", "9999-12-31")):
        a = [x["oc"] for x in gp if lo <= x["date"] < hi]
        b = [x["oc"] for x in gb if lo <= x["date"] < hi]
        sa = (f"n={len(a):<4} 胜率 {sum(1 for x in a if x > 0) / len(a) * 100:5.1f}% "
              f"均值 {sum(a) / len(a):+6.2f}%") if a else "n=0"
        sb = (f"n={len(b):<4} 胜率 {sum(1 for x in b if x > 0) / len(b) * 100:5.1f}% "
              f"均值 {sum(b) / len(b):+6.2f}%") if b else "n=0"
        print(f"  【{wn}】")
        print(f"      放行日候选    {sa}")
        print(f"      被拦截(火热日) {sb}")
        if a and b:
            t, diff = tstat(b, a)
            print(f"      拦截 - 放行 = {diff:+.3f}pp，t={t:+.2f}"
                  f"{'（不显著）' if abs(t) < 2 else '（显著）'}")

    # ── 入场网格：次日开盘跳空过滤（低开不追 / 高开不追）──
    print(f"\n{'=' * 78}\n入场改进网格（主板，次日开盘买入前先看跳空）\n{'=' * 78}")
    for tag, mm in (("全部 K 线", cands_main), ("K线+LHB", None)):
        s = (evaluate(mm, px, idx, info, lhb, False) if mm is not None
             else evaluate(cands_main, px, idx, info, lhb, True))
        print(f"  【{tag}】基线 n={len(s)} OC 均值 "
              f"{sum(x['oc'] for x in s) / len(s):+.2f}%  "
              f"胜率 {sum(1 for x in s if x['oc'] > 0) / len(s) * 100:.1f}%")
        for lo, hi, label in ((-99, -3, "低开<=-3% 放弃"),
                              (-3, -1.5, "低开 -3%~-1.5% 放弃"),
                              (-1.5, 0, "低开 -1.5%~0 放弃"),
                              (0, 3, "高开 0~+3% 放弃"),
                              (3, 99, "高开>+3% 放弃"),
                              (-2, 2, "只做 |跳空|<2%")):
            sub = [x for x in s if lo <= x["gap"] < hi]
            if not sub:
                continue
            print(f"      剔除 {label:<22} 剩 n={len(sub):<4} "
                  f"OC 均值 {sum(x['oc'] for x in sub) / len(sub):+6.2f}%  "
                  f"胜率 {sum(1 for x in sub if x['oc'] > 0) / len(sub) * 100:5.1f}%  "
                  f"大涨率 {sum(1 for x in sub if x['big']) / len(sub) * 100:4.1f}%")

    # ── 持有期漂移：T+n 相对买入价（次日开盘）的均值/胜率 ──
    print(f"\n{'=' * 78}\n持有期漂移（次日开盘买入后第 n 个交易日收盘，主板）\n{'=' * 78}")
    for tag, want in (("仅 K 线", False), ("K线+LHB", True)):
        s = evaluate(cands_main, px, idx, info, lhb, want)
        print(f"  【{tag}】n={len(s)}")
        for n in range(1, MAX_HOLD + 1):
            v = [x["tn"][n] for x in s if x["tn"][n] is not None]
            if not v:
                continue
            w = sum(1 for x in v if x > 0)
            print(f"      T+{n}  锁定样本 n={len(v):<4} 胜率 {w / len(v) * 100:5.1f}%  "
                  f"均值 {sum(v) / len(v):+6.2f}%")

    # 实盘窗口切片
    live_start = "2026-08-21"
    for tag, want in (("仅 K 线", False), ("+龙虎榜", True)):
        sigs = evaluate(cands_main, px, idx, info, lhb, want)
        sigs = [s for s in sigs if s["date"] >= live_start]
        report(f"【主板·实盘窗口 {live_start}~{end}】{tag}", sigs,
               [d for d in dates if d >= live_start])


if __name__ == "__main__":
    main()
