"""
services/stock_service.py —— 股票数据服务
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
from backtest.backtest import Backtester


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
    """单股策略回测"""
    strategy_name = strategy_name if strategy_name in _STRATEGY_MAP else "composite"
    strategy_label, strategy_fn = _STRATEGY_MAP[strategy_name]

    df = get_stock_history(symbol=symbol, start_date=start_date)
    df_s = strategy_fn(df)

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
    }
