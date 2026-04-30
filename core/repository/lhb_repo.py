"""
core/repository/lhb_repo.py —— 龙虎榜 Repository
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


def upsert_lhb_detail(df: pd.DataFrame) -> int:
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
            "code": str(row.get("code", "")),
            "name": str(row.get("name", "")),
            "close": _clean(row.get("close")),
            "pct_change": _clean(row.get("pct_change")),
            "turnover": _clean(row.get("turnover")),
            "total_buy": _clean(row.get("total_buy")),
            "total_sell": _clean(row.get("total_sell")),
            "net_buy": _clean(row.get("net_buy")),
            "reason": str(row.get("reason", "")),
        })
    if not records:
        return 0
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_lhb_detail
              (trade_date, code, name, close, pct_change, turnover,
               total_buy, total_sell, net_buy, reason)
            VALUES
              (:trade_date, :code, :name, :close, :pct_change, :turnover,
               :total_buy, :total_sell, :net_buy, :reason)
            ON CONFLICT(trade_date, code) DO UPDATE SET
              name=excluded.name, close=excluded.close, pct_change=excluded.pct_change,
              turnover=excluded.turnover, total_buy=excluded.total_buy,
              total_sell=excluded.total_sell, net_buy=excluded.net_buy,
              reason=excluded.reason
        """, records)
    return len(records)


def get_lhb_detail(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    start_date = _fmt_date(start_date)
    end_date = _fmt_date(end_date)
    sql = "SELECT * FROM stock_lhb_detail WHERE 1=1"
    params = []
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date DESC, net_buy DESC"
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id"], errors="ignore", inplace=True)
    return df


def get_latest_lhb_date() -> str | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM stock_lhb_detail").fetchone()
    return row["d"] if row and row["d"] else None
