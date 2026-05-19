"""
miner.py —— 策略挖掘引擎

流程: IC 过滤 → 模板穷举 → 回测验证 → 规则评分 → 保存前 N 条
"""
import json
import time
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from core.db import get_conn
from config.settings import MINING, BACKTEST
from strategy.factor_lib import FACTOR_REGISTRY, compute_all_factors
from strategy.rules_store import save_rule
from qlib_engine.strategy_adapter import BacktestConfig, backtest_single_rule

logger = logging.getLogger(__name__)


def _load_factor_matrix(
    stock_data: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """对所有股票计算因子，返回 (N_stocks, N_factors) 矩阵。Index = code"""
    rows = []
    for code, df in stock_data.items():
        if df.empty or len(df) < 60:
            continue
        try:
            factor_df = compute_all_factors(df)
            latest = factor_df.iloc[-1].to_dict()
            latest["code"] = code
            rows.append(latest)
        except Exception:
            continue
    result = pd.DataFrame(rows).set_index("code")
    return result.dropna(axis=1, how="all")


def _compute_ic(
    factor_matrix: pd.DataFrame,
    forward_returns: pd.Series,
) -> pd.Series:
    """计算每个因子的 IC_RANK（Spearman 相关性）"""
    ic_values = {}
    for col in factor_matrix.columns:
        common = factor_matrix[col].dropna().index.intersection(forward_returns.dropna().index)
        if len(common) < 30:
            ic_values[col] = 0.0
            continue
        ic = factor_matrix.loc[common, col].rank().corr(forward_returns.loc[common].rank())
        ic_values[col] = ic if not np.isnan(ic) else 0.0
    return pd.Series(ic_values)


def _enumerate_rules(factor_names: List[str], thresholds: List[float]) -> List[dict]:
    """模板穷举生成候选规则。T1: 单因子阈值, T2: 双因子组合"""
    rules = []
    for f in factor_names:
        for t in thresholds:
            conditions = {f: {">": round(t, 2)}}
            rules.append({
                "rule_name": f"T1_{f}_gt_{t:.2f}",
                "rule_type": "T1",
                "encoding": json.dumps(conditions, ensure_ascii=False),
                "conditions": json.dumps(conditions, ensure_ascii=False),
            })

    for i, f1 in enumerate(factor_names):
        for f2 in factor_names[i + 1:]:
            for t in thresholds:
                conditions = {f1: {">": round(t, 2)}, f2: {">": round(t, 2)}}
                rules.append({
                    "rule_name": f"T2_{f1}_and_{f2}_gt_{t:.2f}",
                    "rule_type": "T2",
                    "encoding": json.dumps(conditions, ensure_ascii=False),
                    "conditions": json.dumps(conditions, ensure_ascii=False),
                })
    return rules


def _generate_signals_for_rule(
    rule: dict,
    factor_matrix: pd.DataFrame,
    trade_date: str,
) -> List[dict]:
    """根据规则条件从因子矩阵生成 Qlib 格式的信号列表"""
    try:
        conditions = json.loads(rule["conditions"])
    except json.JSONDecodeError:
        return []

    signals = []
    for code in factor_matrix.index:
        match = True
        for fname, op_dict in conditions.items():
            if fname not in factor_matrix.columns:
                match = False
                break
            val = factor_matrix.loc[code, fname]
            if pd.isna(val):
                match = False
                break
            for op, threshold in op_dict.items():
                if op == ">" and not (val > threshold):
                    match = False
                if op == "<" and not (val < threshold):
                    match = False
                if not match:
                    break
            if not match:
                break

        if match:
            signals.append({
                "trade_date": trade_date,
                "code": code,
                "score": 1.0,
            })
    return signals


def _score_rule(perf: dict) -> float:
    """综合评分：0.3*收益 + 0.3*胜率 + 0.25*夏普 - 0.15*|回撤|"""
    return (
        0.3 * (perf.get("annual_return", 0) or 0) / 100.0
        + 0.3 * (perf.get("win_rate", 0) or 0) / 100.0
        + 0.25 * (perf.get("sharpe_ratio", 0) or 0)
        - 0.15 * abs(perf.get("max_drawdown", 0) or 0) / 100.0
    )


def mine_strategies(
    trade_date: str,
    end_date: Optional[str] = None,
    progress_callback: Optional[callable] = None,
) -> List[dict]:
    """执行策略挖掘"""
    if end_date is None:
        end_date = trade_date

    t0 = time.time()

    if progress_callback:
        progress_callback("loading", 0, 100)

    stock_data = _load_stock_data_for_mining(trade_date, MINING["sample_stocks"])

    if progress_callback:
        progress_callback("ic_filter", 0, 100)

    factor_matrix = _load_factor_matrix(stock_data)
    forward_returns = _compute_forward_returns(stock_data, trade_date, MINING["ic_forward_days"])
    ic_series = _compute_ic(factor_matrix, forward_returns)

    significant_factors = [
        f for f in factor_matrix.columns
        if f in ic_series.index and abs(ic_series[f]) >= MINING["ic_min_threshold"]
    ]
    logger.info(f"IC filter: {len(factor_matrix.columns)} → {len(significant_factors)} factors")

    if len(significant_factors) < 1:
        logger.warning("No significant factors found")
        return []

    if progress_callback:
        progress_callback("enumerate", 0, 100)

    candidates = _enumerate_rules(significant_factors, MINING["candidate_thresholds"])
    logger.info(f"Generated {len(candidates)} candidate rules")

    config = BacktestConfig(
        start_time=(datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d"),
        end_time=end_date,
        account=BACKTEST["account"],
        benchmark=BACKTEST["benchmark"],
        deal_price=BACKTEST["deal_price"],
        open_cost=BACKTEST["open_cost"],
        close_cost=BACKTEST["close_cost"],
        min_cost=BACKTEST["min_cost"],
        topk=BACKTEST["topk"],
        n_drop=BACKTEST["n_drop"],
    )

    results = []
    total = len(candidates)
    for i, rule in enumerate(candidates):
        signals = _generate_signals_for_rule(rule, factor_matrix, trade_date)
        if len(signals) < MINING["min_trades"]:
            continue

        perf = backtest_single_rule(rule["rule_name"], signals, config)
        if perf.get("error"):
            continue

        fitness = _score_rule(perf)
        rule["fitness"] = round(fitness, 4)
        rule["annual_return"] = perf.get("annual_return", 0)
        rule["win_rate"] = perf.get("win_rate", 0)
        rule["sharpe_ratio"] = perf.get("sharpe_ratio", 0)
        rule["max_drawdown"] = perf.get("max_drawdown", 0)
        rule["total_trades"] = perf.get("total_trades", 0)
        rule["source"] = "template"
        rule["generation"] = 1
        results.append(rule)

        if progress_callback and i % 10 == 0:
            progress_callback("backtest", i + 1, total)

    results.sort(key=lambda r: r["fitness"], reverse=True)
    top_n = results[:MINING["top_n_rules"]]

    saved = []
    for rule in top_n:
        rule_id = save_rule(rule)
        rule["id"] = rule_id
        saved.append(rule)

    elapsed = time.time() - t0
    logger.info(f"Mining complete: {len(saved)} rules saved in {elapsed:.1f}s")
    return saved


def _load_stock_data_for_mining(trade_date: str, sample_size: int) -> Dict[str, pd.DataFrame]:
    """加载采样股票的历史 OHLCV 数据"""
    with get_conn() as conn:
        codes = conn.execute("""
            SELECT DISTINCT dp.code FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (trade_date, sample_size)).fetchall()

    data = {}
    for (code,) in codes:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount, turnover
            FROM daily_price WHERE code = ? AND trade_date <= ?
            ORDER BY trade_date
        """, (code, trade_date)).fetchall()
        if len(rows) < 60:
            continue
        df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
        data[code] = df
    return data


def _compute_forward_returns(
    stock_data: Dict[str, pd.DataFrame],
    base_date: str,
    forward_days: int,
) -> pd.Series:
    """计算前瞻收益 = (N日后收盘价 / 当日收盘价 - 1)"""
    returns = {}
    for code, df in stock_data.items():
        if base_date not in df.index:
            continue
        try:
            idx = df.index.get_loc(base_date)
        except KeyError:
            continue
        future_idx = idx + forward_days
        if future_idx >= len(df):
            continue
        base_close = df.iloc[idx]["close"]
        future_close = df.iloc[future_idx]["close"]
        if base_close > 0:
            returns[code] = future_close / base_close - 1.0
    return pd.Series(returns)
