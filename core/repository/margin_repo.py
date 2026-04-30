"""
core/repository/margin_repo.py —— 融资融券 Repository
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


# ── stock_margin（全市场融资融券汇总）──

def upsert_market_margin(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    records = []
    for _, row in df.iterrows():
        d = row.get("trade_date") or row.get("信用交易日期") or row.get("日期")
        if d is None:
            continue
        if hasattr(d, "date"):
            d = str(d.date())
        elif isinstance(d, "str"):
            d = d[:10] if len(d) >= 10 else d
        else:
            d = str(d)[:10]
        records.append({
            "trade_date": d,
            "sse_margin_balance": _clean(row.get("sse_margin_balance")),
            "sse_margin_buy": _clean(row.get("sse_margin_buy")),
            "sse_short_balance": _clean(row.get("sse_short_balance")),
            "sse_short_volume": _clean(row.get("sse_short_volume")),
            "szse_margin_balance": _clean(row.get("szse_margin_balance")),
            "szse_margin_buy": _clean(row.get("szse_margin_buy")),
            "szse_short_balance": _clean(row.get("szse_short_balance")),
            "szse_short_volume": _clean(row.get("szse_short_volume")),
            "szse_margin_ratio": _clean(row.get("szse_margin_ratio")),
            "total_margin_balance": _clean(row.get("total_margin_balance")),
            "total_margin_buy": _clean(row.get("total_margin_buy")),
            "total_short_balance": _clean(row.get("total_short_balance")),
            "total_margin": _clean(row.get("total_margin")),
        })
    if not records:
        return 0
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_margin
              (trade_date, sse_margin_balance, sse_margin_buy, sse_short_balance, sse_short_volume,
               szse_margin_balance, szse_margin_buy, szse_short_balance, szse_short_volume,
               szse_margin_ratio, total_margin_balance, total_margin_buy, total_short_balance, total_margin)
            VALUES
              (:trade_date, :sse_margin_balance, :sse_margin_buy, :sse_short_balance, :sse_short_volume,
               :szse_margin_balance, :szse_margin_buy, :szse_short_balance, :szse_short_volume,
               :szse_margin_ratio, :total_margin_balance, :total_margin_buy, :total_short_balance, :total_margin)
            ON CONFLICT(trade_date) DO UPDATE SET
              sse_margin_balance=excluded.sse_margin_balance, sse_margin_buy=excluded.sse_margin_buy,
              sse_short_balance=excluded.sse_short_balance, sse_short_volume=excluded.sse_short_volume,
              szse_margin_balance=excluded.szse_margin_balance, szse_margin_buy=excluded.szse_margin_buy,
              szse_short_balance=excluded.szse_short_balance, szse_short_volume=excluded.szse_short_volume,
              szse_margin_ratio=excluded.szse_margin_ratio,
              total_margin_balance=excluded.total_margin_balance, total_margin_buy=excluded.total_margin_buy,
              total_short_balance=excluded.total_short_balance, total_margin=excluded.total_margin
        """, records)
    return len(records)


def get_market_margin(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    start_date = _fmt_date(start_date)
    end_date = _fmt_date(end_date)
    sql = "SELECT * FROM stock_margin WHERE 1=1"
    params = []
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


def get_latest_margin_date() -> str | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM stock_margin").fetchone()
    return row["d"] if row and row["d"] else None


# ── stock_margin_detail（个股融资融券明细）──

def upsert_margin_detail(code: str, df: pd.DataFrame, exchange: str = "SZ") -> int:
    if df is None or df.empty:
        return 0
    records = []
    for idx, row in df.iterrows():
        d = idx
        if d is None or (isinstance(d, str) and d.strip() == ""):
            continue
        if hasattr(d, "date"):
            d = str(d.date())
        elif isinstance(d, str):
            d = d[:10] if len(d) >= 10 else d
        else:
            d = str(d)[:10]
        name = row.get("name") or row.get("证券简称") or row.get("标的证券简称") or ""
        records.append({
            "code": code, "trade_date": d, "name": name, "exchange": exchange,
            "margin_balance": _clean(row.get("margin_balance") or row.get("融资余额")),
            "margin_buy": _clean(row.get("margin_buy") or row.get("融资买入额")),
            "margin_repay": _clean(row.get("margin_repay") or row.get("融资偿还额")),
            "short_volume": _clean(row.get("short_volume") or row.get("融券余量")),
            "short_sell": _clean(row.get("short_sell") or row.get("融券卖出量")),
            "short_repay": _clean(row.get("short_repay") or row.get("融券偿还量")),
            "short_balance": _clean(row.get("short_balance") or row.get("融券余额")),
            "total_balance": _clean(row.get("total_balance") or row.get("融资融券余额")),
        })
    if not records:
        return 0
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_margin_detail
              (code, trade_date, name, exchange,
               margin_balance, margin_buy, margin_repay,
               short_volume, short_sell, short_repay,
               short_balance, total_balance)
            VALUES
              (:code, :trade_date, :name, :exchange,
               :margin_balance, :margin_buy, :margin_repay,
               :short_volume, :short_sell, :short_repay,
               :short_balance, :total_balance)
            ON CONFLICT(code, trade_date) DO UPDATE SET
              name=excluded.name,
              margin_balance=excluded.margin_balance, margin_buy=excluded.margin_buy,
              margin_repay=excluded.margin_repay,
              short_volume=excluded.short_volume, short_sell=excluded.short_sell,
              short_repay=excluded.short_repay,
              short_balance=excluded.short_balance, total_balance=excluded.total_balance
        """, records)
    return len(records)


def get_margin_detail(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    start_date = _fmt_date(start_date)
    end_date = _fmt_date(end_date)
    sql = "SELECT * FROM stock_margin_detail WHERE code=?"
    params = [code]
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
