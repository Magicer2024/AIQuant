"""
stock_repo.py —— stock_info 表的数据访问层
"""
import pandas as pd
from datetime import datetime


def _get_conn():
    from core.db import get_conn
    return get_conn()


def upsert_stock_list(records: list[dict]):
    """
    批量写入/更新股票列表
    records: [{"code": "000001", "name": "平安银行", "market": "SZ"}, ...]
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO stock_info(code, name, market, updated_at)
            VALUES(:code, :name, :market, :updated_at)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                market=excluded.market,
                updated_at=excluded.updated_at
        """, [{**r, "updated_at": now} for r in records])
    print(f"[DB] stock_info 更新 {len(records)} 条")


def update_market_cap(code: str, total_shares: float, circ_shares: float):
    """更新单只股票的总股本和流通股本"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE stock_info SET total_shares=?, circ_shares=? WHERE code=?",
            (total_shares, circ_shares, code)
        )


def batch_update_market_cap(records: list[dict]):
    """
    批量更新市值数据
    records: [{"code": "000001", "total_shares": 19405918198, "circ_shares": 19405600653}, ...]
    """
    with _get_conn() as conn:
        conn.executemany(
            "UPDATE stock_info SET total_shares=?, circ_shares=? WHERE code=?",
            [(r["total_shares"], r["circ_shares"], r["code"]) for r in records]
        )
    print(f"[DB] market_cap 更新 {len(records)} 条")


def get_market_cap_map() -> dict:
    """获取所有股票的股本信息 {code: {"total_shares": ..., "circ_shares": ...}}"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT code, total_shares, circ_shares FROM stock_info WHERE total_shares IS NOT NULL"
        ).fetchall()
    return {r[0]: {"total_shares": r[1], "circ_shares": r[2]} for r in rows}


def get_all_stocks(active_only=True) -> pd.DataFrame:
    """获取所有股票列表"""
    with _get_conn() as conn:
        sql = "SELECT code, name, market FROM stock_info"
        if active_only:
            sql += " WHERE is_active=1"
        sql += " ORDER BY code"
        rows = conn.execute(sql).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_stock_name(code: str) -> str:
    """根据代码查名称"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM stock_info WHERE code=?", (code,)
        ).fetchone()
    return row["name"] if row else code
