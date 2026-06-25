"""
core/em_kline.py —— 东方财富 K 线直连（替代 baostock/akshare 切片拉取）
====================================================================
特点：
  - 单次 HTTP 拿到单只股票全历史 K 线（beg=0, end=20500101）
  - 支持前复权 fqt=1 / 后复权 fqt=2 / 不复权 fqt=0
  - 支持日/周/月 K 线（klt=101/102/103）
  - 内置 SQLite 缓存，按 (code, adjust, klt) 持久化
"""
from __future__ import annotations

import os
import time
import sqlite3
import logging
from datetime import datetime, timedelta

import pandas as pd

from core.em_realtime import _SESSION, _UT, to_em_secid

logger = logging.getLogger(__name__)

# 缓存数据库路径（独立 SQLite，避免污染业务库）
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)
_CACHE_DB = os.path.join(_CACHE_DIR, "em_kline_cache.db")

_ADJUST_MAP = {"qfq": "1", "hfq": "2", "none": "0", "": "0"}
_KLT_MAP = {"d": "101", "w": "102", "m": "103"}


# ─────────────────────────────────────────────
# 缓存层
# ─────────────────────────────────────────────
def _ensure_cache_db() -> None:
    """建缓存表（首次调用时）"""
    with sqlite3.connect(_CACHE_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kline_cache (
                code        TEXT NOT NULL,
                adjust      TEXT NOT NULL,
                klt         TEXT NOT NULL,
                trade_date  TEXT NOT NULL,
                open        REAL, high REAL, low REAL, close REAL,
                volume      REAL, amount REAL,
                pct_change  REAL, turnover REAL,
                PRIMARY KEY (code, adjust, klt, trade_date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kc_code ON kline_cache(code, adjust, klt)")


def _read_cache(code: str, adjust: str, klt: str) -> pd.DataFrame:
    """从缓存读单只股票历史"""
    with sqlite3.connect(_CACHE_DB) as conn:
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, volume, amount, pct_change, turnover "
            "FROM kline_cache WHERE code=? AND adjust=? AND klt=? ORDER BY trade_date",
            conn,
            params=(code, adjust, klt),
        )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    return df


def _write_cache(df: pd.DataFrame, code: str, adjust: str, klt: str) -> None:
    """写入缓存（INSERT OR REPLACE）"""
    if df.empty:
        return
    records = []
    for dt, row in df.iterrows():
        records.append((
            code, adjust, klt,
            str(dt.date()) if hasattr(dt, "date") else str(dt)[:10],
            float(row.get("open", 0) or 0),
            float(row.get("high", 0) or 0),
            float(row.get("low", 0) or 0),
            float(row.get("close", 0) or 0),
            float(row.get("volume", 0) or 0),
            float(row.get("amount", 0) or 0),
            float(row.get("pct_change", 0) or 0),
            float(row.get("turnover", 0) or 0),
        ))
    with sqlite3.connect(_CACHE_DB) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO kline_cache "
            "(code,adjust,klt,trade_date,open,high,low,close,volume,amount,pct_change,turnover) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            records,
        )


# ─────────────────────────────────────────────
# 核心：单只股票全历史 K 线（一次 HTTP 拿全）
# ─────────────────────────────────────────────
def fetch_kline(code: str, adjust: str = "qfq", klt: str = "d",
                beg: int = 0, end: int = 20500101) -> pd.DataFrame:
    """
    单次 HTTP 拉单只股票全历史 K 线（带护栏：单只接口 TTL 内直接走缓存）

    :param code: 纯代码 '600519'
    :param adjust: qfq/hfq/none
    :param klt: d/w/m (日/周/月)
    :param beg: 起始日期 YYYYMMDD（0 表示从最早开始）
    :param end: 结束日期 YYYYMMDD（20500101 表示到最远）
    :return: DataFrame, index=trade_date
    """
    from core.em_guard import cached_fetch, GUARD_CONFIG

    cfg = GUARD_CONFIG.get("kline", {})
    ttl = cfg.get("default_ttl", 86400)

    def _do_fetch():
        secid = to_em_secid(code)
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "ut": _UT,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": _KLT_MAP.get(klt, "101"),
            "fqt": _ADJUST_MAP.get(adjust, "1"),
            "beg": str(beg),
            "end": str(end),
            "lmt": "1000",
        }
        resp = _SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        klines = (data or {}).get("data", {}).get("klines") or []
        if not klines:
            return pd.DataFrame()
        return _parse_klines(klines)

    cache_params = {
        "code": code, "adjust": adjust, "klt": klt,
        "beg": str(beg), "end": str(end),
    }
    df, source = cached_fetch("kline", cache_params, _do_fetch, ttl=ttl)
    return df if df is not None else pd.DataFrame()


def _parse_klines(klines: list[str]) -> pd.DataFrame:
    """把东财返回的 kline 字符串列表解析为 DataFrame"""
    rows = [k.split(",") for k in klines]
    # 字段顺序: 日期, 开, 收, 高, 低, 量, 额, 振幅, 涨跌幅, 换手率, 5日换手
    df = pd.DataFrame(rows, columns=[
        "date", "open", "close", "high", "low",
        "volume", "amount", "amplitude", "pct_change", "turnover",
        "turnover_5d",
    ])
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    for col in ["open", "close", "high", "low", "volume", "amount",
                "amplitude", "pct_change", "turnover", "turnover_5d"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop(columns=["turnover_5d"], errors="ignore")
    return df.sort_index()


# ─────────────────────────────────────────────
# 公开接口：带缓存的历史 K 线（推荐池专享）
# ─────────────────────────────────────────────
def get_stock_history(code: str, start_date: str | None = None,
                      end_date: str | None = None,
                      adjust: str = "qfq",
                      use_cache: bool = True) -> pd.DataFrame:
    """
    获取个股历史 K 线（盘前/盘后推荐主入口）

    流程：
        1. 命中 SQLite 缓存 → 直接返回
        2. 未命中 → 一次 HTTP 拉全历史 → 写缓存 → 切片返回

    :param code: 纯代码 '000001'
    :param start_date: 'YYYYMMDD' 或 'YYYY-MM-DD'，默认近 2 年
    :param end_date: 同上，默认今天
    :param adjust: qfq/hfq/none
    """
    _ensure_cache_db()

    # 默认时间范围
    if end_date is None:
        end_date = datetime.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.today() - timedelta(days=365 * 2)).strftime("%Y%m%d")
    start_date = start_date.replace("-", "")
    end_date = end_date.replace("-", "")

    # 1) 读缓存
    if use_cache:
        cached = _read_cache(code, adjust, "d")
        if not cached.empty:
            # 切片返回
            mask = (cached.index >= start_date) & (cached.index <= end_date + "999999")
            sliced = cached[mask] if isinstance(cached.index[0], str) else cached.loc[start_date:end_date]
            if not sliced.empty and len(sliced) >= 5:
                return sliced

    # 2) 东财直连一次拉全历史
    df_full = pd.DataFrame()
    try:
        df_full = fetch_kline(code, adjust=adjust, klt="d")
    except Exception as e:
        logger.warning("东财 kline 直连失败 %s: %s，尝试 akshare 降级", code, e)
        try:
            import akshare as ak
            df_full = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_date, end_date=end_date, adjust=adjust,
            )
            if not df_full.empty:
                df_full.rename(columns={
                    "日期":"date","开盘":"open","收盘":"close","最高":"high","最低":"low",
                    "成交量":"volume","成交额":"amount","涨跌幅":"pct_change","换手率":"turnover",
                }, inplace=True)
                df_full["date"] = pd.to_datetime(df_full["date"])
                df_full.set_index("date", inplace=True)
                df_full = df_full[[c for c in ["open","close","high","low","volume","amount","pct_change","turnover"] if c in df_full.columns]]
                df_full.sort_index(inplace=True)
        except Exception as e2:
            logger.error("akshare 降级也失败 %s: %s", code, e2)
            return pd.DataFrame()

    if df_full.empty:
        return df_full

    # 写缓存
    if use_cache:
        _write_cache(df_full, code, adjust, "d")

    # 切片
    if isinstance(df_full.index[0], pd.Timestamp):
        mask = (df_full.index.strftime("%Y%m%d") >= start_date) & \
               (df_full.index.strftime("%Y%m%d") <= end_date)
        return df_full[mask]
    return df_full


# ─────────────────────────────────────────────
# 增量更新：补最近 1 个交易日
# ─────────────────────────────────────────────
def incremental_update(code: str, last_date: str, adjust: str = "qfq") -> pd.DataFrame:
    """
    增量更新单只股票：从 last_date 的下一天到今天

    :param last_date: 'YYYY-MM-DD'（数据库中最新日期）
    """
    beg = (pd.Timestamp(last_date) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    end = datetime.today().strftime("%Y%m%d")
    if beg > end:
        return pd.DataFrame()
    df = fetch_kline(code, adjust=adjust, klt="d",
                     beg=int(beg), end=int(end))
    if not df.empty:
        _write_cache(df, code, adjust, "d")
    return df
