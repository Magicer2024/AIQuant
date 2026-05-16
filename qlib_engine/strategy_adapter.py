"""
qlib_engine/strategy_adapter.py —— 规则 → Qlib 回测适配层

职责：
  1. 将 AIQuant 规则信号转为 Qlib Strategy
  2. 单条/批量规则回测
  3. 返回 AIQuant 兼容的绩效 dict
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass

from qlib_engine import init_qlib


@dataclass
class BacktestConfig:
    """Qlib backtest configuration."""
    start_time: str
    end_time: str
    account: float = 1_000_000
    benchmark: str = "SH000300"
    deal_price: str = "close"
    open_cost: float = 0.0005
    close_cost: float = 0.0015
    min_cost: float = 5.0
    limit_threshold: float = 0.095
    topk: int = 30
    n_drop: int = 5


def _signals_to_prediction_df(
    signals: List[dict],
    calendar: List[str],
    instruments: List[str],
) -> pd.Series:
    """
    Convert AIQuant signal list to Qlib-compatible prediction Series.

    Returns pd.Series with MultiIndex (datetime, instrument), values = prediction score
    """
    records = []
    for s in signals:
        score = s.get("score", s.get("confidence", 0))
        records.append({
            "datetime": pd.Timestamp(s["trade_date"]),
            "instrument": s["code"],
            "score": float(score),
        })

    if not records:
        return pd.Series([], dtype=float)

    df = pd.DataFrame(records)
    idx = pd.MultiIndex.from_arrays(
        [df["datetime"], df["instrument"]],
        names=["datetime", "instrument"],
    )
    return pd.Series(df["score"].values, index=idx)


def backtest_single_rule(
    rule_name: str,
    signals: List[dict],
    config: BacktestConfig,
) -> dict:
    """
    Backtest a single rule's signals and return performance metrics.

    Returns dict with annual_return, sharpe_ratio, max_drawdown, win_rate, total_trades, etc.
    """
    init_qlib()

    pred_df = _signals_to_prediction_df(signals, [], [])

    if pred_df.empty:
        return {
            "rule_name": rule_name,
            "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0,
            "win_rate": 0, "total_trades": 0, "error": "No signals",
        }

    # Build Qlib backtest config
    bt_config = {
        "strategy": {
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy",
            "kwargs": {
                "signal": pred_df,
                "topk": config.topk,
                "n_drop": config.n_drop,
            },
        },
        "executor": {
            "class": "SimulatorExecutor",
            "module_path": "qlib.backtest.executor",
            "kwargs": {
                "time_per_step": "day",
                "generate_portfolio_metrics": True,
            },
        },
        "backtest": {
            "start_time": config.start_time,
            "end_time": config.end_time,
            "account": config.account,
            "benchmark": config.benchmark,
            "exchange_kwargs": {
                "limit_threshold": config.limit_threshold,
                "deal_price": config.deal_price,
                "open_cost": config.open_cost,
                "close_cost": config.close_cost,
                "min_cost": config.min_cost,
            },
        },
    }

    try:
        from qlib.utils import init_instance_by_config

        strategy = init_instance_by_config(bt_config["strategy"])
        executor = init_instance_by_config(bt_config["executor"])

        portfolio_metrics, indicator = executor.backtest(
            strategy=strategy,
            **bt_config["backtest"],
        )

        report = indicator.get_latest_report() if hasattr(indicator, "get_latest_report") else {}

        return {
            "rule_name": rule_name,
            "annual_return": round(float(report.get("excess_return_with_cost.annualized_return", 0) or 0) * 100, 2),
            "sharpe_ratio": round(float(report.get("excess_return_with_cost.information_ratio", 0) or 0), 2),
            "max_drawdown": round(float(report.get("excess_return_with_cost.max_drawdown", 0) or 0) * 100, 2),
            "win_rate": round(float(report.get("excess_return_without_cost.win_rate", 0) or 0) * 100, 2),
            "total_trades": int(report.get("total_trades", 0) or 0),
            "calmar_ratio": round(
                float(report.get("excess_return_with_cost.annualized_return", 0) or 0)
                / max(abs(float(report.get("excess_return_with_cost.max_drawdown", 0) or 0)), 1e-9),
                2,
            ),
        }
    except Exception as e:
        return {
            "rule_name": rule_name,
            "error": str(e),
            "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0,
            "win_rate": 0, "total_trades": 0,
        }


def backtest_rules(
    rules_and_signals: Dict[str, List[dict]],
    config: BacktestConfig,
) -> List[dict]:
    """
    Batch backtest multiple rules.

    Returns list of performance dicts, one per rule.
    """
    results = []
    for rule_name, signals in rules_and_signals.items():
        result = backtest_single_rule(rule_name, signals, config)
        results.append(result)
    return results
