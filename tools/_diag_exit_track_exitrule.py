"""诊断 3：同一批推荐票，不同出场纪律的表现对比（找出亏损真正来源）。

对比口径（全部基于 recommend_outcome 里 7/20 起的实际推荐票）：
  A 线上实际   = exit_return（收盘判止损 + 移动止盈 + 到期）
  B 裸持有 T+5 / T+10 / T+20 收盘
  C 仅止损（不移动止盈），止损用推荐自带价
  D 仅止损 + 止损放宽 1.5 倍
"""
import sqlite3
import statistics as st

conn = sqlite3.connect("core/quant.db")
conn.row_factory = sqlite3.Row
START = "2026-07-20"


def series(code, scan_date, limit=40):
    return [dict(r) for r in conn.execute(
        "SELECT trade_date, open, close, high, low FROM daily_price "
        "WHERE code=? AND trade_date>? ORDER BY trade_date ASC LIMIT ?",
        (code, scan_date, limit)).fetchall()]


def bare(px, n):
    if len(px) < n:
        return None
    o, c = px[0]["open"], px[n - 1]["close"]
    return (c - o) / o * 100 if o and c else None


def exit_sim(px, stop_mult=1.0, use_trail=True, trail_pct=0.08, max_hold=10):
    """从 px[0] 开盘建仓，收盘判定。返回 (reason, ret)。"""
    if not px or not px[0]["open"]:
        return None, None
    e = px[0]["open"]
    stop = None
    highest = None
    tline = None
    launched = False
    for j, p in enumerate(px[:max_hold], start=1):
        if not p["close"]:
            break
        highest = p["high"] if highest is None else max(highest, p["high"] or p["close"])
        if not launched and highest >= e * 1.06:
            launched = True
        if launched and use_trail:
            line = highest * (1 - trail_pct)
            tline = line if tline is None else max(tline, line)
        if j == 1:
            continue
        if stop and p["close"] <= stop:
            return "stop", (p["close"] - e) / e * 100
        if launched and tline and p["close"] <= tline:
            return "trail", (p["close"] - e) / e * 100
        if j == max_hold:
            return "hold", (p["close"] - e) / e * 100
    return None, None


rows = [dict(r) for r in conn.execute(
    f"SELECT code, scan_date, COALESCE(horizon,'short') hz, strategy, entry_price, "
    f"stop_loss, take_profit, exit_return, exit_reason FROM recommend_outcome "
    f"WHERE scan_date >= '{START}'")]

print("=" * 88)
print("A/B 对照：线上出场纪律 vs 裸持有（同一批推荐票，基准 = 信号日次日开盘）")
print("=" * 88)
print(f"{'周期':<6}{'口径':<26}{'n':>5}{'均值':>9}{'中位':>9}{'胜率':>9}")
for hz in ("short", "mid", "long"):
    sub = [r for r in rows if r["hz"] == hz]
    # A 线上
    vs = [r["exit_return"] for r in sub if r["exit_return"] is not None]
    if vs:
        print(f"{hz:<6}{'A 线上实际出场':<26}{len(vs):>5}{st.mean(vs):>8.2f}%{st.median(vs):>8.2f}%"
              f"{sum(1 for v in vs if v>0)/len(vs)*100:>8.1f}%")
    # B 裸持有
    for n in (5, 10, 20):
        vs = [v for v in (bare(series(r["code"], r["scan_date"]), n) for r in sub) if v is not None]
        if vs:
            print(f"{hz:<6}{f'B 裸持有 T+{n} 收盘':<26}{len(vs):>5}{st.mean(vs):>8.2f}%{st.median(vs):>8.2f}%"
                  f"{sum(1 for v in vs if v>0)/len(vs)*100:>8.1f}%")
    print()

print("=" * 88)
print("C/D 对照：止损宽度敏感度（短线，收盘判定，含 6% 启动 / 8% 回撤的移动止盈）")
print("=" * 88)
sub = [r for r in rows if r["hz"] == "short"]
print(f"{'方案':<34}{'n':>5}{'均值':>9}{'止损率':>9}{'胜率':>9}")
for label, mult, use_trail in (
        ("线上止损价 + 8% 移动止盈", 1.0, True),
        ("止损放宽 1.5x + 8% 移动止盈", 1.5, True),
        ("线上止损价 + 关闭移动止盈", 1.0, False),
        ("止损放宽 1.5x + 关闭移动止盈", 1.5, False)):
    rets, stops = [], 0
    for r in sub:
        px = series(r["code"], r["scan_date"])
        if not px or not px[0]["open"] or not r["stop_loss"]:
            continue
        e = px[0]["open"]
        s = e + (r["stop_loss"] - r["entry_price"]) * mult if r["entry_price"] else None
        # 重跑含自定义止损
        reason, ret = None, None
        highest, tline, launched = None, None, False
        for j, p in enumerate(px[:10], start=1):
            if not p["close"]:
                break
            highest = p["high"] if highest is None else max(highest, p["high"] or p["close"])
            if not launched and highest >= e * 1.06:
                launched = True
            if launched and use_trail:
                line = highest * 0.92
                tline = line if tline is None else max(tline, line)
            if j == 1:
                continue
            if s and p["close"] <= s:
                reason, ret = "stop", (p["close"] - e) / e * 100
                break
            if launched and tline and p["close"] <= tline:
                reason, ret = "trail", (p["close"] - e) / e * 100
                break
            if j == 10:
                reason, ret = "hold", (p["close"] - e) / e * 100
        if ret is not None:
            rets.append(ret)
            stops += 1 if reason == "stop" else 0
    if rets:
        print(f"{label:<34}{len(rets):>5}{st.mean(rets):>8.2f}%{stops/len(rets)*100:>8.1f}%"
              f"{sum(1 for v in rets if v>0)/len(rets)*100:>8.1f}%")
