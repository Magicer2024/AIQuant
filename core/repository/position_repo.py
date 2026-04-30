"""
position_repo.py —— positions 表的数据访问层
"""
from datetime import datetime, date


def _get_conn():
    from core.db import get_conn
    return get_conn()


def add_position(code: str, name: str, entry_date: str, entry_price: float,
                 shares: int, strategy: str = "", stop_loss: float = None,
                 take_profit: float = None) -> int:
    """添加持仓记录，返回 position_id"""
    with _get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO positions
              (code, name, entry_date, entry_price, shares, strategy,
               stop_loss, take_profit, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'holding', ?)
        """, (code, name, entry_date, entry_price, shares, strategy,
              stop_loss, take_profit, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return cursor.lastrowid


def close_position(position_id: int, exit_price: float, exit_date: str = None,
                   pnl: float = None, pnl_pct: float = None):
    """平仓"""
    if exit_date is None:
        exit_date = date.today().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        conn.execute("""
            UPDATE positions SET
                exit_date=?, exit_price=?, status='closed',
                pnl=?, pnl_pct=?, updated_at=?
            WHERE id=?
        """, (exit_date, exit_price, pnl, pnl_pct,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S"), position_id))


def partial_close_position(position_id: int, exit_price: float, shares_to_sell: int,
                           exit_date: str = None, pnl: float = None, pnl_pct: float = None):
    """部分平仓：原记录减仓，新增一条已平仓记录"""
    if exit_date is None:
        exit_date = date.today().strftime("%Y-%m-%d")
    with _get_conn() as conn:
        # 查原记录
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        if not row:
            return
        orig_shares = row["shares"]
        if shares_to_sell >= orig_shares:
            # 全平
            close_position(position_id, exit_price, exit_date, pnl, pnl_pct)
            return
        # 减仓
        conn.execute(
            "UPDATE positions SET shares=? WHERE id=?",
            (orig_shares - shares_to_sell, position_id)
        )
        # 新增已平仓记录
        conn.execute("""
            INSERT INTO positions
              (code, name, entry_date, entry_price, shares, strategy,
               stop_loss, take_profit, exit_date, exit_price, status, pnl, pnl_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)
        """, (row["code"], row["name"], row["entry_date"], row["entry_price"],
              shares_to_sell, row["strategy"], row["stop_loss"], row["take_profit"],
              exit_date, exit_price, pnl, pnl_pct,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def update_position_price(position_id: int, current_price: float):
    """更新持仓当前价（用于浮动盈亏计算）"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE positions SET current_price=? WHERE id=?",
            (current_price, position_id)
        )


def get_positions(status: str = "holding") -> list[dict]:
    """获取持仓列表"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status=? ORDER BY entry_date DESC",
            (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_position_by_id(position_id: int) -> dict | None:
    """按 ID 查持仓"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


def get_position_by_code(code: str, status: str = "holding") -> dict | None:
    """按代码查持仓"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE code=? AND status=? ORDER BY id DESC LIMIT 1",
            (code, status)
        ).fetchone()
    return dict(row) if row else None


def delete_position(position_id: int):
    """删除持仓记录"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM positions WHERE id=?", (position_id,))


def get_position_summary() -> dict:
    """持仓汇总统计"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='holding'"
        ).fetchall()
    if not rows:
        return {"total_value": 0, "total_pnl": 0, "pnl_pct": 0, "count": 0}
    total_cost = sum(r["entry_price"] * r["shares"] for r in rows)
    total_value = sum((r["current_price"] or r["entry_price"]) * r["shares"] for r in rows)
    total_pnl = total_value - total_cost
    pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
    return {
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "count": len(rows),
    }
