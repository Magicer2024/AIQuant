# -*- coding: utf-8 -*-
"""tools/build_stock_daily_flags.py —— 停牌/涨跌停/ST 标记回补 CLI（只读研究）

用法：
    python tools/build_stock_daily_flags.py [--days 250] [--db core/quant.db]

只新建/覆写 stock_daily_flags 表；不修改 daily_price、不写 stock_signal、
不改任何策略参数。幂等（upsert），可反复执行。
"""
import argparse
import sqlite3
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250, help="回补最近 N 个交易日")
    ap.add_argument("--db", default="core/quant.db")
    args = ap.parse_args()

    sys.path.insert(0, ".")
    from core.stock_flags import rebuild

    conn = sqlite3.connect(args.db)
    t0 = time.time()
    res = rebuild(conn, days=args.days)
    print(f"stock_daily_flags: rows={res['rows']} dates={res['dates']} "
          f"range={res.get('range')} elapsed={time.time()-t0:.1f}s")
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print("suspended:", q("SELECT COUNT(*) FROM stock_daily_flags WHERE suspended=1"))
    print("limit_up_sealed:", q("SELECT COUNT(*) FROM stock_daily_flags WHERE limit_up_sealed=1"))
    print("limit_down_sealed:", q("SELECT COUNT(*) FROM stock_daily_flags WHERE limit_down_sealed=1"))
    print("one_word_up:", q("SELECT COUNT(*) FROM stock_daily_flags WHERE one_word_up=1"))
    print("is_st:", q("SELECT COUNT(*) FROM stock_daily_flags WHERE is_st=1"))
    conn.close()


if __name__ == "__main__":
    main()
