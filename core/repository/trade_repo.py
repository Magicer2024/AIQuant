"""
trade_repo.py —— trades / account_snapshots 表的数据访问层
"""
from datetime import datetime


def _get_conn():
    from core.db import get_conn
    return get_conn()


def add_trade(position_id: int, code: str, name: str, trade_date: str,
              direction: str, price: float, shares: int, pnl: float = None,
              strategy: str = "", remark: str = ""):
    """添加交易记录"""
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO trades
              (position_id, code, name, trade_date, direction, price, shares,
               pnl, strategy, remark, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (position_id, code, name, trade_date, direction, price, shares,
              pnl, strategy, remark, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def get_trades(code: str = None, limit: int = 100) -> list[dict]:
    """获取交易记录"""
    sql = "SELECT * FROM trades"
    params = []
    if code:
        sql += " WHERE code=?"
        params.append(code)
    sql += " ORDER BY trade_date DESC, id DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── 账户快照 ──────────────────────────────────

def save_account_snapshot(total_assets: float, cash: float, position_value: float,
                          total_pnl: float = 0, pnl_pct: float = 0):
    """保存账户快照"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO account_snapshots
              (snapshot_date, total_assets, cash, position_value, total_pnl, pnl_pct)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                total_assets=excluded.total_assets,
                cash=excluded.cash,
                position_value=excluded.position_value,
                total_pnl=excluded.total_pnl,
                pnl_pct=excluded.pnl_pct
        """, (today, total_assets, cash, position_value, total_pnl, pnl_pct))


def get_account_snapshots(days: int = 30) -> list[dict]:
    """获取最近 N 天账户快照"""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM account_snapshots
            ORDER BY snapshot_date DESC LIMIT ?
        """, (days,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_snapshot() -> dict | None:
    """获取最新账户快照"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM account_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
