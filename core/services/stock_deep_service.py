"""
core/services/stock_deep_service.py —— 个股深度面板只读/任务编排（T7 下沉）
=========================================================================

- 明日候选（全市场扫描落库优先，无结果回退已关注池实时计算）
- 全市场深析扫描任务的触发与状态查询（task_queue）
- 单股深度分析（strategy.stock_deep.analyze_stock + 跟踪成交事件）
- 股票搜索 / 近期关注候选
"""
from __future__ import annotations

from core.db import get_conn
from utils.serialization import sanitize_numeric


def market_payload(limit: int) -> dict:
    """明日买入候选：优先最近一次全市场深析扫描结果，无结果时回退已关注池扫描。

    返回 { items, source: 'full'|'pool', scan_date, main_n, reserve_n, note }。
    main_n 取自 DEEP_TRACK.daily_top_n（当日实际建跟踪单只数，前端分层用）。
    """
    from strategy.stock_deep import get_market_signal_latest, scan_market_buy
    with get_conn() as conn:
        items = get_market_signal_latest(conn, limit)
        source = "full"
        scan_date = (items[0].get("scan_date") if items else None) or None
        if not items:
            # 回退：已关注池（今日推荐 + 自选 + 持仓 ≤60 只）实时计算
            codes: dict = {}
            for r in conn.execute(
                "SELECT code FROM stock_signal WHERE scan_date=(SELECT MAX(scan_date) FROM stock_signal)"
            ).fetchall():
                codes.setdefault(r["code"], "推荐")
            for r in conn.execute("SELECT code FROM personal_watchlist").fetchall():
                codes.setdefault(r["code"], "自选")
            for r in conn.execute(
                "SELECT code FROM personal_position WHERE status='holding'"
            ).fetchall():
                codes.setdefault(r["code"], "持仓")
            pool = list(codes.keys())[:60]
            items = scan_market_buy(conn, pool, limit)
            source = "pool"
        # main_n = 当日真正建跟踪单（要花钱买）的只数，取自 DEEP_TRACK.daily_top_n。
        # 前端据此把列表切成「主推(main_n) + 信息储备」两层 —— 配置单一来源，
        # 避免前端再写死一个 4 导致改了配置两边不一致。
        try:
            from config.strategy_params import DEEP_TRACK as _DT
            main_n = int(_DT.get("daily_top_n", 4) or 4)
        except Exception:
            main_n = 4
        reserve_n = max(0, len(items) - main_n)
        return sanitize_numeric({
            "items": items,
            "source": source,
            "scan_date": scan_date,
            "main_n": max(1, min(main_n, len(items) or main_n)),
            "reserve_n": reserve_n,
            "note": "source=full 为全市场深析扫描落库结果；source=pool 为已关注池(今日推荐+自选+持仓)结果；"
                    "main_n 为当日主推（实际建跟踪单）只数，其后 reserve_n 只为信息储备候选",
        })


def submit_scan() -> dict:
    """手动触发全市场深析扫描（异步任务，结果落库 stock_deep_signal）。"""
    from core.task_queue import submit_task, is_any_running
    if is_any_running("sd_scan"):
        return {"running": True, "task_id": None,
                "message": "全市场深析扫描已在运行，请稍候"}

    def _run(progress_callback=None):
        from strategy.stock_deep import run_full_market_scan
        from core.db import get_conn as _gc
        with _gc() as conn:
            return run_full_market_scan(conn, progress_callback=progress_callback)

    task_id = submit_task(_run, kind="sd_scan")
    return {"running": True, "task_id": task_id,
            "message": "全市场深析扫描已启动（约几分钟），完成后可在「明日候选」查看"}


def scan_status_payload() -> dict:
    """全市场深析扫描状态：是否运行 + 进度 + 最近一次扫描日期/候选数。"""
    from core.task_queue import is_any_running, get_running_task
    running = is_any_running("sd_scan")
    running_task = get_running_task("sd_scan")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(scan_date) AS d, COUNT(*) AS c FROM stock_deep_signal"
        ).fetchone()
    return {
        "running": running,
        "progress": (running_task or {}).get("progress", 0),
        "message": (running_task or {}).get("message", ""),
        "scan_date": row["d"] if row else None,
        "candidates": row["c"] if row else 0,
    }


def analyze_payload(code: str, lookback: int, recent_days: int) -> dict:
    """单股深度分析：学习历史走势与量价 → 识别涨跌节奏 → 近期买卖建议。

    返回 { code, name, industry, as_of, latest, trend, volume_price, rhythm,
           signal, recent_advice, chart, track_events }。
    track_events = 该股跟踪单的实际建仓/卖出成交点（K 线叠加用）。
    """
    from strategy.stock_deep import analyze_stock
    from strategy.deep_tracker import get_track_events
    with get_conn() as conn:
        result = analyze_stock(conn, code, lookback=lookback, recent_days=recent_days)
        result["track_events"] = get_track_events(conn, code)
    return sanitize_numeric(result)


def search_payload(q: str, limit: int) -> dict:
    """股票搜索（深度面板切换器自动补全）。

    q 非空：code 前缀或 name 包含，仅 is_active=1 且有行情；
    q 为空：自选/持仓/今日推荐的热门候选种子。
    """
    with get_conn() as conn:
        if q:
            # 非数字时代码前缀置为不可能匹配的值，否则 "" 前缀会匹配全部股票
            is_digit = q.isdigit()
            code_pat = f"{q}%" if is_digit else "zzzz"
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT i.code, i.name, i.industry, i.market
                FROM stock_info i
                WHERE i.is_active = 1
                  AND (i.code LIKE ? OR i.name LIKE ?)
                  AND EXISTS (SELECT 1 FROM daily_price d WHERE d.code = i.code)
                ORDER BY
                    CASE WHEN i.code LIKE ? THEN 0 WHEN i.name LIKE ? THEN 1 ELSE 2 END,
                    i.code
                LIMIT ?
                """,
                (code_pat, like, code_pat, like, limit),
            ).fetchall()
        else:
            # 无关键词：自选 + 持仓 + 今日推荐，作为切换种子
            seed_codes = {}
            for r in conn.execute(
                "SELECT code, '自选' AS src FROM personal_watchlist "
                "UNION ALL SELECT code, '持仓' AS src FROM personal_position WHERE status='holding' "
                "UNION ALL SELECT code, '推荐' AS src FROM stock_signal "
                "WHERE scan_date = (SELECT MAX(scan_date) FROM stock_signal)"
            ).fetchall():
                seed_codes.setdefault(r["code"], r["src"])
            codes = list(seed_codes.keys())
            if not codes:
                return {"items": []}
            ph = ",".join("?" * len(codes))
            rows = conn.execute(
                f"""
                SELECT i.code, i.name, i.industry, i.market
                FROM stock_info i WHERE i.is_active = 1 AND i.code IN ({ph})
                LIMIT ?
                """,
                [*codes, limit],
            ).fetchall()

    items = [dict(r) for r in rows]
    return sanitize_numeric({"items": items, "query": q})


def recent_payload() -> dict:
    """近期关注候选：自选 + 持仓 + 最新推荐（最多 14 只），供一键切换。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT code, '自选' AS src, created_at AS ts FROM personal_watchlist
            UNION ALL
            SELECT code, '持仓' AS src, opened_at AS ts FROM personal_position WHERE status='holding'
            UNION ALL
            SELECT code, '推荐' AS src, scan_date AS ts FROM stock_signal
              WHERE scan_date = (SELECT MAX(scan_date) FROM stock_signal)
            ORDER BY ts DESC
            """,
        ).fetchall()
        seen = set()
        out = []
        for r in rows:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            info = conn.execute(
                "SELECT name, industry FROM stock_info WHERE code = ?", (r["code"],)
            ).fetchone()
            out.append({
                "code": r["code"],
                "name": info["name"] if info else r["code"],
                "industry": info["industry"] if info and "industry" in info.keys() else None,
                "src": r["src"],
            })
            if len(out) >= 14:
                break
    return sanitize_numeric({"items": out})
