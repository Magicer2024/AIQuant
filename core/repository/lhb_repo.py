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
    # 表实际字段（与 core/db.py stock_lhb_detail 定义严格对齐）
    cols = [
        "trade_date", "code", "name", "close_price", "pct_change", "net_buy",
        "buy_amt", "sell_amt", "total_amt", "mkt_total_amt", "net_buy_ratio",
        "turnover_rate", "float_mkt_cap", "reason",
        "perf_1d", "perf_2d", "perf_5d", "perf_10d",
    ]
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
        rec = {c: _clean(row.get(c)) for c in cols}
        rec["trade_date"] = d
        rec["code"] = str(row.get("code", ""))
        rec["name"] = str(row.get("name", "") or "")
        rec["reason"] = str(row.get("reason", "") or "")
        records.append(rec)
    if not records:
        return 0
    placeholders = ", ".join(f":{c}" for c in cols)
    col_names = ", ".join(cols)
    update_set = ", ".join(f"{c}=excluded.{c}" for c in cols
                           if c not in ("trade_date", "code"))
    with _get_conn() as conn:
        conn.executemany(f"""
            INSERT INTO stock_lhb_detail ({col_names})
            VALUES ({placeholders})
            ON CONFLICT(trade_date, code) DO UPDATE SET {update_set}
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


def get_lhb_map_for_date(trade_date: str) -> dict:
    """返回指定日期的龙虎榜 {code: {字段...}} 映射，供隔日动量扰描一次性预加载。"""
    trade_date = _fmt_date(trade_date)
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT trade_date, code, name, pct_change, net_buy, net_buy_ratio, reason
            FROM stock_lhb_detail WHERE trade_date = ?
            """,
            (trade_date,),
        ).fetchall()
    return {r["code"]: dict(r) for r in rows}
