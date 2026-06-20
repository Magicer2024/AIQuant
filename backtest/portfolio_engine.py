"""
portfolio_engine.py —— 个人组合回测引擎

模拟个人投资者的组合交易：
- 每日可买入 N 只股票（可配置）
- 支持多只股票同时持仓
- 完整的仓位管理和卖出规则（固定止损/止盈、移动止损、最大持有天数）
- 真实的费用计算（佣金+印花税）
- 逐日权益曲线
"""
import json
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from core.db import get_conn
from strategy.factor_lib import compute_all_factors

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """单只股票持仓"""
    code: str
    name: str
    entry_date: str
    entry_price: float
    shares: int
    cost: float  # 买入总成本（含手续费）
    holding_days: int = 0
    max_price: float = 0.0  # 持有期间最高价（用于移动止损）

    def __post_init__(self):
        self.max_price = self.entry_price


@dataclass
class PortfolioState:
    """组合状态"""
    cash: float
    positions: Dict[str, Position]  # code -> Position
    trade_date: str

    @property
    def position_count(self):
        return len(self.positions)

    def total_market_value(self, prices: Dict[str, float]) -> float:
        """计算持仓市值"""
        total = 0.0
        for code, pos in self.positions.items():
            px = prices.get(code, pos.entry_price)
            total += pos.shares * px
        return total

    def equity(self, prices: Dict[str, float]) -> float:
        """总资产 = 现金 + 持仓市值"""
        return self.cash + self.total_market_value(prices)


@dataclass
class TradeRecord:
    """交易记录"""
    code: str
    name: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float       # 盈亏金额
    pnl_pct: float    # 盈亏百分比
    holding_days: int
    exit_reason: str
    commission: float  # 总手续费


@dataclass
class PortfolioBacktestResult:
    """组合回测结果"""
    equity_curve: pd.Series           # 逐日权益曲线
    trades: List[TradeRecord]         # 全部交易记录
    daily_positions: List[dict]       # 每日持仓快照
    metrics: dict                     # 汇总指标


class PersonalPortfolioEngine:
    """个人组合回测引擎"""

    def __init__(self, config: dict = None):
        from config.personal_config import get_personal_config
        cfg = config or get_personal_config()

        self.initial_capital = cfg["initial_capital"]
        self.daily_buy_count = cfg["daily_buy_count"]
        self.position_mode = cfg["position_mode"]
        self.fixed_amount = cfg.get("fixed_amount_per_buy", 50000)
        self.max_positions = cfg["max_positions"]
        self.max_single_pct = cfg["max_single_position_pct"]

        self.stop_loss = cfg["stop_loss"]
        self.take_profit = cfg["take_profit"]
        self.trailing_stop_pct = cfg["trailing_stop_pct"]
        self.use_trailing_stop = cfg["use_trailing_stop"]
        self.holding_max = cfg["holding_max_days"]
        self.holding_min = cfg["holding_min_days"]

        self.min_score = cfg["min_score"]
        self.min_confidence = cfg["min_confidence"]
        self.slippage = cfg["slippage_rate"]
        self.commission_rate = cfg["commission_rate"]
        self.stamp_tax = cfg["stamp_tax_rate"]
        self.min_commission = cfg["min_commission"]

        self.cooldown_days = cfg.get("cooldown_days", 3)
        self.signal_rank_by = cfg.get("signal_rank_by", "fusion_score")

    def run(self, start_date: str, end_date: str,
            signals_by_date: Dict[str, List[dict]] = None) -> PortfolioBacktestResult:
        """
        运行组合回测。

        Args:
            start_date: 回测起始日期
            end_date: 回测结束日期
            signals_by_date: {trade_date: [{code, name, score, ...}]}
                             如果为 None，则从 stock_score 表读取

        Returns:
            PortfolioBacktestResult
        """
        if signals_by_date is None:
            signals_by_date = self._load_signals(start_date, end_date)

        price_data = self._load_prices(start_date, end_date)
        trade_dates = self._get_trade_dates(start_date, end_date, price_data)

        if not trade_dates:
            logger.warning("No trade dates in range %s ~ %s", start_date, end_date)
            return PortfolioBacktestResult(
                equity_curve=pd.Series(dtype=float),
                trades=[], daily_positions=[], metrics={}
            )

        # 初始化组合
        portfolio = PortfolioState(
            cash=self.initial_capital,
            positions={},
            trade_date=trade_dates[0],
        )

        equity_list = []
        daily_pos_list = []
        all_trades: List[TradeRecord] = []
        cooldown_map: Dict[str, str] = {}  # code -> 最后卖出日期

        for date_str in trade_dates:
            portfolio.trade_date = date_str
            day_prices = price_data.get(date_str, {})

            # ── Step 1: 更新持仓状态（持有天数、最高价）──
            for code, pos in list(portfolio.positions.items()):
                pos.holding_days += 1
                current_px = day_prices.get(code, pos.entry_price)
                pos.max_price = max(pos.max_price, current_px)

            # ── Step 2: 检查卖出信号 ──
            sells_today = []
            for code, pos in list(portfolio.positions.items()):
                current_px = day_prices.get(code, pos.entry_price)
                if current_px <= 0:
                    continue

                pnl_pct = (current_px / pos.entry_price) - 1
                exit_reason = self._check_exit(pos, pnl_pct, current_px, date_str)

                if exit_reason:
                    sells_today.append((code, current_px, exit_reason))

            # 执行卖出
            for code, exit_px, reason in sells_today:
                pos = portfolio.positions.pop(code)
                # 计算实际卖出价（滑点）
                actual_sell_px = exit_px * (1 - self.slippage)
                sell_value = pos.shares * actual_sell_px

                # 手续费
                commission = max(sell_value * self.commission_rate, self.min_commission)
                tax = sell_value * self.stamp_tax
                net_sell = sell_value - commission - tax

                pnl = net_sell - pos.cost
                pnl_pct = (pnl / pos.cost) * 100 if pos.cost > 0 else 0

                portfolio.cash += net_sell
                cooldown_map[code] = date_str

                trade = TradeRecord(
                    code=code, name=pos.name,
                    entry_date=pos.entry_date, exit_date=date_str,
                    entry_price=pos.entry_price, exit_price=round(actual_sell_px, 2),
                    shares=pos.shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
                    holding_days=pos.holding_days,
                    exit_reason=reason,
                    commission=round(commission + tax, 2),
                )
                all_trades.append(trade)

            # ── Step 3: 选股买入 ──
            day_signals = signals_by_date.get(date_str, [])
            if day_signals and portfolio.position_count < self.max_positions:
                # 过滤已持仓 + 冷却期
                available = []
                for sig in day_signals:
                    code = sig["code"]
                    if code in portfolio.positions:
                        continue
                    # 检查冷却期
                    last_sell = cooldown_map.get(code)
                    if last_sell:
                        try:
                            last_dt = datetime.strptime(last_sell, "%Y-%m-%d")
                            cur_dt = datetime.strptime(date_str, "%Y-%m-%d")
                            if (cur_dt - last_dt).days < self.cooldown_days:
                                continue
                        except ValueError:
                            pass
                    # 检查分数门槛
                    score = sig.get("score", sig.get("fusion_score", 0))
                    if score < self.min_score:
                        continue
                    available.append(sig)

                # 排序取 top N
                available.sort(key=lambda x: x.get(self.signal_rank_by, x.get("score", 0)), reverse=True)
                slots = self.max_positions - portfolio.position_count
                buy_count = min(self.daily_buy_count, slots, len(available))

                for i in range(buy_count):
                    sig = available[i]
                    code = sig["code"]
                    name = sig.get("name", "")
                    signal_px = day_prices.get(code, 0)
                    if signal_px <= 0:
                        continue

                    # T+1：次日开盘价买入
                    actual_buy_px = signal_px * (1 + self.slippage)

                    # 计算买入金额
                    buy_amount = self._calc_buy_amount(portfolio, actual_buy_px)
                    if buy_amount <= 0:
                        continue

                    # 手续费
                    commission = max(buy_amount * self.commission_rate, self.min_commission)
                    actual_cost = buy_amount + commission

                    if actual_cost > portfolio.cash:
                        # 资金不足，调整
                        available_cash = portfolio.cash - self.min_commission
                        if available_cash < actual_buy_px * 100:
                            continue
                        shares = int(available_cash / actual_buy_px / 100) * 100
                        if shares <= 0:
                            continue
                        actual_cost = shares * actual_buy_px + max(shares * actual_buy_px * self.commission_rate, self.min_commission)
                    else:
                        shares = int(buy_amount / actual_buy_px / 100) * 100
                        if shares <= 0:
                            continue
                        actual_cost = shares * actual_buy_px + max(shares * actual_buy_px * self.commission_rate, self.min_commission)

                    portfolio.cash -= actual_cost
                    portfolio.positions[code] = Position(
                        code=code, name=name,
                        entry_date=date_str,
                        entry_price=actual_buy_px,
                        shares=shares,
                        cost=actual_cost,
                    )

            # ── Step 4: 记录当日权益 ──
            eq = portfolio.equity(day_prices)
            equity_list.append({"date": date_str, "equity": eq})

            # 记录持仓快照
            pos_snapshot = {}
            for code, pos in portfolio.positions.items():
                px = day_prices.get(code, pos.entry_price)
                pos_snapshot[code] = {
                    "name": pos.name, "shares": pos.shares,
                    "entry_price": pos.entry_price, "current_price": px,
                    "pnl_pct": round((px / pos.entry_price - 1) * 100, 2),
                    "holding_days": pos.holding_days,
                }
            daily_pos_list.append({
                "date": date_str,
                "cash": round(portfolio.cash, 2),
                "market_value": round(portfolio.total_market_value(day_prices), 2),
                "equity": round(eq, 2),
                "positions": pos_snapshot,
            })

        # 计算汇总指标
        equity_series = pd.Series(
            [e["equity"] for e in equity_list],
            index=pd.DatetimeIndex([e["date"] for e in equity_list]),
        )
        metrics = self._calc_metrics(equity_series, all_trades, trade_dates)

        return PortfolioBacktestResult(
            equity_curve=equity_series,
            trades=all_trades,
            daily_positions=daily_pos_list,
            metrics=metrics,
        )

    # ── 内部方法 ──────────────────────────────

    def _check_exit(self, pos: Position, pnl_pct: float,
                    current_px: float, date_str: str) -> Optional[str]:
        """检查是否触发卖出"""
        # 最小持有天数
        if pos.holding_days < self.holding_min:
            return None

        # 1. 固定止损
        if pnl_pct <= self.stop_loss:
            return "stop_loss"

        # 2. 固定止盈
        if pnl_pct >= self.take_profit:
            return "take_profit"

        # 3. 移动止损
        if self.use_trailing_stop and pos.max_price > pos.entry_price:
            drawdown_from_peak = (current_px / pos.max_price) - 1
            if drawdown_from_peak <= -self.trailing_stop_pct:
                return "trailing_stop"

        # 4. 最大持有天数
        if pos.holding_days >= self.holding_max:
            return "max_hold"

        return None

    def _calc_buy_amount(self, portfolio: PortfolioState, price: float) -> float:
        """计算买入金额"""
        if self.position_mode == "fixed_amount":
            return min(self.fixed_amount, portfolio.cash * self.max_single_pct)
        else:
            # equal_weight: 剩余资金 / 剩余仓位数
            slots = self.max_positions - portfolio.position_count
            if slots <= 0:
                return 0
            per_slot = portfolio.cash / slots
            return min(per_slot, portfolio.cash * self.max_single_pct)

    def _load_signals(self, start_date: str, end_date: str) -> Dict[str, List[dict]]:
        """从 stock_score 表加载信号"""
        signals = {}
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT trade_date, code, name, score, rule_id, rule_name
                FROM stock_score
                WHERE trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date, score DESC
            """, (start_date, end_date)).fetchall()

        for r in rows:
            td = r["trade_date"]
            if td not in signals:
                signals[td] = []
            signals[td].append({
                "code": r["code"],
                "name": r["name"],
                "score": r["score"],
                "rule_id": r["rule_id"],
                "rule_name": r["rule_name"],
                "fusion_score": r["score"],
            })
        return signals

    def _load_prices(self, start_date: str, end_date: str) -> Dict[str, Dict[str, float]]:
        """加载价格数据: {trade_date: {code: close_price}}"""
        # 多取一些数据用于 T+1 计算
        try:
            ext_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
        except ValueError:
            ext_start = start_date

        prices = {}
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT trade_date, code, close
                FROM daily_price
                WHERE trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date, code
            """, (ext_start, end_date)).fetchall()

        for r in rows:
            td = r["trade_date"]
            if td not in prices:
                prices[td] = {}
            prices[td][r["code"]] = float(r["close"] or 0)
        return prices

    def _get_trade_dates(self, start_date: str, end_date: str,
                         price_data: dict) -> List[str]:
        """获取回测区间内的交易日"""
        dates = sorted(d for d in price_data.keys() if start_date <= d <= end_date)
        return dates

    def _calc_metrics(self, equity: pd.Series, trades: List[TradeRecord],
                      trade_dates: List[str]) -> dict:
        """计算回测汇总指标"""
        if equity.empty or len(equity) < 2:
            return {"total_return": 0, "annual_return": 0, "win_rate": 0,
                    "sharpe_ratio": 0, "max_drawdown": 0, "total_trades": 0}

        # 总收益
        total_return = (equity.iloc[-1] / equity.iloc[0] - 1) * 100

        # 年化收益
        try:
            start_dt = datetime.strptime(trade_dates[0], "%Y-%m-%d")
            end_dt = datetime.strptime(trade_dates[-1], "%Y-%m-%d")
            years = max((end_dt - start_dt).days / 365.25, 0.1)
        except ValueError:
            years = 1.0
        annual_return = ((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) * 100

        # 日收益率
        daily_returns = equity.pct_change().dropna()

        # 夏普比率（无风险利率 2%）
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() - 0.02 / 252) / daily_returns.std() * np.sqrt(252)
        else:
            sharpe = 0.0

        # 最大回撤
        running_max = equity.cummax()
        drawdown = (equity / running_max - 1) * 100
        max_drawdown = float(drawdown.min())

        # 胜率
        if trades:
            win_trades = sum(1 for t in trades if t.pnl > 0)
            total_trades = len(trades)
            win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0
        else:
            win_rate = 0
            total_trades = 0

        # 平均持仓天数
        avg_holding = np.mean([t.holding_days for t in trades]) if trades else 0

        # 盈亏比
        win_pnls = [t.pnl for t in trades if t.pnl > 0]
        loss_pnls = [abs(t.pnl) for t in trades if t.pnl < 0]
        avg_win = np.mean(win_pnls) if win_pnls else 0
        avg_loss = np.mean(loss_pnls) if loss_pnls else 1
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        # 总手续费
        total_commission = sum(t.commission for t in trades)

        return {
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return, 2),
            "win_rate": round(win_rate, 2),
            "sharpe_ratio": round(float(sharpe), 2),
            "max_drawdown": round(max_drawdown, 2),
            "total_trades": total_trades,
            "win_trades": sum(1 for t in trades if t.pnl > 0),
            "avg_holding_days": round(float(avg_holding), 1),
            "profit_loss_ratio": round(float(profit_loss_ratio), 2),
            "total_commission": round(total_commission, 2),
            "final_equity": round(float(equity.iloc[-1]), 2),
            "initial_capital": self.initial_capital,
        }
