"""
engine.py —— 统一回测引擎

封装 Qlib SimulatorExecutor，提取汇总指标 + 逐笔交易明细
"""
import json
import io
import sys
import logging
import pandas as pd
from typing import Dict, List, Optional
from datetime import datetime

from qlib_engine import init_qlib
from qlib.log import set_global_logger_level
from qlib_engine.strategy_adapter import BacktestConfig
from config.settings import BACKTEST

logger = logging.getLogger(__name__)


def run_backtest(
    rule_name: str,
    rule_id: str,
    conditions_json: str,
    start_date: str,
    end_date: str,
    save: bool = True,
) -> dict:
    """执行回测并提取汇总指标和逐笔交易"""
    init_qlib()
    set_global_logger_level(logging.ERROR)

    signals = _generate_backtest_signals(conditions_json, start_date, end_date)
    if not signals:
        return {"summary": {}, "trades": [], "error": "No signals generated"}

    config = BacktestConfig(
        start_time=start_date,
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

    summary = _run_qlib_backtest(signals, config)
    trades = _build_trade_details(signals)

    result_id = None
    if save and summary:
        from backtest.trade_store import save_result, save_trades
        result_id = save_result(rule_id, rule_name, start_date, end_date, summary)
        if trades:
            save_trades(result_id, trades)

    return {
        "result_id": result_id,
        "summary": summary,
        "trades": trades,
    }


def _generate_backtest_signals(
    conditions_json: str,
    start_date: str,
    end_date: str,
) -> List[dict]:
    """根据规则条件在整个回测区间生成每日信号"""
    try:
        conditions = json.loads(conditions_json)
    except json.JSONDecodeError:
        return []

    if not conditions:
        return []

    from core.db import get_conn
    from strategy.factor_lib import compute_all_factors

    with get_conn() as conn:
        dates = conn.execute("""
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, (start_date, end_date)).fetchall()

        signals = []
        for (trade_date,) in dates:
            trade_date = trade_date if isinstance(trade_date, str) else str(trade_date)

            codes = conn.execute("""
                SELECT DISTINCT code FROM daily_price WHERE trade_date = ?
            """, (trade_date,)).fetchall()

            for (code,) in codes:
                history = conn.execute("""
                    SELECT trade_date, open, high, low, close, volume, amount, turnover
                    FROM daily_price WHERE code = ? AND trade_date <= ?
                    ORDER BY trade_date
                """, (code, trade_date)).fetchall()

                if len(history) < 60:
                    continue

                df = pd.DataFrame([dict(r) for r in history]).set_index("trade_date")
                try:
                    factor_df = compute_all_factors(df)
                    factor_row = factor_df.iloc[-1].to_dict()
                except Exception:
                    continue

                if _evaluate_condition(factor_row, conditions):
                    signals.append({
                        "trade_date": trade_date,
                        "code": code,
                        "score": 1.0,
                    })

        return signals


def _evaluate_condition(factor_values: dict, conditions: dict) -> bool:
    """评估单只股票是否满足条件"""
    for fname, op_dict in conditions.items():
        value = factor_values.get(fname)
        if value is None or (isinstance(value, float) and pd.isna(value)):
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


def _run_qlib_backtest(signals: List[dict], config: BacktestConfig) -> dict:
    """执行 Qlib 回测，返回汇总指标"""
    from qlib_engine.strategy_adapter import _signals_to_prediction_df
    from qlib.utils import init_instance_by_config

    if not signals:
        return {}

    pred_df = _signals_to_prediction_df(signals, [], [])
    if pred_df.empty:
        return {}

    bt_config = {
        "strategy": {
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy",
            "kwargs": {"signal": pred_df, "topk": config.topk, "n_drop": config.n_drop},
        },
        "executor": {
            "class": "SimulatorExecutor",
            "module_path": "qlib.backtest.executor",
            "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True},
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

    _stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        strategy = init_instance_by_config(bt_config["strategy"])
        executor = init_instance_by_config(bt_config["executor"])
        portfolio_metrics, indicator = executor.backtest(strategy=strategy, **bt_config["backtest"])
    finally:
        sys.stderr = _stderr

    report = indicator.get_latest_report() if hasattr(indicator, "get_latest_report") else {}

    return {
        "annual_return": round(float(report.get("excess_return_with_cost.annualized_return", 0) or 0) * 100, 2),
        "cumulative_return": round(float(report.get("excess_return_without_cost.cumulative_return", 0) or 0) * 100, 2),
        "win_rate": round(float(report.get("excess_return_without_cost.win_rate", 0) or 0) * 100, 2),
        "sharpe_ratio": round(float(report.get("excess_return_with_cost.information_ratio", 0) or 0), 2),
        "max_drawdown": round(float(report.get("excess_return_with_cost.max_drawdown", 0) or 0) * 100, 2),
        "total_trades": int(report.get("total_trades", 0) or 0),
        "win_trades": 0,
    }


def _build_trade_details(signals: List[dict]) -> List[dict]:
    """从信号列表重建逐笔交易明细：按股票分组，最早信号为买入，最后信号为卖出"""
    from core.db import get_conn

    by_code: Dict[str, List[dict]] = {}
    for s in signals:
        code = s["code"]
        if code not in by_code:
            by_code[code] = []
        by_code[code].append(s)

    trades = []
    with get_conn() as conn:
        for code, sigs in by_code.items():
            sigs.sort(key=lambda s: s["trade_date"])
            entry_date = sigs[0]["trade_date"]
            exit_date = sigs[-1]["trade_date"]

            name_row = conn.execute("SELECT name FROM stock_info WHERE code = ?", (code,)).fetchone()
            name = name_row["name"] if name_row else ""

            entry_price = 0.0
            exit_price = 0.0
            price_row = conn.execute(
                "SELECT close FROM daily_price WHERE code = ? AND trade_date = ?",
                (code, entry_date)
            ).fetchone()
            if price_row:
                entry_price = float(price_row["close"])

            price_row = conn.execute(
                "SELECT close FROM daily_price WHERE code = ? AND trade_date = ?",
                (code, exit_date)
            ).fetchone()
            if price_row:
                exit_price = float(price_row["close"])

            holding_days = 0
            pnl_pct = 0.0
            if entry_price > 0:
                pnl_pct = round((exit_price / entry_price - 1) * 100, 2) if exit_price > 0 else 0.0
                try:
                    holding_days = (datetime.strptime(exit_date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
                except Exception:
                    pass

            exit_reason = "expire"
            if pnl_pct <= -8:
                exit_reason = "stop_loss"
            elif pnl_pct >= 20:
                exit_reason = "take_profit"

            trades.append({
                "code": code,
                "name": name,
                "entry_date": entry_date,
                "entry_price": round(entry_price, 2),
                "exit_date": exit_date if exit_date != entry_date else "",
                "exit_price": round(exit_price, 2),
                "holding_days": holding_days,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason,
            })

    trades.sort(key=lambda t: t["entry_date"])
    return trades
