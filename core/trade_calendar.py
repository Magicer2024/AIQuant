"""
core/trade_calendar.py —— A股交易日历缓存

akshare 的 tool_trade_date_hist_sina() 每次调用都拉全量交易日历（网络请求），
is_trading_day / get_last_trade_date 等高频调用会造成大量冗余网络开销。
本模块按 TTL 缓存交易日集合，接口失败时由调用方按工作日近似降级。
"""
import time

_CACHE = {"ts": 0.0, "days": None}
TTL_SECONDS = 12 * 3600  # 交易日历不会中途变化，12 小时缓存足够


def get_trade_days(refresh: bool = False):
    """
    返回 A 股交易日集合（元素 'YYYY-MM-DD'）。

    成功：缓存 12 小时；
    失败：60 秒后允许重试（避免 akshare 抖动期间高频打网络），返回 None。
    """
    now = time.time()
    if refresh or _CACHE["days"] is None or (now - _CACHE["ts"]) > TTL_SECONDS:
        try:
            import akshare as ak
            import pandas as pd
            df = ak.tool_trade_date_hist_sina()
            days = set(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist())
        except Exception:
            _CACHE["ts"] = now - TTL_SECONDS + 60  # 失败后 60 秒再试
            return None
        _CACHE.update({"ts": now, "days": days})
    return _CACHE["days"]


def is_trading_day(dt_str: str) -> bool:
    """判断日期（'YYYY-MM-DD'）是否为交易日；接口失败时按工作日近似降级。"""
    days = get_trade_days()
    if days is not None:
        return dt_str in days
    from datetime import datetime
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d").weekday() < 5
    except ValueError:
        return False
