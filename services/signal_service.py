"""
services/signal_service.py —— 信号历史查询服务
"""
import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, Any, List

import core.db as db


def get_signal_history(start_date: date, end_date: date,
                       min_score: float = 15.0, limit: int = 100) -> Dict[str, Any]:
    """查询5策略融合历史推荐"""
    with db.get_conn() as conn:
        trade_dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date DESC",
            (start_date.isoformat(), end_date.isoformat())
        ).fetchall()]
        ss_dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_signal WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date DESC",
            (start_date.isoformat(), end_date.isoformat())
        ).fetchall()]

    all_dates = sorted(set(trade_dates + ss_dates), reverse=True)
    dates_to_process = all_dates[:60]
    placeholders = ",".join(["?"] * len(dates_to_process))

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT trade_date, code, name, price,
                   fusion_score, vol_score, ma_score, diverge_score,
                   bottom_score, whale_score, trigger_list,
                   buy_price, stop_loss, take_profit,
                   buy_volume, buy_money
            FROM stock_signal
            WHERE trade_date IN ({placeholders})
              AND fusion_score >= ?
              AND (trigger_list IS NULL OR trigger_list NOT LIKE '%超跌反弹%')
            ORDER BY trade_date DESC, fusion_score DESC
        """, dates_to_process + [min_score]).fetchall()

    groups = defaultdict(list)
    ss_counts = {td: 0 for td in dates_to_process}
    seen = defaultdict(set)

    for r in rows:
        td = r["trade_date"]
        code = r["code"]
        if code in seen[td]:
            continue
        seen[td].add(code)
        trigger_list = []
        try:
            trigger_list = json.loads(r["trigger_list"] or "[]")
        except Exception:
            trigger_list = []
        groups[td].append({
            "code": r["code"],
            "name": r["name"] or r["code"],
            "price": r["price"],
            "fusion_score": r["fusion_score"],
            "vol_score": r["vol_score"],
            "ma_score": r["ma_score"],
            "diverge_score": r["diverge_score"],
            "bottom_score": r["bottom_score"],
            "whale_score": r["whale_score"],
            "trigger_list": trigger_list,
            "buy_price": r["buy_price"],
            "stop_loss": r["stop_loss"],
            "take_profit": r["take_profit"],
            "buy_volume": r["buy_volume"],
            "buy_money": r["buy_money"],
        })
        ss_counts[td] += 1

    dates_needing = [d for d in dates_to_process if ss_counts[d] < 3]
    if dates_needing:
        ph = ",".join(["?"] * len(dates_needing))
        with db.get_conn() as conn:
            fb_rows = conn.execute(f"""
                SELECT d.trade_date, d.code, COALESCE(s.name, d.code) AS name,
                       d.close AS price, d.pct_change AS change_pct,
                       d.fusion_score, d.vol_score, d.ma_score, d.diverge_score,
                       d.bottom_score, d.whale_score
                FROM daily_price d
                LEFT JOIN stock_info s ON d.code = s.code
                WHERE d.trade_date IN ({ph})
                  AND d.fusion_score >= ?
                  AND d.bottom_score < 5
                ORDER BY d.trade_date DESC, d.fusion_score DESC
            """, dates_needing + [min_score]).fetchall()

        for row in fb_rows:
            td, code, name = row[0], row[1], row[2]
            if code in {e["code"] for e in groups[td]}:
                continue
            fs = row[5]
            vol_s, ma_s, div_s, bot_s, wha_s = row[6], row[7], row[8], row[9], row[10]
            triggered = []
            for label, val in [("放量突破", vol_s), ("均线粘合", ma_s), ("量价背离", div_s),
                               ("抄底", bot_s), ("主力建仓", wha_s)]:
                if val and val >= 5:
                    triggered.append(label)
            groups[td].append({
                "code": code, "name": name or code,
                "price": round(row[3], 2) if row[3] else 0,
                "fusion_score": round(fs, 1) if fs else 0,
                "vol_score": round(vol_s, 1) if vol_s else 0,
                "ma_score": round(ma_s, 1) if ma_s else 0,
                "diverge_score": round(div_s, 1) if div_s else 0,
                "bottom_score": round(bot_s, 1) if bot_s else 0,
                "whale_score": round(wha_s, 1) if wha_s else 0,
                "trigger_list": triggered,
                "buy_price": None, "stop_loss": None, "take_profit": None,
                "buy_volume": None, "buy_money": None,
            })

    for d in groups:
        groups[d] = sorted(groups[d], key=lambda x: x["fusion_score"], reverse=True)[:limit]

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "dates": sorted(dates_to_process, reverse=True),
        "groups": dict(groups),
    }


def get_signal_history_v4(start_date: date, end_date: date,
                          min_score: float = 15.0, limit: int = 50) -> Dict[str, Any]:
    """查询v4超跌反弹历史推荐"""
    with db.get_conn() as conn:
        trade_dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date DESC",
            (start_date.isoformat(), end_date.isoformat())
        ).fetchall()]
        ss_dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_signal WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date DESC",
            (start_date.isoformat(), end_date.isoformat())
        ).fetchall()]

    all_dates = sorted(set(trade_dates + ss_dates), reverse=True)
    if not all_dates:
        return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "dates": [], "groups": {}}

    dates_to_process = all_dates[:60]
    ph = ",".join(["?"] * len(dates_to_process))

    all_groups = {td: [] for td in dates_to_process}
    ss_counts = {td: 0 for td in dates_to_process}
    seen = defaultdict(set)

    with db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT scan_date, trade_date, code, name, price,
                   fusion_score, vol_score, ma_score, diverge_score,
                   bottom_score, whale_score, trigger_list,
                   buy_price, stop_loss, take_profit
            FROM stock_signal
            WHERE trade_date IN ({ph})
              AND fusion_score >= ?
              AND trigger_list LIKE '%超跌反弹%'
            ORDER BY trade_date DESC, fusion_score DESC
        """, dates_to_process + [min_score]).fetchall()

    for r in rows:
        td = r["trade_date"]
        code = r["code"]
        if code in seen[td]:
            continue
        seen[td].add(code)
        trigger_list = []
        try:
            trigger_list = json.loads(r["trigger_list"] or "[]")
        except Exception:
            if "超跌反弹" in (r["trigger_list"] or ""):
                trigger_list = ["超跌反弹"]

        strategy_scores = {}
        for label, val in [("放量", r["vol_score"]), ("均线", r["ma_score"]),
                           ("背离", r["diverge_score"]), ("抄底", r["bottom_score"]), ("主力", r["whale_score"])]:
            t = val or 0
            strategy_scores[label] = {"score": t, "max": 10, "triggered": t >= 5}

        entry = {
            "symbol": r["code"], "name": r["name"] or r["code"],
            "trade_date": td,
            "price": round(r["price"], 2) if r["price"] else 0,
            "change_pct": 0,
            "score": round(r["fusion_score"], 1) if r["fusion_score"] else 0,
            "strategy_scores": strategy_scores,
            "triggered": trigger_list,
            "buy_price": r["buy_price"],
            "stop_loss": r["stop_loss"],
            "take_profit": r["take_profit"],
        }
        if len(all_groups[td]) < limit:
            all_groups[td].append(entry)
        ss_counts[td] += 1

    dates_needing = [d for d in dates_to_process if ss_counts[d] < 3]
    if dates_needing:
        ph2 = ",".join(["?"] * len(dates_needing))
        with db.get_conn() as conn:
            fb_rows = conn.execute(f"""
                SELECT d.trade_date, d.code, COALESCE(s.name, d.code) AS name,
                       d.close AS price, d.pct_change AS change_pct,
                       d.fusion_score AS score,
                       d.vol_score, d.ma_score, d.diverge_score,
                       d.bottom_score, d.whale_score
                FROM daily_price d
                LEFT JOIN stock_info s ON d.code = s.code
                WHERE d.trade_date IN ({ph2})
                  AND d.bottom_score >= 5
                  AND d.fusion_score >= ?
                ORDER BY d.trade_date DESC, d.bottom_score DESC
            """, dates_needing + [min_score]).fetchall()

        for row in fb_rows:
            td, code, name, price, change_pct = row[0], row[1], row[2], row[3], row[4]
            score = row[5]
            vol_s, ma_s, div_s, bot_s, wha_s = row[6], row[7], row[8], row[9], row[10]
            if code in {e["symbol"] for e in all_groups[td]}:
                continue
            triggered = []
            strategy_scores = {}
            for label, val in [("放量", vol_s), ("均线", ma_s), ("背离", div_s), ("抄底", bot_s), ("主力", wha_s)]:
                t = val or 0
                if t >= 5:
                    triggered.append(label)
                    strategy_scores[label] = {"score": t, "max": 10, "triggered": True}
                else:
                    strategy_scores[label] = {"score": t, "max": 10, "triggered": False}
            entry = {
                "symbol": code, "name": name or code,
                "trade_date": td,
                "price": round(price, 2) if price else 0,
                "change_pct": round(change_pct, 2) if change_pct else 0,
                "score": round(score, 1) if score else 0,
                "strategy_scores": strategy_scores,
                "triggered": triggered if triggered else ["抄底"],
            }
            if len(all_groups[td]) < limit:
                all_groups[td].append(entry)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "dates": sorted(dates_to_process, reverse=True),
        "groups": all_groups,
    }
