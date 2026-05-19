"""
trade_store.py —— 回测交易明细存储
"""
from typing import List, Optional
from core.db import get_conn


def save_result(
    rule_id: str,
    rule_name: str,
    start_date: str,
    end_date: str,
    metrics: dict,
) -> int:
    """保存回测汇总结果，返回 result_id"""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO backtest_results
                (rule_id, rule_name, start_date, end_date,
                 annual_return, cumulative_return, win_rate,
                 sharpe_ratio, max_drawdown, total_trades, win_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            rule_id, rule_name, start_date, end_date,
            metrics.get("annual_return", 0),
            metrics.get("cumulative_return", 0),
            metrics.get("win_rate", 0),
            metrics.get("sharpe_ratio", 0),
            metrics.get("max_drawdown", 0),
            metrics.get("total_trades", 0),
            metrics.get("win_trades", 0),
        ))
        return cur.lastrowid


def save_trades(result_id: int, trades: List[dict]):
    """批量保存逐笔交易"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO backtest_trades
                (result_id, code, name, entry_date, entry_price,
                 exit_date, exit_price, holding_days, pnl_pct, exit_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [
            (
                result_id,
                t.get("code", ""),
                t.get("name", ""),
                t.get("entry_date", ""),
                t.get("entry_price", 0),
                t.get("exit_date", ""),
                t.get("exit_price", 0),
                t.get("holding_days", 0),
                t.get("pnl_pct", 0),
                t.get("exit_reason", "expire"),
            )
            for t in trades
        ])


def get_results() -> List[dict]:
    """获取所有回测结果列表（最新在前）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_results ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_result(result_id: int) -> Optional[dict]:
    """获取单个回测结果"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_results WHERE id = ?", (result_id,)
        ).fetchone()
        return dict(row) if row else None


def get_trades(result_id: int) -> List[dict]:
    """获取回测的逐笔交易明细"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_trades WHERE result_id = ? ORDER BY entry_date
        """, (result_id,)).fetchall()
        return [dict(r) for r in rows]


def get_rule_trades(rule_id: str) -> List[dict]:
    """获取某策略的所有历史交易（跨多次回测）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT bt.* FROM backtest_trades bt
            INNER JOIN backtest_results br ON bt.result_id = br.id
            WHERE br.rule_id = ?
            ORDER BY bt.entry_date
        """, (rule_id,)).fetchall()
        return [dict(r) for r in rows]
