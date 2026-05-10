"""
services/backtest_service.py —— 批量回测服务 + 策略实验室回测
"""
import traceback
import json
from datetime import date, datetime, timedelta
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import numpy as np


# ═══════════════════════════════════════════════════════════════
# 原有批量回测
# ═══════════════════════════════════════════════════════════════

def run_batch_backtest(start_date: str = "2025-04-03", end_date: str = None,
                       min_score: float = 15.0, capital: float = 100000,
                       max_positions: int = 3, max_position_size: float = 400000,
                       stop_loss: float = -0.06, take_profit: float = 0.20,
                       use_market_timing: bool = True, use_dynamic_position: bool = False,
                       weights: List[float] = None, use_v4: bool = True) -> Dict[str, Any]:
    """运行批量回测（v4超跌反弹策略）"""
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    from scripts.batch.batch_backtest import run_batch_backtest as _run

    result = _run(
        start_date=start_date,
        end_date=end_date,
        min_score=min_score,
        init_cash=capital,
        max_positions=max_positions,
        max_position_size=max_position_size,
        stop_loss=stop_loss,
        take_profit=take_profit,
        use_market_timing=use_market_timing,
        use_dynamic_position=use_dynamic_position,
        weights=weights,
        use_fundamental_filter=False,
        trailing_pct=take_profit,
        verbose=False,
        use_v4=use_v4,
    )

    if "error" in result and "trades" not in result:
        raise ValueError(result["error"])

    raw_trades = result.get("trades", [])
    paired = _pair_trades(raw_trades)
    closed = [t for t in paired if t["exit_date"]]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    losses = [t for t in closed if (t["pnl"] or 0) < 0]
    win_rate = len(wins) / max(1, len(closed))
    avg_win_pct = sum(t["pnl_pct"] for t in wins) / max(1, len(wins)) if wins else 0
    avg_loss_pct = abs(sum(t["pnl_pct"] for t in losses) / max(1, len(losses))) if losses else 0

    return {
        "start_date": start_date,
        "end_date": end_date,
        "init_capital": result.get("init_cash"),
        "final_capital": result.get("final_assets"),
        "total_return": result.get("total_return"),
        "annual_return": result.get("ann_return"),
        "max_drawdown": result.get("max_drawdown"),
        "win_rate": win_rate,
        "profit_factor": result.get("profit_factor"),
        "total_trades": result.get("total_trades"),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "avg_hold_days": round(sum(t["holding_days"] for t in closed) / max(1, len(closed)), 1) if closed else 0,
        "total_commission": result.get("total_commission"),
        "unique_stocks": result.get("unique_stocks"),
        "total_cost": result.get("total_cost"),
        "equity_curve": result.get("daily_equity", []),
        "trades": paired,
        "raw_trades": raw_trades,
    }


def _pair_trades(raw_trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将原始交易记录按 code 配对为买入/卖出"""
    code_map = defaultdict(list)
    for t in raw_trades:
        code_map[t["code"]].append(t)

    paired = []
    for code, tlist in code_map.items():
        tlist.sort(key=lambda x: x["date"])
        buys = [t for t in tlist if t["direction"] == "buy"]
        sells = [t for t in tlist if t["direction"] == "sell"]
        for i, buy in enumerate(buys):
            entry_date = buy["date"]
            entry_price = buy["price"]
            shares = buy["shares"]
            trigger = buy.get("trigger", "")
            if i < len(sells):
                sell = sells[i]
                exit_date = sell["date"]
                exit_price = sell["price"]
                pnl = sell.get("pnl", 0) or 0
                pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price else 0
                r = sell.get("reason", "")
                if "stop_loss" in r or "止损" in r:
                    exit_reason = "止损"
                elif "trailing" in r or "跟踪" in r:
                    exit_reason = "跟踪止盈"
                else:
                    exit_reason = "止盈" if pnl > 0 else "止损"
            else:
                exit_date = None
                exit_price = None
                pnl = None
                pnl_pct = None
                exit_reason = "持仓中"
            if exit_date:
                d0 = datetime.strptime(entry_date[:10], "%Y-%m-%d")
                d1 = datetime.strptime(exit_date[:10], "%Y-%m-%d")
                holding_days = (d1 - d0).days
            else:
                holding_days = None
            paired.append({
                "code": code,
                "name": buy.get("name", code),
                "entry_date": entry_date[:10] if entry_date else None,
                "entry_price": round(entry_price, 2),
                "shares": shares,
                "exit_date": exit_date[:10] if exit_date else None,
                "exit_price": round(exit_price, 2) if exit_price else None,
                "holding_days": holding_days,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                "exit_reason": exit_reason,
                "trigger": trigger,
            })
    paired.sort(key=lambda x: x["entry_date"] or "")
    return paired


# ═══════════════════════════════════════════════════════════════
# 策略实验室回测
# ═══════════════════════════════════════════════════════════════

def get_strategy_lab_strategies_for_backtest() -> List[dict]:
    """获取策略实验室中可用于回测的活跃策略列表"""
    from core.db import get_current_active_strategies, get_conn

    strategies = get_current_active_strategies()
    if not strategies:
        return []

    # 直接按 ID 查询规则，不通过 get_active_rules() 的 is_active=1 过滤
    # active_strategies 表已决定哪些策略是当期活跃的
    rule_ids = [s["rule_id"] for s in strategies]
    placeholders = ",".join("?" for _ in rule_ids)
    rules_by_id = {}
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM strategy_rules WHERE id IN ({placeholders})",
            rule_ids,
        ).fetchall()
        rules_by_id = {r["id"]: dict(r) for r in rows}

    result = []
    for s in strategies:
        rule = rules_by_id.get(s.get("rule_id"))
        if rule:
            try:
                conds = json.loads(rule.get("conditions", "[]"))
            except (json.JSONDecodeError, TypeError):
                conds = []
            cond_desc = []
            for c in conds:
                op_map = {">": ">", "<": "<", "cross_above": "上穿", "cross_below": "下穿"}
                op = op_map.get(c.get("operator", ""), c.get("operator", ""))
                ref = f" vs {c['ref_factor']}" if c.get("ref_factor") else ""
                cond_desc.append(f"{c.get('factor', '')} {op} {c.get('threshold', '')}{ref}")
            result.append({
                "rule_id": rule["id"],
                "rule_name": rule["rule_name"],
                "rule_type": rule.get("rule_type", ""),
                "source": rule.get("source", ""),
                "generation": rule.get("generation", 0),
                "fitness": rule.get("fitness", 0),
                "annual_return": rule.get("annual_return", 0),
                "win_rate": rule.get("win_rate", 0),
                "sharpe_ratio": rule.get("sharpe_ratio", 0),
                "max_drawdown": rule.get("max_drawdown", 0),
                "total_trades": rule.get("total_trades", 0),
                "conditions": conds,
                "condition_desc": cond_desc,
                "select_rank": s.get("rank", 0),
                "score_60": s.get("window_60_score", 0),
                "score_120": s.get("window_120_score", 0),
                "final_score": s.get("final_score", 0),
            })
        else:
            # 规则可能已被删除，但仍返回基础信息以保证策略可被选择
            result.append({
                "rule_id": s.get("rule_id", 0),
                "rule_name": s.get("rule_name", ""),
                "rule_type": s.get("rule_type", ""),
                "source": "active_strategies",
                "generation": 0,
                "fitness": 0,
                "annual_return": 0,
                "win_rate": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
                "total_trades": 0,
                "conditions": [],
                "condition_desc": [],
                "select_rank": s.get("rank", 0),
                "score_60": s.get("window_60_score", 0),
                "score_120": s.get("window_120_score", 0),
                "final_score": s.get("final_score", 0),
            })

    # 按 final_score 降序排列
    result.sort(key=lambda x: x.get("final_score", 0) or 0, reverse=True)
    return result


def run_strategy_lab_backtest(
    rule_ids: List[int],
    start_date: str = "2025-01-01",
    end_date: str = None,
    capital: float = 100000,
    max_positions: int = 5,
    buy_num_per_day: int = 3,
    hold_days: int = 10,
    stop_loss: float = -0.06,
    take_profit: float = 0.15,
    trailing_stop: float = 0.0,
    signal_mode: str = "any",
) -> Dict[str, Any]:
    """
    使用策略实验室规则进行多股票筛选回测。

    Args:
        rule_ids: 选中的策略规则 ID 列表
        start_date / end_date: 回测区间
        capital: 初始资金
        max_positions: 最大同时持仓数
        buy_num_per_day: 每日最多买入数
        hold_days: 最大持仓天数
        stop_loss / take_profit / trailing_stop: 风控参数
        signal_mode: "any" = 任一规则触发即买入, "all" = 全部规则触发才买入

    Returns:
        回测结果字典
    """
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    # ── 1. 加载规则 ──
    rules = _load_rules_by_ids(rule_ids)
    if not rules:
        return {"success": False, "error": "未找到有效的策略规则"}

    # ── 2. 加载行情数据 ──
    extra_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
    raw = _load_all_daily(extra_start, end_date)
    if raw.empty:
        return {"success": False, "error": "数据库无数据，请先同步"}

    raw["trade_date"] = pd.to_datetime(raw["trade_date"])

    # ── 3. 按股票分组，计算因子 ──
    from strategy.factor_lib import compute_all_factors

    stock_factors = {}   # code -> DataFrame (index=trade_date, columns=factors)
    stock_info_map = {}  # code -> name
    for code, g in raw.groupby("code", sort=False):
        name = str(g["name"].iloc[0]) if not g.empty else ""
        if not _static_filter(code, name):
            continue
        if len(g) < 60:
            continue
        g = g.sort_values("trade_date").set_index("trade_date")
        try:
            factors = compute_all_factors(g)
            stock_factors[code] = factors
            stock_info_map[code] = name
        except Exception:
            continue

    if not stock_factors:
        return {"success": False, "error": "没有足够的数据计算因子"}

    # ── 4. 获取交易日列表 ──
    all_dates = sorted(raw["trade_date"].unique())
    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date)
    trade_dates = [d for d in all_dates if start_dt <= d <= end_dt]
    if not trade_dates:
        return {"success": False, "error": f"回测区间 {start_date}~{end_date} 内无交易日数据"}

    # ── 5. 加载每个股票的 OHLCV 数据用于交易执行 ──
    stock_prices = {}  # code -> DataFrame (index=trade_date)
    for code, g in raw.groupby("code", sort=False):
        if code not in stock_factors:
            continue
        g = g.sort_values("trade_date").set_index("trade_date")
        stock_prices[code] = g

    # ── 6. 执行回测 ──
    COMMISSION_RATE = 0.0003
    STAMP_DUTY = 0.001
    MIN_COMMISSION = 5.0

    cash = float(capital)
    positions: List[dict] = []
    trades: List[dict] = []
    equity_curve: List[dict] = []

    n_dates = len(trade_dates)

    for day_i, today in enumerate(trade_dates):
        # ── 6a. 检查持仓退出 ──
        still_holding = []
        for pos in positions:
            code = pos["code"]
            px_df = stock_prices.get(code)
            if px_df is None or today not in px_df.index:
                still_holding.append(pos)
                continue

            row = px_df.loc[today]
            close_price = float(row["close"])
            if pd.isna(close_price) or close_price <= 0:
                still_holding.append(pos)
                continue

            entry = pos["entry_price"]
            ret_pct = (close_price - entry) / entry

            if trailing_stop > 0:
                pos["peak"] = max(pos.get("peak", entry), close_price)
                trail_ret = (close_price - pos["peak"]) / pos["peak"]
            else:
                trail_ret = 0

            entry_dt = pd.Timestamp(pos["buy_date"])
            t1_ready = (today - entry_dt).days >= 1
            hold_days_actual = (today - entry_dt).days

            should_sell = False
            sell_reason = ""
            if t1_ready:
                if ret_pct <= stop_loss:
                    should_sell, sell_reason = True, "止损"
                elif ret_pct >= take_profit:
                    should_sell, sell_reason = True, "止盈"
                elif trailing_stop > 0 and trail_ret <= -trailing_stop:
                    should_sell, sell_reason = True, "跟踪止损"
            if not should_sell and hold_days_actual >= hold_days:
                should_sell, sell_reason = True, f"持股{hold_days}天到期"

            if should_sell:
                # T+1 执行：找次日数据
                cur_idx = _index_of(all_dates, today)
                next_day = all_dates[cur_idx + 1] if cur_idx is not None and cur_idx + 1 < len(all_dates) else None
                if next_day is not None and next_day in px_df.index:
                    sell_price = float(px_df.loc[next_day]["open"])
                    if pd.isna(sell_price) or sell_price <= 0:
                        sell_price = close_price
                    sell_date_actual = next_day
                else:
                    sell_price = close_price
                    sell_date_actual = today

                shares = pos["shares"]
                sell_amount = shares * sell_price
                comm = max(sell_amount * COMMISSION_RATE, MIN_COMMISSION)
                stamp = sell_amount * STAMP_DUTY
                net_proceeds = sell_amount - comm - stamp
                buy_cost = pos.get("cost", entry * shares)
                pnl = net_proceeds - buy_cost

                cash += net_proceeds
                trades.append({
                    "code": code,
                    "name": pos["name"],
                    "buy_date": pos["buy_date"].strftime("%Y-%m-%d"),
                    "sell_date": pd.Timestamp(sell_date_actual).strftime("%Y-%m-%d"),
                    "entry_price": round(entry, 3),
                    "exit_price": round(sell_price, 3),
                    "shares": shares,
                    "pnl": round(pnl, 2),
                    "ret_pct": round((pnl / buy_cost) * 100, 2) if buy_cost else 0,
                    "hold_days": (pd.Timestamp(sell_date_actual) - entry_dt).days,
                    "exit_reason": sell_reason,
                    "trigger": pos.get("trigger_rule", ""),
                })
            else:
                still_holding.append(pos)

        positions = still_holding

        # ── 6b. 选股 ──
        held_codes = {p["code"] for p in positions}
        can_buy_slots = max_positions - len(positions)

        # 找次日日期
        cur_idx = _index_of(all_dates, today)
        next_day = all_dates[cur_idx + 1] if cur_idx is not None and cur_idx + 1 < len(all_dates) else None

        candidates = []  # (code, name, num_rules_triggered, rules_list)

        if can_buy_slots > 0 and next_day is not None:
            for code, factor_df in stock_factors.items():
                if code in held_codes:
                    continue
                if today not in factor_df.index:
                    continue

                # 检查该股票次日是否有价格数据
                px_df = stock_prices.get(code)
                if px_df is None or next_day not in px_df.index:
                    continue
                next_open = float(px_df.loc[next_day]["open"])
                if pd.isna(next_open) or next_open <= 0:
                    continue

                # 评估规则
                triggered = []
                for rule_info in rules:
                    try:
                        signal = _evaluate_rule_on_day(rule_info["rule"], factor_df, today)
                        if signal:
                            triggered.append(rule_info["rule_name"])
                    except Exception:
                        continue

                if signal_mode == "all":
                    if len(triggered) == len(rules):
                        candidates.append((code, stock_info_map.get(code, code), len(triggered), triggered))
                else:
                    if len(triggered) > 0:
                        candidates.append((code, stock_info_map.get(code, code), len(triggered), triggered))

            # 按触发规则数降序排列，选 top N
            candidates.sort(key=lambda x: x[2], reverse=True)

            n_buy = min(can_buy_slots, buy_num_per_day, len(candidates))
            for i in range(n_buy):
                code, name, n_trig, trig_rules = candidates[i]
                px_df = stock_prices[code]
                buy_price = float(px_df.loc[next_day]["open"])

                per_pos_capital = cash / max(can_buy_slots - i, 1)
                shares = int(per_pos_capital / buy_price / 100) * 100
                if shares < 100:
                    continue

                buy_amount = shares * buy_price
                comm = max(buy_amount * COMMISSION_RATE, MIN_COMMISSION)
                cost = buy_amount + comm
                if cost > cash:
                    continue

                cash -= cost
                positions.append({
                    "code": code,
                    "name": name,
                    "buy_date": next_day,
                    "entry_price": buy_price,
                    "shares": shares,
                    "peak": buy_price,
                    "cost": cost,
                    "trigger_rule": ", ".join(trig_rules),
                })

        # ── 6c. 记录资金曲线 ──
        pos_value = 0.0
        for pos in positions:
            px_df = stock_prices.get(pos["code"])
            if px_df is not None and today in px_df.index:
                pos_value += float(px_df.loc[today]["close"]) * pos["shares"]
            elif px_df is not None:
                # 停牌：用最近收盘价
                for d in reversed(all_dates):
                    if d > today:
                        continue
                    if d in px_df.index:
                        pos_value += float(px_df.loc[d]["close"]) * pos["shares"]
                        break

        total_equity = cash + pos_value
        equity_curve.append({
            "date": today.strftime("%Y-%m-%d"),
            "equity": round(total_equity, 2),
            "cash": round(cash, 2),
            "pos_value": round(pos_value, 2),
        })

    # ── 7. 汇总统计 ──
    return _summarize_backtest(trades, equity_curve, capital, rules, start_date, end_date)


def _load_rules_by_ids(rule_ids: List[int]) -> List[dict]:
    """从数据库加载指定 ID 的策略规则并反序列化（不限制 is_active，由 active_strategies 决定当期有效性）"""
    from core.db import get_conn
    from strategy.rule_miner import StrategyRule, RuleCondition

    placeholders = ",".join("?" for _ in rule_ids)
    rules = []
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM strategy_rules WHERE id IN ({placeholders})",
            rule_ids,
        ).fetchall()
        for row in rows:
            row = dict(row)
            try:
                conds_data = json.loads(row.get("conditions", "[]"))
                conds = [RuleCondition(**c) for c in conds_data]
            except (json.JSONDecodeError, TypeError):
                continue

            sell_conds = []
            sell_raw = row.get("sell_conditions", "[]")
            if sell_raw and sell_raw != "[]":
                try:
                    sell_conds = [RuleCondition(**s) for s in json.loads(sell_raw)]
                except (json.JSONDecodeError, TypeError):
                    pass

            rule = StrategyRule(
                name=row["rule_name"],
                rule_type=row.get("rule_type", "T1"),
                conditions=conds,
                sell_conditions=sell_conds,
                holding_min=row.get("holding_min", 3),
                holding_max=row.get("holding_max", 20),
                source=row.get("source", "template"),
            )
            rules.append({
                "rule_id": row["id"],
                "rule_name": row["rule_name"],
                "rule_type": row.get("rule_type", ""),
                "rule": rule,
            })
    return rules


def _evaluate_rule_on_day(rule, factor_df: pd.DataFrame, today) -> bool:
    """评估单条规则在指定日是否触发买入信号"""
    if today not in factor_df.index:
        return False
    # 取包含当天及之前所有数据的切片（cross 条件需要前一天的值）
    hist_df = factor_df.loc[:today]
    if len(hist_df) < 2:
        return False
    try:
        signal = rule.get_buy_signal(hist_df)
        return bool(signal.iloc[-1])
    except Exception:
        return False


def _load_all_daily(start_date: str, end_date: str) -> pd.DataFrame:
    """加载回测区间内所有股票的日线数据"""
    from core.db import get_conn
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT dp.code, dp.trade_date,
                   dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover,
                   si.name, si.market
            FROM daily_price dp
            LEFT JOIN stock_info si ON dp.code = si.code
            WHERE dp.trade_date BETWEEN ? AND ?
              AND dp.close IS NOT NULL AND dp.close > 0
              AND dp.code NOT LIKE '688%'
              AND dp.code NOT LIKE '300%'
              AND dp.code NOT LIKE '301%'
            ORDER BY dp.trade_date, dp.code
            """,
            conn,
            params=(start_date, end_date),
        )
    return df


def _static_filter(code: str, name: str) -> bool:
    """静态过滤：排除 ST、科创、创业板"""
    name = name or ""
    if "ST" in name.upper():
        return False
    if code.startswith("688"):
        return False
    if code.startswith("300") or code.startswith("301"):
        return False
    return True


def _index_of(lst, val) -> Optional[int]:
    """查找值在列表中的索引"""
    try:
        return list(lst).index(val)
    except ValueError:
        return None


def _summarize_backtest(trades: list, equity_curve: list, init_cap: float,
                        rules: list, start_date: str, end_date: str) -> dict:
    """汇总回测统计指标"""
    if not equity_curve:
        return {"success": False, "error": "回测无数据"}

    eq_series = pd.Series([e["equity"] for e in equity_curve])
    final_cap = float(eq_series.iloc[-1])
    total_ret = (final_cap - init_cap) / init_cap

    n_days = len(equity_curve)
    annual = (1 + total_ret) ** (252 / max(n_days, 1)) - 1

    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak
    max_dd = float(drawdown.min())

    if len(eq_series) > 1:
        daily_rets = eq_series.pct_change().dropna()
        sharpe = float((daily_rets.mean() - 0.025 / 252) / (daily_rets.std() + 1e-10) * (252 ** 0.5))
    else:
        sharpe = 0.0

    if trades:
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        win_rate = len(wins) / max(len(trades), 1)
        avg_win = float(np.mean([t["ret_pct"] for t in wins])) if wins else 0
        avg_loss = float(np.mean([t["ret_pct"] for t in losses])) if losses else 0
        profit_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
        avg_hold = float(np.mean([t.get("hold_days", 0) for t in trades]))
    else:
        win_rate = avg_win = avg_loss = profit_ratio = avg_hold = 0

    # 策略表现明细
    rule_perf = []
    for r in rules:
        rule_trades = [t for t in trades if r["rule_name"] in (t.get("trigger") or "")]
        if rule_trades:
            r_wins = [t for t in rule_trades if t["pnl"] > 0]
            rule_perf.append({
                "rule_id": r["rule_id"],
                "rule_name": r["rule_name"],
                "trades": len(rule_trades),
                "win_rate": round(len(r_wins) / max(len(rule_trades), 1) * 100, 1),
                "total_pnl": round(sum(t["pnl"] for t in rule_trades), 2),
            })

    return {
        "success": True,
        "init_capital": init_cap,
        "final_capital": round(final_cap, 2),
        "total_return": round(total_ret * 100, 2),
        "annual_return": round(annual * 100, 2),
        "max_drawdown": round(abs(max_dd) * 100, 2),
        "sharpe": round(sharpe, 2),
        "total_trades": len(trades),
        "win_rate": round(win_rate * 100, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_ratio": round(profit_ratio, 2),
        "avg_hold_days": round(avg_hold, 1),
        "equity_curve": _sample_equity(equity_curve),
        "trades": trades[:100],
        "rules_used": [{"rule_id": r["rule_id"], "rule_name": r["rule_name"]} for r in rules],
        "rule_performance": rule_perf,
        "start_date": start_date,
        "end_date": end_date,
    }


def _sample_equity(equity_curve: list, max_points: int = 200) -> list:
    """对资金曲线采样以避免数据过大"""
    if len(equity_curve) <= max_points:
        return equity_curve
    step = len(equity_curve) // max_points
    return [equity_curve[i] for i in range(0, len(equity_curve), step)]
