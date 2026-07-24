"""
routes/scoring.py —— 行情 K 线 + 辅助查询 API

原「每日打分」相关端点（/daily、/run、/stock、/stock/history）已随打分功能下线。
本模块仅保留仍被前端复用的部分：
  - /kline/<code>  K 线数据（openKline 复用，读 daily_price）
  - /latest_date   最新行情日期（screen 页取日期用）
url_prefix 保持 /api/scoring 不变，避免改动前端既有 URL。
"""
from datetime import date
from utils.serialization import sanitize_numeric as _sanitize
from flask import Blueprint, request, jsonify
from core.db import get_conn

scoring_bp = Blueprint("scoring", __name__, url_prefix="/api/scoring")


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

        return jsonify({"success": True, "data": {"kline": kline, "marks": marks}, "error": None})


@scoring_bp.route("/latest_date", methods=["GET"])
def latest_date():
    """获取最新行情日期 GET /api/scoring/latest_date"""
    return jsonify({"success": True, "data": None, "date": _get_latest_trade_date(), "error": None})


def _get_latest_trade_date() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM daily_price").fetchone()
        return row["d"] if row else ""
