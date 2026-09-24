"""深挖「频繁上榜（活跃度）」这条线
======================================================================
上一轮结论：机构**买卖方向**无 alpha（净买 +0.72% vs 净卖 +0.66%，t=+0.53）；
但「近 20 日机构榜出现 ≥4 次」跳出 +1.22%/t=+2.24 —— 疑似挑参数，需过三关：

  关 1  分年 + 剔除单年都要成立（铁律 3）
  关 2  组合口径（每日 top-N 等权）才是最终判据（铁律 7），
        且对**日度收益序列**做 t 检验（截面 t 会因个股相关而虚高）
  关 3  控制混杂：频繁上榜 = 高波动 / 高涨幅 / 小市值？
        主判据是**桶内增量 Δ**，不是绝对收益（铁律 6）

对照的两类榜单：
  JG  = stock_lhb_jg_detail（机构席位榜，window_days=1 单日口径）
  ALL = stock_lhb_detail（全龙虎榜；⚠ 金额字段含 3 日累计污染，本脚本只用「上榜与否」）

用法：
  python tools/_diag_lhb_freq.py [start] [end]
"""
import bisect
import math
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")

LOOKBACK = 20          # 回看交易日数
MAX_FUT = 11
MIN_AMT20 = 80e6       # 流动性门槛（与 rec_filters 一致）
MIN_MKTCAP, MAX_MKTCAP = 30e8, 3000e8
EXCLUDE_PREFIX = ("688", "689", "920", "8", "4", "9")   # 用户要求：不要科创板 / 北交所


def excluded(code):
    c = str(code)
    return c.startswith(EXCLUDE_PREFIX)


def is_st(name):
    n = (name or "").upper()
    return "ST" in n or "退" in n


def load(start, end, buf_days=70):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    info = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, total_shares FROM stock_info")}

    # 机构榜（单日口径，按票归并日期）
    jg = defaultdict(list)
    for r in conn.execute(
            """SELECT trade_date, code, name, pct_change, jg_net_buy,
                      buyer_jg_count, float_mkt_cap
               FROM stock_lhb_jg_detail
               WHERE window_days = 1 AND trade_date >= date(?, ?) AND trade_date <= ?
               ORDER BY code, trade_date""", (start, f"-{buf_days} days", end)):
        jg[r["code"]].append((r["trade_date"], r["jg_net_buy"], r["buyer_jg_count"],
                              r["float_mkt_cap"]))

    # 全龙虎榜（只用上榜事实）
    allb = defaultdict(list)
    for r in conn.execute(
            """SELECT trade_date, code, pct_change, float_mkt_cap
               FROM stock_lhb_detail
               WHERE trade_date >= date(?, ?) AND trade_date <= ?
               ORDER BY code, trade_date""", (start, f"-{buf_days} days", end)):
        allb[r["code"]].append((r["trade_date"], r["pct_change"], r["float_mkt_cap"]))

    codes = {c for c in set(jg) | set(allb) if not excluded(c)}
    px = {}
    for code in codes:
        rows = conn.execute(
            """SELECT trade_date, open, high, low, close, amount
               FROM daily_price
               WHERE code = ? AND trade_date >= date(?, ?) AND trade_date <= date(?, '+25 days')
               ORDER BY trade_date""",
            (code, start, f"-{buf_days} days", end)).fetchall()
        if len(rows) > LOOKBACK + 5:
            px[code] = [tuple(r) for r in rows]

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= ? "
        "AND trade_date <= ? ORDER BY trade_date", (start, end))]

    # 大盘基线：中证1000/中证500 收盘→收盘
    idx_px = {}
    for r in conn.execute(
            """SELECT code, trade_date, close FROM index_daily
               WHERE code IN ('000852','000905')
                 AND trade_date >= ? AND trade_date <= date(?, '+25 days')
               ORDER BY code, trade_date""", (start, end)):
        idx_px.setdefault(r["code"], []).append((r["trade_date"], r["close"]))
    conn.close()
    return info, px, jg, allb, dates, idx_px


def index_baseline(idx_px, dates, n):
    out = {}
    for code, seq in idx_px.items():
        idx = {t: i for i, (t, _) in enumerate(seq)}
        for t in dates:
            j = idx.get(t)
            if j is None or j + n >= len(seq):
                continue
            c0, cn = seq[j][1], seq[j + n][1]
            if not c0 or not cn or c0 <= 0:
                continue
            out.setdefault(t, []).append((cn - c0) / c0 * 100.0)
    return {d: sum(v) / len(v) for d, v in out.items() if v}


def build(px, info, jg, allb, dates):
    """对每个「当日上榜」事件构造样本，附带近 LOOKBACK 日的上榜频次与混杂变量。"""
    dset = set(dates)
    sigs = []
    for code, seq in px.items():
        idx = {t[0]: i for i, t in enumerate(seq)}
        ic = info.get(code) or {}
        name = ic.get("name") or ""
        if is_st(name):
            continue
        jg_d = [x[0] for x in jg.get(code, ())]
        al_d = [x[0] for x in allb.get(code, ())]
        al_pct = {x[0]: x[1] for x in allb.get(code, ())}
        fmc_by_d = {x[0]: x[3] for x in jg.get(code, ())}
        for x in allb.get(code, ()):
            fmc_by_d.setdefault(x[0], x[2])
        for d in sorted(set(jg_d) | set(al_d)):
            if d not in dset:
                continue
            pos = idx.get(d)
            if pos is None or pos < LOOKBACK + 5:
                continue
            cur = seq[pos]
            close = cur[4]
            if not close or close <= 0:
                continue
            fut = seq[pos + 1:pos + 1 + MAX_FUT]
            if not fut or not fut[0][1] or fut[0][1] <= 0:
                continue
            # 一字板（买不到）
            if fut[0][2] and fut[0][3] and fut[0][2] == fut[0][3] == fut[0][1]:
                continue
            lo_d = seq[max(0, pos - LOOKBACK)][0]
            a0 = bisect.bisect_left(al_d, lo_d)
            a1 = bisect.bisect_right(al_d, d)
            j0 = bisect.bisect_left(jg_d, lo_d)
            j1 = bisect.bisect_right(jg_d, d)
            cnt_all = a1 - a0
            cnt_jg = j1 - j0
            # 回看窗口内行情（波动/动量）
            win = seq[max(0, pos - 19):pos + 1]
            amt = [w[5] for w in win]
            if len(amt) < 20 or any(v is None for v in amt):
                continue
            amt20 = sum(amt) / 20.0
            fmc = fmc_by_d.get(d)
            mc = (fmc * 1e8) if fmc else None
            rets = []
            for i in range(1, len(win)):
                p0, p1 = win[i - 1][4], win[i][4]
                if p0 and p1 and p0 > 0:
                    rets.append((p1 - p0) / p0)
            vol20 = (math.sqrt(sum(r * r for r in rets) / len(rets)) * 100
                     if rets else None)
            mom20 = ((close - win[0][4]) / win[0][4] * 100) if win[0][4] else None
            pct = al_pct.get(d)
            if pct is None:
                p0 = seq[pos - 1][4] if pos else None
                pct = ((close - p0) / p0 * 100) if p0 else 0.0
            sigs.append({
                "date": d, "code": code, "name": name,
                "close": close, "pct": pct, "amt20": amt20, "mc": mc,
                "vol20": vol20, "mom20": mom20, "fut": fut,
                "cnt_all": cnt_all, "cnt_jg": cnt_jg,
                "on_jg": d in set(jg_d[max(0, j1 - 1):j1]),
            })
    return sigs


def ret_hold(sig, n):
    f = sig["fut"]
    if len(f) < n:
        return None
    o0, c = f[0][1], f[n - 1][4]
    if not o0 or o0 <= 0 or not c or c <= 0:
        return None
    return (c - o0) / o0 * 100.0


def gap(sig):
    """T+1 跳空 %：开盘相对信号日收盘。"""
    o0 = sig["fut"][0][1]
    c = sig["close"]
    return (o0 - c) / c * 100.0 if (o0 and c) else None


def agg(tag, vals):
    n = len(vals)
    if not n:
        return f"  {tag:<34} n=0"
    w = sum(1 for v in vals if v > 0)
    sv = sorted(vals)
    return (f"  {tag:<34} n={n:<6} 胜率 {w / n * 100:5.1f}%  均值 "
            f"{sum(vals) / n:+6.2f}%  中位 {sv[n // 2]:+6.2f}%  最差 {min(vals):+6.2f}%")


def tstat(a, b):
    if len(a) < 2 or len(b) < 2:
        return None, None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se <= 0:
        return None, None
    return (ma - mb) / se, ma - mb


def t_1samp(v):
    """日度收益序列的单样本 t 检验（组合口径的正确检验方式）。"""
    n = len(v)
    if n < 3:
        return None, None, None
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    t = m / (sd / math.sqrt(n)) if sd > 0 else None
    return t, m, sd


def portfolio(sigs, keyfn, n_day, top_n, reverse=False):
    """组合口径：每个交易日按 keyfn 降序取 top_n，等权持有 n_day 日。
    返回 (日度收益序列, 每日选中标数序列)。"""
    by_date = defaultdict(list)
    for s in sigs:
        by_date[s["date"]].append(s)
    daily, sizes = [], []
    for d in sorted(by_date):
        pool = by_date[d]
        pool.sort(key=keyfn, reverse=not reverse)
        sel = pool[:top_n]
        vals = [x for s in sel for x in [ret_hold(s, n_day)] if x is not None]
        if not vals:
            continue
        daily.append(sum(vals) / len(vals))
        sizes.append(len(vals))
    return daily, sizes


def report_combo(tag, sigs, keyfn, n_day, top_n):
    daily, sizes = portfolio(sigs, keyfn, n_day, top_n)
    if len(daily) < 10:
        print(f"  {tag:<40} 样本不足（{len(daily)} 日）")
        return
    t, m, sd = t_1samp(daily)
    win = sum(1 for x in daily if x > 0) / len(daily) * 100
    cum = 1.0
    for x in daily:
        cum *= (1 + x / 100)
    print(f"  {tag:<40} {len(daily):>4}日 日均 {m:+.3f}% 日胜率 {win:5.1f}% "
          f"t={t:+5.2f}{'  ✓' if abs(t or 0) >= 2 else '   '} "
          f"累计 {(cum - 1) * 100:+7.1f}%  均选{sum(sizes)/len(sizes):.1f}只")


def bucket_delta(sigs, ctrl_key, edges, label, freq_key, cut, n_day):
    """铁律 6：在控制变量分档**内部**比较频次高/低的 Δ（桶内增量）。"""
    print(f"    [{label} · 控制后看「{freq_key}>={cut}」的桶内增量，持有 {n_day} 日]")
    tot_hi = tot_lo = 0
    for lo, hi in edges:
        pool = [s for s in sigs if ctrl_key(s) is not None and lo <= ctrl_key(s) < hi]
        vhi = [x for s in pool if s[freq_key] >= cut for x in [ret_hold(s, n_day)] if x is not None]
        vlo = [x for s in pool if s[freq_key] < cut for x in [ret_hold(s, n_day)] if x is not None]
        tot_hi += len(vhi); tot_lo += len(vlo)
        if len(vhi) < 20 or len(vlo) < 20:
            print(f"      {lo:>7}~{hi:<7}: 样本不足 hi={len(vhi)} lo={len(vlo)}")
            continue
        mhi, mlo = sum(vhi) / len(vhi), sum(vlo) / len(vlo)
        t, d = tstat(vhi, vlo)
        whi = sum(1 for x in vhi if x > 0) / len(vhi) * 100
        wlo = sum(1 for x in vlo if x > 0) / len(vlo) * 100
        print(f"      {lo:>7}~{hi:<7}: hi n={len(vhi):<5}{mhi:+6.2f}%/胜{whi:4.1f}%  "
              f"lo n={len(vlo):<5}{mlo:+6.2f}%/胜{wlo:4.1f}%  Δ={d:+6.2f}pp t={t:+5.2f}"
              f"{'  ✓' if abs(t or 0) >= 2 else ''}")
    print(f"      （合计 hi={tot_hi} lo={tot_lo}）")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-22"
    out_path = os.path.join(ROOT, "tools", "_lhb_freq_out.txt")
    sys.stdout = open(out_path, "w", encoding="utf-8", newline="\n")

    info, px, jg, allb, dates, idx_px = load(start, end)
    sigs = build(px, info, jg, allb, dates)
    print(f"窗口 {start} ~ {end} | {len(dates)} 交易日 | 行情覆盖 {len(px)} 只 "
          f"（已剔除科创板/北交所/ST）")
    print(f"当日上榜事件样本 {len(sigs)} 条 | 其中当日也在机构榜 {sum(1 for s in sigs if s['on_jg'])} 条")
    from collections import Counter
    print(f"  cnt_all 分布: {sorted(Counter(s['cnt_all'] for s in sigs).items())[:10]}")
    print(f"  cnt_jg  分布: {sorted(Counter(s['cnt_jg'] for s in sigs).items())[:10]}")

    mkt = {n: index_baseline(idx_px, dates, n) for n in (1, 5, 10)}
    mavg1 = sum(mkt[1].values()) / len(mkt[1]) if mkt[1] else 0
    print(f"  大盘基线（中证1000/500，T收盘→T+1收盘）日均 {mavg1:+.3f}%")

    # ── 关 0：频次分档的绝对收益 ────────────────────────────
    for fk, flab in (("cnt_all", "全龙虎榜"), ("cnt_jg", "机构榜")):
        print(f"\n{'=' * 112}\n【关0】近 {LOOKBACK} 日上榜次数分档（{flab}）· 持有 1 日\n{'=' * 112}")
        for lo, hi in ((1, 2), (2, 3), (3, 4), (4, 6), (6, 99)):
            v = [x for s in sigs if lo <= s[fk] < hi for x in [ret_hold(s, 1)] if x is not None]
            print(agg(f"  {fk} {lo}~{hi if hi < 99 else '+'}", v))
        allv = [x for s in sigs for x in [ret_hold(s, 1)] if x is not None]
        print(agg("  全部上榜事件(基线)", allv))

    # ── 关 1：分年 + 剔除单年（组合口径）────────────────────
    print(f"\n{'=' * 112}\n【关1】组合口径 · 每日 top-N（按近 {LOOKBACK} 日上榜次数降序）· 持有 1 日\n"
          f"      对**日度收益序列**做 t 检验（|t|>=2 才算显著）\n{'=' * 112}")
    for fk, flab in (("cnt_all", "全榜次数"), ("cnt_jg", "机构榜次数")):
        for top_n in (5, 10):
            report_combo(f"{flab} top{top_n}", sigs, lambda s, k=fk: s[k], 1, top_n)
    report_combo("对照·当日涨幅 top5（纯动量）", sigs, lambda s: s["pct"], 1, 5)
    report_combo("对照·随机（全上榜池等权）", sigs, lambda s: 0, 1, 5)

    print(f"\n  ── 分年（组合口径 top10 / 全榜次数）──")
    for yr in ("2024", "2025", "2026"):
        sub = [s for s in sigs if s["date"].startswith(yr)]
        report_combo(f"  {yr} 全榜次数 top10", sub, lambda s: s["cnt_all"], 1, 10)
    print(f"\n  ── 剔除单年（铁律 3）──")
    for yr in ("2024", "2025", "2026"):
        sub = [s for s in sigs if not s["date"].startswith(yr)]
        report_combo(f"  剔除{yr} 全榜次数 top10", sub, lambda s: s["cnt_all"], 1, 10)
    print(f"\n  ── 分年（组合口径 top10 / 机构榜次数）──")
    for yr in ("2024", "2025", "2026"):
        sub = [s for s in sigs if s["date"].startswith(yr)]
        report_combo(f"  {yr} 机构榜次数 top10", sub, lambda s: s["cnt_jg"], 1, 10)
    for yr in ("2024", "2025", "2026"):
        sub = [s for s in sigs if not s["date"].startswith(yr)]
        report_combo(f"  剔除{yr} 机构榜次数 top10", sub, lambda s: s["cnt_jg"], 1, 10)

    # ── 关 2：控制混杂（铁律 6 桶内增量）─────────────────────
    print(f"\n{'=' * 112}\n【关2】控制混杂后看桶内增量 Δ（频繁上榜是否只是高波动/高涨幅/小市值的代理）\n{'=' * 112}")
    print("  ▸ 控制当日涨幅")
    bucket_delta(sigs, lambda s: s["pct"],
                 [(-99, 0), (0, 3), (3, 7), (7, 9.8), (9.8, 99)],
                 "当日涨幅%", "cnt_all", 4, 1)
    print("  ▸ 控制近20日累计涨幅（动量）")
    bucket_delta(sigs, lambda s: s["mom20"],
                 [(-99, -10), (-10, 0), (0, 10), (10, 25), (25, 99)],
                 "近20日涨幅%", "cnt_all", 4, 1)
    print("  ▸ 控制20日波动率")
    bucket_delta(sigs, lambda s: s["vol20"],
                 [(0, 2), (2, 3.5), (3.5, 5), (5, 7), (7, 99)],
                 "20日波动率%", "cnt_all", 4, 1)
    print("  ▸ 控制流通市值（亿元）")
    bucket_delta(sigs, lambda s: (s["mc"] / 1e8) if s["mc"] else None,
                 [(0, 40), (40, 80), (80, 200), (200, 600), (600, 99999)],
                 "流通市值亿", "cnt_all", 4, 1)

    # ── 关 3：现实约束（跳空 / 一字板 / 流动性）──────────────
    print(f"\n{'=' * 112}\n【关3】现实约束：频繁上榜票的次日跳空与可买性\n{'=' * 112}")
    for lo, hi in ((1, 2), (2, 3), (3, 4), (4, 6), (6, 99)):
        g = [x for s in sigs if lo <= s["cnt_all"] < hi for x in [gap(s)] if x is not None]
        if g:
            print(f"  cnt_all {lo}~{hi if hi < 99 else '+':<2}: 平均跳空 {sum(g)/len(g):+6.2f}%  "
                  f"高开>3%占比 {sum(1 for x in g if x > 3)/len(g)*100:5.1f}%  "
                  f"低开占比 {sum(1 for x in g if x < 0)/len(g)*100:5.1f}%")
    hi4 = [s for s in sigs if s["cnt_all"] >= 4]
    for thr in (0, 3, 5):
        sub = [s for s in hi4 if (gap(s) is not None and gap(s) <= thr)]
        report_combo(f"  cnt_all>=4 且跳空<={thr}% top10", sub,
                     lambda s: s["cnt_all"], 1, 10)

    # ── 关 4：持有期与出场纪律 ──────────────────────────────
    print(f"\n{'=' * 112}\n【关4】组合口径 · 不同持有期（全榜次数 top10）\n{'=' * 112}")
    for n in (1, 2, 3, 5, 10):
        report_combo(f"  持有 {n} 日 top10", sigs, lambda s: s["cnt_all"], n, 10)

    print(f"\n{'=' * 112}\n【关4b】组合口径 · 叠加流动性/市值门槛（与实盘票池一致）\n{'=' * 112}")
    clean = [s for s in sigs if s["amt20"] >= MIN_AMT20
             and s["mc"] and MIN_MKTCAP <= s["mc"] <= MAX_MKTCAP]
    print(f"  过滤后样本 {len(clean)} / {len(sigs)}")
    for top_n in (5, 10):
        report_combo(f"  干净池 全榜次数 top{top_n}", clean, lambda s: s["cnt_all"], 1, top_n)
    for yr in ("2024", "2025", "2026"):
        sub = [s for s in clean if s["date"].startswith(yr)]
        report_combo(f"    分年 {yr} top10", sub, lambda s: s["cnt_all"], 1, 10)
    for yr in ("2024", "2025", "2026"):
        sub = [s for s in clean if not s["date"].startswith(yr)]
        report_combo(f"    剔除{yr} top10", sub, lambda s: s["cnt_all"], 1, 10)

    sys.stdout.flush()
    sys.stdout = sys.__stdout__
    print(f"结果已写入 {out_path}")


if __name__ == "__main__":
    main()
