"""
routes/data_fetch.py —— 数据拉取路由

端点：
  POST /api/data/fetch         —— 拉取单只股票数据
  POST /api/data/fetch_batch   —— 批量拉取数据
  GET  /api/data/fetch/status  —— 查询本地数据库数据概况
"""

from flask import Blueprint, request, jsonify

from services.data_fetcher import fetch_and_save_daily, batch_fetch_and_save

data_fetch_bp = Blueprint("data_fetch", __name__, url_prefix="/api/data")


@data_fetch_bp.route("/fetch", methods=["POST"])
def fetch_single():
    """拉取单只股票日线数据"""
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    days = data.get("days", 730)

    if not code:
        return jsonify({"success": False, "error": "code 不能为空"}), 400

    result = fetch_and_save_daily(code, days=days)
    return jsonify(result)


@data_fetch_bp.route("/fetch_batch", methods=["POST"])
def fetch_batch():
    """批量拉取股票日线数据（留空则更新本地数据库中所有已有股票）"""
    data = request.get_json() or {}
    codes = data.get("codes", [])
    days = data.get("days", 730)

    # 如果未传入codes，自动获取本地数据库中所有已有股票代码进行数据更新
    if not codes:
        try:
            from core.db import get_conn
            with get_conn() as conn:
                rows = conn.execute("SELECT DISTINCT code FROM daily_price ORDER BY code").fetchall()
                codes = [r[0] for r in rows]
        except Exception as e:
            return jsonify({"success": False, "error": f"无法读取本地股票列表: {e}"}), 500

    if not codes:
        return jsonify({"success": False, "error": "本地数据库为空，请先输入股票代码进行首次拉取"}), 400

    result = batch_fetch_and_save(codes, days=days)
    return jsonify(result)


@data_fetch_bp.route("/fetch/status", methods=["GET"])
def fetch_status():
    """查询本地数据库数据概况"""
    try:
        from core.db import get_conn
        with get_conn() as conn:
            # 总记录数
            total_rows = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
            # 股票数量
            stock_count = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_price").fetchone()[0]
            # 最近更新日期
            latest = conn.execute("SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
            # 数据最多的几只股票
            top = conn.execute("""
                SELECT code, COUNT(*) as cnt FROM daily_price
                GROUP BY code ORDER BY cnt DESC LIMIT 5
            """).fetchall()

        return jsonify({
            "success": True,
            "total_rows": total_rows,
            "stock_count": stock_count,
            "latest_date": latest,
            "top_stocks": [{"code": c, "rows": r} for c, r in top],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
