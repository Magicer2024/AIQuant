"""次日强势观察（强势突破）成绩单归因诊断
=================================================
目的：回答「胜率 <45%、收益为负」是信号无效，还是**评价口径与信号设计错配**。

做法：把 stock_signal 里全部 strategy='强势突破' 的信号，用多种出场口径并行回放，
      对比同一批样本在不同口径下的成绩差异（单一变量）。

口径：
  A 现网口径      : 次日开盘买入；收盘判定 -4% 止损 / +8% 止盈 / 5 日到期（建仓当日不判）
  B 纯隔日 OC     : 次日开盘买入，当日收盘无条件了结（= 模块原始设计目标口径）
  C 盘中触价      : 次日开盘买入，盘中 low<=止损 或 high>=止盈 即出场（收盘价成交）
  D 无止损裸持有  : 次日开盘买入，5 日到期了结，不设阈值
  E 次日尾盘买    : 信号日次日收盘买入（放弃隔夜跳空），5 日到期了结

用法：python tools/_diag_surge_perf.py
"""
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
MAX_HOLD = 5


def load_signals(conn):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        """SELECT code, name, scan_date, trade_date, price, buy_price,
                  stop_loss, take_profit, fusion_score
           FROM stock_signal
           WHERE strategy = '强势突破'
           ORDER BY scan_date ASC, code ASC""")]


def bars_after(conn, code, sig_day, n):
    return [dict(r) for r in conn.execute(
        """SELECT trade_date, open, close, high, low FROM daily_price
           WHERE code = ? AND trade_date > ? ORDER BY trade_date ASC LIMIT ?""",
        (code, sig_day, n))]


def stat(tag, rets):
    n = len(rets)
    if not n:
        return f"  {tag:<14} n=0"
    w = sum(1 for x in rets if x > 0)
    avg = sum(rets) / n
    return (f"  {tag:<14} n={n:<3} 胜率 {w / n * 100:5.1f}%  "
            f"均值 {avg:+6.2f}%  最好 {max(rets):+6.2f}%  最差 {min(rets):+6.2f}%")


def replay(conn, sig):
    """返回该信号在各口径下的收益（%），无法回放的口径为 None。"""
    sig_day = sig.get("trade_date") or sig.get("scan_date")
    stop, tp = sig.get("stop_loss"), sig.get("take_profit")
    out = {}
    px = bars_after(conn, sig["code"], sig_day, MAX_HOLD + 2)
    if not px:
        return {k: None for k in ("A", "B", "C", "D", "E")}, None
    e0 = px[0]["open"]
    if not e0 or e0 <= 0:
        return {k: None for k in ("A", "B", "C", "D", "E")}, None

    def pct(p):
        return (p - e0) / e0 * 100.0

    # A 现网口径（收盘判定）
    if tp and e0 > tp:
        out["A"] = None        # 高开越过止盈 → 放弃
    else:
        out["A"] = None
        for j, p in enumerate(px[:MAX_HOLD], start=1):
            cl = p["close"]
            if not cl or cl <= 0:
                break
            if j == 1:
                continue
            if stop and cl <= stop:
                out["A"] = pct(cl); break
            if tp and cl >= tp:
                out["A"] = pct(cl); break
            if j >= MAX_HOLD:
                out["A"] = pct(cl); break
        if out["A"] is None:                       # 未满 5 日 → 用最后收盘
            last = px[min(len(px), MAX_HOLD) - 1]
            out["A"] = pct(last["close"])
    # B 纯隔日 OC：次日开盘 -> 次日收盘
    out["B"] = pct(px[0]["close"])
    # C 盘中触价（先判止损再判止盈，保守）
    c = None
    if not (tp and e0 > tp):
        for j, p in enumerate(px[:MAX_HOLD], start=1):
            if j == 1:
                continue
            if stop and p["low"] and p["low"] <= stop:
                c = pct(stop); break
            if tp and p["high"] and p["high"] >= tp:
                c = pct(tp); break
            if j >= MAX_HOLD:
                c = pct(p["close"]); break
        if c is None:
            c = pct(px[min(len(px), MAX_HOLD) - 1]["close"])
    out["C"] = c
    # D 无阈值裸持有 5 日
    out["D"] = pct(px[min(len(px), MAX_HOLD) - 1]["close"])
    # E 次日收盘买入（放弃跳空），再持有 MAX_HOLD 日
    e1 = px[0]["close"]
    px2 = bars_after(conn, sig["code"], px[0]["trade_date"], MAX_HOLD)
    if px2 and e1 and e1 > 0:
        out["E"] = (px2[min(len(px2), MAX_HOLD) - 1]["close"] - e1) / e1 * 100.0
    else:
        out["E"] = None
    return out, px


def main():
    if not os.path.exists(DB):
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sigs = load_signals(conn)
    print(f"强势突破信号总数: {len(sigs)}  "
          f"区间 {sigs[0]['scan_date']} ~ {sigs[-1]['scan_date']}" if sigs else "无信号")
    buckets = {k: [] for k in ("A", "B", "C", "D", "E")}
    print("\n逐条明细（%）：")
    print(f"{'scan_date':<12}{'code':<8}{'name':<10}{'信号日收盘':>10}"
          f"{'A现网':>9}{'B次日OC':>9}{'C盘中触价':>10}{'D裸持5日':>10}{'E次日尾买':>10}")
    for s in sigs:
        got, px = replay(conn, s)
        line = f"{s['scan_date']:<12}{s['code']:<8}{(s['name'] or '')[:8]:<10}{s['price'] or 0:>10.2f}"
        for k in ("A", "B", "C", "D", "E"):
            v = got[k]
            if v is not None:
                buckets[k].append(v)
                line += f"{v:>+9.2f}" if k != "C" else f"{v:>+10.2f}"
            else:
                line += f"{'—':>9}" if k != "C" else f"{'—':>10}"
        print(line)
        if px:
            cl = "  ".join(f"{p['trade_date'][5:]} C{p['close']:.2f}"
                           f"(L{p['low']:.2f}/H{p['high']:.2f})" for p in px[:MAX_HOLD])
            print(f"              └ {cl}")
    print("\n口径对比（同一批样本，唯一变量 = 出场规则）：")
    for k, tag in (("A", "A 现网(收盘判定)"), ("B", "B 纯隔日OC"),
                   ("C", "C 盘中触价"), ("D", "D 无止损裸持"), ("E", "E 次日尾盘买")):
        print(stat(tag, buckets[k]))

    # 止损穿透分析：收盘价离 -4% 止损线有多远
    print("\n止损穿透分析（现网口径的亏损单）：")
    for s in sigs:
        got, px = replay(conn, s)
        if got["A"] is None or got["A"] >= 0:
            continue
        stop = s.get("stop_loss")
        e0 = px[0]["open"]
        breach = [p for p in px[:MAX_HOLD]
                  if p["low"] and stop and p["low"] <= stop]
        worst_low = min((p["low"] for p in px[:MAX_HOLD] if p["low"]), default=0)
        print(f"  {s['scan_date']} {s['code']} {s['name'][:6]:<8} "
              f"出场 {got['A']:+.2f}%  止损线 {stop:.2f} "
              f"最低价 {worst_low:.2f} ({(worst_low - e0) / e0 * 100:+.2f}%) "
              f"首次触线日 {'/'.join(p['trade_date'][5:] for p in breach) or '从未触线'}")


if __name__ == "__main__":
    main()
