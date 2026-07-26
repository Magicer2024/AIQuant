"""
rule_repo.py —— strategy_rules / strategy_signals / active_strategies 表的数据访问层

从 core/db.py 迁移而来，保持函数签名不变。
"""
from datetime import datetime


def _get_conn():
    from core.db import get_conn
    return get_conn()


def upsert_strategy_rule(rule: dict) -> int:
    """插入或更新策略规则，返回 rule_id"""
    def _py(val):
        return val.item() if hasattr(val, "item") else val

    with _get_conn() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur = conn.execute("""
            INSERT INTO strategy_rules
                (rule_name, rule_type, encoding, conditions, sell_conditions,
                 holding_min, holding_max, source, generation, fitness,
                 annual_return, win_rate, sharpe_ratio, max_drawdown,
                 total_trades, signal_overlap, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(rule_name) DO UPDATE SET
                encoding=excluded.encoding, conditions=excluded.conditions,
                sell_conditions=excluded.sell_conditions, fitness=excluded.fitness,
                annual_return=excluded.annual_return, win_rate=excluded.win_rate,
                sharpe_ratio=excluded.sharpe_ratio, max_drawdown=excluded.max_drawdown,
                total_trades=excluded.total_trades, signal_overlap=excluded.signal_overlap,
                updated_at=?
        """, (
            rule["rule_name"], rule["rule_type"], rule.get("encoding", "[]"),
            rule.get("conditions", "[]"), rule.get("sell_conditions", "[]"),
            _py(rule.get("holding_min", 3)), _py(rule.get("holding_max", 20)),
            rule.get("source", "template"), _py(rule.get("generation", 0)),
            _py(rule.get("fitness", 0)), _py(rule.get("annual_return", 0)),
            _py(rule.get("win_rate", 0)), _py(rule.get("sharpe_ratio", 0)),
            _py(rule.get("max_drawdown", 0)), _py(rule.get("total_trades", 0)),
            _py(rule.get("signal_overlap", 0)), now,
        ))
        return cur.lastrowid


def get_active_rules(rule_type: str = None, min_fitness: float = 0.0, limit: int = 200) -> list:
    """获取活跃策略规则列表"""
    with _get_conn() as conn:
        sql = "SELECT * FROM strategy_rules WHERE is_active=1"
        params = []
        if rule_type:
            sql += " AND rule_type=?"
            params.append(rule_type)
        if min_fitness > 0:
            sql += " AND fitness>=?"
            params.append(min_fitness)
        sql += " ORDER BY fitness DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def degrade_rule(rule_id: int):
    """降级策略规则"""
    with _get_conn() as conn:
        conn.execute("""
            UPDATE strategy_rules SET is_active=0, degraded_at=?
            WHERE id=?
        """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rule_id))


def save_strategy_signal(signal: dict):
    """保存单条信号触发记录"""
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO strategy_signals
                (trade_date, code, rule_id, rule_name, confidence,
                 raw_score, features_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["trade_date"], signal["code"], signal["rule_id"],
            signal.get("rule_name", ""), signal.get("confidence"),
            signal.get("raw_score"), signal.get("features_json", "{}"),
        ))


def get_signals_for_training(start_date: str, end_date: str) -> list:
    """获取带标注的信号用于模型训练"""
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT * FROM strategy_signals
            WHERE trade_date BETWEEN ? AND ? AND label_return IS NOT NULL
            ORDER BY trade_date
        """, (start_date, end_date)).fetchall()]


def update_signal_labels(updates: list):
    """批量更新信号的事后标注（未来20日超额收益）"""
    with _get_conn() as conn:
        conn.executemany("""
            UPDATE strategy_signals SET label_return=?, is_win=?
            WHERE id=?
        """, updates)


def upsert_active_strategies(selections: list):
    """保存当期活跃策略选择结果"""
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO active_strategies
                (select_date, rule_id, rule_name, window_60_score,
                 window_120_score, final_score, rank, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, selections)


def get_current_active_strategies() -> list:
    """获取当前有效的活跃策略"""
    with _get_conn() as conn:
        today = datetime.now().strftime('%Y-%m-%d')
        return [dict(r) for r in conn.execute("""
            SELECT * FROM active_strategies
            WHERE valid_until >= ? AND is_emergency=0
            ORDER BY rank
        """, (today,)).fetchall()]
