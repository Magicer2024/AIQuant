"""
services/backtest_service.py —— 批量回测服务
"""
import traceback
from datetime import date
from collections import defaultdict
from typing import Dict, Any, List


def run_batch_backtest(start_date: str = "2025-04-03", end_date: str = None,
                       min_score: float = 15.0, capital: float = 100000,
                       max_positions: int = 3, max_position_size: float = 400000,
                       stop_loss: float = -0.06, take_profit: float = 0.20,
                       use_market_timing: bool = True, use_dynamic_position: bool = False,
                       weights: List[float] = None, use_v4: bool = True) -> Dict[str, Any]:
    """运行批量回测（v4超跌反弹策略）"""
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    from scripts.batch.batch_backtest import run_batch_backtest as _run

    result = _run(
        start_date=start_date,
        end_date=end_date,
        min_score=min_score,
        init_cash=capital,
        max_positions=max_positions,
        max_position_size=max_position_size,
        stop_loss=stop_loss,
        take_profit=take_profit,
        use_market_timing=use_market_timing,
        use_dynamic_position=use_dynamic_position,
        weights=weights,
        use_fundamental_filter=False,
        trailing_pct=take_profit,
        verbose=False,
        use_v4=use_v4,
    )

    if "error" in result and "trades" not in result:
        raise ValueError(result["error"])

    raw_trades = result.get("trades", [])
    paired = _pair_trades(raw_trades)
    closed = [t for t in paired if t["exit_date"]]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    losses = [t for t in closed if (t["pnl"] or 0) < 0]
    win_rate = len(wins) / max(1, len(closed))
    avg_win_pct = sum(t["pnl_pct"] for t in wins) / max(1, len(wins)) if wins else 0
    avg_loss_pct = abs(sum(t["pnl_pct"] for t in losses) / max(1, len(losses))) if losses else 0

    return {
        "start_date": start_date,
        "end_date": end_date,
        "init_capital": result.get("init_cash"),
        "final_capital": result.get("final_assets"),
        "total_return": result.get("total_return"),
        "annual_return": result.get("ann_return"),
        "max_drawdown": result.get("max_drawdown"),
        "win_rate": win_rate,
        "profit_factor": result.get("profit_factor"),
        "total_trades": result.get("total_trades"),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "avg_hold_days": round(sum(t["holding_days"] for t in closed) / max(1, len(closed)), 1) if closed else 0,
        "total_commission": result.get("total_commission"),
        "unique_stocks": result.get("unique_stocks"),
        "total_cost": result.get("total_cost"),
        "equity_curve": result.get("daily_equity", []),
        "trades": paired,
        "raw_trades": raw_trades,
    }


def _pair_trades(raw_trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将原始交易记录按 code 配对为买入/卖出"""
    code_map = defaultdict(list)
    for t in raw_trades:
        code_map[t["code"]].append(t)

    paired = []
    for code, tlist in code_map.items():
        tlist.sort(key=lambda x: x["date"])
        buys = [t for t in tlist if t["direction"] == "buy"]
        sells = [t for t in tlist if t["direction"] == "sell"]
        for i, buy in enumerate(buys):
            entry_date = buy["date"]
            entry_price = buy["price"]
            shares = buy["shares"]
            trigger = buy.get("trigger", "")
            if i < len(sells):
                sell = sells[i]
                exit_date = sell["date"]
                exit_price = sell["price"]
                pnl = sell.get("pnl", 0) or 0
                pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
                r = sell.get("reason", "")
                if "stop_loss" in r or "止损" in r:
                    exit_reason = "止损"
                elif "trailing" in r or "跟踪" in r:
                    exit_reason = "跟踪止盈"
                else:
                    exit_reason = "止盈" if pnl > 0 else "止损"
            else:
                exit_date = None
                exit_price = None
                pnl = None
                pnl_pct = None
                exit_reason = "持仓中"
            if exit_date:
                from datetime import datetime
                d0 = datetime.strptime(entry_date[:10], "%Y-%m-%d")
                d1 = datetime.strptime(exit_date[:10], "%Y-%m-%d")
                holding_days = (d1 - d0).days
            else:
                holding_days = None
            paired.append({
                "code": code,
                "name": buy.get("name", code),
                "entry_date": entry_date[:10] if entry_date else None,
                "entry_price": round(entry_price, 2),
                "shares": shares,
                "exit_date": exit_date[:10] if exit_date else None,
                "exit_price": round(exit_price, 2) if exit_price else None,
                "holding_days": holding_days,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                "exit_reason": exit_reason,
                "trigger": trigger,
            })
    paired.sort(key=lambda x: x["entry_date"] or "")
    return paired
