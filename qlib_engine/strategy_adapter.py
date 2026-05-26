"""
qlib_engine/strategy_adapter.py —— 规则 → Qlib 回测适配层

职责：
  1. 将 AIQuant 规则信号转为 Qlib Strategy
  2. 单条/批量规则回测
  3. 返回 AIQuant 兼容的绩效 dict
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional
from dataclasses import dataclass


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

    Computes metrics directly from signals (no longer depends on Qlib —
    Qlib cn_data ends at 2020, incompatible with 2024+ dates).

    Returns dict with annual_return, sharpe_ratio, max_drawdown, win_rate, total_trades, etc.
    """
    if not signals:
        return {
            "rule_name": rule_name,
            "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0,
            "win_rate": 0, "total_trades": 0, "total_return": 0,
            "error": "No signals",
        }

    # build trades from signals
    by_code: Dict[str, List[dict]] = {}
    for s in signals:
        code = s["code"]
        if code not in by_code:
            by_code[code] = []
        by_code[code].append(s)

    from core.db import get_conn

    trade_returns = []
    with get_conn() as conn:
        for code, sigs in by_code.items():
            sigs.sort(key=lambda s: s["trade_date"])
            entry_date = sigs[0]["trade_date"]
            exit_date = sigs[-1]["trade_date"]
            # single signal → hold until end of backtest period
            if exit_date == entry_date:
                exit_date = config.end_time

            entry_price = 0.0
            exit_price = 0.0
            price_row = conn.execute(
                "SELECT close FROM daily_price WHERE code = ? AND trade_date = ?",
                (code, entry_date),
            ).fetchone()
            if price_row:
                entry_price = float(price_row["close"])

            price_row = conn.execute(
                "SELECT close FROM daily_price WHERE code = ? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                (code, exit_date),
            ).fetchone()
            if price_row:
                exit_price = float(price_row["close"])

            if entry_price > 0 and exit_price > 0:
                pnl = (exit_price / entry_price - 1) * 100
                trade_returns.append(pnl)

    if not trade_returns:
        return {
            "rule_name": rule_name,
            "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0,
            "win_rate": 0, "total_trades": 0, "total_return": 0,
        }

    r_arr = np.array(trade_returns) / 100.0

    total_trades = len(trade_returns)
    win_trades = int((r_arr > 0).sum())
    win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

    cum_return = float(np.prod(1 + r_arr) - 1) * 100

    try:
        from datetime import datetime as dt
        start_dt = dt.strptime(config.start_time, "%Y-%m-%d")
        end_dt = dt.strptime(config.end_time, "%Y-%m-%d")
        years = max((end_dt - start_dt).days / 365.25, 0.1)
        ann_return = float(((1 + cum_return / 100) ** (1 / years) - 1) * 100)
    except Exception:
        ann_return = cum_return

    mean_r = float(np.mean(r_arr))
    std_r = float(np.std(r_arr, ddof=1))
    sharpe = float(mean_r / std_r * np.sqrt(total_trades / years)) if std_r > 0 and years > 0 else 0

    cumulative = np.cumprod(1 + r_arr)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = (cumulative / running_max - 1) * 100
    max_dd = float(np.min(drawdowns))

    calmar = round(ann_return / max(abs(max_dd), 1e-9), 2)

    return {
        "rule_name": rule_name,
        "annual_return": round(ann_return, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "win_rate": round(win_rate, 2),
        "total_trades": total_trades,
        "calmar_ratio": calmar,
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
