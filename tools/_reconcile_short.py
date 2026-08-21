# -*- coding: utf-8 -*-
"""一次性对账：用 recommend_outcome 真实短线推荐验证回测模拟器口径。只读。

验证三件事：
  1. 复盘表 t1_return 可由 daily_price 复现（entry=信号日收盘）；
  2. 复盘表 exit_return 可由 outcome_tracker 逻辑（盘中触及止损/止盈价、10日强制了结）复现；
  3. 收盘判定口径（回测网格所用）与盘中口径在同一批票上的差异。
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")


def tracker_exit(entry, stop, tp, prices):
    """复刻 outcome_tracker._evaluate_short：盘中触及固定止损/止盈价，否则 10 日收盘了结"""
    for p in prices[:10]:
        if stop and p["low"] and p["low"] <= stop:
            return (stop - entry) / entry * 100, "stop_loss"
        if tp and p["high"] and p["high"] >= tp:
            return (tp - entry) / entry * 100, "take_profit"
    if len(prices) >= 10:
        c = prices[9]["close"]
        return (c - entry) / entry * 100, "max_hold_days"
    return None, None


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT code, scan_date, entry_price, stop_loss, take_profit,
               t1_return, exit_return, exit_reason
        FROM recommend_outcome WHERE horizon='short' ORDER BY scan_date""").fetchall()
    print(f"实盘短线推荐 n={len(rows)}")

    t1_err = 0
    t1_checked = 0
    ex_match = 0
    ex_checked = 0
    close_base = []     # 收盘判定口径模拟结果（回测网格口径）
    for r in rows:
        prices = conn.execute("""
            SELECT trade_date, close, high, low FROM daily_price
            WHERE code=? AND trade_date>? ORDER BY trade_date LIMIT 15""",
                              (r["code"], r["scan_date"])).fetchall()
        prices = [dict(p) for p in prices]
        entry = r["entry_price"]
        if not prices or not entry:
            continue
        # 1) t1 复现
        if r["t1_return"] is not None and prices[0]["close"]:
            t1 = round((prices[0]["close"] - entry) / entry * 100, 2)
            t1_checked += 1
            if abs(t1 - r["t1_return"]) > 0.02:
                t1_err += 1
        # 2) exit 复现（盘中口径）
        ret, reason = tracker_exit(entry, r["stop_loss"], r["take_profit"], prices)
        if r["exit_return"] is not None and ret is not None:
            ex_checked += 1
            if abs(ret - r["exit_return"]) < 0.02 and reason == r["exit_reason"]:
                ex_match += 1
        # 3) 收盘判定口径（hold=10, stop-6, tp+10）
        if len(prices) >= 10:
            exit_p = None
            for j, p in enumerate(prices[:10], start=1):
                cl = p["close"]
                if j == 1:
                    continue
                if cl <= entry * 0.94:
                    exit_p = cl
                    break
                if cl >= entry * 1.10:
                    exit_p = cl
                    break
                if j == 10:
                    exit_p = cl
            if exit_p:
                close_base.append(exit_p / entry * 100 - 100)

    print(f"t1 复现: {t1_checked} 条中不符 {t1_err} 条")
    print(f"exit 复现: {ex_checked} 条中一致 {ex_match} 条")
    if close_base:
        wins = sum(1 for v in close_base if v > 0)
        print(f"同批票收盘判定口径(-6/+10/持10): n={len(close_base)} "
              f"胜率 {wins/len(close_base)*100:.1f}% 均值 {sum(close_base)/len(close_base):+.2f}%")
    conn.close()


if __name__ == "__main__":
    main()
