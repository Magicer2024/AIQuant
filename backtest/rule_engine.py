"""
backtest/rule_engine.py —— 策略规则一键回测
================================================

与 backtest/engine.py（VisualBacktestEngine，用户手配 CONDITION_CATALOG 条件）并列，
本模块面向"strategy_rules 表中的因子规则"提供一键回测：

  规则 conditions（因子阈值 JSON，两种格式兼容）
    → factor_lib.compute_all_factors 全历史向量化计算
    → 逐日评估 (passed, strength)，信号日次日开盘价买入
    → 持仓上限/每日买入上限/strength 优先
    → 出场：holding_max 到期 / 止损 / 止盈 / 数据结束
    → 组合净值 + 交易明细 + 指标 + 沪深300 基准对比

返回 dict 的键与 VisualBacktestEngine.run() 对齐（service 层统一持久化），
另附 benchmark 与 rule 信息。
"""
from __future__ import annotations

import json
import math
import threading
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from core.db import get_conn
from strategy.factor_lib import compute_all_factors
from backtest.engine import BacktestParams


# ─────────────── 条件向量化评估 ───────────────

def _iter_condition_items(conditions) -> List[tuple]:
    """把两种 conditions 格式统一为 (factor, op, threshold) 列表
    - list: [{"factor": "RSI_14", "operator": "<", "threshold": 0.5}, ...]
    - dict: {"RSI_14": {"<": 0.5}, ...}
    """
    items: List[tuple] = []
    if isinstance(conditions, list):
        for c in conditions:
            f, op, thr = c.get("factor"), c.get("operator"), c.get("threshold")
            if f is not None and op is not None and thr is not None:
                items.append((f, op, float(thr)))
    elif isinstance(conditions, dict):
        for f, op_dict in conditions.items():
            if isinstance(op_dict, dict):
                for op, thr in op_dict.items():
                    items.append((f, op, float(thr)))
    return items


def _evaluate_conditions_vec(factor_df: pd.DataFrame, conditions) -> pd.DataFrame:
    """向量化评估：返回 DataFrame(passed: bool, strength: float)，索引同 factor_df。
    与 scorer._evaluate_condition 同语义（AND 通过 + 平均强度）。"""
    items = _iter_condition_items(conditions)
    if not items:
        return pd.DataFrame({"passed": False, "strength": 0.0}, index=factor_df.index)

    passed = pd.Series(True, index=factor_df.index)
    strength_sum = pd.Series(0.0, index=factor_df.index)
    n = 0
    for factor, op, thr in items:
        if factor not in factor_df.columns:
            return pd.DataFrame({"passed": False, "strength": 0.0}, index=factor_df.index)
        v = factor_df[factor].astype(float)
        if op in (">", ">="):
            p = v > thr if op == ">" else v >= thr
            s = (v - thr) / max(1.0 - thr, 0.05)
        elif op in ("<", "<="):
            p = v < thr if op == "<" else v <= thr
            s = (thr - v) / max(thr, 0.05)
        else:
            return pd.DataFrame({"passed": False, "strength": 0.0}, index=factor_df.index)
        passed &= p.fillna(False)
        strength_sum += s.fillna(0.0)
        n += 1

    return pd.DataFrame({"passed": passed, "strength": strength_sum / max(n, 1)})


# ─────────────── 基准（沪深300）───────────────

def _load_benchmark(start_date: str, end_date: str, init_cash: float,
                    index_code: str = "000300") -> Optional[Dict[str, Any]]:
    """从 index_daily 读基准指数，输出净值曲线与累计收益；无数据返回 None"""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT trade_date, close FROM index_daily "
                "WHERE code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (index_code, start_date, end_date),
            ).fetchall()
    except Exception:
        return None
    if len(rows) < 2:
        return None
    base = float(rows[0]["close"])
    if base <= 0:
        return None
    curve = [
        {"date": r["trade_date"], "total": round(float(r["close"]) / base * init_cash, 2)}
        for r in rows
    ]
    return {
        "code": index_code,
        "name": "沪深300",
        "total_return": round(float(rows[-1]["close"]) / base - 1, 4),
        "curve": curve,
    }


# ─────────────── 主入口 ───────────────

def run_rule_backtest(rule: Dict[str, Any],
                      params: BacktestParams,
                      progress_cb: Optional[Callable] = None,
                      cancel_flag: Optional[threading.Event] = None) -> Dict[str, Any]:
    """
    对一条 strategy_rules 规则跑全市场组合回测。

    :param rule: rules_store.get_rule() 的返回（含 conditions/holding_max 等）
    :param params: BacktestParams（max_hold_days 为空时用 rule.holding_max）
    """
    start_date, end_date = params.start_date, params.end_date
    holding_max = params.max_hold_days or int(rule.get("holding_max") or 20)
    stop_loss_pct = params.stop_loss_pct if params.stop_loss_pct is not None else 0.06
    take_profit_pct = params.take_profit_pct if params.take_profit_pct is not None else 0.20
    rf = float(getattr(params, "risk_free_rate", 0.02) or 0.0)

    try:
        conditions = json.loads(rule.get("conditions") or "{}")
    except json.JSONDecodeError:
        return {"success": False, "error": f"规则 conditions 解析失败: {rule.get('rule_name')}"}
    if not _iter_condition_items(conditions):
        return {"success": False, "error": "规则 conditions 为空或格式不支持"}

    # 回测区间交易日列表
    with get_conn() as conn:
        trade_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start_date, end_date),
        ).fetchall()]
    if len(trade_dates) < 2:
        return {"success": False, "error": "回测区间无交易数据"}

    date_set = set(trade_dates)

    def _report(pct: int, msg: str):
        if progress_cb:
            progress_cb({"stage": "scan" if pct < 60 else "sim", "progress": pct, "message": msg})

    def _cancelled() -> bool:
        return bool(cancel_flag and cancel_flag.is_set())

    # ── 1) 逐股计算因子并评估信号 ─────────────────
    from strategy.scorer import _load_stock_data_streaming  # 复用流式加载

    signals: Dict[str, List[tuple]] = {}   # date -> [(code, strength)]
    price_data: Dict[str, pd.DataFrame] = {}  # 有信号的股票保留区间 OHLC
    scanned = 0
    for code, df in _load_stock_data_streaming(end_date):
        scanned += 1
        if scanned % 200 == 0:
            _report(min(59, scanned // 80), f"因子扫描 {scanned} 只...")
        if _cancelled():
            return {"success": False, "cancelled": True, "error": "已取消"}
        if df is None or len(df) < 30:
            continue
        try:
            factor_df = compute_all_factors(df)
        except Exception:
            continue
        if factor_df.empty:
            continue
        ev = _evaluate_conditions_vec(factor_df, conditions)
        hit = ev[ev["passed"] & ev.index.isin(date_set)]
        if hit.empty:
            continue
        # 收盘价也进 price_data（模拟买卖用）
        price_data[code] = df[df.index.isin(date_set)][["open", "close"]]
        for dt, row in hit.iterrows():
            signals.setdefault(dt, []).append((code, float(row["strength"])))

    if not signals:
        return {"success": False, "error": "回测区间内该规则无任何信号",
                "total_trades": 0}

    # 股票名称映射
    with get_conn() as conn:
        name_map = {r["code"]: r["name"] for r in conn.execute(
            "SELECT code, name FROM stock_info").fetchall()}

    # ── 2) 组合模拟 ─────────────────
    cash = float(params.initial_cash)
    slot_cash = float(params.initial_cash) / max(1, params.max_holdings)
    positions: Dict[str, Dict[str, Any]] = {}
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    last_close: Dict[str, float] = {}

    def _price(code: str, dt: str, col: str) -> Optional[float]:
        pdf = price_data.get(code)
        if pdf is None or dt not in pdf.index:
            return None
        v = pdf.loc[dt, col]
        return float(v) if pd.notna(v) and v > 0 else None

    fee_buy = params.commission_rate + params.slippage_rate
    fee_sell = params.commission_rate + params.stamp_tax_rate + params.slippage_rate

    for i, dt in enumerate(trade_dates):
        if _cancelled():
            return {"success": False, "cancelled": True, "error": "已取消"}
        _report(60 + int(40 * i / len(trade_dates)), f"模拟 {dt}")

        # 更新记忆收盘价
        for code, pdf in price_data.items():
            if dt in pdf.index:
                c = pdf.loc[dt, "close"]
                if pd.notna(c) and c > 0:
                    last_close[code] = float(c)

        # ── 卖出检查（先卖后买）──
        for code in list(positions.keys()):
            pos = positions[code]
            close = _price(code, dt, "close")
            if close is None:
                continue
            pos["hold_days"] += 1
            reason = None
            if close <= pos["entry_price"] * (1 - stop_loss_pct):
                reason = "stop_loss"
            elif close >= pos["entry_price"] * (1 + take_profit_pct):
                reason = "take_profit"
            elif pos["hold_days"] >= holding_max:
                reason = "max_hold"
            elif i == len(trade_dates) - 1:
                reason = "end_of_data"
            if reason:
                proceeds = pos["shares"] * close * (1 - fee_sell)
                cash += proceeds
                pnl = proceeds - pos["cost"]
                trades.append({
                    "code": code,
                    "name": name_map.get(code, code),
                    "buy_date": pos["entry_date"],
                    "buy_price": round(pos["entry_price"], 2),
                    "sell_date": dt,
                    "sell_price": round(close, 2),
                    "shares": pos["shares"],
                    "hold_days": pos["hold_days"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(proceeds / pos["cost"] - 1, 4) if pos["cost"] > 0 else 0,
                    "exit_reason": reason,
                })
                del positions[code]

        # ── 买入（信号日次日开盘）──
        if i > 0:  # 第一个交易日没有"昨日信号"
            prev = trade_dates[i - 1]
            todays = sorted(signals.get(prev, []), key=lambda x: x[1], reverse=True)
            bought = 0
            for code, _strength in todays:
                if bought >= params.max_buy_per_day:
                    break
                if len(positions) >= params.max_holdings:
                    break
                if code in positions:
                    continue
                open_p = _price(code, dt, "open")
                if open_p is None:
                    continue
                shares = int(slot_cash / (open_p * (1 + fee_buy)) // 100) * 100
                if shares < 100:
                    continue
                cost = shares * open_p * (1 + fee_buy)
                if cost > cash:
                    shares = int(cash / (open_p * (1 + fee_buy)) // 100) * 100
                    if shares < 100:
                        continue
                    cost = shares * open_p * (1 + fee_buy)
                cash -= cost
                positions[code] = {
                    "entry_price": open_p, "entry_date": dt,
                    "shares": shares, "cost": cost, "hold_days": 0,
                }
                bought += 1

        # ── 日终净值 ──
        mv = sum(p["shares"] * last_close.get(c, p["entry_price"])
                 for c, p in positions.items())
        equity_curve.append({"date": dt, "total": round(cash + mv, 2)})

    # ── 3) 指标 ──
    eq = pd.DataFrame(equity_curve)
    final_assets = float(eq["total"].iloc[-1])
    init = float(params.initial_cash)
    total_return = final_assets / init - 1 if init > 0 else 0.0
    days = max(1, (pd.to_datetime(eq["date"].iloc[-1])
                   - pd.to_datetime(eq["date"].iloc[0])).days)
    annual_return = (1 + total_return) ** (365.0 / days) - 1 if days > 0 else 0.0

    daily_ret = eq["total"].pct_change().fillna(0)
    std = daily_ret.std()
    sharpe = float((daily_ret.mean() - rf / 252) / std * math.sqrt(252)) \
        if std and std > 0 else 0.0

    roll_max = eq["total"].cummax()
    max_drawdown = float(((eq["total"] - roll_max) / roll_max).min())

    win_trades = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = win_trades / len(trades) if trades else 0.0

    return {
        "success": True,
        "start_date": start_date,
        "end_date": end_date,
        "init_cash": params.initial_cash,
        "final_assets": round(final_assets, 2),
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe, 3),
        "win_rate": round(win_rate, 4),
        "total_trades": len(trades),
        "win_trades": win_trades,
        "trades": trades,
        "equity_curve": equity_curve,
        "benchmark": _load_benchmark(start_date, end_date, params.initial_cash),
        "rule": {
            "id": rule.get("id"),
            "rule_name": rule.get("rule_name"),
            "horizon": rule.get("horizon"),
            "holding_max": holding_max,
        },
    }
