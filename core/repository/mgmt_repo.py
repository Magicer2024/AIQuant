"""
core/repository/mgmt_repo.py —— 高管持股 Repository
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


def upsert_mgmt_holding(df: pd.DataFrame) -> int:
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
            "ann_date": str(row.get("ann_date", "")),
            "holder_name": str(row.get("holder_name", "")),
            "hold_vol": _clean(row.get("hold_vol")),
            "hold_ratio": _clean(row.get("hold_ratio")),
            "share_type": str(row.get("share_type", "")),
            "executive_name": str(row.get("executive_name", "")) if not pd.isna(row.get("executive_name")) else "",
            "position": str(row.get("position", "")),
            "relation": str(row.get("relation", "")),
        })
    if not records:
        return 0
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_mgmt_holding
              (trade_date, code, name, ann_date, holder_name, hold_vol,
               hold_ratio, share_type, executive_name, position, relation)
            VALUES
              (:trade_date, :code, :name, :ann_date, :holder_name, :hold_vol,
               :hold_ratio, :share_type, :executive_name, :position, :relation)
            ON CONFLICT(trade_date, code, holder_name) DO UPDATE SET
              name=excluded.name, ann_date=excluded.ann_date,
              hold_vol=excluded.hold_vol, hold_ratio=excluded.hold_ratio,
              share_type=excluded.share_type, executive_name=excluded.executive_name,
              position=excluded.position, relation=excluded.relation
        """, records)
    return len(records)


def get_mgmt_holding(code: str = None, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    start_date = _fmt_date(start_date)
    end_date = _fmt_date(end_date)
    sql = "SELECT * FROM stock_mgmt_holding WHERE 1=1"
    params = []
    if code:
        sql += " AND code=?"
        params.append(code)
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date DESC"
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df.set_index("trade_date", inplace=True)
    df.drop(columns=["id"], errors="ignore", inplace=True)
    return df


def get_latest_mgmt_holding_date() -> str | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM stock_mgmt_holding").fetchone()
    return row["d"] if row and row["d"] else None
