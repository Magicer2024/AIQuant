"""
batch_backtest.py —— 批量回测所有活跃规则

优化：一次加载全市场数据 + 一次计算因子，所有规则共享。
"""
import json
import time
import numpy as np
import pandas as pd
from typing import Dict, List
from datetime import datetime, date
from collections import defaultdict

from core.db import get_conn
from strategy.factor_lib import compute_all_factors
from backtest.engine import (
    _evaluate_conditions, _evaluate_cross_conditions,
    DEFAULT_STOP_LOSS, DEFAULT_TAKE_PROFIT,
)

# 回测参数
START_DATE = "2024-01-01"
END_DATE = date.today().strftime("%Y-%m-%d")


def load_all_stock_data() -> Dict[str, pd.DataFrame]:
    """一次加载所有股票 OHLCV 数据"""
    ext_start = "2023-06-01"  # 预留因子计算窗口
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT dp.code, dp.trade_date, dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover
            FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date >= ? AND dp.trade_date <= ?
            ORDER BY dp.code, dp.trade_date
        """, (ext_start, END_DATE)).fetchall()

    stock_data: dict = defaultdict(list)
    for r in rows:
        td = r["trade_date"] if isinstance(r["trade_date"], str) else str(r["trade_date"])
        stock_data[r["code"]].append({
            "trade_date": td,
            "open": float(r["open"] or 0),
            "high": float(r["high"] or 0),
            "low": float(r["low"] or 0),
            "close": float(r["close"] or 0),
            "volume": float(r["volume"] or 0),
            "amount": float(r["amount"] or 0),
            "turnover": float(r["turnover"] or 0),
        })

    result = {}
    for code, records in stock_data.items():
        df = pd.DataFrame(records).set_index("trade_date")
        if len(df) >= 60:
            result[code] = df
    return result


def compute_all_factors_cached(stock_data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """一次计算所有股票的因子 DataFrame"""
    factors = {}
    total = len(stock_data)
    for i, (code, df) in enumerate(stock_data.items()):
        try:
            fdf = compute_all_factors(df)
            if not fdf.empty:
                factors[code] = fdf
        except Exception:
            pass
        if (i + 1) % 500 == 0:
            print(f"  factors: {i + 1}/{total}")
    return factors


def generate_signals_for_rule(
    factor_data: Dict[str, pd.DataFrame],
    stock_data: Dict[str, pd.DataFrame],
    buy_conditions: list,
    sell_conditions: list,
    holding_max: int,
) -> List[dict]:
    """为单条规则生成买卖信号（复用预计算的因子数据）"""
    all_signals: list[dict] = []

    has_cross_buy = any(
        c.get("operator") in ("cross_above_factor", "cross_below_factor")
        for c in (buy_conditions if isinstance(buy_conditions, list) else [])
    )
    has_cross_sell = sell_conditions and any(
        c.get("operator") in ("cross_above_factor", "cross_below_factor")
        for c in sell_conditions
    )

    for code, factor_df in factor_data.items():
        df = stock_data.get(code)
        if df is None:
            continue

        backtest_dates = [d for d in factor_df.index if START_DATE <= d <= END_DATE]
        if not backtest_dates:
            continue

        # --- 买入信号 ---
        buy_dates: list[str] = []
        if has_cross_buy:
            cross_flags = _evaluate_cross_conditions(factor_df, buy_conditions)
            for d in backtest_dates:
                if d in cross_flags.index and cross_flags.loc[d]:
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

        # --- 卖出信号日期 ---
        sell_signal_dates: set[str] = set()
        if has_cross_sell:
            cross_flags = _evaluate_cross_conditions(factor_df, sell_conditions)
            for d in backtest_dates:
                if d in cross_flags.index and cross_flags.loc[d]:
                    sell_signal_dates.add(d)

        # --- 配对 ---
        price_col = df["close"]
        buy_idx = 0
        while buy_idx < len(buy_dates):
            entry_date = buy_dates[buy_idx]
            entry_price = float(price_col.loc[entry_date]) if entry_date in price_col.index else 0
            if entry_price <= 0:
                buy_idx += 1
                continue

            all_signals.append({
                "trade_date": entry_date, "code": code,
                "type": "buy", "price": entry_price,
            })

            future_dates = [d for d in backtest_dates if d > entry_date]
            exit_date = None
            exit_price = 0.0
            exit_reason = "period_end"
            holding_days = 0

            for d in future_dates:
                holding_days += 1
                close_px = float(price_col.loc[d]) if d in price_col.index else 0
                if close_px <= 0:
                    continue

                pnl_pct = (close_px / entry_price) - 1

                if pnl_pct <= DEFAULT_STOP_LOSS:
                    exit_date, exit_price, exit_reason = d, close_px, "stop_loss"
                    break
                if pnl_pct >= DEFAULT_TAKE_PROFIT:
                    exit_date, exit_price, exit_reason = d, close_px, "take_profit"
                    break
                if holding_days >= holding_max:
                    exit_date, exit_price, exit_reason = d, close_px, "max_hold"
                    break
                if sell_conditions:
                    if d in sell_signal_dates:
                        exit_date, exit_price, exit_reason = d, close_px, "sell_signal"
                        break
                    if d in factor_df.index:
                        row = factor_df.loc[d].to_dict()
                        if _evaluate_conditions(row, sell_conditions):
                            exit_date, exit_price, exit_reason = d, close_px, "sell_signal"
                            break

            if exit_date is None:
                exit_date = END_DATE
                if END_DATE in price_col.index:
                    exit_price = float(price_col.loc[END_DATE])
                else:
                    before = [d for d in price_col.index if d <= END_DATE]
                    exit_price = float(price_col.loc[before[-1]]) if before else entry_price

            all_signals.append({
                "trade_date": exit_date, "code": code,
                "type": "sell", "price": exit_price,
                "exit_reason": exit_reason,
            })

            buy_idx += 1
            while buy_idx < len(buy_dates) and buy_dates[buy_idx] <= exit_date:
                buy_idx += 1

    all_signals.sort(key=lambda s: (s["trade_date"], s["code"]))
    return all_signals


def compute_summary(signals: List[dict]) -> dict:
    """从信号计算回测汇总指标"""
    from backtest.engine import _pair_signals_to_trades

    trades = _pair_signals_to_trades(signals)
    if not trades:
        return {"annual_return": 0, "cumulative_return": 0, "win_rate": 0,
                "sharpe_ratio": 0, "max_drawdown": 0, "total_trades": 0, "win_trades": 0}

    r_arr = np.array([t["pnl_pct"] for t in trades]) / 100.0
    holding_arr = np.array([t.get("holding_days", 1) for t in trades])

    win_trades = int((r_arr > 0).sum())
    total_trades = len(r_arr)
    win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

    avg_return = float(np.mean(r_arr))
    avg_holding = float(np.mean(holding_arr))
    if avg_return > -1 and avg_holding > 0:
        exponent = min(365.0 / avg_holding, 20.0)
        ann_return = float(((1 + avg_return) ** exponent - 1) * 100)
    else:
        ann_return = 0.0

    log_returns = np.log1p(np.clip(r_arr, -0.999, None))
    cum_return = float(np.expm1(np.sum(log_returns)) * 100)

    try:
        start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
        end_dt = datetime.strptime(END_DATE, "%Y-%m-%d")
        years = max((end_dt - start_dt).days / 365.25, 0.1)
    except Exception:
        years = 1.0

    mean_r = float(np.mean(r_arr))
    std_r = float(np.std(r_arr, ddof=1))
    sharpe = float(mean_r / std_r * np.sqrt(total_trades / years)) if std_r > 0 and years > 0 else 0

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


def run_batch():
    print(f"=== Batch Backtest: {START_DATE} → {END_DATE} ===")

    # Phase 1: load data
    t0 = time.time()
    print("[1/4] Loading stock data...")
    stock_data = load_all_stock_data()
    print(f"  {len(stock_data)} stocks loaded ({time.time() - t0:.1f}s)")

    # Phase 2: compute factors
    t0 = time.time()
    print("[2/4] Computing factors...")
    factor_data = compute_all_factors_cached(stock_data)
    print(f"  {len(factor_data)} stocks with factors ({time.time() - t0:.1f}s)")

    # Phase 3: load rules
    print("[3/4] Loading active rules...")
    with get_conn() as conn:
        rules = conn.execute(
            "SELECT * FROM strategy_rules WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        rules = [dict(r) for r in rules]
    print(f"  {len(rules)} active rules")

    # Phase 4: backtest each rule
    print(f"[4/4] Backtesting {len(rules)} rules...")
    from backtest.trade_store import save_result, save_trades

    for idx, rule in enumerate(rules):
        t0 = time.time()
        rule_name = rule["rule_name"]

        try:
            buy_conds = json.loads(rule["conditions"] or "[]")
        except json.JSONDecodeError:
            buy_conds = []

        try:
            sell_raw = rule.get("sell_conditions", "")
            if isinstance(sell_raw, str) and sell_raw.strip() and sell_raw.strip() != "[]":
                sell_conds = json.loads(sell_raw)
            elif isinstance(sell_raw, list) and sell_raw:
                sell_conds = sell_raw
            else:
                sell_conds = []
        except (json.JSONDecodeError, TypeError):
            sell_conds = []

        if not buy_conds:
            continue

        signals = generate_signals_for_rule(
            factor_data, stock_data,
            buy_conds, sell_conds,
            rule.get("holding_max", 20),
        )

        summary = compute_summary(signals)
        trades = []
        if signals:
            from backtest.engine import _pair_signals_to_trades
            trades = _pair_signals_to_trades(signals)
            with get_conn() as conn:
                for t in trades:
                    name_row = conn.execute(
                        "SELECT name FROM stock_info WHERE code = ?", (t["code"],)
                    ).fetchone()
                    t["name"] = name_row["name"] if name_row else ""

        if summary and summary.get("total_trades", 0) > 0:
            result_id = save_result(
                str(rule["id"]), rule_name, START_DATE, END_DATE, summary
            )
            if trades:
                save_trades(result_id, trades)

            # Update rule metrics
            with get_conn() as conn:
                conn.execute("""
                    UPDATE strategy_rules SET
                        annual_return=?, win_rate=?, sharpe_ratio=?, max_drawdown=?,
                        total_trades=?, fitness=?, updated_at=datetime('now','localtime')
                    WHERE id=?
                """, (
                    summary["annual_return"], summary["win_rate"],
                    summary["sharpe_ratio"], summary["max_drawdown"],
                    summary["total_trades"], summary["annual_return"],
                    rule["id"],
                ))

            elapsed = time.time() - t0
            print(f"  [{idx + 1}/{len(rules)}] {rule_name[:60]:60s}  "
                  f"trades={summary['total_trades']:4d}  "
                  f"ann_ret={summary['annual_return']:8.2f}%  "
                  f"win={summary['win_rate']:5.1f}%  "
                  f"sharpe={summary['sharpe_ratio']:6.2f}  "
                  f"({elapsed:.1f}s)")
        else:
            elapsed = time.time() - t0
            print(f"  [{idx + 1}/{len(rules)}] {rule_name[:60]:60s}  NO TRADES  ({elapsed:.1f}s)")

    print("\n=== Batch backtest complete ===")


if __name__ == "__main__":
    run_batch()
