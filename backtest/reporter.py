"""
reporter.py —— 回测报告生成

组合 backtest_results + backtest_trades 为前端友好的格式
"""
from typing import List, Optional
from backtest.trade_store import get_results, get_result, get_trades


def build_result_list() -> List[dict]:
    """构建回测结果列表（供 Dashboard Tab 2/3 使用）"""
    results = get_results()
    for r in results:
        r["start_date"] = r.get("start_date", "") or ""
        r["end_date"] = r.get("end_date", "") or ""
    return results


def build_result_detail(result_id: int) -> Optional[dict]:
    """构建回测详情（汇总 + 交易明细）"""
    result = get_result(result_id)
    if not result:
        return None

    trades = get_trades(result_id)

    for t in trades:
        if not t.get("name"):
            t["name"] = _lookup_stock_name(t.get("code", ""))

    return {
        "summary": {
            "annual_return": result.get("annual_return", 0),
            "cumulative_return": result.get("cumulative_return", 0),
            "win_rate": result.get("win_rate", 0),
            "sharpe_ratio": result.get("sharpe_ratio", 0),
            "max_drawdown": result.get("max_drawdown", 0),
            "total_trades": result.get("total_trades", 0),
            "win_trades": result.get("win_trades", 0),
            "start_date": result.get("start_date", ""),
            "end_date": result.get("end_date", ""),
            "rule_name": result.get("rule_name", ""),
        },
        "trades": trades,
    }


def _lookup_stock_name(code: str) -> str:
    from core.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM stock_info WHERE code = ?", (code,)).fetchone()
        return row["name"] if row else ""
