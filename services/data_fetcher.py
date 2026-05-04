"""
services/data_fetcher.py —— 数据拉取服务

职责：
  1. 从 akshare 在线拉取股票日线数据
  2. 保存到本地数据库（daily_price 表）
  3. 支持单股/批量拉取
  4. 返回拉取统计
"""

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd


def fetch_and_save_daily(code: str, days: int = 730,
                         start_date: str = None, end_date: str = None) -> dict:
    """
    拉取单只股票日线数据并保存到本地数据库
    :param code: 股票代码（如 000001）
    :param days: 拉取天数（默认 2 年）
    :param start_date: 起始日期 YYYY-MM-DD（优先级高于 days）
    :param end_date: 结束日期 YYYY-MM-DD（默认今天）
    :return: {"success": bool, "code": str, "rows": int, "error": str}
    """
    import akshare as ak
    from core.db import upsert_daily_price

    end = datetime.now()
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d")
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d")
    else:
        start = end - timedelta(days=days)

    ak_start = start.strftime("%Y%m%d")
    ak_end = end.strftime("%Y%m%d")

    # 判断交易所前缀
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = f"{prefix}{code}"

    try:
        df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=ak_start,
            end_date=ak_end,
            adjust="qfq"
        )
        if df.empty:
            return {"success": False, "code": code, "rows": 0, "error": "无数据返回"}

        # 统一列名和格式
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df = df.sort_index()

        # 确保必要列存在
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                return {"success": False, "code": code, "rows": 0, "error": f"缺少列 {col}"}

        # pct_change 可能不存在，从 close 计算
        if "pct_change" not in df.columns:
            df["pct_change"] = df["close"].pct_change() * 100

        rows = upsert_daily_price(code, df)
        return {"success": True, "code": code, "rows": rows, "error": ""}

    except Exception as e:
        return {"success": False, "code": code, "rows": 0, "error": str(e)}


def batch_fetch_and_save(codes: list[str], days: int = 730,
                         on_progress: Optional[callable] = None) -> dict:
    """
    批量拉取并保存
    :param codes: 股票代码列表
    :param days: 拉取天数
    :param on_progress: 进度回调 (current, total, code, success, rows)
    :return: 汇总统计
    """
    results = []
    success_count = 0
    total_rows = 0

    for i, code in enumerate(codes):
        result = fetch_and_save_daily(code, days=days)
        results.append(result)
        if result["success"]:
            success_count += 1
            total_rows += result["rows"]
        if on_progress:
            on_progress(i + 1, len(codes), code, result["success"], result.get("rows", 0))

    return {
        "success": True,
        "total": len(codes),
        "success_count": success_count,
        "failed_count": len(codes) - success_count,
        "total_rows": total_rows,
        "details": results,
    }
