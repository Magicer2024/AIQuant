"""
services/strategy_service.py —— 策略运行、批量筛选、多策略并联回测
"""
import math
import traceback
from datetime import date
from typing import Dict, Any, List, Generator, Optional

from core.data_fetcher import get_stock_history
from core.db import get_daily_price, get_all_stocks
import quant
import pandas as pd

from strategy.strategies import (
    strategy_volume_breakout,
    strategy_ma_convergence,
    strategy_price_volume_divergence,
    strategy_bottom_fishing,
    strategy_whale_accumulation,
    fuse_signals,
)
from backtest.backtest import Backtester


_DEFAULT_SCREEN_POOL = [
    "000001", "000002", "000063", "000066", "000100",
    "000333", "000338", "000425", "000651", "000661",
    "000858", "000876", "000895", "000896", "000898",
    "002594", "002714", "300015", "300059", "300122",
    "300750", "600009", "600016", "600019", "600028",
    "600036", "600050", "600104", "600109", "600150",
    "600276", "600309", "600436", "600438", "600519",
    "600570", "600585", "600690", "600703", "600760",
    "600809", "600837", "600887", "600893", "600900",
    "600905", "600918", "600926", "600941", "601006",
    "601012", "601088", "601118", "601138", "601166",
    "601169", "601186", "601288", "601318", "601328",
    "601336", "601398", "601601", "601628", "601658",
    "601668", "601688", "601728", "601766", "601800",
    "601816", "601857", "601888", "601899", "601919",
    "601939", "601988", "601989", "601995", "603259",
    "603288", "603501", "603799", "603986",
]


def run_all_strategies(symbol: str, start_date: str = "",
                       params: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """运行所有策略并返回融合信号"""
    kwargs = {"symbol": symbol}
    if start_date:
        kwargs["start_date"] = start_date
    df = get_stock_history(**kwargs)

    if len(df) < 30:
        raise ValueError("数据不足30条")

    p = params or {}
    s1 = strategy_volume_breakout(df, vol_factor=p.get("s1_vol_factor", 1.5),
                                   rise_3d=p.get("s1_rise_3d", 0.02),
                                   score_min=int(p.get("s1_score_min", 2)))
    s2 = strategy_ma_convergence(df, ma_narrow=p.get("s2_ma_narrow", 0.02))
    s3 = strategy_price_volume_divergence(df)
    s4 = strategy_bottom_fishing(df, drop_threshold=p.get("s4_drop_threshold", -0.05))
    s5 = strategy_whale_accumulation(df, vol_ratio=p.get("s5_vol_ratio", 3.0))

    def _strategy_result(df_s, name):
        last = df_s.iloc[-1]
        raw = float(last.get("BUY_SCORE", 0))
        score = round(raw / 3 * 10, 1)
        buy_sig = bool(last.get("BUY_SIGNAL", False))
        sell_sig = bool(last.get("SELL_SIGNAL", False))
        signal = "BUY" if buy_sig else ("SELL" if sell_sig else "HOLD")
        return {
            "name": name,
            "signal": signal,
            "score": score,
            "strength": round(score / 10.0, 2),
            "reasons": [name + "信号触发"] if buy_sig else ([name + "卖出信号"] if sell_sig else ["无信号"]),
        }

    r1 = _strategy_result(s1, "放量突破")
    r2 = _strategy_result(s2, "均线粘合")
    r3 = _strategy_result(s3, "量价背离")
    r4 = _strategy_result(s4, "抄底")
    r5 = _strategy_result(s5, "主力建仓")

    fused = fuse_signals([s1, s2, s3, s4, s5])
    fused_last = fused.iloc[-1]

    return {
        "symbol": symbol,
        "price": float(df["close"].iloc[-1]),
        "strategies": {
            "s1_volume_breakout": r1,
            "s2_ma_convergence": r2,
            "s3_price_volume_divergence": r3,
            "s4_bottom_fishing": r4,
            "s5_whale_accumulation": r5,
        },
        "fused": {
            "buy_signal": bool(fused_last["BUY_SIGNAL"]),
            "confidence": round(float(fused_last["FUSION_SCORE"]) / 50.0, 2),
            "num_triggered": sum(1 for r in [r1, r2, r3, r4, r5] if r["signal"] == "BUY"),
            "final_score": round(float(fused_last["FUSION_SCORE"]), 2),
            "reason": "综合评分" if fused_last["BUY_SIGNAL"] else "无融合买入信号",
        },
    }


def screen_stocks_generator(symbols_str: str = "", use_v4: bool = True,
                            min_score: float = 0, limit: int = 50) -> Generator[str, None, None]:
    """批量策略筛选 - 返回 SSE 事件流字符串"""
    import json

    if symbols_str:
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        stocks_df = pd.DataFrame({"code": symbols, "name": [""] * len(symbols)})
    else:
        stocks_df = get_all_stocks()
        stocks_df = stocks_df[
            ~stocks_df["code"].str.startswith("688") &
            ~stocks_df["code"].str.startswith("301")
        ]

    if min_score <= 0:
        min_score = 22.5 if use_v4 else 20.0

    total = len(stocks_df)
    results = []

    for idx, row in stocks_df.iterrows():
        code = row["code"]
        name = row.get("name", "") or ""
        try:
            if use_v4:
                item = quant.analyze_stock_v4(code, name)
            else:
                item = quant.analyze_stock_from_db(code, name, min_score=min_score)

            if item is None:
                yield f"event: progress\ndata: {json.dumps({'done': idx + 1, 'total': total, 'current': code, 'stocks': []})}\n\n"
                continue

            price = item["price"]
            df = get_daily_price(code)
            prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else price
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0
            trade_date = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]

            if use_v4:
                def _ct(v):
                    return bool(v and float(v) >= 5.0)
                s_vol = item.get("s1_vol_break", 0)
                s_ma5 = item.get("s2_ma_conv", 0)
                s_drop = item.get("s3_pv_div", 0)
                s_rsi = item.get("s4_bottom", 0)
                stock_info = {
                    "symbol": code,
                    "name": name,
                    "trade_date": trade_date,
                    "price": price,
                    "change_pct": change_pct,
                    "score": item["score"],
                    "raw_score": item.get("raw_score", 0),
                    "num_triggered": 1 if item.get("raw_score", 0) >= 1.8 else 0,
                    "triggered": item.get("trigger_list", []),
                    "strategy_scores": {
                        "放量":     {"score": s_vol,  "max": 10, "triggered": _ct(s_vol)},
                        "站上MA5":  {"score": s_ma5,  "max": 10, "triggered": _ct(s_ma5)},
                        "下跌深度": {"score": s_drop, "max": 10, "triggered": _ct(s_drop)},
                        "RSI区间":  {"score": s_rsi,  "max": 10, "triggered": _ct(s_rsi)},
                    }
                }
            else:
                def _si(v):
                    if v is None: return 0.0
                    if isinstance(v, float) and math.isnan(v): return 0.0
                    return round(float(v), 1)
                s1 = _si(item.get('s1_vol_break'))
                s2 = _si(item.get('s2_ma_conv'))
                s3 = _si(item.get('s3_pv_div'))
                s4 = _si(item.get('s4_bottom'))
                s5 = _si(item.get('s5_whale'))
                triggered = [k for k, v2 in {
                    "放量突破": s1, "均线粘合": s2, "量价背离": s3,
                    "抄底": s4, "主力建仓": s5,
                }.items() if v2 > 0]
                stock_info = {
                    "symbol": code,
                    "name": name,
                    "trade_date": trade_date,
                    "price": price,
                    "change_pct": change_pct,
                    "score": item["score"],
                    "num_triggered": len(triggered),
                    "triggered": triggered,
                    "strategy_scores": {
                        "放量突破": {"score": s1, "max": 10, "triggered": s1 > 0},
                        "均线粘合": {"score": s2, "max": 10, "triggered": s2 > 0},
                        "量价背离": {"score": s3, "max": 10, "triggered": s3 > 0},
                        "抄底":     {"score": s4, "max": 10, "triggered": s4 > 0},
                        "主力建仓": {"score": s5, "max": 10, "triggered": s5 > 0},
                    }
                }

            results.append(stock_info)
            yield f"event: progress\ndata: {json.dumps({'done': idx + 1, 'total': total, 'current': code, 'stocks': [stock_info]})}\n\n"
        except Exception:
            yield f"event: progress\ndata: {json.dumps({'done': idx + 1, 'total': total, 'current': code, 'stocks': []})}\n\n"

    results.sort(key=lambda x: x["score"], reverse=True)
    strategy_label = 'v4' if use_v4 else 'fusion'
    yield f"event: done\ndata: {json.dumps({'stocks': results[:limit], 'total': len(results), 'strategy': strategy_label, 'cached': False})}\n\n"


def multi_strategy_backtest(symbol: str, start_date: str = "20220101",
                            capital: float = 100000) -> Dict[str, Any]:
    """多策略并联回测"""
    df = get_stock_history(symbol=symbol, start_date=start_date)

    strategies = [
        ("放量突破", strategy_volume_breakout),
        ("均线粘合", strategy_ma_convergence),
        ("量价背离", strategy_price_volume_divergence),
        ("抄底型", strategy_bottom_fishing),
        ("主力建仓", strategy_whale_accumulation),
    ]

    results = {}
    for name, strat_fn in strategies:
        s = strat_fn(df.copy())
        df_copy = df.copy()
        df_copy["BUY_SIGNAL"] = s["BUY_SIGNAL"]
        df_copy["SELL_SIGNAL"] = s["SELL_SIGNAL"]
        df_copy["STRATEGY"] = name
        bt = Backtester(initial_capital=capital)
        r = bt.run(df_copy)
        results[name] = {
            "total_return": r.total_return,
            "annual_return": r.annual_return,
            "max_drawdown": r.max_drawdown,
            "sharpe_ratio": r.sharpe_ratio,
            "win_rate": r.win_rate,
            "total_trades": r.total_trades,
            "equity_curve": list(zip(r.equity_dates, r.equity_curve)),
        }

    fused = fuse_signals([strat_fn(df.copy()) for _, strat_fn in strategies])
    df_fused = fused[["close", "volume", "BUY_SIGNAL", "SELL_SIGNAL"]].copy()
    df_fused["STRATEGY"] = "融合策略"
    bt_f = Backtester(initial_capital=capital)
    r_f = bt_f.run(df_fused)
    results["融合策略"] = {
        "total_return": r_f.total_return,
        "annual_return": r_f.annual_return,
        "max_drawdown": r_f.max_drawdown,
        "sharpe_ratio": r_f.sharpe_ratio,
        "win_rate": r_f.win_rate,
        "total_trades": r_f.total_trades,
        "equity_curve": list(zip(r_f.equity_dates, r_f.equity_curve)),
    }

    return {
        "symbol": symbol,
        "start_date": start_date,
        "initial_capital": capital,
        "results": results,
    }


def compare_strategies(symbols_str: str, start_date: str = "20220101",
                       strategy_name: str = "fused") -> Dict[str, Any]:
    """批量比较多个股票的策略表现"""
    if not symbols_str:
        raise ValueError("缺少股票代码列表")

    symbol_list = [s.strip() for s in symbols_str.split(",") if s.strip()]
    results = []

    for symbol in symbol_list:
        try:
            df = get_stock_history(symbol=symbol, start_date=start_date)
            if len(df) < 30:
                continue

            if strategy_name == "fused":
                s = fuse_signals([
                    strategy_volume_breakout(df),
                    strategy_ma_convergence(df),
                    strategy_price_volume_divergence(df),
                    strategy_bottom_fishing(df),
                    strategy_whale_accumulation(df),
                ])
                last = s.iloc[-1]
                buy_signal = bool(last.get("BUY_SIGNAL", False))
                confidence = round(float(last.get("FUSION_SCORE", 0)) / 50.0, 2)
            else:
                strat_map = {
                    "volume_breakout": strategy_volume_breakout,
                    "ma_convergence": strategy_ma_convergence,
                    "price_volume_divergence": strategy_price_volume_divergence,
                    "bottom_fishing": strategy_bottom_fishing,
                    "whale_accumulation": strategy_whale_accumulation,
                }
                s = strat_map.get(strategy_name, strategy_volume_breakout)(df)
                last = s.iloc[-1]
                buy_signal = bool(last.get("BUY_SIGNAL", False))
                raw = float(last.get("BUY_SCORE", 0))
                confidence = round(raw / 3 * 10 / 10.0, 2)

            results.append({
                "symbol": symbol,
                "price": round(float(df["close"].iloc[-1]), 2),
                "buy_signal": buy_signal,
                "confidence": confidence,
            })
        except Exception:
            continue

    return {
        "strategy": strategy_name,
        "stocks": results,
        "count": len(results),
    }
