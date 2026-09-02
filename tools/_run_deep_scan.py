# -*- coding: utf-8 -*-
"""手动触发全市场深析扫描（独立进程，确保用最新 stock_deep 逻辑），结果落库 stock_deep_signal。

用法: python tools/_run_deep_scan.py
与生产 POST /stock_deep/scan 同口径：run_full_market_scan(conn, progress_callback=...)，
默认 lookback=180、scan_date=今天、batch=50。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")


def main():
    from strategy.stock_deep import run_full_market_scan
    from core.db import get_conn

    t0 = time.time()
    last = [-1]

    def cb(pct, msg):
        if pct >= last[0] + 10 or pct >= 100:
            last[0] = pct
            print(f"[{time.strftime('%H:%M:%S')}] {msg}  ({pct:.1f}%)", flush=True)

    print("开始全市场深析扫描（独立进程 · 新逻辑）...", flush=True)
    with get_conn() as conn:
        res = run_full_market_scan(conn, progress_callback=cb)
    print("\n完成，用时 {:.1f}s".format(time.time() - t0))
    print(f"结果: {res}")


if __name__ == "__main__":
    main()
