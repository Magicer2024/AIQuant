"""unified_engine.py ? Unified backtest engine with consistent fee/slippage/position logic."""

from dataclasses import dataclass, field
from typing import List
import pandas as pd


@dataclass
class BacktestResult:
    """Result of a backtest run."""
    equity_curve: pd.Series = field(default_factory=pd.Series)
    trades: List[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class UnifiedBacktester:
    """Unified backtest engine with configurable parameters."""

    def __init__(self,
                 commission_rate: float = 0.0003,
                 stamp_tax: float = 0.001,
                 slippage_rate: float = 0.001,
                 stop_loss: float = -0.08,
                 take_profit: float = 0.20,
                 holding_max: int = 20):
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.slippage_rate = slippage_rate
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.holding_max = holding_max

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """Run backtest on dataframe with BUY_SIGNAL / SELL_SIGNAL columns.

        T+1 execution: signals fire at next day open with slippage.
        Exit triggers in order: stop_loss, take_profit, max_hold, SELL_SIGNAL.
        """
        df = df.copy()
        if "BUY_SIGNAL" not in df.columns:
            df["BUY_SIGNAL"] = False
        if "SELL_SIGNAL" not in df.columns:
            df["SELL_SIGNAL"] = False

        cash = 100000.0
        shares = 0
        trades = []
        equity = []
        entry_price = 0.0
        holding_days = 0

        for i, (idx, row) in enumerate(df.iterrows()):
            if shares > 0:
                holding_days += 1
                pnl_pct = (row["close"] / entry_price) - 1

                should_sell = False
                exit_reason = ""

                if pnl_pct <= self.stop_loss:
                    should_sell = True
                    exit_reason = "stop_loss"
                elif pnl_pct >= self.take_profit:
                    should_sell = True
                    exit_reason = "take_profit"
                elif holding_days >= self.holding_max:
                    should_sell = True
                    exit_reason = "max_hold"
                elif row.get("SELL_SIGNAL", False):
                    should_sell = True
                    exit_reason = "signal"

                if should_sell:
                    price = row["open"] * (1 - self.slippage_rate)
                    sell_value = shares * price
                    commission = sell_value * self.commission_rate
                    tax = sell_value * self.stamp_tax
                    cash = sell_value - commission - tax
                    trades.append({
                        "exit_date": str(idx.date()),
                        "exit_price": price,
                        "exit_reason": exit_reason,
                        "pnl_pct": pnl_pct,
                        "shares": shares,
                    })
                    shares = 0

            if row.get("BUY_SIGNAL", False) and shares == 0 and i + 1 < len(df):
                next_row = df.iloc[i + 1]
                price = next_row["open"] * (1 + self.slippage_rate)
                commission = cash * self.commission_rate
                buy_value = cash - commission
                shares = int(buy_value / price)
                entry_price = price
                holding_days = 0
                if shares > 0 and trades:
                    trades[-1]["entry_date"] = str(next_row.name.date())
                    trades[-1]["entry_price"] = price

            total_equity = cash + (shares * row["close"] if shares > 0 else 0)
            equity.append(total_equity)

        total_return = (equity[-1] / equity[0] - 1) * 100 if equity and equity[0] > 0 else 0.0
        return BacktestResult(
            equity_curve=pd.Series(equity, index=df.index),
            trades=trades,
            metrics={"total_return": total_return, "total_trades": len(trades)},
        )
