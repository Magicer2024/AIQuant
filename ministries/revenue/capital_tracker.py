"""
ministries/revenue/capital_tracker.py -- 户部·资金追踪器

追踪总资产、可用资金、回撤等，供 RiskAgent 使用。
解决之前 RiskAgent._build_account_state() 中 hardcode total_value=100000 的问题。
"""

from typing import Any


class CapitalTracker:
    """资金追踪器（户部）"""

    def get_total_assets(self) -> float:
        """获取当前总资产"""
        from ministries.personnel.account_manager import get_account_manager
        return get_account_manager().get_current_account()["total_value"]

    def get_available_cash(self) -> float:
        """获取可用资金"""
        from ministries.personnel.account_manager import get_account_manager
        return get_account_manager().get_current_account()["available_cash"]

    def get_total_exposure(self) -> float:
        """获取当前总持仓市值"""
        from ministries.personnel.account_manager import get_account_manager
        return get_account_manager().get_current_account()["total_exposure"]

    def get_drawdown(self) -> float:
        """计算当前回撤（从峰值）"""
        from services.account_service import get_snapshots
        snaps = get_snapshots(days=90)
        if not snaps:
            return 0.0
        peak = max(s.get("total_assets", 0) for s in snaps)
        if peak <= 0:
            return 0.0
        current = snaps[0].get("total_assets", peak)
        return (current - peak) / peak

    def get_consecutive_losses(self) -> int:
        """获取连续亏损天数"""
        from services.account_service import get_snapshots
        snaps = get_snapshots(days=30)
        count = 0
        for s in snaps:
            pnl = s.get("total_pnl", 0)
            if pnl < 0:
                count += 1
            else:
                break
        return count

    def get_daily_open_count(self) -> int:
        """获取当天开仓数"""
        from ministries.revenue.position_manager import get_position_manager
        positions = get_position_manager().get_positions("holding")
        from datetime import date
        today_str = date.today().strftime("%Y-%m-%d")
        return sum(1 for p in positions if p.get("entry_date") == today_str)

    def build_account_state(self, market_risk: dict[str, Any]) -> dict[str, Any]:
        """构建 RiskEngine 需要的 account_state 字典"""
        ma5 = market_risk.get("ma5", None)
        ma20 = market_risk.get("ma20", None)
        return {
            "account_id": "default",
            "current_drawdown": abs(market_risk.get("drawdown_from_peak", 0)),
            "positions": [],
            "total_value": self.get_total_assets(),
            "total_exposure": self.get_total_exposure(),
            "available_capital": self.get_available_cash(),
            "consecutive_losses": self.get_consecutive_losses(),
            "daily_open_count": self.get_daily_open_count(),
            "index_ma5": float(ma5) if ma5 is not None else None,
            "index_ma20": float(ma20) if ma20 is not None else None,
        }


_capital_tracker: CapitalTracker | None = None


def get_capital_tracker() -> CapitalTracker:
    global _capital_tracker
    if _capital_tracker is None:
        _capital_tracker = CapitalTracker()
    return _capital_tracker
