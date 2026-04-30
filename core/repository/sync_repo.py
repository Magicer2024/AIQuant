"""
sync_repo.py —— sync_log 操作 + 数据库状态统计
"""
import os
from datetime import datetime


def _get_conn():
    from core.db import get_conn
    return get_conn()


def _db_path():
    from core.db import DB_PATH
    return DB_PATH


def log_sync(sync_type: str, total: int, success: int, failed: int,
             duration_s: float, note: str = ""):
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_log(sync_time, sync_type, total, success, failed, duration_s, note)
            VALUES(?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              sync_type, total, success, failed, round(duration_s, 1), note))


def get_sync_logs(limit: int = 20) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def db_stats() -> dict:
    """返回数据库概要统计信息"""
    with _get_conn() as conn:
        stock_cnt  = conn.execute("SELECT COUNT(*) FROM stock_info WHERE is_active=1").fetchone()[0]
        price_cnt  = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        code_cnt   = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_price").fetchone()[0]
        signal_cnt = conn.execute("SELECT COUNT(*) FROM signal_records").fetchone()[0]
        latest_d   = conn.execute("SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
        earliest_d = conn.execute("SELECT MIN(trade_date) FROM daily_price").fetchone()[0]
        db_size    = os.path.getsize(_db_path()) / 1024 / 1024  # MB
    return {
        "股票列表数":     stock_cnt,
        "有行情股票数":   code_cnt,
        "行情记录总数":   price_cnt,
        "信号记录总数":   signal_cnt,
        "最早日期":       earliest_d or "—",
        "最新日期":       latest_d or "—",
        "数据库大小(MB)": round(db_size, 2),
    }
