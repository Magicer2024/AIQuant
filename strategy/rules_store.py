"""
rules_store.py —— 策略规则 CRUD
"""
from typing import List, Optional, Dict
from core.db import get_conn


def list_rules(active_only: bool = False) -> List[dict]:
    """获取所有策略规则列表"""
    with get_conn() as conn:
        sql = """
            SELECT id, rule_name, rule_type, encoding, conditions, sell_conditions,
                   holding_min, holding_max, source, generation, fitness,
                   annual_return, win_rate, sharpe_ratio, max_drawdown, total_trades,
                   signal_overlap, is_active, created_at, updated_at
            FROM strategy_rules
        """
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY fitness DESC"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_rule(rule_id: int) -> Optional[dict]:
    """获取单条规则"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None


def toggle_active(rule_id: int) -> Optional[dict]:
    """切换规则启用/禁用状态，返回新状态"""
    with get_conn() as conn:
        current = conn.execute(
            "SELECT is_active FROM strategy_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if not current:
            return None
        new_state = 0 if current["is_active"] else 1
        conn.execute(
            "UPDATE strategy_rules SET is_active = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (new_state, rule_id),
        )
        return {"id": rule_id, "is_active": new_state}


def save_rule(rule: dict) -> int:
    """插入或更新一条规则（按 rule_name 去重）"""
    # Cast numpy types to Python native to avoid BLOB storage
    def _py(val):
        return val.item() if hasattr(val, "item") else val

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM strategy_rules WHERE rule_name = ?", (rule["rule_name"],)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE strategy_rules SET
                    rule_type=?, encoding=?, conditions=?, sell_conditions=?,
                    holding_min=?, holding_max=?, fitness=?,
                    annual_return=?, win_rate=?, sharpe_ratio=?, max_drawdown=?,
                    total_trades=?, signal_overlap=?, updated_at=datetime('now','localtime')
                WHERE id=?
            """, (
                rule["rule_type"], rule["encoding"], rule.get("conditions", ""),
                rule.get("sell_conditions", ""), _py(rule.get("holding_min", 3)),
                _py(rule.get("holding_max", 20)), _py(rule.get("fitness", 0)),
                _py(rule.get("annual_return", 0)), _py(rule.get("win_rate", 0)),
                _py(rule.get("sharpe_ratio", 0)), _py(rule.get("max_drawdown", 0)),
                _py(rule.get("total_trades", 0)), _py(rule.get("signal_overlap", 0)),
                existing["id"],
            ))
            return existing["id"]
        else:
            cur = conn.execute("""
                INSERT INTO strategy_rules
                    (rule_name, rule_type, encoding, conditions, sell_conditions,
                     holding_min, holding_max, source, generation, fitness,
                     annual_return, win_rate, sharpe_ratio, max_drawdown,
                     total_trades, signal_overlap, is_active)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                rule["rule_name"], rule["rule_type"], rule["encoding"],
                rule.get("conditions", ""), rule.get("sell_conditions", ""),
                _py(rule.get("holding_min", 3)), _py(rule.get("holding_max", 20)),
                rule.get("source", "template"), _py(rule.get("generation", 1)),
                _py(rule.get("fitness", 0)), _py(rule.get("annual_return", 0)),
                _py(rule.get("win_rate", 0)), _py(rule.get("sharpe_ratio", 0)),
                _py(rule.get("max_drawdown", 0)), _py(rule.get("total_trades", 0)),
                _py(rule.get("signal_overlap", 0)), 1,
            ))
            return cur.lastrowid


def delete_rule(rule_id: int) -> bool:
    """删除规则"""
    with get_conn() as conn:
        conn.execute("DELETE FROM strategy_rules WHERE id = ?", (rule_id,))
        return True


def get_active_rules() -> List[dict]:
    """获取所有启用的规则（打分用）"""
    return list_rules(active_only=True)
