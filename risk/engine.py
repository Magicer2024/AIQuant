"""
risk/engine.py —— 风控引擎核心
"""

from typing import List
from datetime import datetime

from .models import RiskCheckResult, RiskLevel, RiskStatus
from .rules import DEFAULT_RULES
from .config_loader import risk_config


class RiskEngine:
    """风控引擎"""

    def __init__(self, rules=None):
        self.rules = rules or DEFAULT_RULES

    def check(self, account_state: dict) -> tuple[RiskLevel, List[RiskCheckResult]]:
        """
        执行全量风控检查
        :return: (整体风险等级, 所有检查结果)
        """
        results = []
        for rule in self.rules:
            try:
                result = rule(account_state)
                results.append(result)
            except Exception as e:
                results.append(RiskCheckResult(
                    level=RiskLevel.WARNING,
                    category=RiskCategory.DRAWDOWN,
                    rule_name=rule.__name__,
                    message=f"规则执行异常: {e}",
                    metric_value=0,
                    threshold=0,
                    timestamp=datetime.now().isoformat(),
                ))

        # 取最严格的等级
        level_priority = {
            RiskLevel.BLOCK: 3,
            RiskLevel.RESTRICT: 2,
            RiskLevel.WARNING: 1,
            RiskLevel.PASS: 0,
        }
        overall = max(results, key=lambda r: level_priority[r.level]).level

        return overall, results

    def check_and_record(self, account_state: dict, pipeline_id: str = "") -> RiskStatus:
        """执行检查并记录到数据库"""
        overall, results = self.check(account_state)

        status = RiskStatus(
            account_id=account_state.get("account_id", "default"),
            overall_level=overall,
            active_rules=[r.rule_name for r in results if r.level != RiskLevel.PASS],
            current_drawdown=account_state.get("current_drawdown", 0.0),
            current_positions=len(account_state.get("positions", [])),
            total_exposure=account_state.get("total_exposure", 0.0),
            available_capital=account_state.get("available_capital", 0.0),
            last_check=datetime.now().isoformat(),
            block_reason="; ".join([r.message for r in results if r.level == RiskLevel.BLOCK]),
        )

        # 写入数据库
        self._save_results(results, pipeline_id)
        self._save_status(status)

        return status

    def _save_results(self, results: List[RiskCheckResult], pipeline_id: str):
        """保存风控检查结果"""
        try:
            from core.db import get_conn
            with get_conn() as conn:
                for r in results:
                    conn.execute(
                        """
                        INSERT INTO risk_events
                            (pipeline_id, rule_name, category, level, message,
                             metric_value, threshold, suggestion, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pipeline_id,
                            r.rule_name,
                            r.category.value,
                            r.level.value,
                            r.message,
                            r.metric_value,
                            r.threshold,
                            r.suggestion,
                            r.timestamp,
                        ),
                    )
        except Exception as e:
            print(f"[RiskEngine] 保存风险事件失败: {e}")

    def _save_status(self, status: RiskStatus):
        """保存当前风控状态"""
        try:
            from core.db import get_conn
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO risk_status
                        (account_id, overall_level, active_rules, current_drawdown,
                         current_positions, total_exposure, available_capital,
                         last_check, block_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        status.account_id,
                        status.overall_level.value,
                        ",".join(status.active_rules),
                        status.current_drawdown,
                        status.current_positions,
                        status.total_exposure,
                        status.available_capital,
                        status.last_check,
                        status.block_reason,
                    ),
                )
        except Exception as e:
            print(f"[RiskEngine] 保存风控状态失败: {e}")

    def get_latest_status(self, account_id: str = "default") -> RiskStatus | None:
        """查询最新风控状态"""
        try:
            from core.db import get_conn
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM risk_status WHERE account_id = ? ORDER BY last_check DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if not row:
                    return None
                return RiskStatus(
                    account_id=row["account_id"],
                    overall_level=RiskLevel(row["overall_level"]),
                    active_rules=row["active_rules"].split(",") if row["active_rules"] else [],
                    current_drawdown=row["current_drawdown"],
                    current_positions=row["current_positions"],
                    total_exposure=row["total_exposure"],
                    available_capital=row["available_capital"],
                    last_check=row["last_check"],
                    block_reason=row["block_reason"],
                )
        except Exception as e:
            print(f"[RiskEngine] 查询风控状态失败: {e}")
            return None

    def get_recent_events(self, limit: int = 50) -> List[dict]:
        """查询最近风险事件"""
        try:
            from core.db import get_conn
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM risk_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            print(f"[RiskEngine] 查询风险事件失败: {e}")
            return []
