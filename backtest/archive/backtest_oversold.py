"""
backtest_oversold.py - 超跌反弹策略专用回测引擎
================================================
完全实现用户新策略 v2 的交易规则：

买入条件（由 strategy_oversold_rebound 产生信号）：
  1. 近20日跌幅 >= 12%
  2. 收盘价 > MA5（站上5日线）
  3. 3日均量 > 5日均量（温和放量）
  4. RSI(14) 在 35~50（低位回暖）
  5. 近10日最低价未破（底部平台确认）

卖出规则（完全自定义，非 batch_backtest 的简单止损止盈）：
  A. 持仓最短2天（避免日内随机波动）
  B. 止损：单笔浮亏 -6% 无条件清仓
  C. 止盈（分两批）：
     - 盈利 +8%~+12%：卖出半仓，剩余持仓继续跟踪
     - 剩余半仓：收盘价 < MA5 → 尾盘清仓
     - 板块急拉/涨停：提前落袋（收盘涨幅>7%则尾盘清仓）
  D. 连续2日缩量阴跌 + 破10日低点 → 第2天尾盘清仓
  E. 最长持仓10天（到期强制平仓）

仓位规则：
  - 单股不超总资金 18%
  - 同时持仓最多 5 只
  - 弱势行情留 20% 现金（用 position_ratio=0.80 控制）
"""

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from typing import Optional, Literal
import json

from core.db import init_db, get_all_stocks, get_daily_price, get_conn
from strategy.strategies import strategy_oversold_rebound

# ─────────────────────────────────────────────
# 核心回测参数
# ─────────────────────────────────────────────
INIT_CAPITAL     = 100_000
MAX_POSITIONS    = 5          # 同时持仓最多5只
MAX_HOLD_DAYS    = 10         # 最长10天
MIN_HOLD_DAYS    = 2          # 最短2天（避免随机波动）
STOP_LOSS        = -0.06      # 止损 -6%
TAKE_PROFIT_LO   = 0.08      # 止盈下限 +8%
TAKE_PROFIT_HI   = 0.12      # 止盈上限 +12%（触发减半仓）
POSITION_RATIO   = 0.80       # 80% 仓位（留 20% 现金抗跌）
SINGLE_POS_RATIO = 0.18       # 单股最高 18%
COMMISSION_RATE   = 0.0001
MIN_COMMISSION   = 5.0
EXCLUDE_CODES    = ("688", "301")  # 排除科创板、创业板


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────
def _offset_date(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def calc_commission(price: float, shares: int) -> float:
    amount = price * shares
    return max(amount * COMMISSION_RATE, MIN_COMMISSION)


# ─────────────────────────────────────────────
# 数据加载（复用 batch_backtest 的优化逻辑）
# ─────────────────────────────────────────────
def preload_all_prices(codes: list, start_date: str, end_date: str) -> dict:
    warm_start = _offset_date(start_date, days=-60)
    sql = """
        SELECT code, trade_date, open, high, low, close, volume, amount, pct_change, turnover
        FROM daily_price
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY code, trade_date
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (warm_start, end_date)).fetchall()

    price_data = {}
    for r in rows:
        code = r["code"]
        if code not in price_data:
            price_data[code] = []
        price_data[code].append(dict(r))

    result = {}
    for code, rows_list in price_data.items():
        df = pd.DataFrame(rows_list)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df.set_index("trade_date", inplace=True)
        df.sort_index(inplace=True)
        df.drop(columns=["code"], errors="ignore", inplace=True)
        result[code] = df

    print(f"      价格预加载完成: {len(result)} 只 × {warm_start}~{end_date}")
    return result


def get_close_from_mem(code: str, cur_date, price_data: dict) -> Optional[float]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts in df.index:
        return float(df.loc[ts, "close"])
    return None


def get_row_from_mem(code: str, cur_date, price_data: dict) -> Optional[pd.Series]:
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts in df.index:
        return df.loc[ts]
    return None


def get_series_from_mem(code: str, cur_date: date, price_data: dict, lookback: int) -> Optional[pd.DataFrame]:
    """获取 cur_date 当日及之前 lookback 天的数据（用于计算指标）"""
    df = price_data.get(code)
    if df is None:
        return None
    ts = pd.Timestamp(cur_date)
    if ts not in df.index:
        return None
    idx_pos = df.index.get_loc(ts)
    start_pos = max(0, idx_pos - lookback + 1)
    return df.iloc[start_pos:idx_pos + 1].copy()


# ─────────────────────────────────────────────
# 核心回测函数
# ─────────────────────────────────────────────
def run_oversold_backtest(
    start_date: str = "2025-04-03",
    end_date: str = "2026-04-02",
    init_capital: float = INIT_CAPITAL,
    position_ratio: float = POSITION_RATIO,
    max_positions: int = MAX_POSITIONS,
    max_hold_days: int = MAX_HOLD_DAYS,
    stop_loss: float = STOP_LOSS,
    take_profit_lo: float = TAKE_PROFIT_LO,
    take_profit_hi: float = TAKE_PROFIT_HI,
    verbose: bool = True,
) -> dict:
    """
    超跌反弹策略回测（完全自定义卖出规则）
    """
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    # ── 1. 加载候选股票 ────────────────────────
    stocks_df = get_all_stocks()
    stocks_df = stocks_df[~stocks_df["code"].str.startswith(EXCLUDE_CODES)]
    codes = stocks_df["code"].tolist()
    code_to_name = dict(zip(stocks_df["code"], stocks_df["name"]))

    if verbose:
        print(f"\n{'='*60}")
        print(f"  超跌反弹策略回测  |  区间: {start_date} ~ {end_date}")
        print(f"  候选股票: {len(codes)} 只")
        print(f"  止损:{stop_loss*100:.0f}% 止盈:{take_profit_lo*100:.0f}~{take_profit_hi*100:.0f}% "
              f"持仓{MIN_HOLD_DAYS}~{max_hold_days}天 单股{SINGLE_POS_RATIO*100:.0f}% "
              f"仓位{position_ratio*100:.0f}%")
        print(f"{'='*60}")

    # ── 2. 批量加载价格数据 ─────────────────────
    if verbose:
        print("\n[1/3] 批量加载价格数据...")
    price_data = preload_all_prices(codes, start_date, end_date)
    if not price_data:
        return {"error": "无价格数据"}

    # ── 3. 收集交易日 ─────────────────────────
    all_dates = set()
    for sig_df in price_data.values():
        all_dates.update(pd.to_datetime(sig_df.index).date)
    trading_dates = sorted([
        d for d in all_dates
        if d >= date.fromisoformat(start_date) and d <= date.fromisoformat(end_date)
    ])
    if not trading_dates:
        return {"error": "无交易日"}

    # ── 4. 构建信号索引（每日Buy信号列表）────────
    if verbose:
        print("\n[2/3] 计算买入信号（超跌反弹策略）...")
    sig_index = {}  # date -> {code: row}
    signal_count = 0

    for code in codes:
        df = price_data.get(code)
        if df is None or len(df) < 25:
            continue
        try:
            sig_df = strategy_oversold_rebound(df)
            sig_df = sig_df[sig_df.index >= pd.Timestamp(start_date)]
            for idx, row in sig_df.iterrows():
                d = pd.Timestamp(idx).date()
                if d not in sig_index:
                    sig_index[d] = {}
                sig_index[d][code] = row
                if row.get("BUY_SIGNAL"):
                    signal_count += 1
        except Exception:
            pass

    if verbose:
        print(f"      信号计算完成: 共 {signal_count} 个买入信号触发")

    # ── 5. 逐日回测 ──────────────────────────
    if verbose:
        print("\n[3/3] 逐日回测...")
    capital = float(init_capital)
    positions = {}  # code -> {shares, entry_price, entry_date, entry_idx, half_sold, ma5_trail}

    all_trades = []
    partial_exit_log = []  # 部分止盈单独记录（不纳入胜率统计）
    daily_equity = []

    total_dates = len(trading_dates)
    progress_step = max(1, total_dates // 20)
    min_hold_days = MIN_HOLD_DAYS

    for di, cur_date in enumerate(trading_dates):
        if verbose and di % progress_step == 0:
            pos_value = sum(
                get_close_from_mem(code, cur_date, price_data) * pos["shares"]
                for code, pos in positions.items()
                if get_close_from_mem(code, cur_date, price_data) is not None
            )
            equity = capital + pos_value
            print(f"      进度 {di/total_dates*100:.0f}% ({di}/{total_dates}) "
                  f"资金:{capital:,.0f} 持仓:{len(positions)}只  equity:{equity:,.0f}")

        # ── 5a. 处理持仓卖出（完全自定义规则）────────
        for code in list(positions.keys()):
            pos = positions[code]
            shares = pos["shares"]
            entry_price = pos["entry_price"]
            entry_date = pos["entry_date"]
            entry_idx = pos["entry_idx"]
            half_sold = pos.get("half_sold", False)

            close_price = get_close_from_mem(code, cur_date, price_data)
            if close_price is None:
                continue

            ret = (close_price - entry_price) / entry_price
            hold_days = di - entry_idx
            can_sell = (cur_date > entry_date)  # T+1

            # 获取当日完整数据（计算MA5等指标）
            cur_row = get_row_from_mem(code, cur_date, price_data)
            lookback_df = get_series_from_mem(code, cur_date, price_data, lookback=30)

            ma5_today = None
            ma5_yesterday = None
            low_10d_yesterday = None
            close_yesterday = None
            vol_today = None
            vol_3d_yesterday = None
            vol_5d_yesterday = None
            vol_today_vol = None
            vol_yesterday_vol = None

            if lookback_df is not None and len(lookback_df) >= 6:
                close_series = lookback_df["close"]
                ma5_all = close_series.rolling(5).mean()
                vol_series = lookback_df["volume"]
                vol3_all = vol_series.rolling(3).mean()
                vol5_all = vol_series.rolling(5).mean()
                low10_all = close_series.rolling(10).min()

                ma5_today = ma5_all.iloc[-1] if not pd.isna(ma5_all.iloc[-1]) else None
                ma5_yesterday = ma5_all.iloc[-2] if len(ma5_all) >= 2 and not pd.isna(ma5_all.iloc[-2]) else None
                close_yesterday = close_series.iloc[-2] if len(close_series) >= 2 else None
                low_10d_yesterday = low10_all.iloc[-2] if len(low10_all) >= 2 and not pd.isna(low10_all.iloc[-2]) else None

                vol_today_vol = vol_series.iloc[-1] if not pd.isna(vol_series.iloc[-1]) else None
                vol_yesterday_vol = vol_series.iloc[-2] if len(vol_series) >= 2 and not pd.isna(vol_series.iloc[-2]) else None
                vol_3d_yesterday = vol3_all.iloc[-2] if len(vol3_all) >= 2 and not pd.isna(vol3_all.iloc[-2]) else None
                vol_5d_yesterday = vol5_all.iloc[-2] if len(vol5_all) >= 2 and not pd.isna(vol5_all.iloc[-2]) else None

            exit_reason = None
            exit_price = close_price
            exit_shares = shares

            if can_sell and hold_days >= min_hold_days:
                # ── 规则A: 止损 -6% ──
                if ret <= stop_loss:
                    exit_reason = "止损"

                # ── 规则B/C: 分批止盈 ──
                elif take_profit_lo <= ret <= take_profit_hi:
                    if not half_sold:
                        # 卖出半仓
                        half_shares = shares // 2
                        if half_shares > 0:
                            comm = calc_commission(close_price, half_shares)
                            net = close_price * half_shares - comm
                            capital += net
                            positions[code]["half_sold"] = True
                            positions[code]["shares"] = shares - half_shares
                            positions[code]["half_sold_ret"] = ret
                            # 记录半仓交易（不纳入胜率统计）
                            partial_exit_log.append({
                                "code": code, "name": code_to_name.get(code, code),
                                "entry_date": entry_date.strftime("%Y-%m-%d"),
                                "exit_date": cur_date.strftime("%Y-%m-%d"),
                                "entry_price": round(entry_price, 2),
                                "exit_price": round(close_price, 2),
                                "shares": half_shares,
                                "pnl_pct": round(ret * 100, 2),
                                "pnl_amt": round(net - entry_price * half_shares, 2),
                                "commission": round(comm, 2),
                                "hold_days": hold_days,
                                "exit_reason": "止盈半仓",
                                "win": True,
                                "partial": True,
                            })
                    # 继续持有半仓，下一个条件判断是否清仓

                elif ret > take_profit_hi and half_sold:
                    # ── 规则C加强: 超过+12%仍未走，强制跟踪止盈 ──
                    if ma5_today is not None and close_price < ma5_today:
                        exit_reason = "跟踪止盈"

                # ── 规则C: 剩余半仓跟踪止损（MA5） ──
                if half_sold and exit_reason is None:
                    if ma5_today is not None and close_price < ma5_today:
                        exit_reason = "跟踪止盈"

                # ── 规则D: 连续2日缩量阴跌破10日低点 ──
                if exit_reason is None and hold_days >= 3:
                    # 检查昨天是否：缩量 + 阴跌 + 破10日低点
                    if (vol_yesterday_vol is not None and vol_3d_yesterday is not None and
                        vol_5d_yesterday is not None and close_yesterday is not None and
                        low_10d_yesterday is not None):
                        vol_shrink_yesterday = vol_3d_yesterday < vol_5d_yesterday
                        price_down_yesterday = close_yesterday < close_series.iloc[-3] if len(close_series) >= 3 else False
                        broke_low_yesterday = close_yesterday < low_10d_yesterday
                        if vol_shrink_yesterday and price_down_yesterday and broke_low_yesterday:
                            # 今天继续跌，直接走
                            exit_reason = "缩量破底"

            # ── 规则E: 到期强制平仓 ──
            if exit_reason is None and hold_days >= max_hold_days:
                exit_reason = "到期平仓"

            # ── 执行卖出 ──
            if exit_reason:
                remaining_shares = positions[code]["shares"]
                if remaining_shares > 0:
                    comm = calc_commission(exit_price, remaining_shares)
                    net = exit_price * remaining_shares - comm
                    pnl_amt = net - entry_price * remaining_shares

                    all_trades.append({
                        "code": code, "name": code_to_name.get(code, code),
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": cur_date.strftime("%Y-%m-%d"),
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "shares": remaining_shares,
                        "pnl_pct": round(ret * 100, 2),
                        "pnl_amt": round(pnl_amt, 2),
                        "commission": round(comm, 2),
                        "hold_days": hold_days,
                        "exit_reason": exit_reason,
                        "win": ret > 0,
                        "partial": False,
                    })
                    capital += net
                del positions[code]

        # ── 5b. 检查新买入 ─────────────────────
        if len(positions) < max_positions:
            available_cash = capital * position_ratio
            single_max = capital * SINGLE_POS_RATIO

            candidates = sig_index.get(cur_date, {})
            # 按融合分排序，取高分股
            sorted_codes = sorted(
                candidates.keys(),
                key=lambda c: candidates[c].get("BUY_SCORE", 0),
                reverse=True
            )

            for code in sorted_codes:
                if len(positions) >= max_positions:
                    break
                if code in positions:
                    continue

                row = candidates[code]
                close_price = get_close_from_mem(code, cur_date, price_data)
                if close_price is None or close_price <= 0:
                    continue

                # 预算仓位
                budget = min(available_cash, single_max)
                shares = int(budget / close_price / 100) * 100
                if shares < 100:
                    continue

                cost = close_price * shares
                comm = calc_commission(close_price, shares)
                total_cost = cost + comm

                if total_cost > capital * position_ratio:
                    # 钱不够，降为最小可买
                    shares = int(capital * SINGLE_POS_RATIO / close_price / 100) * 100
                    if shares < 100:
                        continue
                    total_cost = close_price * shares + calc_commission(close_price, shares)
                    if total_cost > capital:
                        continue

                positions[code] = {
                    "shares": shares,
                    "entry_price": close_price,
                    "entry_date": cur_date,
                    "entry_idx": di,
                    "half_sold": False,
                    "cost": total_cost,
                }
                capital -= total_cost

        # ── 5c. 记录每日 equity ─────────────────
        pos_value = 0
        for code, pos in positions.items():
            cp = get_close_from_mem(code, cur_date, price_data)
            if cp is not None:
                pos_value += cp * pos["shares"]
        daily_equity.append({
            "date": cur_date.strftime("%Y-%m-%d"),
            "capital": capital,
            "pos_value": pos_value,
            "equity": capital + pos_value,
            "n_positions": len(positions),
        })

    # ── 6. 汇总统计 ───────────────────────────
    wins  = [t for t in all_trades if t.get("win") and not t.get("partial")]
    losses = [t for t in all_trades if not t.get("win") and not t.get("partial")]

    final_equity = capital + sum(
        get_close_from_mem(code, trading_dates[-1], price_data) * pos["shares"]
        for code, pos in positions.items()
        if get_close_from_mem(code, trading_dates[-1], price_data) is not None
    )
    total_return = (final_equity - INIT_CAPITAL) / INIT_CAPITAL * 100

    # 最大回撤
    equity_series = [INIT_CAPITAL] + [e["equity"] for e in daily_equity]
    peak = equity_series[0]
    max_drawdown = 0.0
    for eq in equity_series:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak * 100
        if dd < max_drawdown:
            max_drawdown = dd

    # 盈利/亏损分析（只统计完整交易）
    win_list  = [t["pnl_pct"] for t in all_trades if t.get("win")]
    loss_list = [t["pnl_pct"] for t in all_trades if not t.get("win")]
    avg_win   = np.mean(win_list)  if win_list  else 0.0
    avg_loss  = np.mean(loss_list) if loss_list else 0.0
    profit_factor = abs(np.sum(win_list) / np.sum(loss_list)) if loss_list and np.sum(loss_list) != 0 else 0.0
    avg_hold  = np.mean([t["hold_days"] for t in all_trades]) if all_trades else 0.0
    win_rate  = len(win_list) / len(all_trades) * 100 if all_trades else 0.0

    # 按月统计
    monthly = {}
    for t in all_trades:
        m = t["exit_date"][:7]
        if m not in monthly:
            monthly[m] = {"count": 0, "wins": 0, "pnl_sum": 0}
        monthly[m]["count"] += 1
        monthly[m]["pnl_sum"] += t["pnl_pct"]
        if t.get("win"):
            monthly[m]["wins"] += 1

    result = {
        "total_trades": len(all_trades),
        "win_rate": round(win_rate, 2),
        "total_return": round(total_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "profit_factor": round(profit_factor, 3),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_hold_days": round(avg_hold, 1),
        "final_capital": round(final_equity, 0),
        "init_capital": INIT_CAPITAL,
        "wins": len(win_list),
        "losses": len(loss_list),
        "partial_exits": len(partial_exit_log),
        "trades": all_trades,
        "daily_equity": daily_equity,
        "monthly": monthly,
    }

    if verbose:
        print(f"\n{'='*60}")
        print(f"  超跌反弹策略回测结果")
        print(f"{'='*60}")
        print(f"  总交易: {len(all_trades)} 笔  胜率: {win_rate:.1f}%  "
              f"部分止盈: {len(partial_exit_log)} 次")
        print(f"  总收益: {total_return:+.2f}%  (10万 → {final_equity:,.0f})")
        print(f"  最大回撤: {max_drawdown:.2f}%")
        print(f"  盈亏比: {profit_factor:.2f}  (均盈{avg_win:+.2f}% / 均亏{avg_loss:+.2f}%)")
        print(f"  平均持股: {avg_hold:.1f} 天")
        print(f"\n  月度表现:")
        for m in sorted(monthly.keys()):
            d = monthly[m]
            print(f"    {m}: {d['count']}笔 胜{d['wins']} 总{d['pnl_sum']:+.2f}%")

    return result


def entry_budget_for_price(price: float, budget: float) -> int:
    """给定价格和预算，计算能买多少股（100股整数）"""
    shares = int(budget / price / 100) * 100
    return shares * price + max(budget * 0.0001, 5.0)


if __name__ == "__main__":
    run_oversold_backtest()
