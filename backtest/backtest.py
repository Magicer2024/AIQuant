"""
回测引擎
支持：
- 买入持有策略回测
- 止损止盈管理
- 详细绩效评估（年化收益、最大回撤、夏普比率、胜率等）
- 逐笔交易记录
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime


@dataclass
class Trade:
    """单笔交易记录"""
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    shares: float = 0.0
    direction: str = "long"
    exit_reason: str = ""  # signal/stop_loss/take_profit/end
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0
    slippage: float = 0.0  # 滑点成本（买入多加/卖出少收）
    trigger_strategy: str = ""  # R5: 触发本次买入的策略名称


@dataclass
class BacktestResult:
    """回测结果"""
    total_return: float = 0.0        # 总收益率
    annual_return: float = 0.0       # 年化收益率
    max_drawdown: float = 0.0        # 最大回撤
    sharpe_ratio: float = 0.0        # 夏普比率
    win_rate: float = 0.0            # 胜率
    profit_factor: float = 0.0       # 盈亏比
    total_trades: int = 0            # 总交易次数
    winning_trades: int = 0          # 盈利交易次数
    losing_trades: int = 0           # 亏损交易次数
    avg_holding_days: float = 0.0    # 平均持仓天数
    avg_profit_pct: float = 0.0      # 平均盈利幅度
    avg_loss_pct: float = 0.0        # 平均亏损幅度
    final_capital: float = 0.0       # 最终资金
    benchmark_return: float = 0.0    # 基准收益率（买入持有）
    alpha: float = 0.0               # 超额收益
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    equity_dates: List[str] = field(default_factory=list)


class Backtester:
    """
    中线量化回测引擎
    特性：
    - 每次交易只使用固定比例资金（默认100%单笔）
    - 支持手续费（默认万分之三，双向）
    - 支持止损止盈
    - 不允许做空（A股规则）
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.0003,   # 万三手续费
        stamp_tax: float = 0.001,           # 印花税（卖出单向）
        position_pct: float = 1.0,          # 每次建仓资金比例
        max_holding_days: int = 40,          # 最大持仓天数（中线上限）
        use_stop_loss: bool = True,
        use_take_profit: bool = True,
        slippage_rate: float = 0.001,       # 滑点率（买/卖时额外的价格冲击，千分之一）
        # R1: 回撤熔断
        use_drawdown_guard: bool = True,    # 启用回撤熔断
        drawdown_threshold: float = 0.10,   # 回撤超过此值则暂停开仓（10%）
        consecutive_loss_limit: int = 3,     # 连续亏损达到此数则暂停开仓
        # R2: 大盘择时
        use_market_timing: bool = True,     # 启用大盘均线过滤
        market_timing_ma: int = 20,        # 大盘指数均线周期
        market_timing_field: str = "index_close",  # df中的大盘指数列名
        # R3: 动态仓位（基于ATR波动率）
        use_dynamic_position: bool = True,  # 启用动态仓位
        volatility_period: int = 20,        # ATR计算周期
        min_position_ratio: float = 0.3,   # 最小仓位比例（相对base position_pct）
        max_position_ratio: float = 1.5,   # 最大仓位比例（相对base position_pct）
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.position_pct = position_pct
        self.max_holding_days = max_holding_days
        self.use_stop_loss = use_stop_loss
        self.use_take_profit = use_take_profit
        self.slippage_rate = slippage_rate
        # R1
        self.use_drawdown_guard = use_drawdown_guard
        self.drawdown_threshold = drawdown_threshold
        self.consecutive_loss_limit = consecutive_loss_limit
        # R2
        self.use_market_timing = use_market_timing
        self.market_timing_ma = market_timing_ma
        self.market_timing_field = market_timing_field
        # R3
        self.use_dynamic_position = use_dynamic_position
        self.volatility_period = volatility_period
        self.min_position_ratio = min_position_ratio
        self.max_position_ratio = max_position_ratio

    def _calc_commission(self, price: float, shares: float, side: str = "buy") -> float:
        """计算交易成本"""
        amount = price * shares
        comm = amount * self.commission_rate
        comm = max(comm, 5.0)  # 最低5元
        if side == "sell":
            comm += amount * self.stamp_tax
        return comm

    def run(self, df_strategy: pd.DataFrame) -> BacktestResult:
        """
        执行回测（T+1 模式：信号日触发 → 次日开盘价执行）
        :param df_strategy: 含BUY_SIGNAL, SELL_SIGNAL, close, open 列的DataFrame
        :return: BacktestResult

        T+1 规则：
          - 买入信号日收盘判断 → 次日开盘价买入（含滑点）
          - 止损/止盈/超期/卖出信号 → 当日收盘判断，次日开盘价卖出
          - 当日无法执行当日信号，杜绝未来数据泄露
        """
        capital = self.initial_capital
        position = 0.0       # 持仓股数
        entry_price = 0.0
        entry_date = None
        stop_loss = 0.0
        take_profit = 0.0
        holding_days = 0

        # R5: 持仓中记录触发策略
        _last_trigger = "融合信号"

        # R1: 回撤熔断状态
        consecutive_losses = 0    # 连续亏损计数
        peak_capital = capital    # 权益峰值
        circuit_broken = False   # 熔断标志

        # R2: 大盘均线预计算（仅取已积累足够数据时的MA）
        index_ma_series = pd.Series(dtype=float)
        if self.use_market_timing and self.market_timing_field in df_strategy.columns:
            idx_col = df_strategy[self.market_timing_field].fillna(df_strategy["close"])
            index_ma_series = idx_col.rolling(self.market_timing_ma, min_periods=self.market_timing_ma).mean()

        # R3: ATR波动率预计算（用于动态仓位）
        atr_series = pd.Series(dtype=float)
        if self.use_dynamic_position and len(df_strategy) >= self.volatility_period:
            high = df_strategy["high"] if "high" in df_strategy.columns else df_strategy["close"]
            low = df_strategy["low"] if "low" in df_strategy.columns else df_strategy["close"]
            close_arr = df_strategy["close"]
            tr1 = high - low
            tr2 = (high - close_arr.shift(1)).abs()
            tr3 = (low - close_arr.shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr_series = tr.rolling(self.volatility_period, min_periods=self.volatility_period).mean()

        # R5: 识别各策略评分列（VOL_SCORE, MA_SCORE, DIVERGE_SCORE, BOTTOM_SCORE, WHALE_SCORE）
        strategy_score_cols = {
            "放量突破": "VOL_SCORE",
            "均线粘合": "MA_SCORE",
            "量价背离": "DIVERGE_SCORE",
            "抄底型": "BOTTOM_SCORE",
            "主力建仓": "WHALE_SCORE",
        }
        active_strategy_cols = {label: col for label, col in strategy_score_cols.items() if col in df_strategy.columns}

        trades: List[Trade] = []
        equity_curve = [capital]
        equity_dates = [str(df_strategy.index[0].date())]

        # 预取 open 列，加速次日开盘价查找
        has_open = "open" in df_strategy.columns
        dates_list = df_strategy.index.tolist()
        n = len(dates_list)

        # 延迟执行队列：signal日标记 → next日执行
        pending_buy = False       # 次日开盘买入标记
        pending_buy_trigger = ""  # 触发策略名
        pending_sell_reason = None  # 次日开盘卖出原因

        for i, (date, row) in enumerate(df_strategy.iterrows()):
            date_str = str(date.date())
            close_price = row["close"]

            # R1: 权益恢复则解除熔断
            if self.use_drawdown_guard and circuit_broken:
                if capital >= peak_capital:
                    circuit_broken = False

            # ── T+1：执行昨日标记的延迟操作（用今日开盘价） ──
            open_price = row["open"] if has_open else close_price
            if pd.isna(open_price) or open_price <= 0:
                open_price = close_price

            # 执行延迟卖出
            if pending_sell_reason and position > 0:
                # 用当日开盘价卖出（含滑点）
                slip_price = open_price * (1 - self.slippage_rate)
                sell_comm = self._calc_commission(slip_price, position, "sell")
                sell_amount = slip_price * position - sell_comm
                capital += sell_amount
                trade_slippage = (open_price - slip_price) * position

                buy_comm = self._calc_commission(entry_price, position, "buy")
                pnl = sell_amount - entry_price * position - buy_comm
                pnl_pct = (slip_price - entry_price) / entry_price * 100

                trades.append(Trade(
                    entry_date=str(entry_date.date()),
                    entry_price=round(entry_price, 3),
                    exit_date=date_str,
                    exit_price=round(slip_price, 3),
                    shares=round(position, 0),
                    exit_reason=pending_sell_reason,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 2),
                    holding_days=holding_days,
                    slippage=round(trade_slippage, 2),
                    trigger_strategy=_last_trigger
                ))

                # R1: 更新熔断状态
                if self.use_drawdown_guard:
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    peak_capital = max(peak_capital, capital)
                    drawdown = (peak_capital - capital) / peak_capital if peak_capital > 0 else 0
                    if drawdown >= self.drawdown_threshold or consecutive_losses >= self.consecutive_loss_limit:
                        circuit_broken = True

                position = 0.0
                holding_days = 0
                pending_sell_reason = None

            # 执行延迟买入
            if pending_buy and position == 0:
                # R1: 熔断中，禁止开仓
                if self.use_drawdown_guard and circuit_broken:
                    pass
                else:
                    # R3: 动态仓位
                    effective_pos_pct = self.position_pct
                    if self.use_dynamic_position and len(atr_series) > i:
                        atr_val = atr_series.iloc[i]
                        if not pd.isna(atr_val) and open_price > 0:
                            vol_ratio = atr_val / open_price
                            base_vol = 0.02
                            vol_factor = base_vol / vol_ratio * 2 * self.position_pct
                            effective_pos_pct = np.clip(
                                vol_factor,
                                self.position_pct * self.min_position_ratio,
                                self.position_pct * self.max_position_ratio
                            )

                    buy_capital = capital * effective_pos_pct
                    slip_price = open_price * (1 + self.slippage_rate)
                    shares = int(buy_capital / slip_price / 100) * 100  # 整手
                    if shares >= 100:
                        buy_comm = self._calc_commission(slip_price, shares, "buy")
                        cost = slip_price * shares + buy_comm
                        if cost <= capital:
                            capital -= cost
                            position = shares
                            entry_price = slip_price
                            entry_date = date
                            holding_days = 0
                            stop_loss = float(row.get("STOP_LOSS", close_price * 0.93))
                            take_profit = float(row.get("TAKE_PROFIT", close_price * 1.15))
                            _last_trigger = pending_buy_trigger

                pending_buy = False
                pending_buy_trigger = ""

            # ── 持仓中：用收盘价检查是否触发卖出（标记为次日执行）──
            if position > 0:
                holding_days += 1
                exit_reason = None

                if self.use_stop_loss and close_price <= stop_loss:
                    exit_reason = "止损"
                elif self.use_take_profit and close_price >= take_profit:
                    exit_reason = "止盈"
                elif holding_days >= self.max_holding_days:
                    exit_reason = "超期平仓"
                elif row.get("SELL_SIGNAL", False):
                    exit_reason = "卖出信号"

                if exit_reason and i + 1 < n:
                    pending_sell_reason = exit_reason
                elif exit_reason and i + 1 >= n:
                    # 最后一日无法次日执行，用收盘价直接卖出
                    slip_price = close_price * (1 - self.slippage_rate)
                    sell_comm = self._calc_commission(slip_price, position, "sell")
                    sell_amount = slip_price * position - sell_comm
                    capital += sell_amount
                    trade_slippage = (close_price - slip_price) * position
                    buy_comm = self._calc_commission(entry_price, position, "buy")
                    pnl = sell_amount - entry_price * position - buy_comm
                    pnl_pct = (slip_price - entry_price) / entry_price * 100

                    trades.append(Trade(
                        entry_date=str(entry_date.date()),
                        entry_price=round(entry_price, 3),
                        exit_date=date_str,
                        exit_price=round(slip_price, 3),
                        shares=round(position, 0),
                        exit_reason=exit_reason,
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct, 2),
                        holding_days=holding_days,
                        slippage=round(trade_slippage, 2),
                        trigger_strategy=_last_trigger
                    ))

                    if self.use_drawdown_guard:
                        if pnl < 0:
                            consecutive_losses += 1
                        else:
                            consecutive_losses = 0
                        peak_capital = max(peak_capital, capital)
                        drawdown = (peak_capital - capital) / peak_capital if peak_capital > 0 else 0
                        if drawdown >= self.drawdown_threshold or consecutive_losses >= self.consecutive_loss_limit:
                            circuit_broken = True

                    position = 0.0
                    holding_days = 0

            # ── 空仓：用收盘价检查买入信号（标记为次日执行）──
            elif row.get("BUY_SIGNAL", False) and i + 1 < n:
                # R2: 大盘均线过滤
                if self.use_drawdown_guard and circuit_broken:
                    pass  # 熔断中
                elif self.use_market_timing and len(index_ma_series) > i:
                    index_ma = index_ma_series.iloc[i]
                    if not pd.isna(index_ma) and close_price < index_ma:
                        pass  # 大盘弱势，禁止开仓
                    else:
                        # R5: 信号溯源
                        trigger = "融合信号"
                        if active_strategy_cols:
                            best_label, best_score = "", 0.0
                            for label, col in active_strategy_cols.items():
                                s = float(row.get(col, 0) or 0)
                                if s > best_score:
                                    best_score = s
                                    best_label = label
                            if best_label and best_score > 0:
                                trigger = f"{best_label}(高)" if best_score >= 2.0 else f"{best_label}(低)"

                        pending_buy = True
                        pending_buy_trigger = trigger
                else:
                    # R5: 信号溯源
                    trigger = "融合信号"
                    if active_strategy_cols:
                        best_label, best_score = "", 0.0
                        for label, col in active_strategy_cols.items():
                            s = float(row.get(col, 0) or 0)
                            if s > best_score:
                                best_score = s
                                best_label = label
                        if best_label and best_score > 0:
                            trigger = f"{best_label}(高)" if best_score >= 2.0 else f"{best_label}(低)"

                    pending_buy = True
                    pending_buy_trigger = trigger

            # 记录每日净值
            portfolio_value = capital + position * close_price
            equity_curve.append(portfolio_value)
            equity_dates.append(date_str)

        # ── 强制平仓最后持仓 ──
        if position > 0:
            last_price = df_strategy.iloc[-1]["close"]
            last_date = str(df_strategy.index[-1].date())
            slip_price = last_price * (1 - self.slippage_rate)
            sell_comm = self._calc_commission(slip_price, position, "sell")
            sell_amount = slip_price * position - sell_comm
            capital += sell_amount
            trade_slippage = (last_price - slip_price) * position
            pnl_pct = (slip_price - entry_price) / entry_price * 100
            trades.append(Trade(
                entry_date=str(entry_date.date()),
                entry_price=round(entry_price, 3),
                exit_date=last_date,
                exit_price=round(slip_price, 3),
                shares=round(position, 0),
                exit_reason="回测结束",
                pnl=round(sell_amount - entry_price * position, 2),
                pnl_pct=round(pnl_pct, 2),
                holding_days=holding_days,
                slippage=round(trade_slippage, 2),
                trigger_strategy=_last_trigger
            ))
            equity_curve[-1] = capital

        return self._calc_metrics(trades, equity_curve, equity_dates, df_strategy)

    def _calc_metrics(self, trades, equity_curve, equity_dates, df_strategy) -> BacktestResult:
        """计算绩效指标"""
        result = BacktestResult()
        result.trades = trades
        result.equity_curve = equity_curve
        result.equity_dates = equity_dates

        if not equity_curve:
            return result

        final_capital = equity_curve[-1]
        result.final_capital = round(final_capital, 2)
        result.total_return = round((final_capital / self.initial_capital - 1) * 100, 2)

        # 年化收益
        n_days = max(len(df_strategy), 1)
        years = n_days / 252
        result.annual_return = round((
            (final_capital / self.initial_capital) ** (1 / max(years, 0.1)) - 1
        ) * 100, 2)

        # 最大回撤
        equity_arr = np.array(equity_curve)
        rolling_max = np.maximum.accumulate(equity_arr)
        drawdown = (equity_arr - rolling_max) / rolling_max * 100
        result.max_drawdown = round(float(np.min(drawdown)), 2)

        # 夏普比率
        daily_returns = np.diff(equity_arr) / equity_arr[:-1]
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            result.sharpe_ratio = round(
                float(daily_returns.mean() / daily_returns.std() * np.sqrt(252)), 2
            )

        # 交易统计
        result.total_trades = len(trades)
        winning = [t for t in trades if t.pnl_pct > 0]
        losing = [t for t in trades if t.pnl_pct <= 0]
        result.winning_trades = len(winning)
        result.losing_trades = len(losing)
        result.win_rate = round(len(winning) / max(len(trades), 1) * 100, 1)

        if winning:
            result.avg_profit_pct = round(np.mean([t.pnl_pct for t in winning]), 2)
        if losing:
            result.avg_loss_pct = round(np.mean([t.pnl_pct for t in losing]), 2)

        total_profit = sum(t.pnl for t in winning) if winning else 0
        total_loss = abs(sum(t.pnl for t in losing)) if losing else 1
        result.profit_factor = round(total_profit / max(total_loss, 1), 2)

        if trades:
            result.avg_holding_days = round(np.mean([t.holding_days for t in trades]), 1)

        # 基准收益（买入持有）
        first_price = df_strategy.iloc[0]["close"]
        last_price = df_strategy.iloc[-1]["close"]
        result.benchmark_return = round((last_price / first_price - 1) * 100, 2)
        result.alpha = round(result.total_return - result.benchmark_return, 2)

        return result

    def get_summary(self, result: BacktestResult) -> dict:
        """返回易于展示的摘要字典"""
        total_slippage = sum(t.slippage for t in result.trades)
        return {
            "初始资金": f"¥{self.initial_capital:,.0f}",
            "最终资金": f"¥{result.final_capital:,.2f}",
            "总收益率": f"{result.total_return:+.2f}%",
            "年化收益率": f"{result.annual_return:+.2f}%",
            "最大回撤": f"{result.max_drawdown:.2f}%",
            "夏普比率": f"{result.sharpe_ratio:.2f}",
            "胜率": f"{result.win_rate:.1f}%",
            "盈亏比": f"{result.profit_factor:.2f}",
            "总交易次数": result.total_trades,
            "盈利交易": result.winning_trades,
            "亏损交易": result.losing_trades,
            "平均持仓天数": f"{result.avg_holding_days:.1f}天",
            "平均盈利幅度": f"{result.avg_profit_pct:+.2f}%",
            "平均亏损幅度": f"{result.avg_loss_pct:.2f}%",
            "基准收益（买入持有）": f"{result.benchmark_return:+.2f}%",
            "超额收益（Alpha）": f"{result.alpha:+.2f}%",
            "总滑点成本": f"¥{total_slippage:,.2f}",
        }


if __name__ == "__main__":
    from data_fetcher import get_stock_history
    from strategy import run_strategy

    print("正在回测 平安银行(000001)...")
    df = get_stock_history("000001", start_date="20220101")
    df_s = run_strategy(df, "composite")

    bt = Backtester(initial_capital=100000)
    result = bt.run(df_s)
    summary = bt.get_summary(result)

    print("\n─── 回测结果 ───")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print(f"\n─── 最近5笔交易 ───")
    for t in result.trades[-5:]:
        print(f"  {t.entry_date} 买入 {t.entry_price} → {t.exit_date} 卖出 {t.exit_price}"
              f"  {t.pnl_pct:+.1f}%  [{t.exit_reason}]  持仓{t.holding_days}天")
