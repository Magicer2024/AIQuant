"""龙虎榜「机构专用席位」信号诊断
======================================================================
问题：跟踪机构建仓（机构净买）是否带来可交易的收益优势？

数据：stock_lhb_jg_detail，**只用单日口径**（window_days = 1）。
      多日口径的金额是 N 日累计，与单日额不可比（见 core/db.py 表注释）。

口径（时点正确，铁律 10）：
  T 日盘后龙虎榜才公布 ⇒ 最早 T+1 开盘买入。
  全部价格取 daily_price（前复权），**不使用接口自带 close_price**（原始价，混价源会出错，铁律 8）。

对照组（缺一不可，否则不知道信号是否只是市场 beta）：
  A 机构净买 > 0 且 有买方机构     —— 待验证信号
  B 机构净买 <= 0（同日机构榜其他票）—— 同池反向
  C 全市场同日全票基线              —— 大盘 beta
  D 龙虎榜净买 > 0（旧汇总口径）     —— 新旧口径对比

用法：
  python tools/_diag_lhb_jg.py [start] [end]
"""
import bisect
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")

LOOKBACK = 20          # 「连续建仓」回看交易日数
MAX_FUT = 11           # 预取未来交易日数（T+1 起算，够算 c10）
MIN_AMT20 = 80e6       # 流动性（与 rec_filters 一致）
MIN_MKTCAP, MAX_MKTCAP = 30e8, 3000e8


def is_st(name):
    n = (name or "").upper()
    return "ST" in n or "退" in n


def load(start, end, buf_days=70):
    """加载：行情（限机构榜涉及的票）、股票信息、机构单日口径记录、龙虎榜旧口径、全市场基线。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    info = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, total_shares FROM stock_info")}

    jg = defaultdict(list)
    for r in conn.execute(
            """SELECT trade_date, code, name, pct_change, close_price,
                      buyer_jg_count, seller_jg_count, jg_net_buy, jg_buy_amt,
                      jg_sell_amt, jg_net_ratio, market_total_amt,
                      turnover_rate, float_mkt_cap, reason
               FROM stock_lhb_jg_detail
               WHERE window_days = 1 AND trade_date >= ? AND trade_date <= ?
               ORDER BY code, trade_date""", (start, end)):
        jg[r["code"]].append(dict(r))

    # 仅加载机构榜涉及的股票的行情，控制内存（含回看缓冲）
    px = {}
    for code in jg:
        rows = conn.execute(
            """SELECT trade_date, open, high, low, close, volume, amount, pct_change
               FROM daily_price
               WHERE code = ? AND trade_date >= date(?, ?) AND trade_date <= date(?, '+25 days')
               ORDER BY trade_date""",
            (code, start, f"-{buf_days} days", end)).fetchall()
        if rows:
            px[code] = [tuple(r) for r in rows]

    lhb = {(r["trade_date"], r["code"]) for r in conn.execute(
        "SELECT trade_date, code FROM stock_lhb_detail "
        "WHERE net_buy > 0 AND trade_date >= ? AND trade_date <= ?", (start, end))}

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= ? "
        "AND trade_date <= ? ORDER BY trade_date", (start, end))]

    # 市场基线用中小盘宽基指数（机构榜票多为中小盘），避免扫描 260 万行个股行情
    idx_px = {}
    for r in conn.execute(
            """SELECT code, trade_date, close FROM index_daily
               WHERE code IN ('000852', '000905')
                 AND trade_date >= ? AND trade_date <= date(?, '+25 days')
               ORDER BY code, trade_date""", (start, end)):
        idx_px.setdefault(r["code"], []).append((r["trade_date"], r["close"]))
    conn.close()
    return info, px, jg, lhb, dates, idx_px


def index_baseline(idx_px, dates, n):
    """指数基线：T+1 开盘不可得，故用「T 日收盘 → T+n 日收盘」近似大盘同期涨跌。"""
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


def build(px, info, jg, dates):
    """把机构单日口径记录构造成可回放样本。"""
    dset = set(dates)
    sigs = []
    for code, recs in jg.items():
        seq = px.get(code)
        if not seq:
            continue
        idx = {t[0]: i for i, t in enumerate(seq)}
        rec_dates = [r["trade_date"] for r in recs]
        ic = info.get(code) or {}
        name = ic.get("name") or (recs[0].get("name") or "")
        if is_st(name):
            continue
        for r in recs:
            d = r["trade_date"]
            pos = idx.get(d)
            if pos is None or pos < LOOKBACK + 5 or d not in dset:
                continue
            cur = seq[pos]
            close, pct = cur[4], cur[7]
            if not close or close <= 0:
                continue
            fut = seq[pos + 1:pos + 1 + MAX_FUT]
            if not fut or not fut[0][1] or fut[0][1] <= 0:
                continue
            # 流动性（近 20 日均成交额）
            amt = [x[6] for x in seq[max(0, pos - 19):pos + 1]]
            if len(amt) < 20 or any(v is None for v in amt):
                continue
            amt20 = sum(amt) / 20.0
            # 市值（流通市值优先取接口值，单位亿元）
            fmc = r.get("float_mkt_cap")
            if fmc:
                mc = fmc * 1e8
            else:
                ts = ic.get("total_shares")
                mc = ts * close if ts else None
            net = r.get("jg_net_buy")
            bc = r.get("buyer_jg_count") or 0
            sc = r.get("seller_jg_count") or 0
            # 近 LOOKBACK 日内机构净买次数与累计额（「建仓」连续性）
            lo = bisect.bisect_left(rec_dates, seq[max(0, pos - LOOKBACK)][0])
            window = recs[lo:bisect.bisect_right(rec_dates, d)]
            cnt_in = sum(1 for x in window
                         if (x.get("jg_net_buy") or 0) > 0 and (x.get("buyer_jg_count") or 0) > 0)
            cum_net = sum((x.get("jg_net_buy") or 0) for x in window)
            sigs.append({
                "date": d, "code": code, "name": name,
                "close": close, "pct": pct or 0.0,
                "net": net, "buyer": bc, "seller": sc,
                "ratio": r.get("jg_net_ratio"),
                "jgb_ratio": (net / mc * 100.0) if (net is not None and mc) else None,
                "fmc": fmc, "amt20": amt20,
                "cnt_in": cnt_in, "cum_net": cum_net,
                "net_in": (net or 0) > 0 and bc > 0,
                "fut": fut,
            })
    return sigs


def ret_hold(sig, n, skip_limit_open=True):
    """T+1 开盘买入、持有 n 个交易日后收盘卖。skip_limit_open: 开盘近乎涨停买不到则放弃。"""
    f = sig["fut"]
    if len(f) < n:
        return None
    o0 = f[0][1]
    if not o0 or o0 <= 0:
        return None
    if skip_limit_open and f[0][2] and f[0][3] and f[0][2] == f[0][3] == o0:
        return None                      # 一字板，买不到
    c = f[n - 1][4]
    if not c or c <= 0:
        return None
    return (c - o0) / o0 * 100.0


def ret_exit(sig, stop_pct, take_pct, max_hold):
    """带出场纪律：T+1 开盘买入，收盘判定止损/止盈/max_hold 到期（基准 = 信号日收盘）。"""
    f, base = sig["fut"], sig["close"]
    stop, tp = base * (1 + stop_pct), base * (1 + take_pct)
    o0 = f[0][1]
    if not o0 or o0 <= 0:
        return None
    if f[0][2] and f[0][3] and f[0][2] == f[0][3] == o0:
        return None
    if o0 > tp or o0 <= stop:
        return None                      # 开盘越止盈 / 破止损 → 现实不买
    for j, p in enumerate(f[:max_hold], start=1):
        cl = p[4]
        if not cl or cl <= 0:
            return None
        if cl <= stop:
            return (cl - o0) / o0 * 100.0
        if cl >= tp:
            return (cl - o0) / o0 * 100.0
    return (f[min(len(f), max_hold) - 1][4] - o0) / o0 * 100.0


def agg(tag, vals):
    n = len(vals)
    if not n:
        return f"  {tag:<30} n=0"
    w = sum(1 for v in vals if v > 0)
    return (f"  {tag:<30} n={n:<6} 胜率 {w / n * 100:5.1f}%  均值 "
            f"{sum(vals) / n:+6.2f}%  中位 {sorted(vals)[n // 2]:+6.2f}%  "
            f"最差 {min(vals):+6.2f}%")


def tstat(a, b):
    import math
    if len(a) < 2 or len(b) < 2:
        return None, None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se <= 0:
        return None, None
    return (ma - mb) / se, ma - mb


def by_year(sigs, tag, fn):
    parts = []
    for yr in ("2024", "2025", "2026"):
        v = [x for s in sigs if s["date"].startswith(yr)
             for x in [fn(s)] if x is not None]
        if not v:
            continue
        w = sum(1 for x in v if x > 0)
        parts.append(f"{yr} n={len(v):<5} 胜率 {w / len(v) * 100:5.1f}% 均值 {sum(v) / len(v):+6.2f}%")
    print(f"    {tag}\n        " + "\n        ".join(parts) if parts else f"    {tag} 无样本")


def buckets(sigs, key, edges, label, fn):
    print(f"    [{label}]")
    for lo, hi in edges:
        v = [x for s in sigs if (key(s) is not None and lo <= key(s) < hi)
             for x in [fn(s)] if x is not None]
        if not v:
            print(f"      {lo:>6}~{hi:<6}: 无样本")
            continue
        w = sum(1 for x in v if x > 0)
        print(f"      {lo:>6}~{hi:<6}: n={len(v):<5} 胜率 {w / len(v) * 100:5.1f}%  "
              f"均值 {sum(v) / len(v):+6.2f}%")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-22"
    out_path = os.path.join(ROOT, "tools", "_lhb_jg_out.txt")
    sys.stdout = open(out_path, "w", encoding="utf-8", newline="\n")

    info, px, jg, lhb, dates, idx_px = load(start, end)
    sigs = build(px, info, jg, dates)
    print(f"窗口 {start} ~ {end}：{len(dates)} 个交易日，机构单日口径记录 {sum(len(v) for v in jg.values())} 行，"
          f"成样本 {len(sigs)} 条（{len(set(s['code'] for s in sigs))} 只）")
    print(f"  其中机构净买>0 {sum(1 for s in sigs if s['net_in'])} 条，"
          f"机构净买<=0 {sum(1 for s in sigs if not s['net_in'])} 条")

    mkt = {n: index_baseline(idx_px, dates, n) for n in (1, 5, 10)}

    for n in (1, 5, 10):
        print(f"\n{'=' * 108}\n【持有 {n} 个交易日】T+1 开盘买入 → 第 {n} 日收盘卖\n{'=' * 108}")
        a = [x for s in sigs if s["net_in"] for x in [ret_hold(s, n)] if x is not None]
        b = [x for s in sigs if not s["net_in"] for x in [ret_hold(s, n)] if x is not None]
        d = [x for s in sigs if (s["date"], s["code"]) in lhb
             for x in [ret_hold(s, n)] if x is not None]
        mavg = sum(mkt[n].values()) / len(mkt[n]) if mkt[n] else None
        print(agg("A 机构净买>0", a))
        print(agg("B 机构净买<=0", b))
        print(agg("D 龙虎榜净买>0(旧口径)", d))
        if mavg is not None:
            print(f"  {'C 中证1000/500 基线':<30} 均值 {mavg:+6.2f}%  （近似，非逐笔可比）")
        t, diff = tstat(a, b)
        if t is not None:
            print(f"      A vs B  Δ={diff:+.3f}pp  t={t:+.2f}"
                  f"{'  ✓显著' if abs(t) >= 2 else '  (不显著)'}")

    print(f"\n{'=' * 108}\n【机构净买>0 子集 · 分年稳定性】(持有 1 / 5 日)\n{'=' * 108}")
    A = [s for s in sigs if s["net_in"]]
    by_year(A, "持有 1 日", lambda s: ret_hold(s, 1))
    by_year(A, "持有 5 日", lambda s: ret_hold(s, 5))

    print(f"\n{'=' * 108}\n【分档单调性】机构净买>0 子集，持有 1 日 / 5 日（铁律 4：变量是否真的驱动结果）\n{'=' * 108}")
    f1, f5 = (lambda s: ret_hold(s, 1)), (lambda s: ret_hold(s, 5))
    buckets(A, lambda s: s["jgb_ratio"], [(-99, 0), (0, 0.5), (0.5, 1.5), (1.5, 3), (3, 99)],
            "机构净买额 / 流通市值 %", f1)
    buckets(A, lambda s: s["jgb_ratio"], [(-99, 0), (0, 0.5), (0.5, 1.5), (1.5, 3), (3, 99)],
            "机构净买额 / 流通市值 % (持有5日)", f5)
    buckets(A, lambda s: s["ratio"], [(-99, 0), (0, 1), (1, 3), (3, 8), (8, 99)],
            "机构净买额占成交额 %", f1)
    buckets(A, lambda s: float(s["buyer"]), [(0, 1), (1, 2), (2, 3), (3, 6), (6, 99)],
            "买方机构家数", f1)
    buckets(A, lambda s: float(s["pct"]), [(-99, 0), (0, 3), (3, 7), (7, 9.8), (9.8, 99)],
            "上榜日涨幅 %", f1)
    buckets(A, lambda s: float(s["cnt_in"]), [(1, 2), (2, 3), (3, 5), (5, 99)],
            f"近 {LOOKBACK} 日机构净买次数", f1)

    print(f"\n{'=' * 108}\n【「连续建仓」增量】能否在「机构净买>0」之上再提纯？\n{'=' * 108}")
    base1 = [x for s in A for x in [ret_hold(s, 1)] if x is not None]
    for k in (2, 3, 4):
        sub = [s for s in A if s["cnt_in"] >= k]
        v1 = [x for s in sub for x in [ret_hold(s, 1)] if x is not None]
        v5 = [x for s in sub for x in [ret_hold(s, 5)] if x is not None]
        print(f"  近{LOOKBACK}日机构净买>={k} 次: n={len(v1):<5}  "
              f"1日 {sum(v1) / max(len(v1), 1):+.2f}% / 胜率 "
              f"{sum(1 for x in v1 if x > 0) / max(len(v1), 1) * 100:.1f}%   "
              f"5日 {sum(v5) / max(len(v5), 1):+.2f}% / 胜率 "
              f"{sum(1 for x in v5 if x > 0) / max(len(v5), 1) * 100:.1f}%")
        t, diff = tstat(v1, base1)
        if t is not None:
            print(f"      vs 全子集 Δ={diff:+.3f}pp t={t:+.2f}"
                  f"{'  ✓显著' if abs(t) >= 2 else '  (不显著)'}")

    print(f"\n{'=' * 108}\n【带出场纪律（机构净买>0）】基准=信号日收盘，ATR 前用固定档网格\n{'=' * 108}")
    for stop, take, hold in ((-0.05, 0.08, 1), (-0.05, 0.08, 5), (-0.07, 0.15, 5),
                             (-0.05, 0.12, 10)):
        v = [x for s in A for x in [ret_exit(s, stop, take, hold)] if x is not None]
        n_skip = len(A) - len(v)
        print(agg(f"止损{stop:.0%} 止盈{take:.0%} {hold}日(弃{n_skip})", v))

    sys.stdout.flush()
    sys.stdout = sys.__stdout__
    print(f"结果已写入 {out_path}")


if __name__ == "__main__":
    main()
