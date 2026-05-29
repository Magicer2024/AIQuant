"""
routes/scoring.py —— 打分排名 API
"""
import json
from datetime import date
from utils.serialization import sanitize_numeric as _sanitize
from flask import Blueprint, request, jsonify
from strategy.scorer import score_stocks, get_daily_scores, get_daily_scores_count, get_latest_score_date
from core.db import get_conn

scoring_bp = Blueprint("scoring", __name__, url_prefix="/api/scoring")


@scoring_bp.route("/daily", methods=["GET"])
def daily_scores():
    """获取某日打分排名（支持分页）GET /api/scoring/daily?date=2025-01-15&page=1&per_page=50"""
    trade_date = request.args.get("date") or get_latest_score_date()
    if not trade_date:
        trade_date = _get_latest_trade_date()
    if not trade_date:
        return jsonify({"success": True, "data": [], "date": None, "error": "No trade date available"})
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    per_page = min(per_page, 200)

    results = get_daily_scores(trade_date, page=page, per_page=per_page)
    total = get_daily_scores_count(trade_date)

    return jsonify({
        "success": True,
        "data": _sanitize(results),
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page)
        },
        "date": trade_date,
        "error": None
    })


@scoring_bp.route("/run", methods=["POST"])
def run_scoring():
    """触发打分 POST /api/scoring/run
    支持 sync_first=true 参数，先同步行情再打分
    """
    body = request.get_json(silent=True) or {}
    trade_date = body.get("date") or _get_latest_trade_date()
    if not trade_date:
        return jsonify({"success": False, "data": None, "error": "No trade_date provided"}), 400

    sync_first = body.get("sync_first", False)
    sync_msg = None
    if sync_first:
        try:
            from core.sync import daily_sync, is_trading_day
            from datetime import date
            today_str = date.today().strftime("%Y-%m-%d")
            if is_trading_day(today_str):
                daily_sync(verbose=False)
                sync_msg = "已同步最新行情"
            else:
                sync_msg = "非交易日，跳过同步"
        except Exception as e:
            sync_msg = f"同步失败: {e}"

    try:
        results = score_stocks(trade_date, save=True)
        return jsonify({"success": True, "data": {"count": len(results), "date": trade_date, "sync": sync_msg}, "error": None})
    except Exception as e:
        return jsonify({"success": False, "data": None, "error": str(e)}), 500


@scoring_bp.route("/stock/<code>", methods=["GET"])
def stock_factors(code: str):
    """获取单股因子明细 GET /api/scoring/stock/000001?date=2025-01-15"""
    trade_date = request.args.get("date")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM stock_score WHERE code = ? AND trade_date = ?",
            (code, trade_date)
        ).fetchone()
        if not row:
            return jsonify({"success": True, "data": None, "error": "Not found"})
        data = dict(row)
        try:
            data["factors"] = json.loads(data.get("factors_json", "{}"))
        except json.JSONDecodeError:
            data["factors"] = {}
        return jsonify({"success": True, "data": _sanitize(data), "error": None})


@scoring_bp.route("/stock/<code>/history", methods=["GET"])
def stock_score_history(code: str):
    """获取单股打分历史 GET /api/scoring/stock/000001/history?days=30"""
    days = request.args.get("days", "30")
    if days not in ("7", "30", "60", "90"):
        days = "30"

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT trade_date, score, rule_name
            FROM stock_score
            WHERE code = ? AND trade_date >= date('now', ?)
            ORDER BY trade_date ASC
        """, (code, f"-{days} days")).fetchall()
        return jsonify({"success": True, "data": [dict(r) for r in rows], "error": None})


@scoring_bp.route("/kline/<code>", methods=["GET"])
def kline_data(code: str):
    """获取 K 线数据（含回测买卖点标注）GET /api/scoring/kline/000001?start=2024-01-01&end=2024-12-31&result_id=1"""
    start = request.args.get("start", "2024-01-01")
    end = request.args.get("end", date.today().strftime("%Y-%m-%d"))
    result_id = request.args.get("result_id")

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume
            FROM daily_price WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, (code, start, end)).fetchall()

        kline = []
        for r in rows:
            d = dict(r)
            kline.append([
                d["trade_date"],
                _sanitize(d.get("open")),
                _sanitize(d.get("close")),
                _sanitize(d.get("low")),
                _sanitize(d.get("high")),
                _sanitize(d.get("volume")),
            ])

        marks = []
        if result_id:
            trades = conn.execute("""
                SELECT * FROM backtest_trades WHERE result_id = ? AND code = ?
            """, (int(result_id), code)).fetchall()
            for t in trades:
                t = dict(t)
                if t.get("entry_date"):
                    marks.append({"date": t["entry_date"], "price": _sanitize(t.get("entry_price")), "type": "buy"})
                if t.get("exit_date"):
                    marks.append({"date": t["exit_date"], "price": _sanitize(t.get("exit_price")), "type": "sell"})

        return jsonify({"success": True, "data": {"kline": kline, "marks": marks}, "error": None})


def _get_latest_trade_date() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM daily_price").fetchone()
        return row["d"] if row else ""
