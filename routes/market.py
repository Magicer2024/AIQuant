# -*- coding: utf-8 -*-
"""市场广度聚合接口（管理A seq=477 A2-接口派单，只读）。

GET /api/market/breadth?date=YYYY-MM-DD   （缺省 = daily_price 最新交易日）

返回按日：涨/跌/平家数、涨停家数、炸板家数、连板高度分层（1板/2板/3板+）。
口径锁死：
  - 涨/跌/平 = 与 core/services/market_overview.py 市场速览「涨跌家数」完全同一 SQL 口径
    （daily_price 当日全行 CASE pct_change >0 / <0 / =0，不过滤 ST/新股/量）；
  - 涨停价 = round(前收 × (1+幅度), 2)：60/00 主板 10%、30 创业板 20%；
  - ST（按 stock_info 当前名称含 ST/*ST）从涨停/炸板/连板统计中【显式排除】并计数上报；
  - 新股/缺前收（窗口内无前一日收盘）排除并计数上报，不参与涨停判定；
  - 炸板 = 当日 high ≥ 涨停价 且 收盘未封（close < 涨停价）；封板 = close ≥ 涨停价 - 0.001；
  - 连板 = 自查询日向前逐日封板计数（窗口 45 个交易日，够算 3 板+分层）；
  - 无未来函数：仅使用 trade_date ≤ 查询日的数据。
  - 不做封单额（付费墙，管理A已定）。
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request

from core.db import get_conn

market_bp = Blueprint("market", __name__, url_prefix="/api/market")

STREAK_WINDOW_TDAYS = 45   # 连板回看交易日窗口
SEAL_TOL = 0.001           # 收盘封板容差（分位四舍五入误差）


def _limit_pct(code: str) -> float | None:
    """主板 10% / 创业板 20%；池内无 68/8x/4x（判定A冻结 60/00/30），其余返回 None 排除。"""
    if code.startswith(("60", "00")):
        return 0.10
    if code.startswith("30"):
        return 0.20
    return None


@market_bp.route("/breadth")
def breadth():
    d = request.args.get("date", "").strip()
    with get_conn() as conn:
        if not d:
            row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
            d = row["d"] if row else None
        if not d:
            return jsonify({"ok": False, "error": "daily_price 无数据"}), 404

        # ── 涨跌平：与 market_overview 同一口径（当日全行） ──
        b = conn.execute(
            """SELECT
                 SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up_count,
                 SUM(CASE WHEN pct_change < 0 THEN 1 ELSE 0 END) AS down_count,
                 SUM(CASE WHEN pct_change = 0 THEN 1 ELSE 0 END) AS flat_count,
                 COUNT(*) AS total_rows
               FROM daily_price WHERE trade_date = ?""",
            (d,),
        ).fetchone()

        # ── 回看窗口内的全部行情（含查询日），按 code 分组算前收/涨停/连板 ──
        dates = [
            r["trade_date"] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date <= ? "
                "ORDER BY trade_date DESC LIMIT ?",
                (d, STREAK_WINDOW_TDAYS),
            )
        ]
        if not dates:
            dates = [d]
        earliest = dates[-1]
        st_codes = {
            r["code"] for r in conn.execute(
                "SELECT code FROM stock_info WHERE name LIKE '%ST%'"
            )
        }
        rows = conn.execute(
            """SELECT code, trade_date, high, close
               FROM daily_price WHERE trade_date >= ? AND trade_date <= ?""",
            (earliest, d),
        ).fetchall()

    by_code: dict[str, dict] = {}
    for r in rows:
        by_code.setdefault(r["code"], {})[r["trade_date"]] = (r["high"], r["close"])

    sealed_today = blown_today = 0
    streaks = {1: 0, 2: 0, 3: 0}   # 3 = 3板+
    excl_st = excl_noprev = excl_nopct = 0

    def sealed_on(series: dict, date: str) -> bool | None:
        """该 code 在 date 是否封涨停；无法判定返回 None。"""
        v = series.get(date)
        if v is None:
            return None
        i = dates.index(date)
        if i + 1 >= len(dates):
            return None
        prev = series.get(dates[i + 1])
        if prev is None or not prev[1]:
            return None
        pct = _limit_pct(code)
        if pct is None:
            return None
        limit_price = round(prev[1] * (1 + pct), 2)
        return v[1] is not None and v[1] >= limit_price - SEAL_TOL

    for code, series in by_code.items():
        if code in st_codes:
            excl_st += 1
            continue
        pct = _limit_pct(code)
        if pct is None:
            excl_nopct += 1
            continue
        today = series.get(d)
        if today is None:
            continue  # 当日无行情（停牌等），不参与
        prev = None
        for dd in dates[1:]:
            if dd in series:
                prev = series[dd]
                break
        if prev is None or not prev[1] or today[1] is None:
            excl_noprev += 1
            continue
        limit_price = round(prev[1] * (1 + pct), 2)
        touched = today[0] is not None and today[0] >= limit_price - SEAL_TOL
        sealed = today[1] >= limit_price - SEAL_TOL
        if sealed:
            sealed_today += 1
            n = 1
            # 向前数连板（缺行即中断，停牌打断连板）
            i = 1
            while i < len(dates):
                s = sealed_on(series, dates[i])
                if s is True:
                    n += 1
                    i += 1
                else:
                    break
            streaks[min(n, 3)] += 1
        elif touched:
            blown_today += 1

    return jsonify({
        "ok": True,
        "trade_date": d,
        "breadth": {
            "up_count": b["up_count"] or 0,
            "down_count": b["down_count"] or 0,
            "flat_count": b["flat_count"] or 0,
            "total_rows": b["total_rows"],
        },
        "limit_up": {
            "sealed": sealed_today,
            "blown": blown_today,
            "streaks": {"1": streaks[1], "2": streaks[2], "3plus": streaks[3]},
        },
        "excluded": {"st": excl_st, "no_prev_close": excl_noprev, "prefix_not_supported": excl_nopct},
        "rules": {
            "main_board_pct": "10%", "gem30_pct": "20%",
            "st_handling": "excluded(当前名称含ST)", "blown": "high>=limit 且 close<limit",
            "limit_price": "round(prev_close*(1+pct),2)",
            "streak_window_tdays": STREAK_WINDOW_TDAYS,
        },
        "caliber_note": "涨跌平家数与市场速览 breadth 同 SQL 口径；行业为当前分类快照；仅用 ≤查询日 数据，无未来函数",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
