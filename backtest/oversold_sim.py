"""
backtest/oversold_sim.py —— 超跌反弹v3 专属组合模拟器（只读回测用）

VisualBacktestEngine 只支持固定止损/止盈/最长持仓，无法还原 v3 设计的出场规则。
本模块实现 v3 真实出场口径（OVERSOLD_REBOUND_V4）：
  - 硬止损：相对入场价 <= stop_loss（默认 -6%）清仓
  - 分批止盈：浮盈 >= partial_tp（默认 +10%）先减半仓
  - 移动止盈：价格自持仓期最高点回撤 >= trailing_pct（默认 10%）清剩余
  - 破位离场：减半仓后收盘价跌破 MA5 清剩余
  - 单仓上限：每笔买入 <= single_pos_ratio（默认 40%）× 当前总权益
  - 大盘择时：仅在 上证指数(000001) MA5 > MA20 的交易日开新仓
  - 次日开盘买入、A股交易成本（佣金/印花税/滑点）

输出结构与 VisualBacktestEngine._run_portfolio 对齐（trades/equity_curve/
final_assets/trade_dates），可直接喂 engine._compute_metrics 复用绩效计算。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from core.db import get_index_daily

DEFAULT_COMMISSION = 0.0003
DEFAULT_STAMP_DUTY = 0.001
DEFAULT_SLIPPAGE = 0.001


def market_timing_ok_dates(start_date: str, end_date: str,
                           index_code: str = "000001") -> set:
    """返回 上证 MA5>MA20 的交易日集合（Timestamp）。取不到数据则返回 None（=不择时）。"""
    try:
        idx = get_index_daily(index_code, None, None)
    except Exception:
        return None
    if idx is None or idx.empty or "close" not in idx.columns:
        return None
    close = idx["close"].astype(float).sort_index()
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ok = (ma5 > ma20)
    return {ts for ts, v in ok.items() if bool(v)}


def run_v3_portfolio(
    stock_data: Dict[str, pd.DataFrame],
    stock_signals: Dict[str, pd.Series],
    info_map: Dict[str, dict],
    *,
    start_date: str,
    end_date: str,
    initial_cash: float = 1_000_000.0,
    stop_loss: float = -0.06,
    trailing_pct: float = 0.10,
    partial_tp: float = 0.10,
    single_pos_ratio: float = 0.40,
    max_hold_days: int = 10,
    max_holdings: int = 3,
    max_buy_per_day: int = 2,
    use_market_timing: bool = True,
    commission_rate: float = DEFAULT_COMMISSION,
    stamp_tax_rate: float = DEFAULT_STAMP_DUTY,
    slippage_rate: float = DEFAULT_SLIPPAGE,
) -> Dict[str, Any]:
    all_dates = sorted({d for df in stock_data.values() for d in df.index})
    trade_dates = [d for d in all_dates
                   if start_date <= d.strftime("%Y-%m-%d") <= end_date]
    if not trade_dates:
        return {"trades": [], "equity_curve": [], "positions": [],
                "final_assets": initial_cash, "trade_dates": []}

    timing_ok = market_timing_ok_dates(start_date, end_date) if use_market_timing else None

    cash = float(initial_cash)
    positions: Dict[str, Dict[str, Any]] = {}
    pending_buys: List[str] = []
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []

    def _mark_equity(dt) -> float:
        """按最近一次有效收盘价给持仓估值。

        停牌/数据缺口日不能回退到 entry_price——那会把持仓期内已有的浮盈浮亏
        在停牌日抹平，权益曲线出现瞬时台阶，max_drawdown 失真
        （同类问题见 backtest/engine.py 收盘快照的注释）。
        """
        pv = 0.0
        for c, pos in positions.items():
            df = stock_data.get(c)
            if df is not None and dt in df.index:
                px = float(df.loc[dt, "close"])
                if px > 0 and not pd.isna(px):
                    pos["last_close"] = px
                    pv += pos["shares"] * px
                    continue
            pv += pos["shares"] * float(pos.get("last_close") or pos["entry_price"])
        return cash + pv

    def _sell(code, dt, shares, reason, sell_price, entry_price, entry_date, cost_per_share):
        nonlocal cash
        proceeds = shares * sell_price
        commission_out = max(proceeds * commission_rate, 5.0)
        stamp = proceeds * stamp_tax_rate
        cash += proceeds - commission_out - stamp
        hold_days = (dt.date() - pd.to_datetime(entry_date).date()).days
        trades.append({
            "code": code,
            "name": info_map.get(code, {}).get("name", code),
            "buy_date": entry_date,
            "buy_price": round(entry_price, 4),
            "sell_date": dt.strftime("%Y-%m-%d"),
            "sell_price": round(sell_price, 4),
            "shares": shares,
            "pnl": round((proceeds - commission_out - stamp) - cost_per_share * shares, 2),
            "pnl_pct": round(sell_price / entry_price - 1, 4),
            "hold_days": hold_days,
            "exit_reason": reason,
        })

    for dt in trade_dates:
        # 1) 次日开盘买入挂单
        if pending_buys:
            total_equity = _mark_equity(dt)
            for code in list(pending_buys):
                df = stock_data.get(code)
                if df is None or dt not in df.index:
                    continue
                open_price = float(df.loc[dt, "open"])
                if open_price <= 0 or pd.isna(open_price):
                    continue
                if len(positions) >= max_holdings or code in positions:
                    continue
                fill_price = open_price * (1 + slippage_rate)
                target_cash = min(cash, single_pos_ratio * total_equity)
                if target_cash < fill_price * 100:
                    continue
                shares = int(target_cash // (fill_price * 100)) * 100
                if shares <= 0:
                    continue
                cost = shares * fill_price
                commission = max(cost * commission_rate, 5.0)
                total_cost = cost + commission
                if total_cost > cash:
                    continue
                cash -= total_cost
                positions[code] = {
                    "shares": shares,
                    "cost_per_share": (cost + commission) / shares,
                    "entry_date": dt.strftime("%Y-%m-%d"),
                    "entry_price": fill_price,
                    "last_close": fill_price,
                    "peak": fill_price,
                    "partial_done": False,
                    "name": info_map.get(code, {}).get("name", code),
                }
            pending_buys.clear()

        # 2) 持仓出场检查（收盘价）
        for code in list(positions.keys()):
            pos = positions[code]
            df = stock_data.get(code)
            if df is None or dt not in df.index:
                continue
            row = df.loc[dt]
            close = float(row["close"])
            if close <= 0 or pd.isna(close):
                continue
            sell_price = close * (1 - slippage_rate)
            ret = sell_price / pos["entry_price"] - 1
            pos["peak"] = max(pos["peak"], close)
            hold_days = (dt.date() - pd.to_datetime(pos["entry_date"]).date()).days
            ma5 = float(row.get("MA5", 0) or 0)

            # ① 硬止损：全清
            if ret <= stop_loss:
                _sell(code, dt, pos["shares"], "stop_loss", sell_price,
                      pos["entry_price"], pos["entry_date"], pos["cost_per_share"])
                del positions[code]
                continue

            # ② 分批止盈：浮盈达标先减半仓
            if (not pos["partial_done"]) and ret >= partial_tp:
                half = int(pos["shares"] // 2 // 100) * 100
                if half >= 100:
                    _sell(code, dt, half, "partial_tp", sell_price,
                          pos["entry_price"], pos["entry_date"], pos["cost_per_share"])
                    pos["shares"] -= half
                    pos["partial_done"] = True

            # ③ 移动止盈：自最高点回撤达标清剩余
            if close <= pos["peak"] * (1 - trailing_pct) and pos["peak"] > pos["entry_price"]:
                _sell(code, dt, pos["shares"], "trailing_stop", sell_price,
                      pos["entry_price"], pos["entry_date"], pos["cost_per_share"])
                del positions[code]
                continue

            # ④ 减半后破 MA5 清剩余
            if pos["partial_done"] and ma5 > 0 and close < ma5:
                _sell(code, dt, pos["shares"], "ma5_break", sell_price,
                      pos["entry_price"], pos["entry_date"], pos["cost_per_share"])
                del positions[code]
                continue

            # ⑤ 最长持仓兜底
            if hold_days >= max_hold_days:
                _sell(code, dt, pos["shares"], "max_hold_days", sell_price,
                      pos["entry_price"], pos["entry_date"], pos["cost_per_share"])
                del positions[code]
                continue

        # 3) 选股挂单（大盘择时通过才开新仓）
        can_open = (timing_ok is None) or (dt in timing_ok)
        if can_open:
            candidates = []
            for code, df in stock_data.items():
                if code in positions or code in pending_buys:
                    continue
                if dt not in df.index:
                    continue
                sig = stock_signals.get(code)
                if sig is None or dt not in sig.index or not bool(sig.loc[dt]):
                    continue
                score = float(df.loc[dt].get("fusion_score", 0) or 0)
                candidates.append((code, score))
            candidates.sort(key=lambda x: (-x[1], x[0]))
            daily_buys = 0
            for code, _s in candidates:
                if len(positions) + len(pending_buys) >= max_holdings:
                    break
                if daily_buys >= max_buy_per_day:
                    break
                pending_buys.append(code)
                daily_buys += 1

        # 4) 收盘快照
        total_assets = _mark_equity(dt)
        equity_curve.append({
            "date": dt.strftime("%Y-%m-%d"),
            "total": round(total_assets, 2),
            "cash": round(cash, 2),
            "position_value": round(total_assets - cash, 2),
            "position_count": len(positions),
        })

    # 收市强平
    last_dt = trade_dates[-1]
    for code in list(positions.keys()):
        pos = positions[code]
        df = stock_data.get(code)
        if df is not None and last_dt in df.index:
            close = float(df.loc[last_dt, "close"])
        else:
            # 末日停牌/数据缺口：用最近一次有效收盘价强平。旧版本 continue，
            # 这笔资金既不回现金也不记 trades，等于凭空蒸发（final_assets 只算 cash）。
            close = float(pos.get("last_close") or 0)
        if close <= 0 or pd.isna(close):
            close = pos["entry_price"]
        sell_price = close * (1 - slippage_rate)
        _sell(code, last_dt, pos["shares"], "force_close", sell_price,
              pos["entry_price"], pos["entry_date"], pos["cost_per_share"])
        del positions[code]

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "positions": [],
        "final_assets": round(cash, 2),
        "trade_dates": trade_dates,
    }
