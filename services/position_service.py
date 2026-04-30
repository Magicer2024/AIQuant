"""
services/position_service.py —— 持仓管理服务
"""
import traceback
from datetime import date
from typing import Dict, Any, List

import core.db as db
from core.data_fetcher import get_realtime_price


def get_positions(status: str = "holding") -> List[Dict[str, Any]]:
    """获取持仓列表（含盈亏计算）"""
    positions = db.get_positions(status)
    for p in positions:
        if p.get("current_price") and p.get("entry_price") and p.get("shares"):
            p["pnl"] = round((p["current_price"] - p["entry_price"]) * p["shares"], 2)
            p["pnl_pct"] = round((p["current_price"] / p["entry_price"] - 1) * 100, 2)
        else:
            p["pnl"] = 0
            p["pnl_pct"] = 0
    return positions


def add_position(data: Dict[str, Any]) -> int:
    """新增持仓"""
    return db.add_position(
        code=data.get("code", ""),
        name=data.get("name", ""),
        entry_date=data.get("entry_date", date.today().strftime("%Y-%m-%d")),
        entry_price=float(data.get("entry_price", 0)),
        shares=int(data.get("shares", 0)),
        stop_loss=float(data.get("stop_loss")) if data.get("stop_loss") else None,
        take_profit=float(data.get("take_profit")) if data.get("take_profit") else None,
        strategy=data.get("strategy", ""),
        note=data.get("note", ""),
        commission=float(data.get("commission", 0)) if data.get("commission") else 0,
    )


def close_position(position_id: Any, exit_price: float, reason: str = "manual") -> Dict[str, Any]:
    """平仓"""
    return db.close_position(position_id, exit_price, reason=reason)


def partial_close_position(position_id: Any, exit_price: float, shares: int, reason: str = "manual") -> Dict[str, Any]:
    """部分卖出"""
    return db.partial_close_position(position_id, exit_price, shares, reason=reason)


def update_position_price(position_id: Any, current_price: float) -> None:
    """更新持仓价格"""
    db.update_position_price(position_id, current_price)


def get_position_summary() -> Dict[str, Any]:
    """获取持仓汇总"""
    return db.get_position_summary()


def delete_position(position_id: Any) -> None:
    """删除持仓记录"""
    db.delete_position(position_id)


def refresh_position_prices() -> Dict[str, Any]:
    """批量刷新所有持仓的当前价格"""
    positions = db.get_positions("holding")
    updated = 0
    for pos in positions:
        code = pos["code"]
        try:
            info = get_realtime_price(code)
            if info and info.get("price", 0) > 0:
                db.update_position_price(pos["id"], info["price"])
                updated += 1
        except Exception:
            continue
    return {"success": True, "updated": updated}
