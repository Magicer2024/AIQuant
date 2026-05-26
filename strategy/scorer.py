"""
scorer.py —— 每日打分引擎

加载活跃策略规则 → 计算因子 → 评估条件 → 逐股打分 → 写入 stock_score 表
"""
import json
import pandas as pd
from datetime import date
from typing import Dict, List, Optional
from core.db import get_conn
from strategy.rules_store import get_active_rules
from strategy.factor_lib import compute_all_factors


def _load_stock_data(trade_date: str) -> Dict[str, pd.DataFrame]:
    """从 daily_price 加载指定日期的所有股票 OHLCV 数据，返回 {code: DataFrame}"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT dp.code, dp.trade_date, dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover
            FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY dp.trade_date
        """, (trade_date,)).fetchall()

    data: Dict[str, pd.DataFrame] = {}
    for r in rows:
        d = dict(r)
        code = d.pop("code")
        trade_dt = d.pop("trade_date")
        if code not in data:
            data[code] = []
        data[code].append({"trade_date": trade_dt, **d})

    result = {}
    for code, records in data.items():
        df = pd.DataFrame(records).set_index("trade_date")
        df = df.sort_index().tail(120)
        result[code] = df
    return result


def _evaluate_condition(factor_values: Dict[str, float], condition) -> bool:
    """评估单条规则条件是否满足。condition 为 list 格式：[{factor, operator, threshold}, ...]"""
    if isinstance(condition, list):
        for item in condition:
            factor_name = item.get("factor")
            op = item.get("operator")
            threshold = item.get("threshold")
            if factor_name is None or op is None or threshold is None:
                return False
            value = factor_values.get(factor_name)
            if value is None:
                return False
            if op == ">" and not (value > threshold):
                return False
            if op == "<" and not (value < threshold):
                return False
            if op == ">=" and not (value >= threshold):
                return False
            if op == "<=" and not (value <= threshold):
                return False
        return True
    # legacy dict format: {factor_name: {operator: threshold}}
    for factor_name, op_dict in condition.items():
        value = factor_values.get(factor_name)
        if value is None:
            return False
        for op, threshold in op_dict.items():
            if op == ">" and not (value > threshold):
                return False
            if op == "<" and not (value < threshold):
                return False
            if op == ">=" and not (value >= threshold):
                return False
            if op == "<=" and not (value <= threshold):
                return False
    return True


def score_stocks(trade_date: str, save: bool = True) -> List[dict]:
    """
    对指定交易日所有股票打分。

    Args:
        trade_date: 交易日 'YYYY-MM-DD'
        save: 是否写入 stock_score 表

    Returns:
        [{code, name, score, rule_id, rule_name, factors_json}], 按 score 降序
    """
    rules = get_active_rules()
    if not rules:
        print(f"[scorer] {trade_date}: no active rules")
        return []

    stock_data = _load_stock_data(trade_date)
    print(f"[scorer] {trade_date}: loaded {len(stock_data)} stocks")

    results = []
    for code, df in stock_data.items():
        if df.empty or len(df) < 20:
            continue

        # 使用 factor_lib 的 compute_all_factors 批量计算所有因子
        try:
            factor_df = compute_all_factors(df)
            if factor_df.empty:
                continue
            factor_row = factor_df.iloc[-1].dropna().to_dict()
        except Exception:
            continue

        if not factor_row:
            continue

        # 逐条规则评估
        best_score = 0.0
        best_rule = None

        for rule in rules:
            try:
                conditions = json.loads(rule["conditions"] or "{}")
            except json.JSONDecodeError:
                conditions = {}

            if not conditions:
                continue

            if _evaluate_condition(factor_row, conditions):
                score = rule.get("win_rate", 0) or 0
                if score > best_score:
                    best_score = score
                    best_rule = rule

        if best_rule and best_score > 0:
            with get_conn() as conn:
                name_row = conn.execute(
                    "SELECT name FROM stock_info WHERE code = ?", (code,)
                ).fetchone()
                stock_name = name_row["name"] if name_row else ""

            results.append({
                "code": code,
                "name": stock_name,
                "score": round(best_score, 1),
                "rule_id": str(best_rule["id"]),
                "rule_name": best_rule["rule_name"],
                "factors_json": json.dumps(factor_row, ensure_ascii=False),
            })

    results.sort(key=lambda x: x["score"], reverse=True)

    if save and results:
        _save_scores(trade_date, results)

    return results


def _save_scores(trade_date: str, results: List[dict]):
    """批量写入 stock_score 表"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO stock_score
                (trade_date, code, name, score, rule_id, rule_name, factors_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (trade_date, r["code"], r["name"], r["score"],
             r["rule_id"], r["rule_name"], r["factors_json"])
            for r in results
        ])


def get_daily_scores(trade_date: str) -> List[dict]:
    """获取某日打分排名（从 stock_score 表读取）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM stock_score
            WHERE trade_date = ?
            ORDER BY score DESC
        """, (trade_date,)).fetchall()
        return [dict(r) for r in rows]


def get_latest_score_date() -> Optional[str]:
    """获取最近一次打分的日期"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) as d FROM stock_score"
        ).fetchone()
        return row["d"] if row else None
