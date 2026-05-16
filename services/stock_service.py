"""
services/stock_service.py —— 股票数据服务（Qlib 集成版）
"""
import traceback
from typing import List, Dict, Any, Optional

from core.data_fetcher import get_stock_history, get_stock_info, search_stocks, get_hot_stocks
from core.db import get_daily_price
import core.db as db

from strategy.strategy import run_strategy, get_latest_signal
from strategy.strategies import (
    strategy_volume_breakout,
    strategy_ma_convergence,
    strategy_price_volume_divergence,
    strategy_bottom_fishing,
    strategy_whale_accumulation,
    fuse_signals,
)

# Backtester: prefer Qlib adapter, fall back to legacy engine
try:
    from qlib_engine.strategy_adapter import backtest_single_rule, BacktestConfig
    _HAS_QLIB_BACKTEST = True
except ImportError:
    _HAS_QLIB_BACKTEST = False

try:
    from backtest.backtest import Backtester
    _HAS_LEGACY_BACKTEST = True
except ImportError:
    Backtester = None
    _HAS_LEGACY_BACKTEST = False


def _backtest_via_qlib(df_s, symbol, capital, strategy_label="composite"):
    """Run backtest via Qlib SimulatorExecutor.

    Converts DataFrame BUY_SIGNAL rows to Qlib signal list,
    runs backtest_single_rule, and returns a result object
    compatible with the legacy Backtester interface.
    """
    from dataclasses import dataclass, field

    signals = []
    for idx, row in df_s.iterrows():
        if row.get("BUY_SIGNAL", False):
            trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
            signals.append({
                "code": symbol,
                "trade_date": trade_date,
                "score": float(row.get("COMPOSITE_SCORE", row.get("BUY_SCORE", 50))),
            })

    if len(signals) < 2:
        return _empty_backtest_result()

    start_date = str(df_s.index[0].date()) if hasattr(df_s.index[0], "date") else str(df_s.index[0])[:10]
    end_date = str(df_s.index[-1].date()) if hasattr(df_s.index[-1], "date") else str(df_s.index[-1])[:10]

    config = BacktestConfig(
        start_time=start_date,
        end_time=end_date,
        account=capital,
        topk=1,
        n_drop=0,
    )

    perf = backtest_single_rule(symbol, signals, config)

    @dataclass
    class QlibBacktestResult:
        total_return: float = 0.0
        annual_return: float = 0.0
        max_drawdown: float = 0.0
        sharpe_ratio: float = 0.0
        win_rate: float = 0.0
        profit_factor: float = 0.0
        total_trades: int = 0
        benchmark_return: float = 0.0
        alpha: float = 0.0
        avg_holding_days: float = 0.0
        trades: list = field(default_factory=list)
        equity_dates: list = field(default_factory=list)
        equity_curve: list = field(default_factory=list)

    return QlibBacktestResult(
        total_return=perf.get("annual_return", 0),
        annual_return=perf.get("annual_return", 0),
        max_drawdown=perf.get("max_drawdown", 0),
        sharpe_ratio=perf.get("sharpe_ratio", 0),
        win_rate=perf.get("win_rate", 0),
        total_trades=perf.get("total_trades", 0),
    )


def _empty_backtest_result():
    """Return a null backtest result compatible with Backtester interface."""
    from dataclasses import dataclass, field

    @dataclass
    class EmptyResult:
        total_return: float = 0.0
        annual_return: float = 0.0
        max_drawdown: float = 0.0
        sharpe_ratio: float = 0.0
        win_rate: float = 0.0
        profit_factor: float = 0.0
        total_trades: int = 0
        benchmark_return: float = 0.0
        alpha: float = 0.0
        avg_holding_days: float = 0.0
        trades: list = field(default_factory=list)
        equity_dates: list = field(default_factory=list)
        equity_curve: list = field(default_factory=list)

    return EmptyResult()


def get_stock_list(with_score: bool = True) -> Dict[str, Any]:
    """获取所有股票最新行情"""
    with db.get_conn() as conn:
        latest = conn.execute(
            "SELECT MAX(trade_date) as d FROM daily_price"
        ).fetchone()["d"]
        if not latest:
            return {"stocks": [], "trade_date": None}

        score_cols = ", p.fusion_score" if with_score else ""
        rows = conn.execute(f"""
            SELECT p.code, s.name, s.market,
                   p.trade_date, p.close, p.open, p.high, p.low,
                   p.volume, p.pct_change, p.turnover{score_cols}
            FROM daily_price p
            JOIN stock_info s ON p.code = s.code
            WHERE p.trade_date = (
                SELECT MAX(dp.trade_date) FROM daily_price dp WHERE dp.code = p.code
            )
            ORDER BY p.volume DESC
        """).fetchall()

        return {"stocks": [dict(row) for row in rows], "trade_date": latest}


def get_kline_data(code: str, start_date: str = "", end_date: str = "") -> Dict[str, Any]:
    """获取单股K线数据"""
    from routes.common import safe_float
    df = get_daily_price(code, start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        raise ValueError(f"数据库中未找到股票 {code} 的数据")

    data = []
    for dt, row in df.iterrows():
        data.append({
            "date": str(dt.date()) if hasattr(dt, "date") else str(dt)[:10],
            "open": round(safe_float(row["open"], 0.0), 2),
            "close": round(safe_float(row["close"], 0.0), 2),
            "high": round(safe_float(row["high"], 0.0), 2),
            "low": round(safe_float(row["low"], 0.0), 2),
            "volume": safe_float(row["volume"], 0.0),
            "pct_change": round(safe_float(row.get("pct_change"), 0.0), 2),
            "turnover": round(safe_float(row.get("turnover"), 0.0), 2),
        })
    return {"code": code, "data": data}


def analyze_stock(symbol: str, start_date: str = "", strategy_name: str = "composite") -> Dict[str, Any]:
    """股票综合分析（K线 + 指标 + 信号）"""
    from routes.common import safe_float, df_to_json_safe

    kwargs = {"symbol": symbol}
    if start_date:
        kwargs["start_date"] = start_date
    df = get_stock_history(**kwargs)

    df_s = run_strategy(df, strategy_name)
    signal = get_latest_signal(df_s)

    kline = []
    for idx, row in df_s.iterrows():
        kline.append({
            "date": str(idx.date()),
            "open": safe_float(row["open"]),
            "high": safe_float(row["high"]),
            "low": safe_float(row["low"]),
            "close": safe_float(row["close"]),
            "volume": safe_float(row["volume"]),
        })

    buy_signals = []
    sell_signals = []
    for idx, row in df_s.iterrows():
        if row.get("BUY_SIGNAL", False):
            buy_signals.append({
                "date": str(idx.date()),
                "price": safe_float(row["close"]),
                "score": safe_float(row.get("COMPOSITE_SCORE", 0)),
                "stop_loss": safe_float(row.get("STOP_LOSS")),
                "take_profit": safe_float(row.get("TAKE_PROFIT")),
            })
        if row.get("SELL_SIGNAL", False):
            sell_signals.append({
                "date": str(idx.date()),
                "price": safe_float(row["close"]),
            })

    score_data = []
    if "COMPOSITE_SCORE" in df_s.columns:
        score_data = df_to_json_safe(df_s, ["COMPOSITE_SCORE"])

    return {
        "symbol": symbol,
        "signal": signal,
        "kline": kline,
        "ma": df_to_json_safe(df_s, ["MA5", "MA10", "MA20", "MA60"]),
        "macd": df_to_json_safe(df_s, ["MACD_DIF", "MACD_DEA", "MACD_HIST"]),
        "rsi": df_to_json_safe(df_s, ["RSI6", "RSI14", "RSI24"]),
        "kdj": df_to_json_safe(df_s, ["KDJ_K", "KDJ_D", "KDJ_J"]),
        "boll": df_to_json_safe(df_s, ["BOLL_UPPER", "BOLL_MID", "BOLL_LOWER"]),
        "score": score_data,
        "buy_signals": buy_signals,
        "sell_signals": sell_signals,
    }


_STRATEGY_MAP = {
    "composite": (
        "融合策略",
        lambda df: fuse_signals([
            strategy_volume_breakout(df),
            strategy_ma_convergence(df),
            strategy_price_volume_divergence(df),
            strategy_bottom_fishing(df),
            strategy_whale_accumulation(df),
        ])
    ),
    "ma_trend": ("放量突破", lambda df: strategy_volume_breakout(df)),
    "macd": ("量价背离", lambda df: strategy_price_volume_divergence(df)),
    "boll": ("主力建仓", lambda df: strategy_whale_accumulation(df)),
}


def run_single_backtest(symbol: str, start_date: str = "20220101",
                        strategy_name: str = "composite", capital: float = 100000) -> Dict[str, Any]:
    """单股策略回测（优先使用 Qlib 引擎）"""
    strategy_name = strategy_name if strategy_name in _STRATEGY_MAP else "composite"
    strategy_label, strategy_fn = _STRATEGY_MAP[strategy_name]

    df = get_stock_history(symbol=symbol, start_date=start_date)
    df_s = strategy_fn(df)

    # Try Qlib first, fall back to legacy Backtester
    if _HAS_QLIB_BACKTEST:
        try:
            result = _backtest_via_qlib(df_s, symbol, capital, strategy_name)
            summary = {
                "total_trades": result.total_trades,
                "win_rate": result.win_rate,
                "total_return": result.total_return,
                "annual_return": result.annual_return,
                "max_drawdown": result.max_drawdown,
                "sharpe_ratio": result.sharpe_ratio,
            }
            return {
                "symbol": symbol,
                "summary": summary,
                "metrics": {
                    "total_return": result.total_return,
                    "annual_return": result.annual_return,
                    "max_drawdown": result.max_drawdown,
                    "sharpe_ratio": result.sharpe_ratio,
                    "win_rate": result.win_rate,
                    "profit_factor": result.profit_factor,
                    "total_trades": result.total_trades,
                    "benchmark_return": result.benchmark_return,
                    "alpha": result.alpha,
                    "avg_holding_days": result.avg_holding_days,
                },
                "equity_curve": [
                    {"date": d, "value": v}
                    for d, v in zip(result.equity_dates, result.equity_curve)
                ],
                "trades": [{
                    "entry_date": t.entry_date if hasattr(t, "entry_date") else "",
                    "entry_price": t.entry_price if hasattr(t, "entry_price") else 0,
                    "exit_date": t.exit_date if hasattr(t, "exit_date") else "",
                    "exit_price": t.exit_price if hasattr(t, "exit_price") else 0,
                    "shares": t.shares if hasattr(t, "shares") else 0,
                    "exit_reason": t.exit_reason if hasattr(t, "exit_reason") else "signal",
                    "pnl": t.pnl if hasattr(t, "pnl") else 0,
                    "pnl_pct": t.pnl_pct if hasattr(t, "pnl_pct") else 0,
                    "holding_days": t.holding_days if hasattr(t, "holding_days") else 0,
                } for t in (result.trades or [])],
                "engine": "qlib",
            }
        except Exception:
            pass

    # Fallback to legacy Backtester
    if _HAS_LEGACY_BACKTEST:
        bt = Backtester(initial_capital=capital)
        result = bt.run(df_s)
        summary = bt.get_summary(result)

        trades_list = []
        for t in result.trades:
            trades_list.append({
                "entry_date": t.entry_date,
                "entry_price": t.entry_price,
                "exit_date": t.exit_date,
                "exit_price": t.exit_price,
                "shares": t.shares,
                "exit_reason": t.exit_reason,
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "holding_days": t.holding_days,
            })

        return {
            "symbol": symbol,
            "summary": summary,
            "metrics": {
                "total_return": result.total_return,
                "annual_return": result.annual_return,
                "max_drawdown": result.max_drawdown,
                "sharpe_ratio": result.sharpe_ratio,
                "win_rate": result.win_rate,
                "profit_factor": result.profit_factor,
                "total_trades": result.total_trades,
                "benchmark_return": result.benchmark_return,
                "alpha": result.alpha,
                "avg_holding_days": result.avg_holding_days,
            },
            "equity_curve": [
                {"date": d, "value": v}
                for d, v in zip(result.equity_dates, result.equity_curve)
            ],
            "trades": trades_list,
            "engine": "legacy",
        }

    raise RuntimeError("No backtest engine available (Qlib or legacy)")
