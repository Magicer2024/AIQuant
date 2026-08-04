"""
backtest/engine.py —— 智能可视化回测引擎

特点：
  1. 复用主项目的 strategy.indicators 计算技术指标
  2. 从 core.db.daily_price 加载行情（按日期批量拉取）
  3. 选股条件求值（多类目：技术/基本面/量价/因子）
  4. 逐日驱动组合回测：先卖后买、止盈止损、最大持仓/单日买入
  5. 计算完整绩效：总收益/年化/夏普/最大回撤/胜率/盈亏比/连续盈亏/平均持仓天数
  6. 输出每日权益曲线、月度收益、交易明细、当前持仓
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 复用主项目已有的指标与数据库 ──
from core.db import get_conn
from strategy.indicators import calc_ma, calc_macd, calc_rsi, calc_kdj


# ─────────────── 默认交易成本（A 股）───────────────
DEFAULT_COMMISSION = 0.0003   # 手续费：万三（双向）
DEFAULT_STAMP_DUTY = 0.001    # 印花税：千一（仅卖出）
DEFAULT_SLIPPAGE   = 0.001    # 滑点：千一


# ─────────────── 单条条件求值 ───────────────

class ConditionError(ValueError):
    """条件解析/求值异常"""


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    在 DataFrame 上补充回测所需技术指标。
    列名遵循 strategy.indicators 的约定：MA5/MA20/RSI14/KDJ_K/KDJ_D/KDJ_J/MACD_DIF/MACD_DEA/MACD_HIST
    """
    out = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            out[col] = np.nan
        out[col] = out[col].astype(float)

    # 移动平均
    ma = calc_ma(out["close"], periods=[5, 10, 20, 30, 60])
    for c in ma.columns:
        out[c] = ma[c]

    # MACD
    macd = calc_macd(out["close"], fast=12, slow=26, signal=9)
    for c in macd.columns:
        out[c] = macd[c]

    # RSI
    rsi = calc_rsi(out["close"], periods=[6, 14, 24])
    for c in rsi.columns:
        out[c] = rsi[c]

    # KDJ
    kdj = calc_kdj(out["high"], out["low"], out["close"], n=9, m1=3, m2=3)
    for c in kdj.columns:
        out[c] = kdj[c]

    return out


def _ensure_series(v) -> pd.Series:
    return v if isinstance(v, pd.Series) else pd.Series(v)


def _eval_condition(cond: Dict[str, Any], df: pd.DataFrame) -> pd.Series:
    """对单条条件返回与 df.index 对齐的 bool Series"""
    indicator = cond.get("indicator")
    operator = cond.get("operator", "golden_cross")
    params = cond.get("params") or {}
    close = df["close"]

    # ──────── MA 均线 ────────
    if indicator == "ma_cross":
        short_p = int(params.get("short_period", 5))
        long_p = int(params.get("long_period", 20))
        ma_s = df.get(f"MA{short_p}")
        if ma_s is None:
            ma_s = calc_ma(close, [short_p])[f"MA{short_p}"]
        ma_l = df.get(f"MA{long_p}")
        if ma_l is None:
            ma_l = calc_ma(close, [long_p])[f"MA{long_p}"]
        s = ma_s.astype(float)
        l = ma_l.astype(float)
        if operator == "golden_cross":
            return ((s > l) & (s.shift(1) <= l.shift(1))).fillna(False)
        if operator == "death_cross":
            return ((s < l) & (s.shift(1) >= l.shift(1))).fillna(False)
        if operator == "bull_above":
            return (s > l).fillna(False)
        raise ConditionError(f"ma_cross 不支持 operator: {operator}")

    # ──────── MACD ────────
    if indicator == "macd_cross":
        dif = df.get("MACD_DIF")
        dea = df.get("MACD_DEA")
        hist = df.get("MACD_HIST")
        if dif is None or dea is None or hist is None:
            macd = calc_macd(close)
            dif = macd["MACD_DIF"]
            dea = macd["MACD_DEA"]
            hist = macd["MACD_HIST"]
        if operator == "golden_cross":
            return ((dif > dea) & (dif.shift(1) <= dea.shift(1))).fillna(False)
        if operator == "death_cross":
            return ((dif < dea) & (dif.shift(1) >= dea.shift(1))).fillna(False)
        if operator == "hist_positive":
            return (hist > 0).fillna(False)
        if operator == "hist_negative":
            return (hist < 0).fillna(False)
        raise ConditionError(f"macd_cross 不支持 operator: {operator}")

    # ──────── KDJ ────────
    if indicator == "kdj_cross":
        k = df.get("KDJ_K")
        d = df.get("KDJ_D")
        j = df.get("KDJ_J")
        if k is None:
            kdj = calc_kdj(df["high"], df["low"], close)
            k, d, j = kdj["KDJ_K"], kdj["KDJ_D"], kdj["KDJ_J"]
        if operator == "golden_cross":
            return ((k > d) & (k.shift(1) <= d.shift(1))).fillna(False)
        if operator == "death_cross":
            return ((k < d) & (k.shift(1) >= d.shift(1))).fillna(False)
        if operator == "oversold":
            thr = float(params.get("oversold", 20))
            return (j < thr).fillna(False)
        if operator == "overbought":
            thr = float(params.get("overbought", 80))
            return (j > thr).fillna(False)
        raise ConditionError(f"kdj_cross 不支持 operator: {operator}")

    # ──────── RSI ────────
    if indicator == "rsi":
        period = int(params.get("period", 14))
        rsi_col = f"RSI{period}"
        rsi = df.get(rsi_col)
        if rsi is None:
            rsi = calc_rsi(close, [period])[rsi_col]
        if operator == "oversold":
            thr = float(params.get("oversold", 30))
            return (rsi < thr).fillna(False)
        if operator == "overbought":
            thr = float(params.get("overbought", 70))
            return (rsi > thr).fillna(False)
        raise ConditionError(f"rsi 不支持 operator: {operator}")

    # ──────── 价格突破 ────────
    if indicator == "price_breakout":
        period = int(params.get("period", 20))
        if operator == "breakout_high":
            high_n = close.rolling(period).max().shift(1)
            return (close > high_n).fillna(False)
        if operator == "breakout_low":
            low_n = close.rolling(period).min().shift(1)
            return (close < low_n).fillna(False)
        raise ConditionError(f"price_breakout 不支持 operator: {operator}")

    # ──────── 收盘在 MA 上下 ────────
    if indicator == "close_above_ma":
        period = int(params.get("period", 20))
        ma_col = f"MA{period}"
        ma = df.get(ma_col)
        if ma is None:
            ma = calc_ma(close, [period])[ma_col]
        if operator == "above":
            return (close > ma).fillna(False)
        if operator == "below":
            return (close < ma).fillna(False)
        raise ConditionError(f"close_above_ma 不支持 operator: {operator}")

    # ──────── BIAS 乖离率 ────────
    if indicator == "bias":
        period = int(params.get("period", 6))
        ma_col = f"MA{period}"
        ma = df.get(ma_col)
        if ma is None:
            ma = calc_ma(close, [period])[ma_col]
        bias = (close - ma) / ma.replace(0, np.nan) * 100
        if operator == "gt":
            thr = float(params.get("value", params.get("min", 5)))
            return (bias > thr).fillna(False)
        if operator == "lt":
            thr = float(params.get("value", params.get("max", -5)))
            return (bias < thr).fillna(False)
        raise ConditionError(f"bias 不支持 operator: {operator}")

    # ──────── 基本面：市值/PE/PB（在 daily_price 不存在，落到 stock_info/daily_fund）────────
    if indicator in ("market_cap", "pe_ttm", "pb"):
        # 基本面回测需要单独的日频表，回测引擎里以"通过即可"的方式处理：返回全 False 让上层兜底
        # 注：完整实现需要 daily_fund 序列加载；当前主项目尚未提供该数据
        return pd.Series(False, index=df.index)

    # ──────── 量价：换手率/量比/涨跌幅 ────────
    if indicator == "turnover_rate":
        # daily_price 没有 turnover 列，用 volume/流通股本估算太复杂，先返回全 False
        return pd.Series(False, index=df.index)
    if indicator == "volume_ratio":
        # 量比 = 今日量 / 5日均量
        base = df["volume"].rolling(5, min_periods=1).mean()
        ratio = df["volume"] / base.replace(0, np.nan)
        thr = float(params.get("min", 1.5))
        if operator == "gt":
            return (ratio > thr).fillna(False)
        raise ConditionError(f"volume_ratio 不支持 operator: {operator}")
    if indicator == "pct_change":
        s = close.pct_change() * 100
        lo = float(params.get("min", -5))
        hi = float(params.get("max", 5))
        return ((s >= lo) & (s <= hi)).fillna(False)

    # ──────── 因子分（daily_price 已有） ────────
    if indicator in ("fusion_score", "whale_score", "bottom_score", "ma_score", "vol_score", "diverge_score"):
        s = df.get(indicator)
        if s is None:
            return pd.Series(False, index=df.index)
        thr = float(params.get("min", params.get("value", 60)))
        if operator == "gt":
            return (s > thr).fillna(False)
        if operator == "lt":
            return (s < thr).fillna(False)
        raise ConditionError(f"{indicator} 不支持 operator: {operator}")

    raise ConditionError(f"未知指标: {indicator}")


# ─────────────── 回测参数 ───────────────

@dataclass
class BacktestParams:
    """可视化回测参数（与前端表单一一对应）"""
    start_date: str
    end_date: str
    initial_cash: float = 1_000_000.0
    max_holdings: int = 5
    max_buy_per_day: int = 3
    buy_timing: str = "next_day_open"     # next_day_open | current_close
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    max_hold_days: Optional[int] = None
    commission_rate: float = DEFAULT_COMMISSION
    stamp_tax_rate: float = DEFAULT_STAMP_DUTY
    slippage_rate: float = DEFAULT_SLIPPAGE
    exclude_st: bool = True
    exclude_kcb: bool = True
    exclude_cyb: bool = False
    logic: str = "AND"  # 预留 OR 扩展
    risk_free_rate: float = 0.02  # Sharpe 无风险年化利率（默认 2%）


# ─────────────── 数据提供器（默认从 core.db.daily_price 读取）───────────────

def _load_all_daily(start_date: str, end_date: str, codes: List[str]) -> pd.DataFrame:
    """
    按日期批量拉取所有入选股票在区间内的日线。
    返回列：code, trade_date, open, high, low, close, volume + 因子列（如有）
    """
    if not codes:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(codes))
    cols = ("code, trade_date, open, high, low, close, volume, "
            "COALESCE(fusion_score,0) AS fusion_score, "
            "COALESCE(whale_score,0)  AS whale_score, "
            "COALESCE(bottom_score,0) AS bottom_score, "
            "COALESCE(ma_score,0)     AS ma_score, "
            "COALESCE(vol_score,0)    AS vol_score, "
            "COALESCE(diverge_score,0) AS diverge_score, "
            "COALESCE(strategy_score,0) AS strategy_score, "
            "COALESCE(pct_change,0)   AS pct_change")
    sql = f"""
        SELECT {cols}
        FROM daily_price
        WHERE trade_date BETWEEN ? AND ?
          AND code IN ({placeholders})
        ORDER BY code, trade_date
    """
    with get_conn() as conn:
        df = pd.read_sql_query(sql, conn, params=[start_date, end_date] + codes)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _load_stock_info(codes: List[str]) -> pd.DataFrame:
    """获取股票名称/板块信息"""
    if not codes:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(codes))
    sql = f"SELECT code, name, market FROM stock_info WHERE code IN ({placeholders})"
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=codes)


def _load_stock_pool(exclude_kcb: bool, exclude_cyb: bool) -> List[str]:
    """从 stock_info 读取活跃股票池"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM stock_info WHERE is_active = 1"
        ).fetchall()
    codes = [r["code"] for r in rows]
    if exclude_kcb:
        codes = [c for c in codes if not c.startswith("688")]
    if exclude_cyb:
        codes = [c for c in codes if not (c.startswith("300") or c.startswith("301"))]
    return codes


# ─────────────── 主引擎 ───────────────

class VisualBacktestEngine:
    """
    智能可视化回测引擎

    用法：
        params = BacktestParams(...)
        engine = VisualBacktestEngine(params)
        result = engine.run(conditions=[...], progress_cb=lambda x: ...)
    """

    def __init__(
        self,
        params: BacktestParams,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_flag: Optional[threading.Event] = None,
    ):
        self.params = params
        self.progress_cb = progress_cb
        self.cancel_flag = cancel_flag or threading.Event()

    def _report(self, **info):
        if self.progress_cb:
            try:
                self.progress_cb(info)
            except Exception:
                pass

    # ──────── 公共入口 ────────
    def run(self, conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
        params = self.params
        if not conditions:
            return self._empty("请至少添加一条选股条件")

        # 1) 加载股票池
        self._report(stage="init", progress=2, message="加载股票池...")
        all_codes = _load_stock_pool(params.exclude_kcb, params.exclude_cyb)
        if not all_codes:
            return self._empty("无活跃股票，请先在【管理】中初始化 stock_info")
        self._report(stage="init", progress=6, message=f"股票池共 {len(all_codes)} 只，开始加载行情...")

        # 2) 批量拉取日线
        daily = _load_all_daily(params.start_date, params.end_date, all_codes)
        if daily.empty:
            return self._empty("区间内无行情数据，请先同步 daily_price")
        stock_info = _load_stock_info(all_codes)
        info_map = {r["code"]: r.to_dict() for _, r in stock_info.iterrows()}

        # 3) 按股票拆分 + 计算指标
        self._report(stage="indicators", progress=20, message="计算技术指标...")
        stock_data: Dict[str, pd.DataFrame] = {}
        for code, df in daily.groupby("code", sort=False):
            df = df.set_index("trade_date").sort_index()
            df = _add_indicators(df)
            stock_data[code] = df
        if self.cancel_flag.is_set():
            return self._empty("已取消")

        # 4) 求出每个股票每日的命中掩码
        self._report(stage="signal", progress=30, message="计算选股信号...")
        stock_signals: Dict[str, pd.Series] = {}
        total = len(stock_data)
        for i, (code, df) in enumerate(stock_data.items()):
            mask = self._combine_conditions(df, conditions, params.logic)
            stock_signals[code] = mask.reindex(df.index, fill_value=False)
            if (i + 1) % 100 == 0 or i == total - 1:
                self._report(
                    stage="signal",
                    progress=int(30 + (i + 1) / total * 15),
                    message=f"信号计算 {i + 1}/{total}",
                )
        if self.cancel_flag.is_set():
            return self._empty("已取消")

        # 5) 组合回测
        self._report(stage="running", progress=45, message="开始组合回测...")
        run_result = self._run_portfolio(stock_data, stock_signals, info_map)
        if self.cancel_flag.is_set():
            run_result["cancelled"] = True
            run_result["cancelled_message"] = "已取消"

        # 6) 绩效
        self._report(stage="metrics", progress=92, message="计算绩效指标...")
        metrics = self._compute_metrics(run_result["equity_curve"], run_result["trades"], run_result["final_assets"])

        result = {
            "success": True,
            "cancelled": bool(run_result.get("cancelled", False)),
            "start_date": params.start_date,
            "end_date": params.end_date,
            "init_cash": params.initial_cash,
            "final_assets": run_result["final_assets"],
            "total_return": metrics["total_return"],
            "annual_return": metrics["annual_return"],
            "max_drawdown": metrics["max_drawdown"],
            "sharpe_ratio": metrics["sharpe_ratio"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "avg_win_pct": metrics["avg_win_pct"],
            "avg_loss_pct": metrics["avg_loss_pct"],
            "avg_hold_days": metrics["avg_hold_days"],
            "max_consecutive_wins": metrics["max_consecutive_wins"],
            "max_consecutive_losses": metrics["max_consecutive_losses"],
            "total_trades": metrics["total_trades"],
            "win_trades": metrics["win_trades"],
            "loss_trades": metrics["loss_trades"],
            "equity_curve": run_result["equity_curve"],
            "monthly_returns": self._monthly_returns(run_result["equity_curve"]),
            "trades": run_result["trades"],
            "positions": run_result["positions"],
            "stock_pool_size": len(stock_data),
            "trade_days": len(run_result["trade_dates"]),
        }
        self._report(stage="done", progress=100, message="回测完成")
        return result

    # ──────── 条件组合 ────────
    def _combine_conditions(self, df: pd.DataFrame, conditions: List[Dict[str, Any]], logic: str) -> pd.Series:
        if not conditions:
            return pd.Series(False, index=df.index)
        masks: List[pd.Series] = []
        for c in conditions:
            try:
                m = _eval_condition(c, df)
            except ConditionError:
                m = pd.Series(False, index=df.index)
            masks.append(_ensure_series(m).reindex(df.index, fill_value=False))
        if logic.upper() == "OR":
            out = masks[0]
            for m in masks[1:]:
                out = out | m
            return out
        out = masks[0]
        for m in masks[1:]:
            out = out & m
        return out

    # ──────── 组合回测主循环 ────────
    def _run_portfolio(
        self,
        stock_data: Dict[str, pd.DataFrame],
        stock_signals: Dict[str, pd.Series],
        info_map: Dict[str, dict],
    ) -> Dict[str, Any]:
        p = self.params
        # 取交易日并集（按升序）
        all_dates = sorted({d for df in stock_data.values() for d in df.index})
        trade_dates = [d for d in all_dates if p.start_date <= d.strftime("%Y-%m-%d") <= p.end_date]
        if not trade_dates:
            return {
                "trades": [], "equity_curve": [], "positions": [],
                "final_assets": p.initial_cash, "trade_dates": [],
            }

        cash = float(p.initial_cash)
        positions: Dict[str, Dict[str, Any]] = {}     # code -> {shares, cost, entry_date, entry_price, name}
        pending_buys: Dict[str, Dict[str, Any]] = {}
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []

        total = len(trade_dates)
        for idx, dt in enumerate(trade_dates):
            if self.cancel_flag.is_set():
                break

            # ── 1) 处理昨日挂单（次日开盘买入）──
            if p.buy_timing == "next_day_open" and pending_buys:
                for code, plan in list(pending_buys.items()):
                    df = stock_data.get(code)
                    if df is None or dt not in df.index:
                        continue
                    open_price = float(df.loc[dt, "open"])
                    if open_price <= 0 or pd.isna(open_price):
                        continue
                    fill_price = open_price * (1 + p.slippage_rate)
                    if len(positions) >= p.max_holdings:
                        continue
                    target_cash = cash / max(1, p.max_holdings - len(positions))
                    buy_cash = min(target_cash, cash)
                    if buy_cash < fill_price * 100:
                        continue
                    shares = int(buy_cash // (fill_price * 100)) * 100
                    if shares <= 0:
                        continue
                    cost = shares * fill_price
                    commission = max(cost * p.commission_rate, 5.0)
                    total_cost = cost + commission
                    if total_cost > cash:
                        # 退档：再算一次
                        shares = int((cash - 5) // (fill_price * (1 + p.commission_rate)) // 100) * 100
                        if shares <= 0:
                            continue
                        cost = shares * fill_price
                        commission = max(cost * p.commission_rate, 5.0)
                        total_cost = cost + commission
                    cash -= total_cost
                    info = info_map.get(code, {})
                    positions[code] = {
                        "shares": shares,
                        "cost": cost + commission,        # 入账成本（计入佣金）
                        "entry_date": dt.strftime("%Y-%m-%d"),
                        "entry_price": fill_price,
                        "name": info.get("name", code),
                        "entry_signal_date": plan.get("signal_date", ""),
                    }
                pending_buys.clear()

            # ── 2) 当日卖出检查（按收盘价）──
            for code in list(positions.keys()):
                pos = positions[code]
                df = stock_data.get(code)
                if df is None or dt not in df.index:
                    continue
                row = df.loc[dt]
                close = float(row["close"])
                if close <= 0 or pd.isna(close):
                    continue
                sell_price = close * (1 - p.slippage_rate)
                ret_pct = sell_price / pos["entry_price"] - 1
                hold_days = (dt.date() - pd.to_datetime(pos["entry_date"]).date()).days
                sell_reason = None
                if p.stop_loss_pct is not None and ret_pct <= p.stop_loss_pct:
                    sell_reason = "stop_loss"
                elif p.take_profit_pct is not None and ret_pct >= p.take_profit_pct:
                    sell_reason = "take_profit"
                elif p.max_hold_days is not None and hold_days >= p.max_hold_days:
                    sell_reason = "max_hold_days"
                if sell_reason is None:
                    continue

                proceeds = pos["shares"] * sell_price
                commission_out = max(proceeds * p.commission_rate, 5.0)
                stamp = proceeds * p.stamp_tax_rate
                cash += proceeds - commission_out - stamp
                trades.append({
                    "code": code,
                    "name": pos.get("name", code),
                    "buy_date": pos["entry_date"],
                    "buy_price": round(pos["entry_price"], 4),
                    "sell_date": dt.strftime("%Y-%m-%d"),
                    "sell_price": round(sell_price, 4),
                    "shares": pos["shares"],
                    "pnl": round((proceeds - commission_out - stamp) - pos["cost"], 2),
                    "pnl_pct": round(ret_pct, 4),
                    "hold_days": hold_days,
                    "exit_reason": sell_reason,
                })
                del positions[code]

            # ── 3) 当日选股 + 挂单（次日开盘买入）──
            candidates: List[Tuple[str, float]] = []
            for code, df in stock_data.items():
                if code in positions:
                    continue
                if dt not in df.index:
                    continue
                sig = stock_signals.get(code)
                if sig is None or dt not in sig.index or not bool(sig.loc[dt]):
                    continue
                # 排序键：fusion_score 降序
                row = df.loc[dt]
                score = float(row.get("fusion_score", 0) or 0)
                candidates.append((code, score))
            candidates.sort(key=lambda x: (-x[1], x[0]))

            daily_buys = 0
            for code, _score in candidates:
                if len(positions) + len(pending_buys) >= p.max_holdings:
                    break
                if daily_buys >= p.max_buy_per_day:
                    break
                if code in positions or code in pending_buys:
                    continue
                if p.buy_timing == "next_day_open":
                    pending_buys[code] = {"signal_date": dt.strftime("%Y-%m-%d")}
                    daily_buys += 1
                else:  # current_close
                    df = stock_data[code]
                    close = float(df.loc[dt, "close"])
                    if close <= 0 or pd.isna(close):
                        continue
                    fill_price = close * (1 + p.slippage_rate)
                    if len(positions) >= p.max_holdings:
                        break
                    target_cash = cash / max(1, p.max_holdings - len(positions))
                    buy_cash = min(target_cash, cash)
                    if buy_cash < fill_price * 100:
                        continue
                    shares = int(buy_cash // (fill_price * 100)) * 100
                    if shares <= 0:
                        continue
                    cost = shares * fill_price
                    commission = max(cost * p.commission_rate, 5.0)
                    total_cost = cost + commission
                    if total_cost > cash:
                        continue
                    cash -= total_cost
                    info = info_map.get(code, {})
                    positions[code] = {
                        "shares": shares,
                        "cost": cost + commission,
                        "entry_date": dt.strftime("%Y-%m-%d"),
                        "entry_price": fill_price,
                        "name": info.get("name", code),
                        "entry_signal_date": dt.strftime("%Y-%m-%d"),
                    }
                    daily_buys += 1

            # ── 4) 收盘快照 ──
            # 停牌/数据缺口日按最近一次有效收盘价估值。旧版本直接 continue，
            # 该仓位市值会凭空消失，权益曲线出现 20~60% 的瞬时假坑，max_drawdown
            # 完全失真（2026-07-31 实测：同一组总收益 -31.9% 却报回撤 -71.6%）。
            position_value = 0.0
            for code, pos in positions.items():
                df = stock_data.get(code)
                close = None
                if df is not None and dt in df.index:
                    c = float(df.loc[dt, "close"])
                    if not pd.isna(c) and c > 0:
                        close = c
                if close is None:
                    close = float(pos.get("last_close") or pos["entry_price"])
                else:
                    pos["last_close"] = close
                position_value += pos["shares"] * close
            total_assets = cash + position_value
            equity_curve.append({
                "date": dt.strftime("%Y-%m-%d"),
                "total": round(total_assets, 2),
                "cash": round(cash, 2),
                "position_value": round(position_value, 2),
                "position_count": len(positions),
            })

            # 进度
            if (idx + 1) % 5 == 0 or idx == total - 1:
                self._report(
                    stage="running",
                    progress=int(45 + (idx + 1) / total * 45),
                    message=f"回测进度 {idx + 1}/{total}",
                )

        # ── 收市：未平仓按最后一日收盘价强平 ──
        if positions and trade_dates:
            last_dt = trade_dates[-1]
            for code in list(positions.keys()):
                pos = positions[code]
                df = stock_data.get(code)
                if df is None:
                    continue
                if last_dt in df.index:
                    close = float(df.loc[last_dt, "close"])
                else:
                    # 末日停牌/数据缺口：用最近一次有效收盘价强平。旧版本 continue，
                    # 这笔资金既不回现金也不记 trades，等于凭空蒸发。
                    close = float(pos.get("last_close") or 0)
                if pd.isna(close) or close <= 0:
                    close = float(pos["entry_price"])
                sell_price = close * (1 - p.slippage_rate)
                proceeds = pos["shares"] * sell_price
                commission_out = max(proceeds * p.commission_rate, 5.0)
                stamp = proceeds * p.stamp_tax_rate
                cash += proceeds - commission_out - stamp
                hold_days = (last_dt.date() - pd.to_datetime(pos["entry_date"]).date()).days
                trades.append({
                    "code": code,
                    "name": pos.get("name", code),
                    "buy_date": pos["entry_date"],
                    "buy_price": round(pos["entry_price"], 4),
                    "sell_date": last_dt.strftime("%Y-%m-%d"),
                    "sell_price": round(sell_price, 4),
                    "shares": pos["shares"],
                    "pnl": round((proceeds - commission_out - stamp) - pos["cost"], 2),
                    "pnl_pct": round(sell_price / pos["entry_price"] - 1, 4),
                    "hold_days": hold_days,
                    "exit_reason": "force_close",
                })
                del positions[code]

        # 当前持仓（截止日仍持有的）
        live_positions = []
        for code, pos in positions.items():
            df = stock_data.get(code)
            if df is None or df.empty:
                last_close = pos["entry_price"]
            else:
                last_close = float(df.iloc[-1]["close"])
                if pd.isna(last_close) or last_close <= 0:
                    last_close = pos["entry_price"]
            live_positions.append({
                "code": code,
                "name": pos.get("name", code),
                "shares": pos["shares"],
                "buy_price": round(pos["entry_price"], 4),
                "current_price": round(last_close, 4),
                "pnl": round((last_close - pos["entry_price"]) * pos["shares"], 2),
                "pnl_pct": round(last_close / pos["entry_price"] - 1, 4),
            })

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "positions": live_positions,
            "final_assets": round(cash, 2),
            "trade_dates": trade_dates,
        }

    # ──────── 绩效指标 ────────
    def _compute_metrics(
        self,
        equity_curve: List[Dict[str, Any]],
        trades: List[Dict[str, Any]],
        final_assets: float,
    ) -> Dict[str, Any]:
        if not equity_curve:
            return self._zero_metrics()
        eq = pd.DataFrame(equity_curve)
        eq["date"] = pd.to_datetime(eq["date"])
        eq = eq.sort_values("date").reset_index(drop=True)
        total = final_assets if final_assets else float(eq["total"].iloc[-1])
        init = float(self.params.initial_cash)
        total_return = (total / init - 1) if init > 0 else 0.0

        days = max(1, (eq["date"].iloc[-1] - eq["date"].iloc[0]).days)
        annual_return = (1 + total_return) ** (365.0 / days) - 1 if days > 0 else 0.0

        daily_ret = eq["total"].pct_change().fillna(0)
        std = daily_ret.std()
        rf = float(getattr(self.params, "risk_free_rate", 0.02) or 0.0)
        if std and std > 0:
            sharpe = float((daily_ret.mean() - rf / 252) / std * math.sqrt(252))
        else:
            sharpe = 0.0

        roll_max = eq["total"].cummax()
        drawdown = (eq["total"] - roll_max) / roll_max
        max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0

        closed = trades  # 全部交易都算
        wins = [t for t in closed if (t.get("pnl") or 0) > 0]
        losses = [t for t in closed if (t.get("pnl") or 0) < 0]
        win_rate = len(wins) / max(1, len(closed))
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 0.0
        profit_factor = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses else (
            99.0 if wins else 0.0
        )

        max_cw, max_cl, cur_cw, cur_cl = 0, 0, 0, 0
        for t in closed:
            if (t.get("pnl") or 0) > 0:
                cur_cw += 1
                cur_cl = 0
                max_cw = max(max_cw, cur_cw)
            else:
                cur_cl += 1
                cur_cw = 0
                max_cl = max(max_cl, cur_cl)

        avg_hold = sum(t["hold_days"] for t in closed) / len(closed) if closed else 0.0

        return {
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "max_drawdown": round(max_drawdown, 4),
            "sharpe_ratio": round(sharpe, 3),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3),
            "avg_win_pct": round(avg_win, 4),
            "avg_loss_pct": round(avg_loss, 4),
            "avg_hold_days": round(avg_hold, 1),
            "max_consecutive_wins": max_cw,
            "max_consecutive_losses": max_cl,
            "total_trades": len(closed),
            "win_trades": len(wins),
            "loss_trades": len(losses),
        }

    def _zero_metrics(self) -> Dict[str, Any]:
        return {
            "total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
            "sharpe_ratio": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "avg_hold_days": 0.0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "total_trades": 0, "win_trades": 0, "loss_trades": 0,
        }

    # ──────── 月度收益 ────────
    def _monthly_returns(self, equity_curve: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not equity_curve:
            return []
        df = pd.DataFrame(equity_curve)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        monthly = df["total"].resample("ME").last()
        prev = monthly.shift(1)
        init = float(self.params.initial_cash)
        first = monthly.index[0]
        if first in prev and pd.isna(prev.loc[first]):
            prev.loc[first] = init
        mret = (monthly / prev - 1).fillna(0)
        out = []
        for d, r in mret.items():
            out.append({"month": d.strftime("%Y-%m"), "return": round(float(r), 4)})
        return out

    def _empty(self, reason: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error": reason,
            "trades": [],
            "equity_curve": [],
            "monthly_returns": [],
            "positions": [],
            "init_cash": self.params.initial_cash,
            "final_assets": self.params.initial_cash,
            "stock_pool_size": 0,
            "trade_days": 0,
        }
