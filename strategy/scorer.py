"""
scorer.py —— 每日打分引擎

加载活跃策略规则 → 计算因子 → 评估条件 → 逐股打分 → 写入 stock_score 表
"""
import json
import math
import pandas as pd
from datetime import date
from typing import Dict, List, Optional
from functools import lru_cache
from core.db import get_conn
from strategy.rules_store import get_active_rules
from strategy.factor_lib import compute_all_factors
from utils.cache import ttl_cache


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


def _load_stock_data_streaming(trade_date, batch_size=100):
    """流式加载股票数据，逐股 yield (code, DataFrame)，减少内存峰值"""
    import pandas as pd
    with get_conn() as conn:
        cursor = conn.execute("""
            SELECT dp.code, dp.trade_date, dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover
            FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY dp.code, dp.trade_date
        """, (trade_date,))

        batch = []
        current_code = None

        for row in cursor:
            if row["code"] != current_code:
                if batch:
                    yield current_code, pd.DataFrame(batch).set_index("trade_date")
                current_code = row["code"]
                batch = []
            batch.append(dict(row))

        if batch:
            yield current_code, pd.DataFrame(batch).set_index("trade_date")


def _evaluate_condition(factor_values: Dict[str, float], condition):
    """评估单条规则条件是否满足，同时计算信号强度。

    condition 为 list 格式：[{factor, operator, threshold}, ...]

    Returns:
        (passed: bool, strength: float) — passed 表示是否满足所有条件，
        strength 为 0~1 的连续值，表示因子值超出阈值的程度。
    """
    if isinstance(condition, list):
        strengths = []
        for item in condition:
            factor_name = item.get("factor")
            op = item.get("operator")
            threshold = item.get("threshold")
            if factor_name is None or op is None or threshold is None:
                return False, 0.0
            value = factor_values.get(factor_name)
            if value is None:
                return False, 0.0

            if op in (">", ">="):
                if not (value > threshold if op == ">" else value >= threshold):
                    return False, 0.0
                denom = max(1.0 - threshold, 0.05)
                strengths.append((value - threshold) / denom)
            elif op in ("<", "<="):
                if not (value < threshold if op == "<" else value <= threshold):
                    return False, 0.0
                denom = max(threshold, 0.05)
                strengths.append((threshold - value) / denom)
            else:
                return False, 0.0
        avg_strength = sum(strengths) / len(strengths) if strengths else 0.0
        return True, avg_strength

    # legacy dict format: {factor_name: {operator: threshold}}
    strengths = []
    for factor_name, op_dict in condition.items():
        value = factor_values.get(factor_name)
        if value is None:
            return False, 0.0
        for op, threshold in op_dict.items():
            if op in (">", ">="):
                if not (value > threshold if op == ">" else value >= threshold):
                    return False, 0.0
                denom = max(1.0 - threshold, 0.05)
                strengths.append((value - threshold) / denom)
            elif op in ("<", "<="):
                if not (value < threshold if op == "<" else value <= threshold):
                    return False, 0.0
                denom = max(threshold, 0.05)
                strengths.append((threshold - value) / denom)
            else:
                return False, 0.0
    avg_strength = sum(strengths) / len(strengths) if strengths else 0.0
    return True, avg_strength


def _rule_quality(rule: dict) -> float:
    """计算规则质量分（0~1），综合胜率、夏普比率、适应度。"""
    win_rate = rule.get("win_rate", 0) or 0
    sharpe = rule.get("sharpe_ratio", 0) or 0
    fitness = rule.get("fitness", 0) or 0
    return (
        0.5 * (win_rate / 100.0)
        + 0.3 * min(sharpe / 5.0, 1.0)
        + 0.2 * min(fitness / 200.0, 1.0)
    )


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
        best_strength = 0.0

        for rule in rules:
            try:
                conditions = json.loads(rule["conditions"] or "{}")
            except json.JSONDecodeError:
                conditions = {}

            if not conditions:
                continue

            passed, strength = _evaluate_condition(factor_row, conditions)
            if passed:
                quality = _rule_quality(rule)
                score = quality * 100.0 / (1.0 + math.exp(-3.0 * (strength - 0.5)))
                if score > best_score:
                    best_score = score
                    best_rule = rule
                    best_strength = strength

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


def get_daily_scores(trade_date: str, page: int = 1, per_page: int = 50) -> List[dict]:
    """获取某日打分排名（分页），附带板块(market)和市值(market_cap)"""
    per_page = min(per_page, 10000)
    offset = (page - 1) * per_page
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.*, i.market,
                   CAST(COALESCE(dp.close * i.total_shares / 1e8, 0) AS REAL) AS market_cap
            FROM stock_score s
            LEFT JOIN stock_info i ON s.code = i.code
            LEFT JOIN daily_price dp ON s.code = dp.code AND dp.trade_date = s.trade_date
            WHERE s.trade_date = ?
            ORDER BY s.score DESC
            LIMIT ? OFFSET ?
        """, (trade_date, per_page, offset)).fetchall()
        return [dict(r) for r in rows]


def get_daily_scores_count(trade_date: str) -> int:
    """获取某日打分总数"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_score WHERE trade_date = ?",
            (trade_date,)
        ).fetchone()
        return row["cnt"]


def get_latest_score_date() -> Optional[str]:
    """获取最近一次打分的日期"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) as d FROM stock_score"
        ).fetchone()
        return row["d"] if row else None


@lru_cache(maxsize=1)
def get_latest_score_date_cached():
    """缓存最新打分日期（下次调用需手动 cache_clear）"""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM stock_score").fetchone()
        return row[0]


@ttl_cache(ttl_seconds=300)
def get_active_stocks_cached():
    """缓存活跃股票列表 5 分钟"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM stock_info WHERE is_active = 1"
        ).fetchall()
        return [dict(r) for r in rows]
