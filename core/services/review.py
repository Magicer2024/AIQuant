"""
core/services/review.py —— 推荐复盘闭环编排（T7 从 routes/investor.py 下沉）
=========================================================================

- outcome_summary / outcome_list：直接读 recommend_outcome 持久化复盘
- recommendations_history：优先读合并口径复盘表，异常/无数据时回退实时计算
  （短线 + 连续推荐段合并 + 主板过滤，与今日推荐/出场跟踪同口径）
"""
from __future__ import annotations

from config.personal_config import main_board_filter
from config.strategy_params import get_param
from core.db import get_conn
from utils.serialization import sanitize_numeric


def outcome_summary_payload(days: int) -> dict:
    """推荐胜率闭环汇总（recommend_outcome 表）。"""
    from core.outcome_tracker import get_summary
    return {"summary": get_summary(days), "days": days}


def outcome_list_payload(days: int, limit: int) -> dict:
    """推荐结果明细列表。"""
    from core.outcome_tracker import get_outcome_list
    items = get_outcome_list(days, limit)
    return sanitize_numeric({"items": items, "days": days, "total": len(items)})


def recommendations_history_payload(days: int) -> dict:
    """过去 N 天短线推荐的实际表现（默认 60，上限 90）。

    口径：仅统计短线（horizon=short）；同一股票连续交易日不间断被推荐时合并为
    一段，只显示首次推荐日并给出连续推荐天数。优先读 recommend_outcome，
    读不到再实时计算（两段口径一致）。
    """
    # 优先尝试从 recommend_outcome 读取
    try:
        payload = _history_from_outcome_table(days)
        if payload is not None:
            return payload
    except Exception:
        pass  # fallback 到实时计算

    return _history_realtime(days)


def _history_from_outcome_table(days: int):
    """读合并口径复盘表；有数据返回 payload，无数据返回 None（触发实时回退）。"""
    from core.outcome_tracker import get_merged_outcome_list, get_merged_summary
    items = get_merged_outcome_list(days, "short")
    if not items:
        return None
    summary = get_merged_summary(days, "short")
    # 转换为前端兼容格式
    results = []
    for it in items:
        ret = (it.get("exit_return") or it.get("t5_return")
               or it.get("t3_return") or it.get("t1_return") or 0)
        results.append({
            "code": it["code"],
            "name": it.get("name"),  # get_merged_outcome_list 已 JOIN stock_info 补股名
            "scan_date": it["scan_date"],          # 兼容字段 = 首次推荐日
            "first_scan_date": it["first_scan_date"],
            "last_scan_date": it["last_scan_date"],
            "streak_days": it["streak_days"],       # 连续推荐天数
            "entry_price": round(it["entry_price"], 2) if it.get("entry_price") else None,
            "current_price": None,
            "pnl_pct": ret,
            "hit_stop_loss": bool(it.get("hit_stop")),
            "hit_take_profit": bool(it.get("hit_tp")),
            "fusion_score": round(it.get("fusion_score") or 0, 1),
            "t1_return": it.get("t1_return"),
            "t2_return": it.get("t2_return"),
            "t3_return": it.get("t3_return"),
            "t5_return": it.get("t5_return"),
            "exit_reason": it.get("exit_reason"),
            "days_held": None,
        })
    # 按首次推荐日降序（同日按收益降序），复盘列表以时间线为主
    results.sort(key=lambda x: (x["first_scan_date"], x["pnl_pct"]), reverse=True)
    return {
        "items": sanitize_numeric(results),
        "summary": summary,
        "days": days,
        "source": "recommend_outcome",
        "merged": True,
    }


def _history_realtime(days: int) -> dict:
    """实时计算回退路径：短线 + 连续推荐合并 + 主板过滤（与复盘表同口径）。"""
    from core.outcome_tracker import _merge_continuous_segments
    from datetime import datetime as _dt

    board_filter = main_board_filter("s.code")

    with get_conn() as conn:
        # 最近 N 天的短线推荐（每日取 fusion_score 前 short_top_n，与今日推荐同口径）
        signals = conn.execute(
            f"""
            SELECT code, name, scan_date, buy_price, stop_loss, take_profit, fusion_score
            FROM (
                SELECT s.code, s.name, s.scan_date, s.buy_price, s.stop_loss, s.take_profit,
                       s.fusion_score,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.scan_date
                           ORDER BY COALESCE(s.fusion_score, 0) DESC
                       ) AS rn
                FROM stock_signal s
                WHERE s.scan_date >= date('now', ?)
                  AND s.buy_price IS NOT NULL
                  AND COALESCE(s.horizon, 'short') = 'short'
                  {board_filter}
            )
            WHERE rn <= ?
            ORDER BY code ASC, scan_date ASC
            """,
            (f"-{days} days", max(1, int(get_param("short_top_n")))),
        ).fetchall()

        if not signals:
            return {"items": [], "summary": {}, "days": days}

        # 连续交易日不间断的推荐合并为一段（每段取首次推荐日 + 连续天数）
        segments = _merge_continuous_segments(signals, conn)

        results = []
        for seg in segments:
            code = seg["code"]
            entry = seg.get("buy_price") or 0
            if entry <= 0:
                continue

            # 段首推荐日之后最多 5 个交易日收盘价，计算 T1/T2/T3/T5
            prices = conn.execute(
                """
                SELECT trade_date, close FROM daily_price
                WHERE code = ? AND trade_date > ?
                ORDER BY trade_date ASC LIMIT 5
                """,
                (code, seg["scan_date"]),
            ).fetchall()

            def _ret(n):
                if len(prices) >= n and prices[n - 1]["close"]:
                    return round((prices[n - 1]["close"] - entry) / entry * 100, 2)
                return None

            t1, t2, t3, t5 = _ret(1), _ret(2), _ret(3), _ret(5)
            pnl_pct = t5 if t5 is not None else (t1 if t1 is not None else 0)
            hit_stop = False
            hit_tp = False
            if prices:
                current = prices[0]["close"]
                current_date = prices[0]["trade_date"]
                if seg.get("stop_loss") and current <= seg["stop_loss"]:
                    hit_stop = True
                if seg.get("take_profit") and current >= seg["take_profit"]:
                    hit_tp = True
            else:
                current_date = None

            # 判断是否仍在持仓中（首次推荐日后 N 天内）
            try:
                scan = _dt.strptime(seg["scan_date"], "%Y-%m-%d")
                days_held = (_dt.now() - scan).days
            except (ValueError, TypeError):
                days_held = 0

            results.append({
                "code": code,
                "name": seg.get("name"),
                "scan_date": seg["scan_date"],
                "first_scan_date": seg["first_scan_date"],
                "last_scan_date": seg["last_scan_date"],
                "streak_days": seg["streak_days"],
                "entry_price": round(entry, 2),
                "current_price": round(prices[0]["close"], 2) if prices else None,
                "current_date": current_date,
                "pnl_pct": pnl_pct,
                "hit_stop_loss": hit_stop,
                "hit_take_profit": hit_tp,
                "fusion_score": round(seg.get("fusion_score") or 0, 1),
                "t1_return": t1,
                "t2_return": t2,
                "t3_return": t3,
                "t5_return": t5,
                "days_held": days_held,
            })

        summary = _realtime_summary(results)

        # 按首次推荐日降序（同日按收益降序），复盘列表以时间线为主
        results.sort(key=lambda x: (x["first_scan_date"], x["pnl_pct"]), reverse=True)

        return {
            "items": sanitize_numeric(results),
            "summary": summary,
            "days": days,
            "merged": True,
        }


def _realtime_summary(results: list) -> dict:
    """实时回退路径的汇总统计（合并口径）；无结果返回空 dict。"""
    if not results:
        return {}

    def _win_stat(key):
        vals = [r[key] for r in results if r.get(key) is not None]
        if not vals:
            return {"n": 0, "win": 0, "win_rate": 0, "avg_return": 0}
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        return {"n": len(vals), "win": win,
                "win_rate": round(win / len(vals) * 100, 1),
                "avg_return": round(avg, 2)}

    wins = [r for r in results if r["pnl_pct"] > 0]
    losses = [r for r in results if r["pnl_pct"] <= 0]
    avg_return = sum(r["pnl_pct"] for r in results) / len(results)
    win_rate = len(wins) / len(results) * 100 if results else 0
    return {
        "total": len(results),
        "t1": _win_stat("t1_return"),
        "t2": _win_stat("t2_return"),
        "t3": _win_stat("t3_return"),
        "t5": _win_stat("t5_return"),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 2),
        "best_return": round(max(r["pnl_pct"] for r in results), 2),
        "worst_return": round(min(r["pnl_pct"] for r in results), 2),
        "stop_loss_count": sum(1 for r in results if r["hit_stop_loss"]),
        "take_profit_count": sum(1 for r in results if r["hit_take_profit"]),
    }
