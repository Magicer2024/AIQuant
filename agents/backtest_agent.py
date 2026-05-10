"""
agents/backtest_agent.py —— 回测验证器

职责：
  1. 对SignalAgent产出的候选股，做快速历史回测验证
  2. 计算各候选股近3个月/6个月/1年的策略表现
  3. 参数敏感度分析（止损、止盈、持仓天数）
  4. 输出：各候选股的历史胜率、盈亏比、最大回撤
"""

from __future__ import annotations

import pandas as pd
from datetime import date, timedelta, datetime

from agents.base import BaseAgent, AgentContext
from backtest.backtest_v4 import run_oversold_v4
from backtest.strategy_screen_backtest import BacktestParams, StrategyScreenBacktest
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

        # 对融合策略候选做回测
        for item in fusion_candidates:
            report = self._backtest_single(item, strategy="fusion", exec_date=exec_date)
            reports.append(report)

        # 对v4候选做回测
        for item in v4_candidates:
            report = self._backtest_single(item, strategy="v4", exec_date=exec_date)
            reports.append(report)

        # 汇总统计
        avg_winrate = sum(r["win_rate"] for r in reports) / len(reports) if reports else 0
        avg_return = sum(r["avg_return"] for r in reports) / len(reports) if reports else 0

        ctx.set("backtest_reports", reports)

        return {
            "status": "ok",
            "reports_count": len(reports),
            "avg_win_rate": round(avg_winrate, 3),
            "avg_return": round(avg_return, 3),
            "reports": reports,
        }

    def _backtest_single(self, item: dict, strategy: str, exec_date: date | None = None) -> dict:
        """对单只股票做近3个月快速回测"""
        code = item["code"]
        name = item["name"]
        if exec_date is None:
            exec_date = date.today()

        try:
            end_date = exec_date.strftime("%Y-%m-%d")
            start_date = (exec_date - timedelta(days=90)).strftime("%Y-%m-%d")

            if strategy == "v4":
                # v4策略回测
                result = run_oversold_v4(
                    start_date=start_date,
                    end_date=end_date,
                    init_capital=100_000,
                    max_positions=4,
                    max_hold_days=8,
                    min_hold_days=3,
                    stop_loss=-0.06,
                    trailing_pct=0.10,
                    single_pos_ratio=0.40,
                    use_market_timing=True,
                )
                # 过滤只关注该股票的交易记录
                trades = [t for t in result.get("trades", []) if t.get("code") == code]
                if not trades:
                    return self._empty_report(code, name, strategy)

                wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
                win_rate = wins / len(trades) if trades else 0
                avg_ret = sum(t.get("pnl_pct", 0) for t in trades) / len(trades) if trades else 0
                max_dd = result.get("max_drawdown", 0)

                return {
                    "code": code,
                    "name": name,
                    "strategy": strategy,
                    "trade_count": len(trades),
                    "win_rate": round(win_rate, 3),
                    "avg_return": round(avg_ret, 4),
                    "max_drawdown": round(max_dd, 4),
                    "annual_return": round(result.get("annual_return", 0), 4),
                    "status": "ok",
                }
            else:
                # 5策略融合回测（简化版）
                df = get_data_source_manager().get_daily_price_df(code, end_date=end_date)
                if df is None or df.empty:
                    return self._empty_report(code, name, strategy)

                # 运行策略生成信号
                s1 = strategy_volume_breakout(df)
                s2 = strategy_ma_convergence(df)
                s3 = strategy_price_volume_divergence(df)
                s4 = strategy_bottom_fishing(df)
                s5 = strategy_whale_accumulation(df)
                fused = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)

                # 统计近90天信号触发次数和后续表现
                signals = fused[fused["BUY_SIGNAL"] == True]
                if signals.empty:
                    return self._empty_report(code, name, strategy)

                returns_5d = []
                returns_10d = []
                for sig_idx in signals.index[-10:]:  # 最近10个信号
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
                    "win_rate_5d": round(win_rate_5d, 3),
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
