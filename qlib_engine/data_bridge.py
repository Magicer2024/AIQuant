"""
qlib_engine/data_bridge.py —— 日常增量同步桥接

将 AkShare/baostock 拉取的增量数据直接写入 Qlib 二进制格式。
替代 core/sync.py 中直接写入 daily_price 表的逻辑。

用法：
  from qlib_engine.data_bridge import append_daily_data
  append_daily_data(code, df_new)
"""

import os
import struct
import numpy as np
import pandas as pd
from typing import List, Dict

from qlib_engine import PROVIDER_URI


def append_daily_data(code: str, df_new: pd.DataFrame) -> int:
    """
    Append new OHLCV rows to an existing stock's Qlib binary files.

    Args:
        code: stock code (e.g. '000001.SZ')
        df_new: DataFrame with columns [trade_date, open, high, low, close, volume]

    Returns:
        Number of new rows appended.
    """
    if df_new.empty:
        return 0

    df = df_new.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["trade_date"]):
        df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date")

    code_dir = os.path.join(PROVIDER_URI, "features", code)
    os.makedirs(code_dir, exist_ok=True)

    fields = ["open", "high", "low", "close", "volume"]
    new_count = 0

    for field in fields:
        bin_path = os.path.join(code_dir, f"{field}.bin")
        new_values = df[field].fillna(0).values.astype(np.float32)

        if os.path.exists(bin_path):
            with open(bin_path, "rb") as f:
                old_count = struct.unpack("i", f.read(4))[0]
                old_data = np.frombuffer(f.read(), dtype=np.float32)

            merged = np.concatenate([old_data, new_values])
            with open(bin_path, "wb") as f:
                f.write(struct.pack("i", len(merged)))
                f.write(merged.tobytes())
        else:
            with open(bin_path, "wb") as f:
                f.write(struct.pack("i", len(new_values)))
                f.write(new_values.tobytes())

        new_count = len(new_values)

    # Update trade_date index
    date_path = os.path.join(code_dir, "trade_date.bin")
    new_dates = df["trade_date"].apply(
        lambda d: int(d.strftime("%Y%m%d"))
    ).values.astype(np.int32)

    if os.path.exists(date_path):
        with open(date_path, "rb") as f:
            old_count = struct.unpack("i", f.read(4))[0]
            old_dates = np.frombuffer(f.read(), dtype=np.int32)
        merged_dates = np.concatenate([old_dates, new_dates])
        with open(date_path, "wb") as f:
            f.write(struct.pack("i", len(merged_dates)))
            f.write(merged_dates.tobytes())
    else:
        with open(date_path, "wb") as f:
            f.write(struct.pack("i", len(new_dates)))
            f.write(new_dates.tobytes())

    return new_count


def batch_append_daily(data: Dict[str, pd.DataFrame]) -> dict:
    """
    Append daily data for multiple stocks at once.

    Args:
        data: {code: df_new} mapping

    Returns:
        {code: rows_appended} mapping
    """
    result = {}
    for code, df in data.items():
        try:
            n = append_daily_data(code, df)
            result[code] = n
        except Exception as e:
            result[code] = {"error": str(e)}
    return result


def append_calendar_dates(new_dates: List[str]) -> int:
    """
    Append new trading dates to calendars/day.txt.
    Skips dates already present.

    Returns count of new dates added.
    """
    cal_path = os.path.join(PROVIDER_URI, "calendars", "day.txt")
    os.makedirs(os.path.dirname(cal_path), exist_ok=True)

    existing = set()
    if os.path.exists(cal_path):
        with open(cal_path, "r") as f:
            existing = set(line.strip() for line in f)

    new_set = set(new_dates) - existing
    if new_set:
        with open(cal_path, "a") as f:
            for d in sorted(new_set):
                f.write(f"{d}\n")

    return len(new_set)
