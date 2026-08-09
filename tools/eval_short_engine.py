"""
tools/eval_short_engine.py —— 短线引擎回测对比（只读评估，不写库）

对比两套短线入场信号在同一回测口径下的表现：
  OLD = 纯抄底 strategy_bottom_fishing.BUY_SIGNAL（现网 fusion_score 等价物）
  NEW = 超跌反弹v3 BUY_SIGNAL & 趋势闸门 & 质量过滤（本次改造）

两套信号喂入同一个 VisualBacktestEngine._run_portfolio + _compute_metrics，
交易口径（止损/止盈/持仓上限/单日买入/成本）完全一致，做苹果对苹果对比。

用法：
    python tools/eval_short_engine.py                 # 近1年、全活跃池
    python tools/eval_short_engine.py --limit 300     # 采样 300 只做快速冒烟
    python tools/eval_short_engine.py --start 2025-01-01 --end 2026-01-01
"""
import sys
import os
import argparse
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.db import get_conn, get_market_cap_map
from backtest.engine import (
    BacktestParams,
    VisualBacktestEngine,
    _load_stock_pool,
    _add_indicators,
    _load_stock_info,
)
from strategy.strategies import (strategy_bottom_fishing, strategy_bottom_fishing_v2,
                                 strategy_oversold_rebound)
from strategy.rec_filters import trend_gate_series, quality_series
from config.strategy_params import OVERSOLD_REBOUND_V4
from backtest.oversold_sim import run_v3_portfolio


# ─────────────────────────────────────────────
# 数据加载（复用回测引擎口径，额外带上 amount 供流动性过滤）
# ─────────────────────────────────────────────

def _latest_trade_date() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
    return row["d"] if row and row["d"] else None


def _load_daily_with_amount(start_date: str, end_date: str, codes: List[str]) -> pd.DataFrame:
    """与 engine._load_all_daily 同口径，额外选出 amount（成交额，供质量过滤）"""
    if not codes:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(codes))
    cols = ("code, trade_date, open, high, low, close, volume, "
            "COALESCE(amount, 0) AS amount, "
            "COALESCE(fusion_score, 0) AS fusion_score")
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


def _build_stock_data(daily: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    stock_data: Dict[str, pd.DataFrame] = {}
    for code, df in daily.groupby("code", sort=False):
        df = df.set_index("trade_date").sort_index()
        df = _add_indicators(df)
        stock_data[code] = df
    return stock_data


# ─────────────────────────────────────────────
# 两套入场信号掩码
# ─────────────────────────────────────────────

def _old_signals(stock_data: Dict[str, pd.DataFrame], engine: str = "v1") -> Dict[str, pd.Series]:
    """OLD：纯抄底 BUY_SIGNAL（engine=v1 已反弹 / v2 买回踩，A2 组合口径对照）"""
    fn = strategy_bottom_fishing_v2 if engine == "v2" else strategy_bottom_fishing
    out: Dict[str, pd.Series] = {}
    for code, df in stock_data.items():
        try:
            res = fn(df)
            out[code] = res["BUY_SIGNAL"].reindex(df.index, fill_value=False)
        except Exception:
            out[code] = pd.Series(False, index=df.index)
    return out


def _new_signals(stock_data: Dict[str, pd.DataFrame],
                 name_map: Dict[str, str],
                 mktcap_map: Dict[str, dict]) -> Dict[str, pd.Series]:
    """NEW：超跌反弹v3 & 趋势闸门 & 质量过滤"""
    out: Dict[str, pd.Series] = {}
    for code, df in stock_data.items():
        try:
            res = strategy_oversold_rebound(df)
            buy = res["BUY_SIGNAL"].reindex(df.index, fill_value=False)
            gate = trend_gate_series(df).reindex(df.index, fill_value=False)
            ts = (mktcap_map.get(code) or {}).get("total_shares")
            qual = quality_series(name_map.get(code, code), df, ts).reindex(df.index, fill_value=False)
            out[code] = (buy & gate & qual).fillna(False)
        except Exception:
            out[code] = pd.Series(False, index=df.index)
    return out


# ─────────────────────────────────────────────
# 回测执行
# ─────────────────────────────────────────────

def _run(params: BacktestParams, stock_data, signals, info_map) -> dict:
    engine = VisualBacktestEngine(params)
    run_result = engine._run_portfolio(stock_data, signals, info_map)
    metrics = engine._compute_metrics(
        run_result["equity_curve"], run_result["trades"], run_result["final_assets"]
    )
    return metrics


def _run_v3(params: BacktestParams, stock_data, signals, info_map) -> dict:
    """v3 专属出场（移动止盈/分批止盈/40%单仓/大盘择时）下的组合回测"""
    v3 = OVERSOLD_REBOUND_V4
    engine = VisualBacktestEngine(params)  # 仅用其 _compute_metrics
    run_result = run_v3_portfolio(
        stock_data, signals, info_map,
        start_date=params.start_date, end_date=params.end_date,
        initial_cash=params.initial_cash,
        stop_loss=v3["stop_loss"],
        trailing_pct=v3["trailing_pct"],
        single_pos_ratio=v3["single_pos_ratio"],
        use_market_timing=v3["use_market_timing"],
        max_hold_days=params.max_hold_days or 10,
    )
    return engine._compute_metrics(
        run_result["equity_curve"], run_result["trades"], run_result["final_assets"]
    )


def _count_signals(signals: Dict[str, pd.Series]) -> int:
    return int(sum(int(s.sum()) for s in signals.values()))


# ─────────────────────────────────────────────
# 报告
# ─────────────────────────────────────────────

_ROWS = [
    ("win_rate",       "胜率",        "pct"),
    ("profit_factor",  "盈亏比(PF)",  "num"),
    ("avg_win_pct",    "平均盈利",    "pct"),
    ("avg_loss_pct",   "平均亏损",    "pct"),
    ("max_drawdown",   "最大回撤",    "pct"),
    ("annual_return",  "年化收益",    "pct"),
    ("total_return",   "总收益",      "pct"),
    ("total_trades",   "交易笔数",    "int"),
]


def _fmt(v, kind: str) -> str:
    if kind == "pct":
        return f"{v * 100:+.2f}%"
    if kind == "int":
        return f"{int(v)}"
    return f"{v:.3f}"


def _print_report(old: dict, new: dict, old_sig: int, new_sig: int,
                  params: BacktestParams, exit_mode: str = "simple",
                  engine: str = "v1"):
    print("=" * 62)
    print("  短线引擎回测对比  OLD(纯抄底)  vs  NEW(超跌反弹v3+闸门+质量)")
    print(f"  OLD 引擎: {engine}")
    print(f"  窗口: {params.start_date} ~ {params.end_date}")
    if exit_mode == "v3":
        v3 = OVERSOLD_REBOUND_V4
        print(f"  出场: v3专属 — 硬止损 {v3['stop_loss']:+.0%} / 移动止盈回撤 {v3['trailing_pct']:.0%}"
              f" / 分批止盈 +10% / 单仓 {v3['single_pos_ratio']:.0%} / 大盘择时 上证MA5>MA20")
    else:
        print(f"  出场: simple — 固定止损 {params.stop_loss_pct:+.0%} / 止盈 "
              f"{('%.0f%%' % (params.take_profit_pct * 100)) if params.take_profit_pct else '—'}"
              f" / 最长持仓 {params.max_hold_days}日 / 持仓上限 {params.max_holdings}")
    print(f"  入场信号数(全历史触发点): OLD={old_sig}  NEW={new_sig}")
    print("-" * 62)
    print(f"  {'指标':<12}{'OLD':>16}{'NEW':>16}")
    print("-" * 62)
    for key, label, kind in _ROWS:
        print(f"  {label:<12}{_fmt(old.get(key, 0), kind):>16}{_fmt(new.get(key, 0), kind):>16}")
    print("-" * 62)

    # go/no-go 门槛
    wr_ok = new["win_rate"] >= old["win_rate"]
    pf_ok = new["profit_factor"] >= old["profit_factor"]
    # max_drawdown 是负数（drawdown.min()），"不恶化"= 不比基准更负。
    # 2026-07-31 修正：原写法 new <= old + 0.03 方向反了，反而要求新方案回撤更深才 PASS。
    dd_ok = new["max_drawdown"] >= old["max_drawdown"] - 0.03  # 允许多回撤 3 个百分点
    verdict = "GO 建议上线（触发重算）" if (wr_ok and pf_ok and dd_ok) else "NO-GO 数字未达标，交用户定夺"
    print("  上线门槛:")
    print(f"    胜率不降  : {'[PASS]' if wr_ok else '[FAIL]'}  ({old['win_rate']*100:.2f}% -> {new['win_rate']*100:.2f}%)")
    print(f"    盈亏比不降: {'[PASS]' if pf_ok else '[FAIL]'}  ({old['profit_factor']:.3f} -> {new['profit_factor']:.3f})")
    print(f"    回撤可控  : {'[PASS]' if dd_ok else '[FAIL]'}  ({old['max_drawdown']*100:.2f}% -> {new['max_drawdown']*100:.2f}%，容忍+3pct)")
    print("-" * 62)
    print(f"  结论: {verdict}")
    print("=" * 62)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="短线引擎 OLD vs NEW 回测对比（只读）")
    ap.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD，默认 end 前 365 天")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD，默认最新交易日")
    ap.add_argument("--limit", type=int, default=0, help="股票池采样上限（0=全池），用于快速冒烟")
    ap.add_argument("--take-profit", type=float, default=0.12,
                    help="固定止盈比例（引擎不支持移动止盈，用固定值近似，OLD/NEW 一致）")
    ap.add_argument("--max-hold", type=int, default=10, help="最长持仓天数")
    ap.add_argument("--exit", choices=["v3", "simple"], default="v3",
                    help="出场口径：v3=移动止盈/分批止盈/40%单仓/大盘择时（默认）；simple=固定止损止盈")
    ap.add_argument("--engine", choices=["v1", "v2"], default="v1",
                    help="OLD 引擎：v1=已反弹（默认）/ v2=买回踩（A2 组合口径对照）")
    args = ap.parse_args()

    end = args.end or _latest_trade_date()
    if not end:
        print("[ERR] daily_price 无数据，请先同步行情")
        return
    if args.start:
        start = args.start
    else:
        start = (pd.to_datetime(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    print(f"[1/5] 加载股票池...")
    codes = _load_stock_pool(exclude_kcb=True, exclude_cyb=False)
    if args.limit and args.limit > 0:
        codes = codes[: args.limit]
    if not codes:
        print("[ERR] 无活跃股票，请先初始化 stock_info")
        return
    print(f"      股票池 {len(codes)} 只")

    print(f"[2/5] 加载行情 {start} ~ {end}...")
    daily = _load_daily_with_amount(start, end, codes)
    if daily.empty:
        print("[ERR] 区间内无行情数据")
        return
    stock_info = _load_stock_info(codes)
    name_map = {r["code"]: r["name"] for _, r in stock_info.iterrows()}
    info_map = {r["code"]: r.to_dict() for _, r in stock_info.iterrows()}
    mktcap_map = get_market_cap_map()

    print(f"[3/5] 计算指标...")
    stock_data = _build_stock_data(daily)

    print(f"[4/5] 生成两套入场信号（OLD 引擎={args.engine}）...")
    old_sig = _old_signals(stock_data, engine=args.engine)
    new_sig = _new_signals(stock_data, name_map, mktcap_map)

    params = BacktestParams(
        start_date=start,
        end_date=end,
        initial_cash=1_000_000.0,
        max_holdings=5,
        max_buy_per_day=3,
        buy_timing="next_day_open",
        take_profit_pct=args.take_profit,
        stop_loss_pct=OVERSOLD_REBOUND_V4["stop_loss"],
        max_hold_days=args.max_hold,
        exclude_st=True,
        exclude_kcb=True,
        exclude_cyb=False,
    )

    print(f"[5/5] 回测中（OLD / NEW 同口径，出场={args.exit}）...")
    if args.exit == "v3":
        old_metrics = _run_v3(params, stock_data, old_sig, info_map)
        new_metrics = _run_v3(params, stock_data, new_sig, info_map)
    else:
        old_metrics = _run(params, stock_data, old_sig, info_map)
        new_metrics = _run(params, stock_data, new_sig, info_map)

    _print_report(old_metrics, new_metrics,
                  _count_signals(old_sig), _count_signals(new_sig), params, args.exit,
                  args.engine)


if __name__ == "__main__":
    main()
