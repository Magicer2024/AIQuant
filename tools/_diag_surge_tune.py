"""次日强势观察（强势突破）· 参数调优网格
======================================================================
目的：在**时点正确**的口径下，找出一组能同时提升胜率与均值的入场 + 出场规则。

背景（见 `reference/surge-observation.md`）：
  现网成绩单口径 = 次日开盘买入 + 收盘判定 -4% 止损 / +8% 止盈 / 5 日到期，
  且 **建仓当日不判出场**（`_surge_history` 的 `j == 1: continue`）。
  两个可疑点：
    ① 建仓当日不判出场 —— 跳空破位的票要拖到最后一天才止损（实测最差 -26%）。
    ② 持仓 5 日 —— 信号只有 T+1 一天有效性，T+2 起负漂移。

⚠ 口径陷阱：`tools/_diag_surge_perf.py` 的 C 口径（盘中触价但跳过 T+1）**有前视偏差** ——
  它让「开盘已跌破止损线」的票在几天后反弹回止损线时按止损价成交，现实中不可能。
  本脚本单列 `judge_entry_day`（建仓当日纳入判定）与 `intraday`（日内触价）两种真实口径。

用法：
  python tools/_diag_surge_tune.py [start] [end]
  python tools/_diag_surge_tune.py 2024-07-01 2026-09-22
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
EXCLUDED_PREFIX = ("30", "68")
MAX_HOLD = 5


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


def collect(px, info, dates, mkt, lhb):
    """构造全部信号样本（K 线 + 质量 + 市值 + 门控），带 lhb 标记，并预取未来行情。"""
    idx = {c: {t[0]: i for i, t in enumerate(seq)} for c, seq in px.items()}
    sigs = []
    for d in dates:
        if (mkt.get(d) or 0) >= MAX_MARKET_PCT:
            continue
        for code, seq in px.items():
            pos = idx[code].get(d)
            if pos is None or pos < WINDOW + 1:
                continue
            cur = seq[pos]
            pct = cur[7]
            if pct is None or not (MIN_PCT <= pct < MAX_PCT):
                continue
            if not cur[5] or cur[5] <= 0 or not cur[4] or cur[4] <= 0:
                continue
            ic = info.get(code) or {}
            if is_st(ic.get("name")):
                continue
            pv = [x[5] for x in seq[pos - 5:pos]]
            if len(pv) < 5 or any(v is None for v in pv) or sum(pv) <= 0:
                continue
            if cur[5] / (sum(pv) / 5.0) < VOL_MIN:
                continue
            ph = [x[2] for x in seq[pos - WINDOW:pos]]
            if len(ph) < WINDOW or any(v is None for v in ph) or cur[4] <= max(ph):
                continue
            rng = max(cur[2] - cur[3], 1e-9)
            if (cur[4] - cur[3]) / rng < CLOSE_POS_MIN:
                continue
            ts = ic.get("total_shares")
            if ts and ts > 0:
                mc = ts * cur[4]
                if not (MIN_MKTCAP_QUAL <= mc <= MAX_MKTCAP_QUAL):
                    continue
                if mc > SURGE_MAX_MKTCAP:
                    continue
            amt = [x[6] for x in seq[max(0, pos - 19):pos + 1]]
            if any(v is None for v in amt) or len(amt) < 20 or sum(amt) / 20 < MIN_AMT20:
                continue
            fut = seq[pos + 1:pos + 1 + MAX_HOLD + 2]
            if not fut or not fut[0][1] or fut[0][1] <= 0:
                continue
            sigs.append({"date": d, "code": code, "name": ic.get("name"),
                         "close": cur[4], "signal_pct": pct, "fut": fut,
                         "lhb": (d, code) in lhb})
    return sigs


def sim(sig, stop_pct=-0.04, take_pct=0.08, max_hold=5,
        judge_entry_day=False, intraday=False, skip_gap_hi=None, skip_gap_lo=None):
    """回放一条信号，返回 (ret_pct | None, reason)。

    stop/take 以**信号日收盘价**为基准（与 strategy/surge_breakout.py 一致）。
    - judge_entry_day: 建仓当日是否纳入止损/止盈判定（False = 现网口径）
    - intraday:       True = 日内触价成交（跳空穿线按更差价）；False = 收盘价判定
    - skip_gap_hi/lo: 次日开盘跳空 >=hi% 或 <=lo% 时放弃（现实可执行，开盘即可见）
    """
    f = sig["fut"]
    base = sig["close"]
    stop = base * (1 + stop_pct)
    tp = base * (1 + take_pct)
    o0 = f[0][1]
    gap = (o0 - base) / base * 100.0
    if o0 > tp:
        return None, "skip_gap_above_tp"
    if skip_gap_hi is not None and gap >= skip_gap_hi:
        return None, "skip_high_open"
    if skip_gap_lo is not None and gap <= skip_gap_lo:
        return None, "skip_low_open"
    if o0 <= stop:
        # 开盘已在止损线下 → 该票已跳空破位，不买入（现实可执行）
        return None, "skip_open_below_stop"

    def pct(v):
        return (v - o0) / o0 * 100.0

    for j, p in enumerate(f[:max_hold], start=1):
        _, op, hi, lo, cl = p[0], p[1], p[2], p[3], p[4]
        if not cl or cl <= 0:
            break
        if j == 1 and not judge_entry_day:
            continue
        if intraday:
            hit_stop = lo and lo <= stop
            hit_tp = hi and hi >= tp
            if hit_stop:                 # 同日双触发无法判序 → 保守按止损
                fill = min(op, stop) if op else stop
                return pct(fill), "stop_loss"
            if hit_tp:
                fill = max(op, tp) if op else tp
                return pct(fill), "take_profit"
        else:
            if cl <= stop:
                return pct(cl), "stop_loss"
            if cl >= tp:
                return pct(cl), "take_profit"
        if j >= max_hold:
            return pct(cl), "max_hold_days"
    last = f[min(len(f), max_hold) - 1]
    return pct(last[4]), "hold"


SKIP_LABEL = {"skip_gap_above_tp": "弃·开越止盈", "skip_high_open": "弃·高开",
              "skip_low_open": "弃·低开", "skip_open_below_stop": "弃·开破止损"}


def agg(tag, results):
    """results: list of (ret, reason)。放弃/None 不计入统计。"""
    vals = [r for r, _ in results if r is not None]
    n = len(vals)
    if not n:
        return f"  {tag:<38} n=0"
    w = sum(1 for v in vals if v > 0)
    reasons = defaultdict(int)
    for r, why in results:
        reasons[SKIP_LABEL.get(why, why) if r is None else why] += 1
    rb = " ".join(f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
    return (f"  {tag:<38} n={n:<5} 胜率 {w / n * 100:5.1f}%  均值 "
            f"{sum(vals) / n:+6.2f}%  最差 {min(vals):+6.2f}%  | {rb}")


def tstat(a, b):
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


GRID = [
    ("0 基线·现网(5日/收盘/不判建仓日)", dict()),
    ("1 建仓当日纳入判定(5日/收盘)", dict(judge_entry_day=True)),
    ("2 5日/日内触价(含建仓当日)", dict(judge_entry_day=True, intraday=True)),
    ("3 隔日了结(1日/收盘/判当日)", dict(max_hold=1, judge_entry_day=True)),
    ("4 隔日了结+日内触价", dict(max_hold=1, judge_entry_day=True, intraday=True)),
    ("5 隔日+日内+弃高开>=3%", dict(max_hold=1, judge_entry_day=True, intraday=True,
                                 skip_gap_hi=3)),
    ("6 2日+日内+弃高开>=3%", dict(max_hold=2, judge_entry_day=True, intraday=True,
                                skip_gap_hi=3)),
    ("7 3日+日内+弃高开>=3%", dict(max_hold=3, judge_entry_day=True, intraday=True,
                                skip_gap_hi=3)),
    ("8 隔日(1日/收盘/判当日)+弃高开>=3%", dict(max_hold=1, judge_entry_day=True,
                                          skip_gap_hi=3)),
    ("9 隔日+日内+止盈5%", dict(max_hold=1, judge_entry_day=True, intraday=True,
                            take_pct=0.05)),
]

KEY = [
    ("基线·现网", dict()),
    ("候选3 隔日了结(收盘判定)", dict(max_hold=1, judge_entry_day=True)),
    ("候选5 隔日+日内+弃高开3%", dict(max_hold=1, judge_entry_day=True, intraday=True,
                                  skip_gap_hi=3)),
    ("候选6 2日+日内+弃高开3%", dict(max_hold=2, judge_entry_day=True, intraday=True,
                                  skip_gap_hi=3)),
]


def by_year(sigs, tag, kw):
    parts = []
    for yr in ("2024", "2025", "2026"):
        v = []
        for s in sigs:
            if not s["date"].startswith(yr):
                continue
            r, _ = sim(s, **kw)
            if r is not None:
                v.append(r)
        if not v:
            continue
        w = sum(1 for x in v if x > 0)
        parts.append(f"{yr} n={len(v):<4} 胜率 {w / len(v) * 100:5.1f}% "
                     f"均值 {sum(v) / len(v):+6.2f}%")
    print(f"    {tag}\n        " + "\n        ".join(parts))


def buckets(sigs, kw, key, edges, label):
    """在固定口径下按某个变量分档，看「该变量是否真的驱动结果」（铁律 4）。"""
    print(f"    [{label}]")
    base = [(sim(s, **kw), s) for s in sigs]
    for lo, hi in edges:
        v, sk = [], 0
        for (r, _), s in base:
            if not (lo <= key(s) < hi):
                continue
            if r is None:
                sk += 1
            else:
                v.append(r)
        if not v:
            print(f"      {lo}~{hi}: 全部放弃({sk})")
            continue
        w = sum(1 for x in v if x > 0)
        print(f"      {lo:>5}~{hi:<5}: n={len(v):<4} 胜率 {w / len(v) * 100:5.1f}%  "
              f"均值 {sum(v) / len(v):+6.2f}%  放弃{sk}")


def _gap(s):
    return (s["fut"][0][1] - s["close"]) / s["close"] * 100.0


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-22"
    out_path = os.path.join(ROOT, "tools", "_surge_tune_out.txt")
    sys.stdout = open(out_path, "w", encoding="utf-8", newline="\n")

    info, px, lhb, dates, mkt = load(start, end)
    print(f"窗口 {start} ~ {end}：{len(dates)} 个交易日，{len(px)} 只有行情，"
          f"龙虎榜净买 {len(lhb)} 条")

    allsigs = collect(px, info, dates, mkt, lhb)
    groups = (("【主板·全族（无龙虎榜过滤）】", [s for s in allsigs if not s["lhb"]]),
              ("【主板·龙虎榜净买子集（线上实际口径）】",
               [s for s in allsigs if s["lhb"]]))

    for lab, sigs in groups:
        print(f"\n{'=' * 104}\n{lab}  样本 {len(sigs)} 条 / {len(dates)} 交易日"
              f"（日均 {len(sigs) / max(len(dates), 1):.2f} 只）\n{'=' * 104}")
        if not sigs:
            print("  无样本")
            continue
        for tag, kw in GRID:
            print(agg(tag, [sim(s, **kw) for s in sigs]))

        print("\n  ── 分年稳定性（分年都成立才算数，铁律 3）──")
        for tag, kw in KEY:
            by_year(sigs, tag, kw)

        if lab.startswith("【主板·全族"):
            print("\n  ── 入场变量分档（候选3 口径：隔日了结 / 建仓当日判定 / 开盘破止损不入场）──")
            KW3 = dict(max_hold=1, judge_entry_day=True)
            buckets(sigs, KW3, lambda s: s["signal_pct"],
                    [(5, 6), (6, 7), (7, 8), (8, 9), (9, 9.81)], "信号日涨幅 %")
            buckets(sigs, KW3, _gap,
                    [(-99, -4), (-4, -2), (-2, 0), (0, 2), (2, 3), (3, 99)],
                    "次日开盘跳空 %")

        print("\n  ── 剔除单年检验（候选 − 基线 的均值差）──")
        base = [sim(s, **dict()) for s in sigs]
        for tag, kw in KEY[1:]:
            alt = [sim(s, **kw) for s in sigs]
            for drop in (None, "2024", "2025", "2026"):
                keep = [i for i, s in enumerate(sigs)
                        if drop is None or not s["date"].startswith(drop)]
                a = [alt[i][0] for i in keep if alt[i][0] is not None]
                b = [base[i][0] for i in keep if base[i][0] is not None]
                t, diff = tstat(a, b)
                seg = "全期" if drop is None else f"剔除{drop}"
                if t is None:
                    print(f"      {tag:<26} {seg:<9} 样本不足")
                else:
                    print(f"      {tag:<26} {seg:<9} n={len(a)}/{len(b)} "
                          f"Δ={diff:+.3f}pp t={t:+.2f}"
                          f"{'  ✓显著' if abs(t) >= 2 else '  (不显著)'}")
    sys.stdout.flush()
    sys.stdout = sys.__stdout__
    print(f"结果已写入 {out_path}")


if __name__ == "__main__":
    main()
