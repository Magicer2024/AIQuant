"""
strategy/exit_advisor.py —— 推荐出场建议引擎
=============================================

复用 backtest/oversold_sim.py 中验证有效的 v3 出场纪律，
对已推荐且仍"活跃"的票（推荐日后 N 个交易日内）逐日评估出场状态。

出场规则（按优先级）：
  1. 硬止损：close <= entry * (1 + stop_loss_pct)，默认 -6%  → 清仓
  2. 浮盈减半：浮盈 >= +10% 且未执行过            → 减仓50%
  3. 移动止盈：自持仓最高点回撤 >= trailing_pct    → 清仓
  4. 破MA5：已减半后 close < MA5                  → 清仓
  5. 超期：持仓 > max_hold_days 交易日            → 清仓
  6. 正常：以上均未触发                           → 持有

供 routes/investor.py 的 /api/investor/exit_advice 和 /api/investor/today 调用。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from config.strategy_params import OVERSOLD_REBOUND_V4


# 默认参数（与 v3 回测一致）
DEFAULT_STOP_LOSS = OVERSOLD_REBOUND_V4.get("stop_loss", -0.06)
DEFAULT_PARTIAL_TP = 0.10       # 浮盈 +10% 减半
DEFAULT_TRAILING_PCT = OVERSOLD_REBOUND_V4.get("trailing_pct", 0.10)
DEFAULT_MAX_HOLD_DAYS = 10


def evaluate_exit(
    entry_price: float,
    entry_date: str,
    df: pd.DataFrame,
    *,
    stop_loss_pct: float = DEFAULT_STOP_LOSS,
    partial_tp: float = DEFAULT_PARTIAL_TP,
    trailing_pct: float = DEFAULT_TRAILING_PCT,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
) -> dict:
    """评估单只推荐票的出场状态。

    Args:
        entry_price: 推荐买入价
        entry_date: 推荐日期 (YYYY-MM-DD)
        df: 该股的日线 DataFrame（含 close/high/low 列，日期索引升序）
        stop_loss_pct: 硬止损比例（负数，如 -0.06）
        partial_tp: 浮盈减半阈值（如 0.10 = +10%）
        trailing_pct: 移动止盈回撤比例（如 0.10 = 10%）
        max_hold_days: 最长持仓交易日数

    Returns:
        dict: {
            status: "hold" | "reduce" | "clear",
            reason: str,
            detail: {
                hold_days, highest_since_entry, current_price,
                current_pnl_pct, drawdown_from_high, ma5,
                partial_done, stop_price, trailing_stop_price
            }
        }
    """
    if df is None or df.empty or entry_price <= 0:
        return {
            "status": "hold",
            "reason": "数据不足，暂无法评估",
            "detail": {},
        }

    # 确保索引是字符串日期或可比较
    if hasattr(df.index, 'strftime'):
        # DatetimeIndex
        mask = df.index > pd.Timestamp(entry_date)
        after = df[mask]
    else:
        # 字符串索引
        after = df[df.index > entry_date]

    if after.empty:
        return {
            "status": "hold",
            "reason": "推荐后尚无新行情",
            "detail": {"hold_days": 0, "current_price": entry_price},
        }

    hold_days = len(after)
    current_price = float(after.iloc[-1]["close"])
    current_pnl = (current_price - entry_price) / entry_price

    # 持仓期最高价（用 high 列更精确）
    if "high" in after.columns:
        highest = float(after["high"].max())
    else:
        highest = float(after["close"].max())

    drawdown_from_high = (highest - current_price) / highest if highest > 0 else 0

    # MA5
    ma5 = None
    if len(df) >= 5:
        ma5 = float(df["close"].iloc[-5:].mean())

    # 硬止损价
    stop_price = entry_price * (1 + stop_loss_pct)

    # 移动止盈触发价
    trailing_stop_price = highest * (1 - trailing_pct)

    # 判断是否已触发过减半（浮盈曾达到 partial_tp）
    if "high" in after.columns:
        max_pnl = (float(after["high"].max()) - entry_price) / entry_price
    else:
        max_pnl = (float(after["close"].max()) - entry_price) / entry_price
    partial_done = max_pnl >= partial_tp

    # ── 按优先级逐条判定 ──

    # 1. 硬止损
    if "low" in after.columns:
        min_low = float(after["low"].min())
        if min_low <= stop_price:
            return {
                "status": "clear",
                "reason": f"触发硬止损（止损价 {stop_price:.2f}）",
                "detail": _detail(hold_days, highest, current_price, current_pnl,
                                  drawdown_from_high, ma5, partial_done,
                                  stop_price, trailing_stop_price),
            }
    elif current_price <= stop_price:
        return {
            "status": "clear",
            "reason": f"触发硬止损（止损价 {stop_price:.2f}）",
            "detail": _detail(hold_days, highest, current_price, current_pnl,
                              drawdown_from_high, ma5, partial_done,
                              stop_price, trailing_stop_price),
        }

    # 2. 浮盈减半（当前浮盈 >= partial_tp 且之前未触发过）
    # 注意：这里判断的是"当前是否应该减仓"，如果当前浮盈首次达到阈值
    if current_pnl >= partial_tp and not _was_partial_done_before(after, entry_price, partial_tp):
        return {
            "status": "reduce",
            "reason": f"浮盈 +{current_pnl*100:.1f}% 达到减半线（+{partial_tp*100:.0f}%），建议减仓50%",
            "detail": _detail(hold_days, highest, current_price, current_pnl,
                              drawdown_from_high, ma5, False,
                              stop_price, trailing_stop_price),
        }

    # 3. 移动止盈（自最高点回撤 >= trailing_pct）
    if partial_done and drawdown_from_high >= trailing_pct:
        return {
            "status": "clear",
            "reason": f"自高点回撤 {drawdown_from_high*100:.1f}%（阈值 {trailing_pct*100:.0f}%），移动止盈清仓",
            "detail": _detail(hold_days, highest, current_price, current_pnl,
                              drawdown_from_high, ma5, partial_done,
                              stop_price, trailing_stop_price),
        }

    # 4. 破MA5（已减半后 close < MA5）
    if partial_done and ma5 is not None and current_price < ma5:
        return {
            "status": "clear",
            "reason": f"已减半后跌破MA5（MA5={ma5:.2f}，现价={current_price:.2f}）",
            "detail": _detail(hold_days, highest, current_price, current_pnl,
                              drawdown_from_high, ma5, partial_done,
                              stop_price, trailing_stop_price),
        }

    # 5. 超期
    if hold_days >= max_hold_days:
        return {
            "status": "clear",
            "reason": f"持仓 {hold_days} 个交易日，超过最长持仓期 {max_hold_days} 天",
            "detail": _detail(hold_days, highest, current_price, current_pnl,
                              drawdown_from_high, ma5, partial_done,
                              stop_price, trailing_stop_price),
        }

    # 6. 正常持有
    reason = "持有"
    if current_pnl >= 0:
        reason = f"持有（浮盈 +{current_pnl*100:.1f}%）"
    else:
        reason = f"持有（浮亏 {current_pnl*100:.1f}%，未触及止损）"

    return {
        "status": "hold",
        "reason": reason,
        "detail": _detail(hold_days, highest, current_price, current_pnl,
                          drawdown_from_high, ma5, partial_done,
                          stop_price, trailing_stop_price),
    }


# 三周期持仓上限：短线 1 个交易日（买入次日了结，T+1 制度下最早可卖日；
# 对应 TUNABLE_PARAMS.short_max_hold_days 默认值，改参数需同步本处）
# / 中线 60 个交易日 / 长线不限（None）
HORIZON_MAX_HOLD: dict = {"short": 1, "mid": 60, "long": None}


def get_max_hold(horizon: str) -> Optional[int]:
    """持仓上限：短线优先读 TUNABLE_PARAMS.short_max_hold_days（DB 可覆盖，60s TTL），
    读取失败/非短线回退 HORIZON_MAX_HOLD 常量（单一事实来源，避免双源漂移）。"""
    if horizon == "short":
        try:
            from config.strategy_params import get_param
            return int(get_param("short_max_hold_days"))
        except Exception:
            pass
    return HORIZON_MAX_HOLD.get(horizon)


def evaluate_exit_by_prices(
    entry_price: float,
    entry_date: str,
    df: pd.DataFrame,
    *,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    max_hold_days: Optional[int] = None,
) -> dict:
    """按推荐自带止损/止盈价逐日模拟出场状态（出场跟踪新口径）。

    口径：
      - 推荐日(entry_date)起持有，买入价 = 推荐日后首个交易日开盘价(entry_price)
      - A股 T+1：买入日当天不可卖出（只计入持仓），从买入日后第 2 个交易日开始逐日检查
      - 卖出判定统一用当日收盘价（不做盘中插针触发），触发按收盘价卖出：
          1. 收盘价跌破止损价       → 卖出（止损）
          2. 收盘价达到止盈价       → 卖出（止盈）
          3. 持仓达到 max_hold_days → 卖出（到期；None 表示不限）
          4. 均未触发               → 持续持有
      - 已卖出返回 status='clear'，仍持有返回 status='hold'

    Args:
        entry_price: 买入价（推荐日后首个交易日开盘价）
        entry_date: 推荐日期 (YYYY-MM-DD)
        df: 该股日线 DataFrame（含 open/high/low/close 列，日期索引升序）
        stop_loss: 绝对止损价（推荐自带），None 则不检查
        take_profit: 绝对止盈价（推荐自带），None 则不检查
        max_hold_days: 最长持仓交易日数，None 表示不限

    Returns:
        dict: {
            status: "hold" | "clear",
            reason: str,
            detail: {
                entry_date, entry_price, hold_days, current_price,
                current_pnl_pct, highest_since_entry, stop_loss, take_profit,
                exit_date, exit_price, exit_reason
            }
        }
    """
    if df is None or df.empty or entry_price <= 0:
        return {
            "status": "hold",
            "reason": "数据不足，暂无法评估",
            "detail": {"entry_price": round(entry_price, 2) if entry_price else None},
        }

    # 防御：非法止损/止盈价（<=0 或止盈低于止损）视为未设置，避免历史脏数据误触发
    if stop_loss is not None and stop_loss <= 0:
        stop_loss = None
    if take_profit is not None and take_profit <= 0:
        take_profit = None
    if stop_loss is not None and take_profit is not None and take_profit <= stop_loss:
        take_profit = None

    # 推荐日之后的交易日（含买入日）
    if hasattr(df.index, 'strftime'):
        after = df[df.index > pd.Timestamp(entry_date)]
    else:
        after = df[df.index > entry_date]

    if after.empty:
        return {
            "status": "hold",
            "reason": "推荐后尚无交易日，等待买入",
            "detail": {
                "entry_date": None,
                "entry_price": round(entry_price, 2),
                "hold_days": 0,
                "current_price": round(entry_price, 2),
                "current_pnl_pct": 0.0,
                "highest_since_entry": None,
                "stop_loss": round(stop_loss, 2) if stop_loss else None,
                "take_profit": round(take_profit, 2) if take_profit else None,
                "exit_date": None,
                "exit_price": None,
                "exit_reason": None,
            },
        }

    # ── 逐日模拟（A股 T+1：买入日当天不可卖出，最早次日才能出场） ──
    # 卖出判定统一用当日收盘价（不做盘中插针触发）：收盘价破止损/达止盈按收盘价卖出
    hold_days = 0
    highest = None
    exit_date = None
    exit_price = None
    exit_reason = None
    for i, (ts, row) in enumerate(after.iterrows(), start=1):
        close = float(row["close"])
        high = float(row["high"]) if "high" in after.columns and pd.notna(row.get("high")) else close
        if highest is None or high > highest:
            highest = high
        hold_days = i

        # T+1 规则：买入日（第 1 个交易日）当天买入不可卖出，只累计持仓，不检查出场
        if i == 1:
            continue

        # 1. 止损：收盘价跌破止损价 → 按收盘价卖出
        if stop_loss is not None and close <= stop_loss:
            exit_date, exit_price, exit_reason = ts, close, f"收盘跌破止损价 {stop_loss:.2f}"
            break
        # 2. 止盈：收盘价达到止盈价 → 按收盘价卖出
        if take_profit is not None and close >= take_profit:
            exit_date, exit_price, exit_reason = ts, close, f"收盘达到止盈价 {take_profit:.2f}"
            break
        # 3. 超期（到期卖出）
        if max_hold_days is not None and i >= max_hold_days:
            exit_date, exit_price, exit_reason = ts, close, f"持仓满 {i} 个交易日，到期卖出"
            break

    entry_date_str = str(after.index[0])[:10]
    if exit_date is not None:
        current_price = float(exit_price)
        current_pnl = (current_price - entry_price) / entry_price
        exit_date_str = str(exit_date)[:10]
        return {
            "status": "clear",
            "reason": f"已卖出（{exit_reason}）",
            "detail": {
                "entry_date": entry_date_str,
                "entry_price": round(entry_price, 2),
                "hold_days": hold_days,
                "current_price": round(current_price, 2),
                "current_pnl_pct": round(current_pnl * 100, 2),
                "highest_since_entry": round(highest, 2) if highest else None,
                "stop_loss": round(stop_loss, 2) if stop_loss else None,
                "take_profit": round(take_profit, 2) if take_profit else None,
                "exit_date": exit_date_str,
                "exit_price": round(current_price, 2),
                "exit_reason": exit_reason,
            },
        }

    # 仍在持有
    current_price = float(after.iloc[-1]["close"])
    current_pnl = (current_price - entry_price) / entry_price
    if current_pnl >= 0:
        reason = f"持有（浮盈 +{current_pnl*100:.1f}%）"
    else:
        reason = f"持有（浮亏 {current_pnl*100:.1f}%，未触及止损）"
    return {
        "status": "hold",
        "reason": reason,
        "detail": {
            "entry_date": entry_date_str,
            "entry_price": round(entry_price, 2),
            "hold_days": hold_days,
            "current_price": round(current_price, 2),
            "current_pnl_pct": round(current_pnl * 100, 2),
            "highest_since_entry": round(highest, 2) if highest else None,
            "stop_loss": round(stop_loss, 2) if stop_loss else None,
            "take_profit": round(take_profit, 2) if take_profit else None,
            "exit_date": None,
            "exit_price": None,
            "exit_reason": None,
        },
    }


def _was_partial_done_before(after: pd.DataFrame, entry_price: float, partial_tp: float) -> bool:
    """检查在今天之前是否已经触发过减半（即之前的交易日是否已达到 partial_tp）。

    用于区分"今天首次达到"和"之前已达到过"。
    """
    if len(after) <= 1:
        return False
    # 排除最后一天，看之前的最高价
    prev = after.iloc[:-1]
    if "high" in prev.columns:
        prev_max = float(prev["high"].max())
    else:
        prev_max = float(prev["close"].max())
    prev_pnl = (prev_max - entry_price) / entry_price
    return prev_pnl >= partial_tp


def _detail(hold_days, highest, current_price, current_pnl,
            drawdown_from_high, ma5, partial_done,
            stop_price, trailing_stop_price) -> dict:
    """构建 detail 字典。"""
    return {
        "hold_days": hold_days,
        "highest_since_entry": round(highest, 2) if highest else None,
        "current_price": round(current_price, 2) if current_price else None,
        "current_pnl_pct": round(current_pnl * 100, 2),
        "drawdown_from_high_pct": round(drawdown_from_high * 100, 2),
        "ma5": round(ma5, 2) if ma5 else None,
        "partial_done": partial_done,
        "stop_price": round(stop_price, 2) if stop_price else None,
        "trailing_stop_price": round(trailing_stop_price, 2) if trailing_stop_price else None,
    }
