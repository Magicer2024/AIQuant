"""
tools/backfill_lhb_jg.py —— 龙虎榜「机构专用席位」买卖统计回填（akshare 东方财富）
============================================================
把指定区间的机构买卖每日统计抓取入库 stock_lhb_jg_detail。

⚠ 与 backfill_lhb.py 的关键差异：**保留原始多行**，不按 (trade_date, code) 聚合。
   同一股票同日会因多个上榜原因各占一行，而金额字段的统计窗口随 reason 变化
   （「日涨幅偏离7%」= 单日额；「连续三个交易日内…」= 近 3 日累计额），
   按票聚合会把 3 日额当单日额。口径明细见 core/db.py 表注释。

用法：
    python tools/backfill_lhb_jg.py                                  # 默认近 2 年
    python tools/backfill_lhb_jg.py --start 2024-07-01 --end 2026-09-22
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import init_db, get_conn                                    # noqa: E402
from core.data_fetcher import fetch_lhb_jg_detail                        # noqa: E402
from core.repository.lhb_repo import (                                   # noqa: E402
    upsert_lhb_jg_detail, get_latest_lhb_jg_date,
)


def main():
    ap = argparse.ArgumentParser(description="龙虎榜机构席位统计回填（只写 stock_lhb_jg_detail）")
    today = date.today()
    ap.add_argument("--start", default=(today - timedelta(days=730)).strftime("%Y-%m-%d"))
    ap.add_argument("--end", default=today.strftime("%Y-%m-%d"))
    args = ap.parse_args()

    init_db()
    print(f"[1/3] 抓取机构席位统计 {args.start} ~ {args.end} ...")

    def _cb(s, e, i, total):
        print(f"      [{i+1:>2}/{total}] {s}~{e}", flush=True)

    df = fetch_lhb_jg_detail(args.start, args.end, progress_cb=_cb)
    if df is None or df.empty:
        print("  未抓到任何记录，退出")
        return
    print(f"      抓到 {len(df)} 行（未聚合），日期跨度 "
          f"{df['trade_date'].min()} ~ {df['trade_date'].max()}")

    print("[2/3] 写入 stock_lhb_jg_detail ...")
    n = upsert_lhb_jg_detail(df)
    print(f"      upsert {n} 行")

    print("[3/3] 校验 ...")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT trade_date) AS days, "
            "       COUNT(DISTINCT code) AS codes, "
            "       SUM(window_days = 1) AS single_day, "
            "       SUM(jg_net_buy > 0) AS net_in, "
            "       MIN(trade_date) AS mn, MAX(trade_date) AS mx "
            "FROM stock_lhb_jg_detail"
        ).fetchone()
    print(f"      表内 {row['n']} 行 / {row['days']} 个交易日 / {row['codes']} 只股票")
    print(f"      单日口径 {row['single_day']} 行（{row['single_day'] / max(row['n'], 1) * 100:.1f}%），"
          f"机构净买为正 {row['net_in']} 行")
    print(f"      跨度 {row['mn']} ~ {row['mx']}")
    print(f"      最新机构上榜日期: {get_latest_lhb_jg_date()}")
    print("完成。")


if __name__ == "__main__":
    main()
