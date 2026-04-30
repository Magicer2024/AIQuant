"""
core/repository/futures_repo.py —— 指数期货 Repository
"""
import pandas as pd


def _get_conn():
    from core.db import get_conn
    return get_conn()


def _clean(v):
    if v == "-" or v == "" or v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _fmt_date(d):
    if d and len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def upsert_index_futures(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    records = []
    for _, row in df.iterrows():
        d = row.get("trade_date")
        if d is None:
            continue
        if hasattr(d, "date"):
            d = str(d.date())
        elif isinstance(d, str):
            d = d[:10] if len(d) >= 10 else d
        else:
            d = str(d)[:10]
        records.append({
            "trade_date": d,
            "symbol": str(row.get("symbol", "")),
            "variety": str(row.get("variety", "")),
            "open": _clean(row.get("open")),
            "high": _clean(row.get("high")),
            "low": _clean(row.get("low")),
            "close": _clean(row.get("close")),
            "volume": _clean(row.get("volume")),
            "open_interest": _clean(row.get("open_interest")),
            "turnover": _clean(row.get("turnover")),
            "settle": _clean(row.get("settle")),
            "pre_settle": _clean(row.get("pre_settle")),
        })
    if not records:
        return 0
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO index_futures
              (trade_date, symbol, variety, open, high, low, close,
               volume, open_interest, turnover, settle, pre_settle)
            VALUES
              (:trade_date, :symbol, :variety, :open, :high, :low, :close,
               :volume, :open_interest, :turnover, :settle, :pre_settle)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
              variety=excluded.variety, open=excluded.open, high=excluded.high,
              low=excluded.low, close=excluded.close, volume=excluded.volume,
              open_interest=excluded.open_interest, turnover=excluded.turnover,
              settle=excluded.settle, pre_settle=excluded.pre_settle
        """, records)
    return len(records)


def get_index_futures(variety: str = None, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    start_date = _fmt_date(start_date)
    end_date = _fmt_date(end_date)
    sql = "SELECT * FROM index_futures WHERE 1=1"
    params = []
    if variety:
        sql += " AND variety=?"
        params.append(variety)
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC"
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id"], errors="ignore", inplace=True)
    return df


def get_latest_futures_date(variety: str = None) -> str | None:
    sql = "SELECT MAX(trade_date) as d FROM index_futures WHERE 1=1"
    params = []
    if variety:
        sql += " AND variety=?"
        params.append(variety)
    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return row["d"] if row and row["d"] else None
