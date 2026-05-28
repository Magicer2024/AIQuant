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
            # Save version before update
            _save_rule_version_internal(existing["id"])
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


def _save_rule_version_internal(rule_id: int):
    """Save current rule as version (called internally from save_rule on UPDATE)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()
        if not row:
            return
        rule = dict(row)

        latest = conn.execute(
            "SELECT conditions, sell_conditions FROM strategy_rule_versions "
            "WHERE rule_id = ? ORDER BY version DESC LIMIT 1",
            (rule_id,)
        ).fetchone()
        if latest and latest["conditions"] == (rule["conditions"] or "") \
                and latest["sell_conditions"] == (rule["sell_conditions"] or ""):
            return

        max_ver = conn.execute(
            "SELECT MAX(version) as v FROM strategy_rule_versions WHERE rule_id = ?",
            (rule_id,)
        ).fetchone()["v"] or 0

        conn.execute("""INSERT INTO strategy_rule_versions
            (rule_id, version, conditions, sell_conditions, holding_min, holding_max,
             fitness, annual_return, win_rate, sharpe_ratio, max_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rule_id, max_ver + 1, rule["conditions"], rule["sell_conditions"],
             rule.get("holding_min", 3), rule.get("holding_max", 20),
             rule.get("fitness", 0), rule.get("annual_return", 0),
             rule.get("win_rate", 0), rule.get("sharpe_ratio", 0),
             rule.get("max_drawdown", 0)))


def save_rule_version(rule_id: int):
    """公开API：保存当前规则为历史版本。返回版本号或None。"""
    with get_conn() as conn:
        rule = conn.execute("SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            return None
    _save_rule_version_internal(rule_id)
    with get_conn() as conn:
        max_ver = conn.execute(
            "SELECT MAX(version) as v FROM strategy_rule_versions WHERE rule_id = ?",
            (rule_id,)
        ).fetchone()["v"] or 0
    return max_ver


def get_rule_versions(rule_id: int) -> list:
    """查询某规则所有历史版本，按版本号降序"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, rule_id, version, fitness, annual_return, win_rate, "
            "sharpe_ratio, max_drawdown, created_at "
            "FROM strategy_rule_versions WHERE rule_id = ? ORDER BY version DESC",
            (rule_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def rollback_rule(rule_id: int, version: int) -> bool:
    """回滚到指定版本。先自动保存当前版本，再覆盖。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_rule_versions WHERE rule_id = ? AND version = ?",
            (rule_id, version)
        ).fetchone()
        if not row:
            return False
        ver = dict(row)

        save_rule_version(rule_id)

        conn.execute("""UPDATE strategy_rules SET
            conditions = ?, sell_conditions = ?,
            holding_min = ?, holding_max = ?,
            fitness = ?, annual_return = ?, win_rate = ?,
            sharpe_ratio = ?, max_drawdown = ?,
            updated_at = datetime('now','localtime')
            WHERE id = ?""",
            (ver["conditions"], ver["sell_conditions"],
             ver.get("holding_min", 3), ver.get("holding_max", 20),
             ver.get("fitness", 0), ver.get("annual_return", 0),
             ver.get("win_rate", 0), ver.get("sharpe_ratio", 0),
             ver.get("max_drawdown", 0),
             rule_id))
    # Save version for the rolled-back state
    _save_rule_version_internal(rule_id)
    return True
