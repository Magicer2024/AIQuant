"""
core/repository/north_repo.py —— 北向资金 Repository
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


def upsert_north_money(df: pd.DataFrame) -> int:
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
            "north_net_buy": _clean(row.get("north_net_buy")),
            "north_buy_amt": _clean(row.get("north_buy_amt")),
            "north_sell_amt": _clean(row.get("north_sell_amt")),
            "north_cum_net": _clean(row.get("north_cum_net")),
            "north_daily_flow": _clean(row.get("north_daily_flow")),
            "north_balance": _clean(row.get("north_balance")),
            "north_market_cap": _clean(row.get("north_market_cap")),
            "hgt_net_buy": _clean(row.get("hgt_net_buy")),
            "hgt_buy_amt": _clean(row.get("hgt_buy_amt")),
            "hgt_sell_amt": _clean(row.get("hgt_sell_amt")),
            "hgt_cum_net": _clean(row.get("hgt_cum_net")),
            "hgt_daily_flow": _clean(row.get("hgt_daily_flow")),
            "sgt_net_buy": _clean(row.get("sgt_net_buy")),
            "sgt_buy_amt": _clean(row.get("sgt_buy_amt")),
            "sgt_sell_amt": _clean(row.get("sgt_sell_amt")),
            "sgt_cum_net": _clean(row.get("sgt_cum_net")),
            "sgt_daily_flow": _clean(row.get("sgt_daily_flow")),
        })
    if not records:
        return 0
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_hsgt_north
              (trade_date, north_net_buy, north_buy_amt, north_sell_amt,
               north_cum_net, north_daily_flow, north_balance, north_market_cap,
               hgt_net_buy, hgt_buy_amt, hgt_sell_amt, hgt_cum_net, hgt_daily_flow,
               sgt_net_buy, sgt_buy_amt, sgt_sell_amt, sgt_cum_net, sgt_daily_flow)
            VALUES
              (:trade_date, :north_net_buy, :north_buy_amt, :north_sell_amt,
               :north_cum_net, :north_daily_flow, :north_balance, :north_market_cap,
               :hgt_net_buy, :hgt_buy_amt, :hgt_sell_amt, :hgt_cum_net, :hgt_daily_flow,
               :sgt_net_buy, :sgt_buy_amt, :sgt_sell_amt, :sgt_cum_net, :sgt_daily_flow)
            ON CONFLICT(trade_date) DO UPDATE SET
              north_net_buy=excluded.north_net_buy, north_buy_amt=excluded.north_buy_amt,
              north_sell_amt=excluded.north_sell_amt, north_cum_net=excluded.north_cum_net,
              north_daily_flow=excluded.north_daily_flow, north_balance=excluded.north_balance,
              north_market_cap=excluded.north_market_cap,
              hgt_net_buy=excluded.hgt_net_buy, hgt_buy_amt=excluded.hgt_buy_amt,
              hgt_sell_amt=excluded.hgt_sell_amt, hgt_cum_net=excluded.hgt_cum_net,
              hgt_daily_flow=excluded.hgt_daily_flow,
              sgt_net_buy=excluded.sgt_net_buy, sgt_buy_amt=excluded.sgt_buy_amt,
              sgt_sell_amt=excluded.sgt_sell_amt, sgt_cum_net=excluded.sgt_cum_net,
              sgt_daily_flow=excluded.sgt_daily_flow
        """, records)
    return len(records)


def get_north_money(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    start_date = _fmt_date(start_date)
    end_date = _fmt_date(end_date)
    sql = "SELECT * FROM stock_hsgt_north WHERE 1=1"
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


def get_latest_hsgt_date() -> str | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM stock_hsgt_north").fetchone()
    return row["d"] if row and row["d"] else None
