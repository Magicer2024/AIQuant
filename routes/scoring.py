"""
routes/scoring.py —— 打分排名 API
"""
import json
import math
from datetime import date
from flask import Blueprint, request, jsonify
from strategy.scorer import score_stocks, get_daily_scores, get_latest_score_date
from core.db import get_conn

scoring_bp = Blueprint("scoring", __name__, url_prefix="/api/scoring")


def _sanitize(obj):
    """递归替换 NaN/Inf 为 None，numpy 类型转 Python 原生"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if hasattr(obj, "item"):  # numpy scalar → Python native
        return _sanitize(obj.item())
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@scoring_bp.route("/daily", methods=["GET"])
def daily_scores():
    """获取某日打分排名 GET /api/scoring/daily?date=2025-01-15"""
    trade_date = request.args.get("date") or get_latest_score_date()
    if not trade_date:
        trade_date = _get_latest_trade_date()
    if not trade_date:
        return jsonify({"success": True, "data": [], "date": None, "error": "No trade date available"})
    results = get_daily_scores(trade_date)
    return jsonify({"success": True, "data": _sanitize(results), "date": trade_date, "error": None})


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
