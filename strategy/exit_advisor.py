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
