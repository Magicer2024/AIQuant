"""
core/audit.py —— 审计日志系统

提供统一的审计日志记录和查询功能。
"""

import functools
import time
from datetime import datetime
from typing import Callable, Any
from flask import request

from core.db import get_conn


def log_audit(action: str, resource: str = "", detail: str = "",
              user_id: str = "", ip_address: str = "", result: str = ""):
    """
    记录一条审计日志

    :param action: 操作类型，如 "trade.buy", "risk.override", "config.update"
    :param resource: 操作对象，如股票代码、配置项名
    :param detail: 详细描述
    :param user_id: 操作人ID
    :param ip_address: 客户端IP
    :param result: 操作结果，如 "success", "failed", "blocked"
    """
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (user_id, action, resource, detail, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id or "system", action, resource, detail, ip_address,
                 datetime.now().isoformat())
            )
    except Exception as e:
        print(f"[Audit] 记录审计日志失败: {e}")


def audit_log(action: str, log_result: bool = True):
    """
    审计日志装饰器，自动记录 API 调用

    用法：
        @audit_log("trade.buy")
        def buy_stock():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            ip = request.remote_addr if request else ""
            user = getattr(request, "user_id", "anonymous") if request else "system"

            try:
                result = func(*args, **kwargs)
                duration_ms = int((time.time() - start_time) * 1000)

                # 尝试提取结果信息
                result_status = "success"
                detail = f"耗时 {duration_ms}ms"
                if isinstance(result, tuple) and len(result) == 2:
                    resp, status_code = result
                    if status_code and status_code >= 400:
                        result_status = "failed"
                elif hasattr(result, "status_code") and result.status_code >= 400:
                    result_status = "failed"

                if log_result:
                    log_audit(
                        action=action,
                        resource=_extract_resource(result),
                        detail=detail,
                        user_id=user,
                        ip_address=ip,
                        result=result_status,
                    )
                return result
            except Exception as e:
                log_audit(
                    action=action,
                    resource="",
                    detail=f"异常: {str(e)}",
                    user_id=user,
                    ip_address=ip,
                    result="error",
                )
                raise
        return wrapper
    return decorator


def _extract_resource(result: Any) -> str:
    """从响应中提取资源标识"""
    try:
        if hasattr(result, "get_json"):
            data = result.get_json()
            if isinstance(data, dict):
                return data.get("code", "") or data.get("id", "")
    except Exception:
        pass
    return ""


def get_audit_logs(action: str = "", user_id: str = "", limit: int = 100,
                   offset: int = 0) -> tuple[list[dict], int]:
    """
    查询审计日志

    :return: (日志列表, 总数)
    """
    try:
        with get_conn() as conn:
            # 构建查询条件
            conditions = []
            params = []
            if action:
                conditions.append("action = ?")
                params.append(action)
            if user_id:
                conditions.append("user_id = ?")
                params.append(user_id)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            # 查询总数
            total = conn.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE {where_clause}",
                params
            ).fetchone()[0]

            # 查询分页数据
            sql = f"""SELECT * FROM audit_log
                      WHERE {where_clause}
                      ORDER BY created_at DESC
                      LIMIT ? OFFSET ?"""
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
            logs = [dict(r) for r in rows]

            return logs, total
    except Exception as e:
        print(f"[Audit] 查询审计日志失败: {e}")
        return [], 0
