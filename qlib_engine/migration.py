"""
qlib_engine/migration.py —— 一次性数据迁移工具

用法：
  python -m qlib_engine.migration

将 SQLite daily_price + index_daily + stock_info 迁移到 Qlib 二进制格式。
迁移完成后旧表保留为只读备份，不自动删除。
"""

import os
import sys
import struct
import time
import pandas as pd
import numpy as np
from datetime import datetime
from collections import defaultdict

from core.db import get_conn, init_db
from qlib_engine import PROVIDER_URI


def export_instruments(output_dir: str) -> int:
    """
    Generate instruments/all.txt from stock_info table.

    Format: <code>\t<name>\t<listing_date>\t<delisting_date>

    Returns number of instruments written.
    """
    os.makedirs(os.path.join(output_dir, "instruments"), exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM stock_info WHERE is_active=1 ORDER BY code"
        ).fetchall()

    path = os.path.join(output_dir, "instruments", "all.txt")
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            code = r["code"]
            name = r["name"]
            f.write(f"{code}\t{name}\t2024-01-01\t2099-12-31\n")
            count += 1

    print(f"[Migration] Wrote {count} instruments to {path}")
    return count


def export_calendars(output_dir: str) -> int:
    """
    Generate calendars/day.txt from index_daily or daily_price distinct trade_date.

    Returns number of trading days written.
    """
    os.makedirs(os.path.join(output_dir, "calendars"), exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM index_daily ORDER BY trade_date"
        ).fetchall()

    if not rows:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date"
        ).fetchall()

    path = os.path.join(output_dir, "calendars", "day.txt")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r['trade_date']}\n")

    print(f"[Migration] Wrote {len(rows)} trading days to {path}")
    return len(rows)


def export_features(output_dir: str) -> dict:
    """
    Export OHLCV data from daily_price to Qlib binary format.

    For each stock, creates:
      features/<code>/open.bin
      features/<code>/high.bin
      features/<code>/low.bin
      features/<code>/close.bin
      features/<code>/volume.bin

    Returns stats dict: {stocks_exported, total_rows}.
    """
    features_dir = os.path.join(output_dir, "features")
    os.makedirs(features_dir, exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, trade_date, open, high, low, close, volume "
            "FROM daily_price ORDER BY code, trade_date"
        ).fetchall()

    if not rows:
        print("[Migration] No data in daily_price table")
        return {"stocks_exported": 0, "total_rows": 0}

    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    fields = ["open", "high", "low", "close", "volume"]
    stats = {"stocks_exported": 0, "total_rows": len(df)}

    for code, group in df.groupby("code"):
        code_dir = os.path.join(features_dir, code)
        os.makedirs(code_dir, exist_ok=True)

        group = group.sort_values("trade_date")

        for field in fields:
            values = group[field].fillna(0).values.astype(np.float32)
            bin_path = os.path.join(code_dir, f"{field}.bin")

            with open(bin_path, "wb") as f:
                f.write(struct.pack("i", len(values)))
                f.write(values.tobytes())

        # Write date index mapping
        date_path = os.path.join(code_dir, "trade_date.bin")
        date_ints = group["trade_date"].apply(
            lambda d: int(d.strftime("%Y%m%d"))
        ).values.astype(np.int32)
        with open(date_path, "wb") as f:
            f.write(struct.pack("i", len(date_ints)))
            f.write(date_ints.tobytes())

        stats["stocks_exported"] += 1

    print(f"[Migration] Exported {stats['stocks_exported']} stocks, "
          f"{stats['total_rows']} rows to {features_dir}")
    return stats


def run_migration(output_dir: str = None) -> dict:
    """
    Run the full migration: instruments -> calendars -> features.

    Returns summary dict.
    """
    start = time.time()
    output_dir = output_dir or PROVIDER_URI

    print(f"[Migration] Starting migration to {output_dir}")
    init_db()

    instruments = export_instruments(output_dir)
    calendars = export_calendars(output_dir)
    features = export_features(output_dir)

    elapsed = time.time() - start
    summary = {
        "instruments": instruments,
        "trading_days": calendars,
        "stocks_exported": features["stocks_exported"],
        "total_rows": features["total_rows"],
        "elapsed_seconds": round(elapsed, 1),
    }

    print(f"[Migration] Complete in {elapsed:.1f}s: {summary}")
    return summary


if __name__ == "__main__":
    run_migration()
