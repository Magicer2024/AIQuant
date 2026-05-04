"""
routes/risk.py —— 风控API

路由：
  GET  /api/risk/status       → 当前风控状态
  GET  /api/risk/events       → 最近风控事件
  POST /api/risk/check        → 手动触发风控检查
  GET  /api/risk/config       → 风控配置
  POST /api/risk/config/reload → 热加载配置

  GET  /api/risk/blacklist    → 黑名单列表
  POST /api/risk/blacklist    → 添加黑名单
  DELETE /api/risk/blacklist/<code> → 移除黑名单

  POST /api/risk/override     → 申请风控豁免
  GET  /api/risk/overrides    → 豁免记录
"""

from flask import Blueprint, jsonify, request

from risk.engine import RiskEngine
from risk.models import RiskLevel
from risk.guard import build_account_state
from risk.config_loader import risk_config

risk_bp = Blueprint("risk", __name__, url_prefix="/api/risk")


# ── 基础接口 ────────────────────────────────────

@risk_bp.route("/status", methods=["GET"])
def get_risk_status():
    """获取当前风控状态"""
    engine = RiskEngine()
    status = engine.get_latest_status()
    if not status:
        return jsonify({"success": True, "status": None})
    return jsonify({
        "success": True,
        "status": {
            "overall_level": status.overall_level.value,
            "blocked": status.overall_level == RiskLevel.BLOCK,
            "current_drawdown": status.current_drawdown,
            "current_positions": status.current_positions,
            "active_rules": status.active_rules,
            "block_reason": status.block_reason,
            "last_check": status.last_check,
        }
    })


@risk_bp.route("/events", methods=["GET"])
def get_risk_events():
    """获取最近风控事件"""
    limit = request.args.get("limit", 50, type=int)
    level = request.args.get("level", "", type=str)

    engine = RiskEngine()
    events = engine.get_recent_events(limit)

    if level:
        events = [e for e in events if e.get("level") == level]

    return jsonify({"success": True, "count": len(events), "events": events})


@risk_bp.route("/check", methods=["POST"])
def manual_check():
    """手动触发风控检查"""
    data = request.get_json() or {}
    account_state = data.get("account_state", build_account_state())

    engine = RiskEngine()
    overall, results = engine.check(account_state)

    return jsonify({
        "success": True,
        "overall_level": overall.value,
        "blocked": overall == RiskLevel.BLOCK,
        "results": [r.to_dict() for r in results]
    })


# ── 配置管理 ────────────────────────────────────

@risk_bp.route("/config", methods=["GET"])
def get_risk_config():
    """获取当前风控配置"""
    return jsonify({
        "success": True,
        "config": risk_config.to_dict(),
        "config_path": risk_config.__class__.__dict__.get("CONFIG_PATH", "config/risk_config.yaml"),
    })


@risk_bp.route("/config/reload", methods=["POST"])
def reload_risk_config():
    """热加载风控配置"""
    try:
        risk_config.reload()
        return jsonify({
            "success": True,
            "message": "风控配置已热加载",
            "config": risk_config.to_dict(),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── 黑名单管理 ──────────────────────────────────

@risk_bp.route("/blacklist", methods=["GET"])
def get_blacklist():
    """获取黑名单列表"""
    try:
        from core.db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, stock_code, reason, created_by, created_at, expiry_date
                   FROM blacklist WHERE expiry_date > datetime('now')
                   ORDER BY created_at DESC"""
            ).fetchall()
            items = [dict(r) for r in rows]
        return jsonify({"success": True, "count": len(items), "items": items})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/blacklist", methods=["POST"])
def add_to_blacklist():
    """添加股票到黑名单"""
    data = request.get_json() or {}
    stock_code = data.get("stock_code", "").strip()
    reason = data.get("reason", "").strip()
    expiry_days = data.get("expiry_days", risk_config.get("blacklist.default_expiry_days", 30))

    if not stock_code:
        return jsonify({"success": False, "error": "stock_code required"}), 400
    if not reason:
        return jsonify({"success": False, "error": "reason required"}), 400

    try:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO blacklist (stock_code, reason, created_by, expiry_date)
                   VALUES (?, ?, ?, datetime('now', '+' || ? || ' days'))""",
                (stock_code, reason, data.get("operator", "system"), expiry_days)
            )
        return jsonify({"success": True, "message": f"{stock_code} 已加入黑名单"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/blacklist/<stock_code>", methods=["DELETE"])
def remove_from_blacklist(stock_code):
    """从黑名单移除股票"""
    try:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM blacklist WHERE stock_code = ?",
                (stock_code,)
            )
        return jsonify({"success": True, "message": f"{stock_code} 已从黑名单移除"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── 风控豁免 ────────────────────────────────────

@risk_bp.route("/override", methods=["POST"])
def apply_override():
    """申请风控豁免"""
    data = request.get_json() or {}
    rule_name = data.get("rule_name", "").strip()
    reason = data.get("reason", "").strip()
    duration_hours = data.get("duration_hours", risk_config.get("override.max_duration_hours", 4))
    operator = data.get("operator", "unknown")

    if not rule_name:
        return jsonify({"success": False, "error": "rule_name required"}), 400
    if risk_config.get("override.require_reason", True) and not reason:
        return jsonify({"success": False, "error": "reason required"}), 400

    try:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO risk_overrides
                   (rule_name, reason, operator, status, expires_at)
                   VALUES (?, ?, ?, ?, datetime('now', '+' || ? || ' hours'))""",
                (rule_name, reason, operator,
                 "approved" if not risk_config.get("override.require_approval", False) else "pending",
                 duration_hours)
            )
        return jsonify({
            "success": True,
            "message": f"{rule_name} 豁免已{'生效' if not risk_config.get('override.require_approval') else '提交审批'}",
            "rule_name": rule_name,
            "duration_hours": duration_hours,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/overrides", methods=["GET"])
def get_overrides():
    """获取豁免记录"""
    status = request.args.get("status", "")
    try:
        from core.db import get_conn
        with get_conn() as conn:
            sql = """SELECT id, rule_name, reason, operator, status, created_at, expires_at
                     FROM risk_overrides WHERE expires_at > datetime('now')"""
            params = []
            if status:
                sql += " AND status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
            items = [dict(r) for r in rows]
        return jsonify({"success": True, "count": len(items), "items": items})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/override/<int:override_id>/approve", methods=["POST"])
def approve_override(override_id):
    """审批通过豁免申请"""
    data = request.get_json() or {}
    approver = data.get("approver", "system")

    try:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE risk_overrides SET status = 'approved', approver = ? WHERE id = ?",
                (approver, override_id)
            )
        return jsonify({"success": True, "message": f"豁免 #{override_id} 已审批通过"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/drawdown", methods=["GET"])
def get_drawdown():
    """获取账户资产回撤曲线（基于 account_snapshots 表）

    计算方式：逐日遍历，维护历史最高总资产，
    当日回撤 = (当日总资产 - 历史最高) / 历史最高 * 100
    """
    days = request.args.get("days", 30, type=int)
    try:
        from core.db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT snapshot_date, total_assets
                   FROM account_snapshots
                   ORDER BY snapshot_date ASC
                   LIMIT -1 OFFSET (SELECT MAX(0, COUNT(*) - ?) FROM account_snapshots)""",
                (days,)
            ).fetchall()

        if not rows:
            return jsonify({
                "success": True,
                "dates": [],
                "drawdowns": [],
                "max_drawdown": 0,
                "current_drawdown": 0,
            })

        peak = 0
        dates = []
        drawdowns = []
        max_dd = 0
        for r in rows:
            snapshot_date = r["snapshot_date"]
            total = float(r["total_assets"] or 0)
            dates.append(snapshot_date)
            if total > peak:
                peak = total
            dd = round((total - peak) / peak * 100, 2) if peak > 0 else 0
            drawdowns.append(dd)
            if dd < max_dd:
                max_dd = dd

        return jsonify({
            "success": True,
            "dates": dates,
            "drawdowns": drawdowns,
            "max_drawdown": max_dd,
            "current_drawdown": drawdowns[-1] if drawdowns else 0,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
