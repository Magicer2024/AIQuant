# -*- coding: utf-8 -*-
"""tools/backfill_signal_extension.py —— 回填 stock_signal.pct_above_ma20

口径与 core/sync.py::_build_signal_records 的 _pct_above_ma20 完全一致：
    pct_above_ma20 = 信号日收盘 / MA20(收盘, 20) - 1（小数；MA20 缺失按 0）

只写 stock_signal.pct_above_ma20 一列，其余数据不动。
用法：python tools/backfill_signal_extension.py [--since 2026-01-01]
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")


def main():
    since = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--since" else None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    where = "WHERE pct_above_ma20 IS NULL"
    params: list = []
    if since:
        where += " AND scan_date >= ?"
        params.append(since)
    # 一次加载全部待回填行（避免逐股查询）
    todo = conn.execute(
        f"SELECT scan_date, code, horizon FROM stock_signal {where}").fetchall()
    todo_by_code = {}
    for r in todo:
        todo_by_code.setdefault(r["code"], []).append((r["scan_date"], r["horizon"]))
    codes = list(todo_by_code.keys())
    print(f"[回填] 待回填股票 {len(codes)} 只 / 行 {len(todo)}（since={since or '全部'}）")
    if not codes:
        print("[回填] 无待回填行")
        conn.close()
        return

    # 逐股加载收盘价序列
    px = {}
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        r2 = conn.execute(
            f"SELECT code, trade_date, close FROM daily_price "
            f"WHERE code IN ({ph}) ORDER BY code, trade_date", chunk).fetchall()
        for r in r2:
            px.setdefault(r["code"], []).append((r["trade_date"], r["close"]))

    updates = []
    done = 0
    for code, seq in px.items():
        closes = [c for _, c in seq]
        dates = [d for d, _ in seq]
        s = pd_series(closes)
        ma20 = s.rolling(20).mean()
        by_date = {}
        for i, d in enumerate(dates):
            m = ma20.iloc[i]
            c = closes[i]
            if m and m > 0 and c:
                by_date[d] = round(c / m - 1.0, 4)
            else:
                by_date[d] = 0.0
        for scan_date, horizon in todo_by_code.get(code, []):
            updates.append((by_date.get(scan_date, 0.0), scan_date, code, horizon))
        done += 1
        if done % 500 == 0 or done == len(px):
            print(f"[回填] {done}/{len(px)} 只，累计 UPDATE {len(updates)} 行")
            conn.executemany(
                "UPDATE stock_signal SET pct_above_ma20 = ? "
                "WHERE scan_date = ? AND code = ? AND horizon = ?", updates)
            conn.commit()
            updates = []
    if updates:
        conn.executemany(
            "UPDATE stock_signal SET pct_above_ma20 = ? "
            "WHERE scan_date = ? AND code = ? AND horizon = ?", updates)
        conn.commit()
    # 校验
    left = conn.execute(
        "SELECT COUNT(*) c FROM stock_signal WHERE pct_above_ma20 IS NULL").fetchone()["c"]
    conn.close()
    print(f"[回填] 完成。剩余 NULL 行 = {left}")


def pd_series(closes):
    import pandas as pd
    return pd.Series([float(c) if c else 0.0 for c in closes])


if __name__ == "__main__":
    main()
