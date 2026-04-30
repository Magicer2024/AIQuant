"""
backtest/independent_backtest.py
================================
单股独立回测引擎（与扫描结果一致）

每只股票独立回测，不受资金/持股数约束：
  - 信号日触发 → 次日开盘价买入（T+1）
  - 卖出条件满足 → 次日开盘价卖出
  - 扣手续费
  - 所有股票的所有信号笔数合计统计

与 custom_strategy_backtest.py（组合模式）的区别：
  - 组合模式：全市场共享资金，受 max_positions 限制
  - 独立模式：每只股票独立，不限资金，胜率=全市场信号总胜率
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Callable, Optional
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import get_conn, get_all_stocks
from backtest.strategy_screen_backtest import add_indicators

# ── 交易成本（与扫描引擎一致）──
COMMISSION_RATE = 0.0003   # 买入佣金率
STAMP_DUTY      = 0.001    # 卖出印花税


def _load_stock_data(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """加载单只股票历史数据"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT dp.trade_date,
                   dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.pct_change, dp.turnover,
                   dp.vol_score, dp.ma_score, dp.diverge_score,
                   dp.bottom_score, dp.whale_score, dp.fusion_score,
                   si.name
            FROM daily_price dp
            LEFT JOIN stock_info si ON dp.code = si.code
            WHERE dp.code = ?
              AND dp.trade_date BETWEEN ? AND ?
              AND dp.close IS NOT NULL AND dp.close > 0
            ORDER BY dp.trade_date ASC
            """,
            conn,
            params=(code, start_date, end_date)
        )
    if df.empty:
        return df
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df['vol'] = df['volume']
    return df


def _load_all_stocks_info() -> pd.DataFrame:
    """加载所有活跃股票信息"""
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT code, name, market FROM stock_info WHERE is_active=1",
            conn
        )


def _static_filter(code: str, name: str,
                   exclude_st=True, exclude_kcb=True, exclude_cyb=False) -> bool:
    name = name or ''
    if exclude_st and 'ST' in name.upper():
        return False
    if exclude_kcb and code.startswith('688'):
        return False
    if exclude_cyb and (code.startswith('300') or code.startswith('301')):
        return False
    return True


def _backtest_single(df: pd.DataFrame, signal_mask,
                     hold_days: int,
                     stop_loss: float = -0.08,
                     take_profit: float = 0.20) -> list[dict]:
    """
    单股回测（与扫描引擎 backtest_signal 完全一致）

    Args:
        df:           含 trade_date/open/close 的 DataFrame
        signal_mask:  boolean Series aligned with df.index
        hold_days:    最大持股天数（日历天数）
        stop_loss:    止损线（负数）
        take_profit:  止盈线（正数）
    """
    sig = pd.Series(np.asarray(signal_mask, dtype=bool), index=df.index)
    if sig.sum() < 1:
        return []

    dates = df.index.tolist() if hasattr(df.index, 'tolist') else list(df.index)
    n = len(dates)

    trades = []
    position = 0
    entry_price = 0.0
    entry_date = None

    for i, date in enumerate(dates):
        row = df.iloc[i]

        # ── 买入：信号日选股，次日开盘价买入 ──
        if sig.get(date, False) and position == 0 and i + 1 < n:
            next_row = df.iloc[i + 1]
            buy_price = next_row['open']
            if pd.isna(buy_price) or buy_price <= 0:
                continue
            # 扣买入佣金
            cost_price = buy_price * (1 + COMMISSION_RATE)
            position = 1
            entry_price = cost_price
            entry_date = dates[i + 1]  # 实际买入日=次日
            continue

        # ── 持仓中：检查卖出条件 ──
        if position > 0:
            days_held = (date - entry_date).days if hasattr(date, '__sub__') else 0
            cur_close = row['close']
            if pd.isna(cur_close):
                continue

            raw_ret = (cur_close - entry_price) / entry_price

            should_sell = False
            # T+1：次日才能卖出
            if days_held >= 1:
                if raw_ret <= stop_loss:
                    should_sell = True
                elif raw_ret >= take_profit:
                    should_sell = True
            # 到期卖出
            if not should_sell and days_held >= hold_days:
                should_sell = True

            if should_sell and i + 1 < n:
                # 次日开盘价卖出
                next_row = df.iloc[i + 1]
                sell_price = next_row['open']
                if pd.isna(sell_price) or sell_price <= 0:
                    continue
                net_sell = sell_price * (1 - STAMP_DUTY - COMMISSION_RATE)
                net_ret = (net_sell - entry_price) / entry_price

                trades.append({
                    'entry': entry_date,
                    'exit':  dates[i + 1],
                    'ret':   net_ret,
                    'hold':  (dates[i + 1] - entry_date).days if hasattr(dates[i + 1], '__sub__') else 0,
                })
                position = 0
            elif should_sell and i + 1 >= n:
                # 最后一天无法次日卖出，用收盘价
                net_sell = cur_close * (1 - STAMP_DUTY - COMMISSION_RATE)
                net_ret = (net_sell - entry_price) / entry_price
                trades.append({
                    'entry': entry_date,
                    'exit':  date,
                    'ret':   net_ret,
                    'hold':  days_held,
                })
                position = 0

    return trades


def run_independent_backtest(
    strategy_fn: Callable,
    start_date: str,
    end_date: str,
    hold_days: int = 5,
    stop_loss: float = -0.08,
    take_profit: float = 0.20,
    warmup_days: int = 90,
    exclude_st: bool = True,
    exclude_kcb: bool = True,
    exclude_cyb: bool = False,
    stock_pool: Optional[list] = None,
    strategy_params: Optional[dict] = None,
    progress_cb=None,
) -> dict:
    """
    独立回测：对每只股票单独运行策略函数，统计全市场信号总胜率

    Returns: dict 含胜率、总交易数、收益率分布等
    """
    params = strategy_params or {}

    # ── 确定股票池 ──
    if stock_pool:
        pool_codes = stock_pool
    else:
        stocks_df = _load_all_stocks_info()
        pool_codes = stocks_df['code'].tolist()

    # ── 时间范围（含预热期） ──
    start_dt  = datetime.strptime(start_date, '%Y-%m-%d')
    warm_start = (start_dt - timedelta(days=warmup_days)).strftime('%Y-%m-%d')

    # ── 逐股回测 ──
    all_trades = []
    errors = []
    total = len(pool_codes)

    from backtest.indicator_engine import compute_indicators

    for i, code in enumerate(pool_codes):
        # 静态过滤
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT name FROM stock_info WHERE code=?", (code,)
                ).fetchone()
                name_row = row[0] if row else ''
        except Exception:
            name_row = ''

        if not _static_filter(code, name_row, exclude_st, exclude_kcb, exclude_cyb):
            continue

        # 加载数据
        df = _load_stock_data(code, warm_start, end_date)
        if df.empty or len(df) < 20:
            continue

        try:
            # 计算指标
            df = add_indicators(df)
            df = compute_indicators(df, extra_ranks=False)
            extra_params = dict(params)
            extra_params['stock_code'] = code
            result_df = strategy_fn(df.copy(), **extra_params)

            if result_df is None or 'signal' not in result_df.columns:
                continue

            # 生成信号 mask
            signal_mask = result_df['signal'] == 1

            # 只取回测区间内的信号
            mask_in_range = (
                (result_df['trade_date'] >= pd.Timestamp(start_date)) &
                (result_df['trade_date'] <= pd.Timestamp(end_date))
            )
            # 用 trade_date 作为 index 方便回测
            result_indexed = result_df.set_index('trade_date')
            signal_in_range = signal_mask[mask_in_range]

            # 单股回测
            trades = _backtest_single(
                result_indexed, signal_in_range,
                hold_days=hold_days,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            all_trades.extend(trades)

        except Exception as e:
            errors.append(f"{code}: {type(e).__name__}: {e}")
            continue

        if progress_cb and i % 50 == 0:
            pct = int(i / max(total, 1) * 90)
            progress_cb(pct, 100, f"独立回测 {code} ({i}/{total})")

    # ── 统计 ──
    if not all_trades:
        return {
            "error": "无交易信号",
            "total_trades": 0,
        }

    rets = np.asarray([t['ret'] for t in all_trades], dtype=np.float64)
    n = len(rets)
    wins   = rets[rets > 0]
    losses = rets[rets <= 0]

    win_rate    = len(wins) / n * 100
    avg_win     = float(np.mean(wins)) * 100 if len(wins) else 0
    avg_loss    = float(np.mean(losses)) * 100 if len(losses) else 0
    avg_ret     = float(np.mean(rets)) * 100
    profit_factor = abs(float(np.sum(wins)) / float(np.sum(losses))) if len(losses) and float(np.sum(losses)) < 0 else 0
    std = float(np.std(rets)) * 100
    sharpe = (float(np.mean(rets)) / std * np.sqrt(252 / 5)) if std > 1e-10 else 0

    # 几何平均
    safe_rets = np.clip(rets, -0.9999, 100)
    log_sum   = float(np.sum(np.log1p(safe_rets)))
    gmean     = np.expm1(log_sum / n) if n > 0 else 0
    total_ret = min(float(np.expm1(log_sum)), 1e6) * 100

    # 构造与组合模式兼容的结果格式
    # 为每笔交易构造标准格式
    trade_records = []
    for t in all_trades:
        entry_d = t['entry']
        exit_d = t['exit']
        trade_records.append({
            'buy_date':    entry_d.strftime('%Y-%m-%d') if hasattr(entry_d, 'strftime') else str(entry_d),
            'sell_date':   exit_d.strftime('%Y-%m-%d') if hasattr(exit_d, 'strftime') else str(exit_d),
            'code':        '',
            'name':        '',
            'entry_price': 0,
            'sell_price':  0,
            'shares':      0,
            'pnl':         0,
            'ret_pct':     round(t['ret'] * 100, 2),
            'hold_days':   t.get('hold', 0),
            'reason':      '独立回测',
        })

    # 用累计收益率构造资金曲线（无真实资金，用 100000 为基准按几何累计）
    eq_value = 100000.0
    equity_curve = [{"date": start_date, "equity": eq_value, "cash": eq_value, "pos_value": 0}]
    for t in sorted(all_trades, key=lambda x: x['exit'] if hasattr(x['exit'], '__lt__') else x['entry']):
        exit_d = t['exit']
        eq_value *= (1 + t['ret'])
        equity_curve.append({
            "date": exit_d.strftime('%Y-%m-%d') if hasattr(exit_d, 'strftime') else str(exit_d),
            "equity": round(eq_value, 2),
            "cash": round(eq_value, 2),
            "pos_value": 0,
        })

    result = {
        "init_capital":   100000,
        "final_capital":  round(eq_value, 2),
        "total_return":   round(total_ret, 2),
        "annual_return":  round(((1 + total_ret/100) ** (252 / max(n, 1)) - 1) * 100, 2) if total_ret < 1e6 else 0,
        "max_drawdown":   0,  # 独立模式无组合回撤
        "sharpe":         round(sharpe, 2),
        "total_trades":   n,
        "win_rate":       round(win_rate, 2),
        "avg_win_pct":    round(avg_win, 2),
        "avg_loss_pct":   round(avg_loss, 2),
        "profit_ratio":   round(profit_factor, 2),
        "equity_curve":   equity_curve,
        "trades":         trade_records,
        "daily_selected": [],
        "daily_signals":  [],
        "errors":         errors[:20],
        "_independent":   True,  # 标记为独立模式结果
        "_gmean_pct":     round(gmean * 100, 4),  # 几何均盈%
    }

    if progress_cb:
        progress_cb(100, 100, f"独立回测完成：{n}笔交易，胜率{win_rate:.1f}%")

    return result
