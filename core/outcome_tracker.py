"""
core/outcome_tracker.py —— 推荐结果闭环追踪
=============================================

将 stock_signal 中的每条推荐持久化到 recommend_outcome 表，
并在后续交易日逐步填充 T+1/3/5/10 收益、最大浮盈/浮亏、
是否触发止损/止盈、出场原因等，形成完整的胜率闭环。

调用时机：
  - recalc_all_scores 末尾（best-effort）
  - 可独立调用：python -c "from core.outcome_tracker import run; run()"
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from core.db import get_conn


# ─────────────────────────────────────────────
# 1. 从 stock_signal 导入新推荐到 recommend_outcome
# ─────────────────────────────────────────────

def insert_new_outcomes(days_back: int = 60):
    """将 stock_signal 中近 N 天、尚未录入 recommend_outcome 的推荐写入。

    去重逻辑：UNIQUE(code, scan_date, horizon)，INSERT OR IGNORE。
    """
    cutoff = f"-{days_back} days"
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO recommend_outcome
                (code, scan_date, horizon, strategy, entry_price,
                 stop_loss, take_profit, fusion_score)
            SELECT
                s.code,
                s.scan_date,
                COALESCE(s.horizon, 'short'),
                s.strategy,
                s.buy_price,
                s.stop_loss,
                s.take_profit,
                s.fusion_score
            FROM stock_signal s
            WHERE s.scan_date >= date('now', ?)
              AND s.buy_price IS NOT NULL
              AND s.buy_price > 0
        """, (cutoff,))


# ─────────────────────────────────────────────
# 2. 评估未完成的推荐结果
# ─────────────────────────────────────────────

def evaluate_outcomes():
    """遍历 recommend_outcome 中尚未完全评估的记录，用 daily_price 填充收益。

    评估逻辑：
      - T+N return = (第N个交易日close - entry_price) / entry_price * 100
      - max_return / min_return = 推荐日后所有交易日的最高/最低收益
      - hit_stop / hit_tp = 是否触及止损/止盈价
      - exit_reason / exit_date / exit_return = 首次触发止损或止盈的记录
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        # 取尚未评估完的记录（t10_return 为 NULL 或 evaluated_at 为空）
        rows = conn.execute("""
            SELECT id, code, scan_date, entry_price, stop_loss, take_profit
            FROM recommend_outcome
            WHERE entry_price > 0
              AND (t10_return IS NULL OR evaluated_at IS NULL)
            ORDER BY scan_date ASC
        """).fetchall()

        if not rows:
            return 0

        updated = 0
        for row in rows:
            rid = row["id"]
            code = row["code"]
            scan_date = row["scan_date"]
            entry = row["entry_price"]
            stop = row["stop_loss"]
            tp = row["take_profit"]

            # 取推荐日之后的行情（按交易日升序）
            prices = conn.execute("""
                SELECT trade_date, close, high, low
                FROM daily_price
                WHERE code = ? AND trade_date > ?
                ORDER BY trade_date ASC
                LIMIT 15
            """, (code, scan_date)).fetchall()

            if not prices:
                continue

            # 计算 T+N 收益
            def _ret(n):
                if len(prices) >= n:
                    c = prices[n - 1]["close"]
                    if c and entry > 0:
                        return round((c - entry) / entry * 100, 2)
                return None

            t1 = _ret(1)
            t3 = _ret(3)
            t5 = _ret(5)
            t10 = _ret(10)

            # 最大浮盈/浮亏（用 high/low 更精确）
            max_ret = None
            min_ret = None
            for p in prices:
                if p["high"] and entry > 0:
                    r = (p["high"] - entry) / entry * 100
                    max_ret = r if max_ret is None else max(max_ret, r)
                if p["low"] and entry > 0:
                    r = (p["low"] - entry) / entry * 100
                    min_ret = r if min_ret is None else min(min_ret, r)

            if max_ret is not None:
                max_ret = round(max_ret, 2)
            if min_ret is not None:
                min_ret = round(min_ret, 2)

            # 止损/止盈判定
            hit_stop = 0
            hit_tp = 0
            exit_reason = None
            exit_date = None
            exit_return = None

            for p in prices:
                # 止损：当日最低价 <= stop_loss
                if stop and p["low"] and p["low"] <= stop:
                    hit_stop = 1
                    if exit_reason is None:
                        exit_reason = "stop_loss"
                        exit_date = p["trade_date"]
                        exit_return = round((stop - entry) / entry * 100, 2)
                    break
                # 止盈：当日最高价 >= take_profit
                if tp and p["high"] and p["high"] >= tp:
                    hit_tp = 1
                    if exit_reason is None:
                        exit_reason = "take_profit"
                        exit_date = p["trade_date"]
                        exit_return = round((tp - entry) / entry * 100, 2)
                    break

            # 如果持有期满（>=10 个交易日）且未触发止损止盈，用 T+10 收盘作为出场
            if exit_reason is None and len(prices) >= 10:
                exit_reason = "max_hold_days"
                exit_date = prices[9]["trade_date"]
                exit_return = t10

            conn.execute("""
                UPDATE recommend_outcome
                SET t1_return = ?, t3_return = ?, t5_return = ?, t10_return = ?,
                    max_return = ?, min_return = ?,
                    hit_stop = ?, hit_tp = ?,
                    exit_reason = ?, exit_date = ?, exit_return = ?,
                    evaluated_at = ?
                WHERE id = ?
            """, (t1, t3, t5, t10, max_ret, min_ret,
                  hit_stop, hit_tp, exit_reason, exit_date, exit_return,
                  now_str, rid))
            updated += 1

    return updated


# ─────────────────────────────────────────────
# 3. 汇总统计
# ─────────────────────────────────────────────

def get_summary(days: int = 30) -> dict:
    """返回近 N 日推荐的胜率/平均收益/盈亏比等汇总。

    以 t5_return 作为主要评判标准（短线 5 个交易日）。
    若 t5_return 为空则用 t3 或 t1 代替。
    """
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT code, scan_date, horizon, strategy, entry_price,
                   fusion_score, t1_return, t3_return, t5_return, t10_return,
                   max_return, min_return, hit_stop, hit_tp,
                   exit_reason, exit_return
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?)
              AND entry_price > 0
            ORDER BY scan_date DESC
        """, (cutoff,)).fetchall()

    if not rows:
        return {"total": 0, "win_rate": 0, "avg_return": 0,
                "profit_factor": 0, "best_return": 0, "worst_return": 0,
                "stop_loss_count": 0, "take_profit_count": 0}

    # 用 exit_return 作为最终收益（若有），否则用 t5 > t3 > t1
    returns = []
    for r in rows:
        ret = r["exit_return"]
        if ret is None:
            ret = r["t5_return"]
        if ret is None:
            ret = r["t3_return"]
        if ret is None:
            ret = r["t1_return"]
        if ret is not None:
            returns.append(ret)

    if not returns:
        return {"total": len(rows), "win_rate": 0, "avg_return": 0,
                "profit_factor": 0, "best_return": 0, "worst_return": 0,
                "stop_loss_count": 0, "take_profit_count": 0}

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_return = sum(returns) / len(returns)
    win_rate = len(wins) / len(returns) * 100

    # 盈亏比 = 平均盈利 / 平均亏损绝对值
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else 99.0

    stop_count = sum(1 for r in rows if r["hit_stop"])
    tp_count = sum(1 for r in rows if r["hit_tp"])

    return {
        "total": len(returns),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 2),
        "profit_factor": profit_factor,
        "best_return": round(max(returns), 2),
        "worst_return": round(min(returns), 2),
        "stop_loss_count": stop_count,
        "take_profit_count": tp_count,
    }


# ─────────────────────────────────────────────
# 4. 获取明细列表（供 API 使用）
# ─────────────────────────────────────────────

def get_outcome_list(days: int = 30, limit: int = 200) -> list:
    """返回近 N 日推荐结果明细列表。"""
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT code, scan_date, horizon, strategy, entry_price,
                   stop_loss, take_profit, fusion_score,
                   t1_return, t3_return, t5_return, t10_return,
                   max_return, min_return, hit_stop, hit_tp,
                   exit_reason, exit_date, exit_return, evaluated_at
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?)
              AND entry_price > 0
            ORDER BY scan_date DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 5. 一键运行入口
# ─────────────────────────────────────────────

def run():
    """完整执行：导入新推荐 + 评估结果。"""
    t0 = time.time()
    insert_new_outcomes()
    updated = evaluate_outcomes()
    elapsed = time.time() - t0
    print(f"[outcome_tracker] 评估完成: {updated} 条更新，耗时 {elapsed:.1f}s")
    return updated
