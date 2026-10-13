"""
watchlist_repo.py —— personal_watchlist（个人自选股）只读数据访问层

T10 自 core/db.py 迁入。表由 routes.investor.init_investor_tables 创建，
可能晚于 init_db，故所有查询保留「表不存在 → 空自选」的降级语义，不抛异常。
"""
import pandas as pd


def _get_conn():
    # 惰性导入：与其余 repo 一致，规避包初始化期的循环依赖
    from core.db import get_conn
    return get_conn()


def get_watchlist_codes() -> list[str]:
    """返回所有自选股代码（去重、按 created_at 升序，便于展示稳定）。

    表不存在或为空时返回空列表（让同步层走降级分支）。
    """
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT code FROM personal_watchlist "
                "WHERE code IS NOT NULL AND code != '' "
                "GROUP BY code ORDER BY MIN(created_at)"
            ).fetchall()
    except Exception:
        # 表尚未创建（init_investor_tables 未跑）—— 视作空自选
        return []
    return [r[0] for r in rows]


def get_watchlist_with_names() -> pd.DataFrame:
    """返回自选股的 code / name / market / created_at / note。

    LEFT JOIN stock_info，name 缺失时退化为 code。
    """
    try:
        with _get_conn() as conn:
            rows = conn.execute("""
                SELECT w.code, COALESCE(s.name, w.code) AS name,
                       s.market, w.created_at, w.note
                FROM personal_watchlist w
                LEFT JOIN stock_info s ON s.code = w.code
                GROUP BY w.code
                ORDER BY MIN(w.created_at)
            """).fetchall()
    except Exception:
        return pd.DataFrame(columns=["code", "name", "market", "created_at", "note"])
    return pd.DataFrame([dict(r) for r in rows])


def count_watchlist() -> int:
    """返回自选股去重后的数量（表不存在时返回 0）"""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM personal_watchlist"
            ).fetchone()
    except Exception:
        return 0
    return int(row[0] or 0)
