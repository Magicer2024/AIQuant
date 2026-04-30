"""
backtest/unified_engine.py —— 通用回测引擎（策略插件化）
=======================================================
职责：资金曲线、手续费、滑点、持仓管理、止损止盈
不区分具体策略，只接收 Strategy 实例列表。

当前状态：框架已建立，逐步替换 backtest.py / strategy_screen_backtest.py / custom_strategy_backtest.py
中的重复资金曲线逻辑。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import pandas as pd
import numpy as np
from datetime import datetime

from backtest.strategy_interface import Strategy


@dataclass
class Trade:
    """单笔交易记录"""
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    shares: float = 0.0
    direction: str = "long"
    exit_reason: str = ""  # signal / stop_loss / take_profit / end
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0
    slippage: float = 0.0
    trigger_strategy: str = ""


@dataclass
class BacktestResult:
    """回测结果"""
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_holding_days: float = 0.0
    avg_profit_pct: float = 0.0
    avg_loss_pct: float = 0.0
    final_capital: float = 0.0
    benchmark_return: float = 0.0
    alpha: float = 0.0
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    equity_dates: List[str] = field(default_factory=list)


class UnifiedBacktester:
    """
    通用回测引擎（策略插件化版本）

    特性：
    - 接收 Strategy 实例列表，支持多策略同时运行
    - T+1 交易（次日开盘价执行）
    - 手续费（默认万三双向）+ 印花税（卖出千一）
    - 滑点模拟
    - 止损止盈 + 最大持仓天数
    - 回撤熔断 + 大盘择时（可选）
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.0003,
        stamp_tax: float = 0.001,
        position_pct: float = 1.0,
        max_holding_days: int = 40,
        use_stop_loss: bool = True,
        use_take_profit: bool = True,
        slippage_rate: float = 0.001,
        use_drawdown_guard: bool = True,
        drawdown_threshold: float = 0.10,
        consecutive_loss_limit: int = 3,
        use_market_timing: bool = True,
        market_timing_index: str = "000001",
        use_dynamic_position: bool = False,
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.position_pct = position_pct
        self.max_holding_days = max_holding_days
        self.use_stop_loss = use_stop_loss
        self.use_take_profit = use_take_profit
        self.slippage_rate = slippage_rate
        self.use_drawdown_guard = use_drawdown_guard
        self.drawdown_threshold = drawdown_threshold
        self.consecutive_loss_limit = consecutive_loss_limit
        self.use_market_timing = use_market_timing
        self.market_timing_index = market_timing_index
        self.use_dynamic_position = use_dynamic_position
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

    def run(self, df: pd.DataFrame, strategy: Strategy) -> BacktestResult:
        """
        对单只股票运行回测。

        Args:
            df: OHLCV 行情数据（index=date，含 open/high/low/close/volume）
            strategy: Strategy 实例

        Returns:
            BacktestResult
        """
        # 生成信号
        sig_df = strategy.generate(df.copy())

        # 确保有 open 列（T+1 执行需要）
        if "open" not in sig_df.columns:
            raise ValueError("策略返回的 DataFrame 必须包含 'open' 列（T+1 执行需要）")

        # TODO: 将 backtest.py / custom_strategy_backtest.py 中的核心回测逻辑迁移到这里
        # 当前为框架占位，后续迭代完成统一引擎
        result = BacktestResult()
        result.trades = []
        result.equity_curve = [self.initial_capital]
        result.equity_dates = [str(sig_df.index[0].date())] if len(sig_df) > 0 else []
        return result

    def run_batch(self, stock_data: Dict[str, pd.DataFrame], strategy: Strategy) -> Dict[str, BacktestResult]:
        """批量回测多只股票"""
        results = {}
        for code, df in stock_data.items():
            try:
                results[code] = self.run(df, strategy)
            except Exception as e:
                print(f"[Backtest] {code} 回测失败: {e}")
        return results
