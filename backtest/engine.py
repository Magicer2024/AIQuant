"""
backtest/engine.py —— Backtrader 回测引擎

职责：
  1. 封装 Backtrader 回测流程
  2. 策略适配器
  3. 结果分析
"""

import backtrader as bt
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BacktestResult:
    """回测结果"""
    strategy_name: str
    start_date: str
    end_date: str
    initial_capital: float
    final_value: float
    total_return: float
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_duration: int
    total_trades: int
    win_rate: float
    profit_factor: float
    avg_profit: float
    avg_loss: float
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)


class FusionStrategy(bt.Strategy):
    """
    融合策略的 Backtrader 适配器
    基于多因子评分进行买卖决策
    """

    params = (
        ("score_threshold", 20.0),
        ("stop_loss", -0.06),
        ("take_profit", 0.20),
        ("max_positions", 5),
        ("single_pos_ratio", 0.20),
    )

    def __init__(self):
        self.orders = {}
        self.positions_count = 0

    def next(self):
        """每个bar执行"""
        # 这里简化处理，实际应接入多因子评分
        # TODO: 接入 strategy_engine 的评分逻辑
        pass

    def notify_order(self, order):
        """订单状态回调"""
        if order.status in [order.Completed]:
            if order.isbuy():
                self.positions_count += 1
            else:
                self.positions_count -= 1


class BacktestEngine:
    """回测引擎"""

    def __init__(self):
        self.cerebro = bt.Cerebro()
        self.results = []

    def run_backtest(self, data: pd.DataFrame, strategy_params: dict = None,
                     initial_cash: float = 100000.0,
                     commission: float = 0.0003) -> BacktestResult:
        """
        执行回测

        :param data: DataFrame 含 open/high/low/close/volume
        :param strategy_params: 策略参数字典
        :param initial_cash: 初始资金
        :param commission: 手续费率
        :return: 回测结果
        """
        cerebro = bt.Cerebro()

        # 设置初始资金
        cerebro.broker.setcash(initial_cash)
        cerebro.broker.setcommission(commission=commission)

        # 添加数据
        data_feed = bt.feeds.PandasData(dataname=data)
        cerebro.adddata(data_feed)

        # 添加策略
        params = strategy_params or {}
        cerebro.addstrategy(FusionStrategy, **params)

        # 添加分析器
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")

        # 运行回测
        results = cerebro.run()
        strat = results[0]

        # 提取结果
        sharpe = strat.analyzers.sharpe.get_analysis()
        drawdown = strat.analyzers.drawdown.get_analysis()
        trades = strat.analyzers.trades.get_analysis()
        returns = strat.analyzers.returns.get_analysis()

        # 构建结果
        result = BacktestResult(
            strategy_name="fusion",
            start_date=str(data.index[0]),
            end_date=str(data.index[-1]),
            initial_capital=initial_cash,
            final_value=cerebro.broker.getvalue(),
            total_return=(cerebro.broker.getvalue() / initial_cash - 1) * 100,
            annual_return=returns.get("rnorm100", 0),
            sharpe_ratio=sharpe.get("sharperatio", 0) or 0,
            max_drawdown=drawdown.get("max", {}).get("drawdown", 0),
            max_drawdown_duration=drawdown.get("max", {}).get("len", 0),
            total_trades=trades.get("total", {}).get("total", 0) if trades else 0,
            win_rate=(trades.get("won", {}).get("total", 0) / trades.get("total", {}).get("total", 1) * 100) if trades else 0,
            profit_factor=0,
            avg_profit=0,
            avg_loss=0,
        )

        return result

    def optimize(self, data: pd.DataFrame, param_grid: list[dict],
                 initial_cash: float = 100000.0) -> list[BacktestResult]:
        """
        参数优化

        :param data: 行情数据
        :param param_grid: 参数网格列表
        :param initial_cash: 初始资金
        :return: 各参数组合的回测结果
        """
        results = []
        for params in param_grid:
            try:
                result = self.run_backtest(data, params, initial_cash)
                result.strategy_name = f"fusion_{params.get('name', 'default')}"
                results.append(result)
            except Exception as e:
                print(f"[Backtest] 参数 {params} 回测失败: {e}")

        return results


# 全局单例
_backtest_engine: BacktestEngine | None = None


def get_backtest_engine() -> BacktestEngine:
    """获取回测引擎单例"""
    global _backtest_engine
    if _backtest_engine is None:
        _backtest_engine = BacktestEngine()
    return _backtest_engine
