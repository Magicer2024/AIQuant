"""
ministries/justice/audit_manager.py -- 刑部·审计管理器

封装 core/audit.py 的审计功能。
"""

from typing import Any


class AuditManager:
    """审计管理器（刑部）"""

    def log(self, action: str, resource: str = "", detail: str = "",
            user_id: str = "", ip_address: str = "", result: str = ""):
        from core.audit import log_audit
        log_audit(action, resource, detail, user_id, ip_address, result)

    def audit_decorator(self, action: str, log_result: bool = True):
        """审计日志装饰器"""
        from core.audit import audit_log
        return audit_log(action, log_result=log_result)

    def query(self, action: str = "", user_id: str = "",
              limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        from core.audit import get_audit_logs
        return get_audit_logs(action, user_id, limit, offset)


_audit_manager: AuditManager | None = None


def get_audit_manager() -> AuditManager:
    global _audit_manager
    if _audit_manager is None:
        _audit_manager = AuditManager()
    return _audit_manager
