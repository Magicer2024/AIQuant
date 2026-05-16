"""
agents/backtest_agent.py —— 回测验证器（Qlib 集成版）

职责：
  1. 对SignalAgent产出的候选股，做快速历史回测验证
  2. 使用 Qlib SimulatorExecutor 计算策略表现
  3. 计算各候选股近3个月/6个月/1年的策略表现
  4. 输出：各候选股的历史胜率、盈亏比、最大回撤
"""

from __future__ import annotations

import pandas as pd
from datetime import date, timedelta, datetime

from agents.base import BaseAgent, AgentContext
from strategy.strategies import (
    strategy_volume_breakout,
    strategy_ma_convergence,
    strategy_price_volume_divergence,
    strategy_bottom_fishing,
    strategy_whale_accumulation,
    fuse_signals,
    DEFAULT_WEIGHTS,
)
from ministries.rites.data_source_manager import get_data_source_manager


class BacktestAgent(BaseAgent):
    """回测验证器（尚书省·回测官）：对候选信号做历史回测验证"""

    name = "BacktestAgent"
    governance_role = "尚书省·回测官"

    def _execute(self, ctx: AgentContext) -> dict:
        fusion_candidates = ctx.get("fusion_candidates", [])
        v4_candidates = ctx.get("v4_candidates", [])

        if not fusion_candidates and not v4_candidates:
            return {"status": "no_candidates", "reports": []}

        exec_date = datetime.strptime(ctx.run_date, "%Y-%m-%d").date() if ctx.run_date else date.today()

        reports = []

        for item in fusion_candidates:
            report = self._backtest_single(item, strategy="fusion", exec_date=exec_date)
            reports.append(report)

        for item in v4_candidates:
            report = self._backtest_v4_qlib(item, exec_date=exec_date)
            reports.append(report)

        valid_reports = [r for r in reports if r.get("status") == "ok"]
        avg_winrate = sum(r["win_rate"] for r in valid_reports) / len(valid_reports) if valid_reports else 0
        avg_return  = sum(r["avg_return"] for r in valid_reports) / len(valid_reports) if valid_reports else 0

        ctx.set("backtest_reports", reports)

        return {
            "status": "ok",
            "reports_count": len(reports),
            "valid_count": len(valid_reports),
            "avg_win_rate": round(avg_winrate, 3),
            "avg_return": round(avg_return, 3),
            "reports": reports,
        }

    def _backtest_v4_qlib(self, item: dict, exec_date: date | None = None) -> dict:
        """V4 backtest using Qlib SimulatorExecutor."""
        code = item["code"]
        name = item["name"]
        if exec_date is None:
            exec_date = date.today()

        try:
            from qlib_engine.strategy_adapter import backtest_single_rule, BacktestConfig

            start_date = (exec_date - timedelta(days=90)).strftime("%Y-%m-%d")
            end_date = exec_date.strftime("%Y-%m-%d")

            signals = [{
                "code": code,
                "trade_date": end_date,
                "score": item.get("score", 50),
            }]

            config = BacktestConfig(
                start_time=start_date,
                end_time=end_date,
                topk=1,
                n_drop=0,
            )

            result = backtest_single_rule(f"v4_{code}", signals, config)

            if result.get("error"):
                return self._empty_report(code, name, "v4")

            return {
                "code": code,
                "name": name,
                "strategy": "v4",
                "trade_count": result.get("total_trades", 0),
                "win_rate": round(result.get("win_rate", 0) / 100.0, 3),
                "avg_return": round(result.get("annual_return", 0) / 100.0, 4),
                "max_drawdown": round(result.get("max_drawdown", 0) / 100.0, 4),
                "annual_return": round(result.get("annual_return", 0) / 100.0, 4),
                "sharpe_ratio": result.get("sharpe_ratio", 0),
                "status": "ok",
            }
        except Exception as e:
            return self._empty_report(code, name, "v4")

    def _backtest_single(self, item: dict, strategy: str, exec_date: date | None = None) -> dict:
        """对单只股票做近3个月快速回测"""
        code = item["code"]
        name = item["name"]
        if exec_date is None:
            exec_date = date.today()

        try:
            end_date = exec_date.strftime("%Y-%m-%d")

            if strategy == "v4":
                return self._backtest_v4_qlib(item, exec_date)
            else:
                # 5策略融合回测（简化版）
                df = get_data_source_manager().get_daily_price_df(code, end_date=end_date)
                if df is None or df.empty:
                    return self._empty_report(code, name, strategy)

                s1 = strategy_volume_breakout(df)
                s2 = strategy_ma_convergence(df)
                s3 = strategy_price_volume_divergence(df)
                s4 = strategy_bottom_fishing(df)
                s5 = strategy_whale_accumulation(df)
                fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)

                signals = fused[fused["BUY_SIGNAL"] == True]
                if signals.empty:
                    return self._empty_report(code, name, strategy)

                returns_5d = []
                returns_10d = []
                for sig_idx in signals.index[-10:]:
                    try:
                        pos = fused.index.get_loc(sig_idx)
                        if pos + 5 < len(fused):
                            ret_5 = fused.iloc[pos + 5]["close"] / fused.iloc[pos]["close"] - 1
                            returns_5d.append(ret_5)
                        if pos + 10 < len(fused):
                            ret_10 = fused.iloc[pos + 10]["close"] / fused.iloc[pos]["close"] - 1
                            returns_10d.append(ret_10)
                    except Exception:
                        continue

                avg_5d = sum(returns_5d) / len(returns_5d) if returns_5d else 0
                avg_10d = sum(returns_10d) / len(returns_10d) if returns_10d else 0
                wins_5d = sum(1 for r in returns_5d if r > 0)
                win_rate_5d = wins_5d / len(returns_5d) if returns_5d else 0

                return {
                    "code": code,
                    "name": name,
                    "strategy": strategy,
                    "signal_count_90d": len(signals),
                    "win_rate": round(win_rate_5d, 3),
                    "win_rate_5d": round(win_rate_5d, 3),
                    "avg_return": round(avg_5d, 4),
                    "avg_return_5d": round(avg_5d, 4),
                    "avg_return_10d": round(avg_10d, 4),
                    "status": "ok",
                }
        except Exception as e:
            return {
                "code": code,
                "name": name,
                "strategy": strategy,
                "status": "error",
                "error": str(e)[:200],
            }

    def _empty_report(self, code: str, name: str, strategy: str) -> dict:
        return {
            "code": code,
            "name": name,
            "strategy": strategy,
            "trade_count": 0,
            "win_rate": 0,
            "avg_return": 0,
            "status": "no_signals",
        }
