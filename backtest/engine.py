"""
engine.py —— 统一回测引擎

直接从信号计算回测指标（Qlib cn_data 只到 2020 年，不兼容 2024+ 日期）
"""
import json
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from qlib_engine import init_qlib
from qlib.log import set_global_logger_level
from qlib_engine.strategy_adapter import BacktestConfig
from config.settings import BACKTEST

logger = logging.getLogger(__name__)

# 默认退出阈值
DEFAULT_STOP_LOSS = -0.08
DEFAULT_TAKE_PROFIT = 0.20


def run_backtest(
    rule_name: str,
    rule_id: str,
    conditions_json: str,
    start_date: str,
    end_date: str,
    sell_conditions_json: str = None,
    holding_max: int = 20,
    save: bool = True,
    progress_callback=None,
) -> dict:
    """执行回测并提取汇总指标和逐笔交易"""
    init_qlib()
    set_global_logger_level(logging.ERROR)

    signals = _generate_backtest_signals(
        conditions_json, start_date, end_date,
        sell_conditions_json=sell_conditions_json,
        holding_max=holding_max,
        progress_callback=progress_callback,
    )
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
    trades = _build_trade_details(signals, end_date)

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


def _load_stock_history(end_date: str, lookback_days: int = 180) -> Dict[str, pd.DataFrame]:
    """加载所有股票的 OHLCV 历史，按 code 分组返回 DataFrame"""
    from datetime import datetime as dt, timedelta
    try:
        ext_start = (dt.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    except Exception:
        ext_start = end_date

    from core.db import get_conn
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT code, trade_date, open, high, low, close, volume, amount, turnover
            FROM daily_price
            WHERE trade_date >= ? AND trade_date <= ?
            ORDER BY code, trade_date
        """, (ext_start, end_date)).fetchall()

    from collections import defaultdict
    stock_data: dict = defaultdict(list)
    for r in rows:
        td = r["trade_date"] if isinstance(r["trade_date"], str) else str(r["trade_date"])
        stock_data[r["code"]].append({
            "trade_date": td,
            "open": float(r["open"] or 0), "high": float(r["high"] or 0),
            "low": float(r["low"] or 0), "close": float(r["close"] or 0),
            "volume": float(r["volume"] or 0), "amount": float(r["amount"] or 0),
            "turnover": float(r["turnover"] or 0),
        })

    result = {}
    for code, records in stock_data.items():
        df = pd.DataFrame(records).set_index("trade_date")
        if len(df) >= 20:
            result[code] = df
    return result


def _evaluate_row_condition(factor_values: dict, condition_item: dict) -> bool:
    """评估单条条件（行级，支持简单比较运算符）"""
    fname = condition_item.get("factor")
    op = condition_item.get("operator")
    threshold = condition_item.get("threshold")
    if fname is None or op is None:
        return False

    value = factor_values.get(fname)
    if value is None or (isinstance(value, float) and pd.isna(value)):
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


def _evaluate_conditions(factor_row: dict, conditions) -> bool:
    """评估全部条件（行级），conditions 为 list 或 dict"""
    if isinstance(conditions, list):
        for item in conditions:
            if item.get("operator") in ("cross_above_factor", "cross_below_factor"):
                continue  # 交叉类条件不能在行级评估，跳过
            if not _evaluate_row_condition(factor_row, item):
                return False
        return True
    # legacy dict format
    for fname, op_dict in conditions.items():
        for op, threshold in op_dict.items():
            if not _evaluate_row_condition(factor_row, {"factor": fname, "operator": op, "threshold": threshold}):
                return False
    return True


def _evaluate_cross_conditions(factor_df: pd.DataFrame, conditions) -> pd.Series:
    """在完整 factor DataFrame 上评估交叉类条件，返回 bool Series"""
    if isinstance(conditions, list):
        items = [c for c in conditions if c.get("operator") in ("cross_above_factor", "cross_below_factor")]
    else:
        items = []

    if not items:
        return pd.Series(True, index=factor_df.index)

    result = pd.Series(True, index=factor_df.index)
    for item in items:
        fname = item["factor"]
        op = item["operator"]
        ref = item.get("ref_factor")
        if fname not in factor_df.columns or (ref and ref not in factor_df.columns):
            return pd.Series(False, index=factor_df.index)

        vals = factor_df[fname]
        ref_vals = factor_df[ref] if ref else pd.Series(0, index=factor_df.index)

        if op == "cross_above_factor":
            cond = (vals > ref_vals) & (vals.shift(1) <= ref_vals.shift(1))
        elif op == "cross_below_factor":
            cond = (vals < ref_vals) & (vals.shift(1) >= ref_vals.shift(1))
        else:
            cond = pd.Series(True, index=factor_df.index)
        result = result & cond.fillna(False)
    return result


def _generate_backtest_signals(
    conditions_json: str,
    start_date: str,
    end_date: str,
    sell_conditions_json: str = None,
    holding_max: int = 20,
    progress_callback=None,
) -> List[dict]:
    """生成买卖信号配对。

    返回 [{trade_date, code, type: "buy"|"sell", price, exit_reason}]，
    按 trade_date, code 排序。
    """
    try:
        buy_conditions = json.loads(conditions_json) if isinstance(conditions_json, str) else conditions_json
    except (json.JSONDecodeError, TypeError):
        return []

    try:
        sell_conditions = (json.loads(sell_conditions_json)
                           if isinstance(sell_conditions_json, str) and sell_conditions_json
                           else [])
    except (json.JSONDecodeError, TypeError):
        sell_conditions = []

    if not buy_conditions:
        return []

    stock_data = _load_stock_history(end_date)
    if not stock_data:
        return []

    from strategy.factor_lib import compute_all_factors

    all_signals: list[dict] = []
    codes = list(stock_data.keys())
    total = len(codes)

    for idx, code in enumerate(codes):
        df = stock_data[code]
        if len(df) < 60:
            continue

        try:
            factor_df = compute_all_factors(df)
            if factor_df.empty:
                continue
        except Exception:
            continue

        # 查找回测区间内的日期
        backtest_dates = [d for d in factor_df.index if start_date <= d <= end_date]
        if not backtest_dates:
            continue

        # --- 买入信号 ---
        buy_dates: list[str] = []
        has_cross_buy = any(
            c.get("operator") in ("cross_above_factor", "cross_below_factor")
            for c in (buy_conditions if isinstance(buy_conditions, list) else [])
        )

        if has_cross_buy:
            cross_flags = _evaluate_cross_conditions(factor_df, buy_conditions)
            for d in backtest_dates:
                if d in cross_flags.index and cross_flags.loc[d]:
                    # 还要检查非交叉条件
                    row = factor_df.loc[d].to_dict()
                    if _evaluate_conditions(row, buy_conditions):
                        buy_dates.append(d)
        else:
            for d in backtest_dates:
                row = factor_df.loc[d].to_dict()
                if _evaluate_conditions(row, buy_conditions):
                    buy_dates.append(d)

        if not buy_dates:
            continue

        # --- 卖出信号日期（从 sell_conditions 预计算）---
        sell_signal_dates: set[str] = set()
        has_cross_sell = sell_conditions and any(
            c.get("operator") in ("cross_above_factor", "cross_below_factor")
            for c in sell_conditions
        )
        if has_cross_sell:
            cross_flags = _evaluate_cross_conditions(factor_df, sell_conditions)
            for d in backtest_dates:
                if d in cross_flags.index and cross_flags.loc[d]:
                    sell_signal_dates.add(d)

        # --- 配对买卖信号 ---
        price_col = df["close"]
        buy_idx = 0
        while buy_idx < len(buy_dates):
            entry_date = buy_dates[buy_idx]
            entry_price = float(price_col.loc[entry_date]) if entry_date in price_col.index else 0
            if entry_price <= 0:
                buy_idx += 1
                continue

            all_signals.append({
                "trade_date": entry_date,
                "code": code,
                "type": "buy",
                "price": entry_price,
            })

            # 寻找卖出日期
            exit_date = None
            exit_price = 0.0
            exit_reason = "period_end"

            # 获取 entry_date 之后的所有交易日
            future_dates = [d for d in backtest_dates if d > entry_date]
            holding_days = 0

            for d in future_dates:
                holding_days += 1
                close_px = float(price_col.loc[d]) if d in price_col.index else 0
                if close_px <= 0:
                    continue

                pnl_pct = (close_px / entry_price) - 1

                # 1. 检查固定止损
                if pnl_pct <= DEFAULT_STOP_LOSS:
                    exit_date = d
                    exit_price = close_px
                    exit_reason = "stop_loss"
                    break

                # 2. 检查固定止盈
                if pnl_pct >= DEFAULT_TAKE_PROFIT:
                    exit_date = d
                    exit_price = close_px
                    exit_reason = "take_profit"
                    break

                # 3. 检查超期
                if holding_days >= holding_max:
                    exit_date = d
                    exit_price = close_px
                    exit_reason = "max_hold"
                    break

                # 4. 检查 sell_conditions 信号（因子卖出条件 + 简单比较条件）
                if sell_conditions:
                    if d in sell_signal_dates:
                        exit_date = d
                        exit_price = close_px
                        exit_reason = "sell_signal"
                        break
                    # 也检查简单比较条件
                    if d in factor_df.index:
                        row = factor_df.loc[d].to_dict()
                        if _evaluate_conditions(row, sell_conditions):
                            exit_date = d
                            exit_price = close_px
                            exit_reason = "sell_signal"
                            break

            # 没找到任何退出信号 → 持有到 end_date
            if exit_date is None:
                exit_date = end_date
                if end_date in price_col.index:
                    exit_price = float(price_col.loc[end_date])
                else:
                    # 取 end_date 前最近的交易日
                    before = [d for d in price_col.index if d <= end_date]
                    exit_price = float(price_col.loc[before[-1]]) if before else entry_price

            all_signals.append({
                "trade_date": exit_date,
                "code": code,
                "type": "sell",
                "price": exit_price,
                "exit_reason": exit_reason,
            })

            # 跳过被此卖出覆盖的后续买入信号
            buy_idx += 1
            while buy_idx < len(buy_dates) and buy_dates[buy_idx] <= exit_date:
                buy_idx += 1

        if progress_callback:
            progress_callback(idx + 1, total)

    all_signals.sort(key=lambda s: (s["trade_date"], s["code"]))
    return all_signals


def _run_qlib_backtest(signals: List[dict], config: BacktestConfig) -> dict:
    """根据买卖信号配对计算回测汇总指标"""
    if not signals:
        return {}

    # 配对 buy/sell
    trades = _pair_signals_to_trades(signals)
    if not trades:
        return {"annual_return": 0, "cumulative_return": 0, "win_rate": 0,
                "sharpe_ratio": 0, "max_drawdown": 0, "total_trades": 0, "win_trades": 0}

    r_arr = np.array([t["pnl_pct"] for t in trades]) / 100.0
    holding_arr = np.array([t.get("holding_days", 1) for t in trades])

    win_trades = int((r_arr > 0).sum())
    total_trades = len(r_arr)
    win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

    # Annualized return: compound avg trade return by trade frequency
    # Avoid np.prod(1+r_arr) — explodes with overlapping trades
    avg_return = float(np.mean(r_arr))
    avg_holding = float(np.mean(holding_arr))
    if avg_return > -1 and avg_holding > 0:
        # Cap the exponent to prevent absurd annualized returns from very short holding periods
        exponent = min(365.0 / avg_holding, 20.0)
        ann_return = float(((1 + avg_return) ** exponent - 1) * 100)
    else:
        ann_return = 0.0

    # Cumulative return: log-sum to avoid overflow
    log_returns = np.log1p(np.clip(r_arr, -0.999, None))
    cum_return = float(np.expm1(np.sum(log_returns)) * 100)

    try:
        start_dt = datetime.strptime(config.start_time, "%Y-%m-%d")
        end_dt = datetime.strptime(config.end_time, "%Y-%m-%d")
        years = max((end_dt - start_dt).days / 365.25, 0.1)
    except Exception:
        years = 1.0

    mean_r = float(np.mean(r_arr))
    std_r = float(np.std(r_arr, ddof=1))
    sharpe = float(mean_r / std_r * np.sqrt(total_trades / years)) if std_r > 0 and years > 0 else 0

    # max drawdown from cumulative trade returns (log-space to avoid overflow)
    cumulative = np.exp(np.cumsum(log_returns))
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = (cumulative / running_max - 1) * 100
    max_dd = float(np.min(drawdowns))

    return {
        "annual_return": round(ann_return, 2),
        "cumulative_return": round(cum_return, 2),
        "win_rate": round(win_rate, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown": round(max_dd, 2),
        "total_trades": total_trades,
        "win_trades": win_trades,
    }


def _pair_signals_to_trades(signals: List[dict]) -> List[dict]:
    """将买卖信号配对为交易列表"""
    by_code: Dict[str, List[dict]] = {}
    for s in signals:
        if s["code"] not in by_code:
            by_code[s["code"]] = []
        by_code[s["code"]].append(s)

    trades = []
    for code, sigs in by_code.items():
        sigs.sort(key=lambda s: s["trade_date"])
        i = 0
        while i < len(sigs):
            if sigs[i]["type"] != "buy":
                i += 1
                continue
            buy = sigs[i]
            # 找下一个 sell
            sell = None
            for j in range(i + 1, len(sigs)):
                if sigs[j]["type"] == "sell":
                    sell = sigs[j]
                    i = j + 1
                    break
            if sell is None:
                break

            entry_price = buy.get("price", 0)
            exit_price = sell.get("price", 0)
            pnl_pct = round((exit_price / entry_price - 1) * 100, 2) if entry_price > 0 else 0
            try:
                holding_days = (datetime.strptime(sell["trade_date"], "%Y-%m-%d")
                                - datetime.strptime(buy["trade_date"], "%Y-%m-%d")).days
            except Exception:
                holding_days = 0

            trades.append({
                "code": code,
                "entry_date": buy["trade_date"],
                "entry_price": round(entry_price, 2),
                "exit_date": sell["trade_date"],
                "exit_price": round(exit_price, 2),
                "pnl_pct": pnl_pct,
                "holding_days": holding_days,
                "exit_reason": sell.get("exit_reason", "unknown"),
            })
    return trades


def _build_trade_details(signals: List[dict], end_date: str = None) -> List[dict]:
    """从买卖信号构建逐笔交易明细"""
    trades = _pair_signals_to_trades(signals)

    from core.db import get_conn
    with get_conn() as conn:
        for t in trades:
            name_row = conn.execute(
                "SELECT name FROM stock_info WHERE code = ?", (t["code"],)
            ).fetchone()
            t["name"] = name_row["name"] if name_row else ""

    trades.sort(key=lambda t: t["entry_date"], reverse=True)
    return trades
