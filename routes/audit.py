"""
routes/audit.py —— 审计日志API

路由：
  GET  /api/audit/logs      → 审计日志查询（支持分页、过滤）
  GET  /api/audit/stats     → 审计统计
"""

from flask import Blueprint, jsonify, request

from core.audit import get_audit_logs

audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit")


@audit_bp.route("/logs", methods=["GET"])
def get_logs():
    """查询审计日志"""
    action = request.args.get("action", "")
    user_id = request.args.get("user_id", "")
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)

    logs, total = get_audit_logs(
        action=action,
        user_id=user_id,
        limit=min(limit, 500),  # 最大500条
        offset=offset,
    )

    return jsonify({
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(logs),
        "logs": logs,
    })


@audit_bp.route("/stats", methods=["GET"])
def get_stats():
    """审计统计"""
    try:
        from core.db import get_conn
        with get_conn() as conn:
            # 今日操作数
            today_count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE date(created_at) = date('now')"
            ).fetchone()[0]

            # 各操作类型统计
            action_stats = conn.execute(
                """SELECT action, COUNT(*) as cnt
                   FROM audit_log
                   WHERE date(created_at) = date('now')
                   GROUP BY action
                   ORDER BY cnt DESC
                   LIMIT 10"""
            ).fetchall()

            # 最近7天趋势
            daily = conn.execute(
                """SELECT date(created_at) as day, COUNT(*) as cnt
                   FROM audit_log
                   WHERE created_at >= datetime('now', '-7 days')
                   GROUP BY day
                   ORDER BY day"""
            ).fetchall()

        return jsonify({
            "success": True,
            "today_count": today_count,
            "action_stats": [dict(r) for r in action_stats],
            "daily_trend": [dict(r) for r in daily],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
