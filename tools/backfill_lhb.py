"""
tools/backfill_lhb.py —— 龙虎榜明细回填（akshare 东方财富）
============================================================
把指定区间的龙虎榜明细抓取入库 stock_lhb_detail，
东方财富接口自带「上榜后 1/2/5/10 日涨幅」直接落为 perf_1d/2d/5d/10d，
无需另行关联 daily_price 计算标签。

用法：
    python tools/backfill_lhb.py                      # 默认近 2 年
    python tools/backfill_lhb.py --start 2024-07-01 --end 2026-07-27
"""
import sys
import os
import argparse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import init_db, get_conn
from core.data_fetcher import fetch_lhb_detail
from core.repository.lhb_repo import upsert_lhb_detail, get_latest_lhb_date


def main():
    ap = argparse.ArgumentParser(description="龙虎榜明细回填（只写 stock_lhb_detail）")
    today = date.today()
    ap.add_argument("--start", default=(today - timedelta(days=730)).strftime("%Y-%m-%d"))
    ap.add_argument("--end", default=today.strftime("%Y-%m-%d"))
    args = ap.parse_args()

    init_db()
    print(f"[1/3] 抓取龙虎榜 {args.start} ~ {args.end} ...")

    def _cb(s, e, i, total):
        print(f"      [{i+1:>2}/{total}] {s}~{e}")

    df = fetch_lhb_detail(args.start, args.end, progress_cb=_cb)
    if df is None or df.empty:
        print("  未抓到任何龙虎榜记录，退出")
        return
    print(f"      抓到 {len(df)} 条（去重后），日期跨度 "
          f"{df['trade_date'].min()} ~ {df['trade_date'].max()}")

    print("[2/3] 写入 stock_lhb_detail ...")
    n = upsert_lhb_detail(df)
    print(f"      upsert {n} 条")

    print("[3/3] 校验 ...")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(perf_1d IS NOT NULL) AS n_perf, "
            "MIN(trade_date) AS mn, MAX(trade_date) AS mx "
            "FROM stock_lhb_detail"
        ).fetchone()
    print(f"      表内共 {row['n']} 条，含 perf_1d 标签 {row['n_perf']} 条，"
          f"跨度 {row['mn']} ~ {row['mx']}")
    print(f"      最新龙虎榜日期: {get_latest_lhb_date()}")
    print("完成。")


if __name__ == "__main__":
    main()
