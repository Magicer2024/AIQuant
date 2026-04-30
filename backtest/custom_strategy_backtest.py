"""
backtest/custom_strategy_backtest.py
=====================================
自定义策略函数回测引擎
====================

支持用户直接编写 strategy_function(df, **params) 形式的策略代码进行全市场/指定股票回测。

strategy_function 约定：
  - 接收单只股票的历史 DataFrame（含 open/high/low/close/vol/pct_change/turnover 列）
  - 可使用 params 传入额外参数（如 stock_code 用于财务数据查询）
  - 必须在 df 中返回 'signal' 列：1=买入，-1=卖出，0=持有/无操作
  - 返回修改后的 DataFrame

列名约定（与 daily_price 表对应）：
  close, open, high, low, volume(vol), amount, pct_change, turnover
  + 策略评分: vol_score/ma_score/diverge_score/bottom_score/whale_score/fusion_score
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Callable, Optional
import sys, os
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import get_conn, get_all_stocks
from backtest.strategy_screen_backtest import add_indicators


# ──────────────────────────────────────────────────────────────────────────────
# 回测参数
# ──────────────────────────────────────────────────────────────────────────────

class CustomBacktestParams:
    def __init__(
        self,
        start_date: str        = "2025-01-01",
        end_date: str          = "2026-04-15",
        capital: float         = 100_000,
        # 仓位管理
        max_positions: int     = 5,          # 同时最大持股数
        position_pct: float    = 0.2,        # 每仓占总资金比例（0~1）
        # 止盈止损（额外保护，strategy_function 内部可自行处理出场）
        extra_stop_loss: float = -0.15,      # 额外止损线（None=禁用）
        extra_take_profit: float = None,     # 额外止盈线（None=禁用）
        max_hold_days: int     = None,       # 最大持仓天数（None=禁用）
        # 买入模式
        buy_at_open: bool      = True,       # True=次日开盘价买入，False=当日收盘价买入
        sell_at_open: bool     = True,       # True=次日开盘卖出，False=收盘价卖出
        # 手续费
        commission_rate: float = 0.0003,     # 买入手续费率
        stamp_duty: float      = 0.001,      # 卖出印花税
        min_commission: float  = 5.0,        # 最低佣金（元）
        # 股票池过滤
        exclude_st: bool       = True,
        exclude_kcb: bool      = True,
        exclude_cyb: bool      = False,
        # 回测股票池（None=全市场，list=指定股票代码列表）
        stock_pool: Optional[list] = None,
        # 历史数据加载额外天数（用于技术指标预热）
        warmup_days: int       = 90,
        # 传递给 strategy_function 的额外 params
        strategy_params: dict  = None,
        # 候选股排序规则（买入时按此字段排序，1=最优先）
        sort_by: str           = "fusion_score",
        sort_ascending: bool   = False,
    ):
        self.start_date       = start_date
        self.end_date         = end_date
        self.capital          = capital
        self.max_positions    = max_positions
        self.position_pct     = position_pct
        self.extra_stop_loss  = extra_stop_loss
        self.extra_take_profit = extra_take_profit
        self.max_hold_days    = max_hold_days
        self.buy_at_open      = buy_at_open
        self.sell_at_open     = sell_at_open
        self.commission_rate  = commission_rate
        self.stamp_duty       = stamp_duty
        self.min_commission   = min_commission
        self.exclude_st       = exclude_st
        self.exclude_kcb      = exclude_kcb
        self.exclude_cyb      = exclude_cyb
        self.stock_pool       = stock_pool
        self.warmup_days      = warmup_days
        self.strategy_params  = strategy_params or {}
        self.sort_by          = sort_by
        self.sort_ascending   = sort_ascending


# ──────────────────────────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────────────────────────

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
                   si.name,
                   si.total_shares, si.circ_shares
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
    # strategy_function 中可能用 'vol' 列，也可能用 'volume'，提供别名
    df['vol'] = df['volume']
    # 计算市值（亿元），注意：total_shares 可能为 NULL，要排除 NaN
    ts = df['total_shares'].iloc[0] if 'total_shares' in df.columns and not pd.isna(df['total_shares'].iloc[0]) else None
    cs = df['circ_shares'].iloc[0] if 'circ_shares' in df.columns and not pd.isna(df['circ_shares'].iloc[0]) else None
    if ts and ts > 0:
        df['total_mv'] = df['close'] * ts / 1e8   # 亿元
    if cs and cs > 0:
        df['circ_mv'] = df['close'] * cs / 1e8     # 亿元
    return df


def _load_all_stocks_info() -> pd.DataFrame:
    """加载所有活跃股票信息"""
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT code, name, market FROM stock_info WHERE is_active=1",
            conn
        )
    return df


# ──────────────────────────────────────────────────────────────────────────────
# 沙盒执行自定义策略代码
# ──────────────────────────────────────────────────────────────────────────────

def compile_strategy(code_str: str) -> tuple[Callable | None, str]:
    """
    编译并提取用户策略代码中的 strategy_function。

    返回: (function_object, error_message)
    若成功: (fn, '')
    若失败: (None, '错误说明')
    """
    # 预置安全的命名空间
    namespace = {
        '__builtins__': __builtins__,
        'pd': pd,
        'np': np,
    }

    # 自动注入 utils.finance_data 函数
    try:
        from utils.finance_data import (
            get_total_shares, get_float_shares,
            calculate_turnover_rate, get_stock_finance
        )
        namespace['get_total_shares'] = get_total_shares
        namespace['get_float_shares'] = get_float_shares
        namespace['calculate_turnover_rate'] = calculate_turnover_rate
        namespace['get_stock_finance'] = get_stock_finance
    except ImportError:
        pass  # 财务函数可选

    try:
        exec(compile(code_str, '<strategy>', 'exec'), namespace)
    except SyntaxError as e:
        return None, f"语法错误（第 {e.lineno} 行）: {e.msg}"
    except Exception as e:
        return None, f"代码执行错误: {e}"

    fn = namespace.get('strategy_function')
    if fn is None:
        return None, "未找到 strategy_function 函数定义，请确保函数名为 'strategy_function'"
    if not callable(fn):
        return None, "strategy_function 不是一个可调用函数"

    return fn, ''


# ──────────────────────────────────────────────────────────────────────────────
# 主回测引擎
# ──────────────────────────────────────────────────────────────────────────────

class CustomStrategyBacktest:

    def __init__(self, strategy_fn: Callable, params: CustomBacktestParams):
        self.fn = strategy_fn
        self.p  = params

    def _static_filter(self, code: str, name: str) -> bool:
        name = name or ''
        if self.p.exclude_st and 'ST' in name.upper():
            return False
        if self.p.exclude_kcb and code.startswith('688'):
            return False
        if self.p.exclude_cyb and (code.startswith('300') or code.startswith('301')):
            return False
        return True

    def run(self, progress_cb=None) -> dict:
        p = self.p

        # ── 确定股票池 ──
        if p.stock_pool:
            pool_codes = p.stock_pool
        else:
            stocks_df = _load_all_stocks_info()
            pool_codes = stocks_df['code'].tolist()

        # ── 时间范围（含预热期） ──
        start_dt  = datetime.strptime(p.start_date, '%Y-%m-%d')
        end_dt    = datetime.strptime(p.end_date, '%Y-%m-%d')
        warm_start = (start_dt - timedelta(days=p.warmup_days)).strftime('%Y-%m-%d')

        # ── Step1: 对每只股票运行策略函数，生成信号 ──
        # 结构: { code: DataFrame(含 signal 列) }
        signal_map: dict[str, pd.DataFrame] = {}
        errors: list[str] = []

        total = len(pool_codes)
        for i, code in enumerate(pool_codes):
            # 静态过滤（在加载数据前先过滤）
            name_row = None
            try:
                with get_conn() as conn:
                    row = conn.execute(
                        "SELECT name FROM stock_info WHERE code=?", (code,)
                    ).fetchone()
                    name_row = row[0] if row else ''
            except Exception:
                name_row = ''

            if not self._static_filter(code, name_row):
                continue

            # 加载数据
            df = _load_stock_data(code, warm_start, p.end_date)
            if df.empty or len(df) < 20:
                continue

            # 调用策略函数
            try:
                # 预计算技术指标
                from backtest.indicator_engine import compute_indicators
                df = add_indicators(df)
                assert 'trade_date' in df.columns, f"add_indicators 后缺少 trade_date: {list(df.columns)}"
                df = compute_indicators(df, extra_ranks=False)
                assert 'trade_date' in df.columns, f"compute_indicators 后缺少 trade_date: {list(df.columns)}"
                extra_params = dict(p.strategy_params)
                extra_params['stock_code'] = code
                result_df = self.fn(df.copy(), **extra_params)
                if result_df is None or 'signal' not in result_df.columns:
                    continue
                # 策略函数返回后，验证 trade_date 仍然存在
                assert 'trade_date' in result_df.columns, (
                    f"策略函数删除了 trade_date！股票: {code}，"
                    f"返回列: {list(result_df.columns)}"
                )
                signal_map[code] = result_df
            except AssertionError:
                raise  # 让 assert 直接抛出，显示具体信息
            except Exception as e:
                errors.append(f"{code}: {type(e).__name__}: {e}")
                continue

            if progress_cb and i % 50 == 0:
                pct = int(i / max(total, 1) * 60)  # 前60%进度用于策略计算
                progress_cb(pct, 100, f"策略计算 {code} ({i}/{total})")

        if progress_cb:
            # 信号统计：帮助用户排查为什么没信号
            total_buy_signals = 0
            codes_with_signal = 0
            sample_signals = []
            for code, df_s in signal_map.items():
                buy_count = int((df_s['signal'] == 1).sum())
                if buy_count > 0:
                    codes_with_signal += 1
                    total_buy_signals += buy_count
                    if len(sample_signals) < 5:
                        # 找第一个买入信号所在行
                        first_buy = df_s[df_s['signal'] == 1].iloc[0]
                        sample_signals.append(
                            f"{code}@{first_buy['trade_date'].strftime('%Y-%m-%d')}"
                        )

            stats_msg = (
                f"✅ 信号统计：{len(signal_map)}只股票有signal列，"
                f"{codes_with_signal}只产生了{total_buy_signals}个买入信号"
            )
            if sample_signals:
                stats_msg += f"\n示例: {', '.join(sample_signals)}"
            if errors:
                stats_msg += f"\n⚠️ {len(errors)}只股票执行出错: {errors[:3]}"
            progress_cb(60, 100, stats_msg)

        # ── Step2: 按时间模拟交易 ──
        # 获取回测区间内的所有交易日
        all_dates: list[pd.Timestamp] = []
        for code, df in signal_map.items():
            # 防御：若 trade_date 列不存在，报明确错误（含策略函数返回的列名列表）
            if 'trade_date' not in df.columns:
                raise RuntimeError(
                    f"策略函数返回的 DataFrame 缺少 'trade_date' 列！\n"
                    f"  股票: {code}\n"
                    f"  现有列: {list(df.columns)}\n"
                    f"  提示: 策略代码中请勿删除 'trade_date' 列"
                )
            dates_in_range = df[
                (df['trade_date'] >= pd.Timestamp(p.start_date)) &
                (df['trade_date'] <= pd.Timestamp(p.end_date))
            ]['trade_date'].tolist()
            all_dates.extend(dates_in_range)

        if not all_dates:
            return {"error": "回测区间内无任何有效交易日数据，请检查日期范围或数据是否已同步"}

        trade_dates = sorted(set(all_dates))

        # 构建快速查找索引: {code: {trade_date: row_index}}
        lookup: dict[str, dict] = {}
        for code, df in signal_map.items():
            df_reset = df.reset_index(drop=True)
            lookup[code] = {row['trade_date']: idx for idx, row in df_reset.iterrows()}
            signal_map[code] = df_reset

        capital    = float(p.capital)
        positions: list[dict] = []   # 当前持仓列表
        trades:    list[dict] = []
        equity:    list[dict] = []
        daily_signals: list[dict] = []  # 每日产生买入信号的股票

        n_dates = len(trade_dates)
        for day_i, today_ts in enumerate(trade_dates):

            # ── 卖出检查 ──
            # T+1 卖出逻辑：收盘时判断卖出条件 → 次日开盘价执行
            still_holding = []
            for pos in positions:
                code  = pos['code']
                df_s  = signal_map.get(code)
                if df_s is None:
                    still_holding.append(pos)
                    continue

                idx = lookup[code].get(today_ts)
                if idx is None:
                    still_holding.append(pos)
                    continue

                row = df_s.iloc[idx]
                # 用收盘价判断卖出条件（收盘后才确定是否需要卖出）
                close_price = float(row['close'])
                if pd.isna(close_price) or close_price <= 0:
                    still_holding.append(pos)
                    continue

                entry = pos['entry_price']
                ret   = (close_price - entry) / entry  # 用收盘价算收益率判断
                hold_n = pos['hold_days']
                pos['hold_days'] += 1

                # 跟踪最高价（用收盘价更新，保守估计）
                pos['peak'] = max(pos.get('peak', entry), close_price)

                # T+1 检查：A股当日买入，次日及之后才能触发止盈止损
                t1_ready = hold_n >= 1

                # 策略函数发出 -1 卖出信号
                sig = int(row.get('signal', 0))

                should_sell = False
                sell_reason = ''

                if sig == -1:
                    should_sell, sell_reason = True, '策略卖出'
                elif t1_ready:
                    if p.extra_stop_loss is not None and ret <= p.extra_stop_loss:
                        should_sell, sell_reason = True, f'强制止损{p.extra_stop_loss*100:.0f}%'
                    elif p.extra_take_profit is not None and ret >= p.extra_take_profit:
                        should_sell, sell_reason = True, f'强制止盈{p.extra_take_profit*100:.0f}%'
                if not should_sell and p.max_hold_days is not None and hold_n >= p.max_hold_days:
                    should_sell, sell_reason = True, f'强制到期{p.max_hold_days}天'

                if should_sell:
                    # T+1 延迟执行：收盘时决定卖出 → 次日开盘价执行
                    next_dates = [d for d in trade_dates if d > today_ts]
                    if next_dates:
                        next_day = next_dates[0]
                        next_idx = lookup[code].get(next_day)
                        if next_idx is not None:
                            next_row = df_s.iloc[next_idx]
                            sell_price = float(next_row['open'])
                            sell_date_actual = next_day
                        else:
                            # 次日无数据（停牌），用当日收盘价 fallback
                            sell_price = close_price
                            sell_date_actual = today_ts
                    else:
                        # 最后一个交易日，无法次日执行
                        sell_price = close_price
                        sell_date_actual = today_ts

                    if pd.isna(sell_price) or sell_price <= 0:
                        sell_price = close_price
                        sell_date_actual = today_ts

                    shares   = pos['shares']
                    amount   = sell_price * shares
                    comm     = max(amount * p.commission_rate, p.min_commission)
                    stamp    = amount * p.stamp_duty
                    proceeds = amount - comm - stamp
                    pnl      = proceeds - pos['cost']
                    capital += proceeds
                    actual_ret = (sell_price - entry) / entry
                    trades.append({
                        'buy_date':    pos['buy_date'].strftime('%Y-%m-%d'),
                        'sell_date':   pd.Timestamp(sell_date_actual).strftime('%Y-%m-%d'),
                        'code':        code,
                        'name':        pos['name'],
                        'entry_price': round(entry, 3),
                        'sell_price':  round(sell_price, 3),
                        'shares':      shares,
                        'pnl':         round(pnl, 2),
                        'ret_pct':     round(actual_ret * 100, 2),
                        'hold_days':   hold_n,
                        'reason':      sell_reason,
                    })
                else:
                    still_holding.append(pos)

            positions = still_holding

            # ── 买入检查 ──
            can_buy = p.max_positions - len(positions)
            if can_buy > 0:
                held_codes = {pos['code'] for pos in positions}
                buy_candidates = []

                for code, df_s in signal_map.items():
                    if code in held_codes:
                        continue
                    idx = lookup[code].get(today_ts)
                    if idx is None:
                        continue
                    row = df_s.iloc[idx]
                    sig = int(row.get('signal', 0))
                    if sig == 1:
                        buy_candidates.append((code, row))

                if not buy_candidates:
                    continue

                # ── 计算当日候选股的截面排名 ──
                sort_col  = p.sort_by
                asc       = p.sort_ascending

                # 量能类字段（需要额外计算排名）
                rank_cols = {'vol_rank', 'amount_rank', 'vol5d_rank', 'volprev_rank'}
                need_rank = sort_col in rank_cols or sort_col == 'fusion_score'
                if need_rank and len(buy_candidates) > 1:
                    # 提取候选股当日数据
                    cand_df = pd.DataFrame([
                        {**dict(r), 'c_code': c} for c, r in buy_candidates
                    ])
                    # 计算排名（1=最大，rank升序时1=最小）
                    for col in ['volume', 'amount', 'vol_sum_5d', 'vol_vs_prev_ratio', 'vol_5d_vs_prev_ratio', 'fusion_score']:
                        if col in cand_df.columns:
                            cand_df[col] = pd.to_numeric(cand_df[col], errors='coerce').fillna(0)
                            cand_df[f'{col}_rnk'] = cand_df[col].rank(ascending=False, pct=False).astype(int)

                    # 回填到候选元组
                    rnk_map = {row['c_code']: row for _, row in cand_df.iterrows()}
                    buy_candidates_ranked = []
                    for code, row in buy_candidates:
                        rn = rnk_map.get(code, {})
                        buy_candidates_ranked.append((code, row, rn))
                    buy_candidates = buy_candidates_ranked
                else:
                    buy_candidates = [(c, r, {}) for c, r in buy_candidates]

                # ── 记录每日信号股票（含截面排名） ──
                for code, cand_row, rnk_row in buy_candidates:
                    sig_record = {
                        'date':         today_ts.strftime('%Y-%m-%d'),
                        'code':         code,
                        'name':         str(cand_row.get('name', '')),
                        'close':        round(float(cand_row.get('close', 0) or 0), 3),
                        'pct_change':   round(float(cand_row.get('pct_change', 0) or 0), 2),
                        'turnover':     round(float(cand_row.get('turnover', 0) or 0), 3),
                        'fusion_score': round(float(cand_row.get('fusion_score', 0) or 0), 1),
                        'amount':       round(float(cand_row.get('amount', 0) or 0) / 1e4, 1),  # 万
                        'sort_val':     round(float(cand_row.get(sort_col, 0) or 0), 3),
                    }
                    # 截面排名
                    col_map = {
                        'vol_rank': 'volume_rnk',
                        'amount_rank': 'amount_rnk',
                        'vol5d_rank': 'vol_sum_5d_rnk',
                        'volprev_rank': 'vol_vs_prev_ratio_rnk',
                        'vol_5d_vs_prev_ratio': 'vol_5d_vs_prev_ratio_rnk',
                    }
                    for rc, src in col_map.items():
                        sig_record[rc] = rnk_row.get(src, 0) or 0
                    daily_signals.append(sig_record)

                # ── 按配置字段排序（优先买排前的票） ──
                def _sort_key(item):
                    code, cand_row, rnk_row = item
                    col = sort_col
                    if col in rank_cols:
                        src = col_map.get(col, 'volume_rnk')
                        return rnk_row.get(src, 9999)
                    val = float(cand_row.get(col, 0) or 0)
                    return val if not asc else -val

                buy_candidates.sort(key=_sort_key, reverse=(not asc))

                for code, cand_row, _ in buy_candidates[:can_buy]:
                    # 取买入价格
                    # buy_at_open=True: 信号日选股 → 次日开盘价买入（T+1）
                    # buy_at_open=False: 信号日收盘价买入（存在未来数据泄露风险，但保留用户选择）
                    df_s = signal_map[code]
                    if p.buy_at_open:
                        # 找次日开盘价
                        next_dates = [d for d in trade_dates if d > today_ts]
                        if not next_dates:
                            continue
                        next_day = next_dates[0]
                        next_idx = lookup[code].get(next_day)
                        if next_idx is None:
                            continue
                        next_row   = df_s.iloc[next_idx]
                        buy_price  = float(next_row['open'])
                        buy_date   = next_day
                    else:
                        # 收盘价买入 = 当天收盘价（注意：盘中无法知道收盘价，存在look-ahead bias）
                        buy_price = float(cand_row['close'])
                        buy_date  = today_ts

                    if pd.isna(buy_price) or buy_price <= 0:
                        continue

                    # 计算买入股数（等比仓位）
                    per_pos = capital * p.position_pct
                    shares  = int(per_pos / buy_price / 100) * 100
                    if shares < 100:
                        continue
                    amount = shares * buy_price
                    comm   = max(amount * p.commission_rate, p.min_commission)
                    cost   = amount + comm
                    if cost > capital:
                        continue

                    capital -= cost
                    positions.append({
                        'code':        code,
                        'name':        str(cand_row.get('name', '')),
                        'buy_date':    pd.Timestamp(buy_date),
                        'entry_price': buy_price,
                        'shares':      shares,
                        'cost':        cost,
                        'hold_days':   1,
                        'peak':        buy_price,
                    })

            # ── 资金曲线 ──
            pos_value = 0.0
            for pos in positions:
                df_s = signal_map.get(pos['code'])
                if df_s is not None:
                    idx = lookup[pos['code']].get(today_ts)
                    if idx is not None:
                        pos_value += df_s.iloc[idx]['close'] * pos['shares']
                    else:
                        # 当日无数据（停牌/数据缺失）→ 用最近可用收盘价延续估值
                        past_dates = [d for d in trade_dates[:day_i+1] if lookup[pos['code']].get(d) is not None]
                        if past_dates:
                            last_idx = lookup[pos['code']].get(past_dates[-1])
                            if last_idx is not None:
                                pos_value += df_s.iloc[last_idx]['close'] * pos['shares']
            total_equity = capital + pos_value
            equity.append({
                'date':      today_ts.strftime('%Y-%m-%d'),
                'equity':    round(total_equity, 2),
                'cash':      round(capital, 2),
                'pos_value': round(pos_value, 2),
            })

            if progress_cb and day_i % 10 == 0:
                pct = 60 + int(day_i / max(n_dates, 1) * 39)
                progress_cb(pct, 100, f"撮合 {today_ts.strftime('%Y-%m-%d')} ({day_i+1}/{n_dates})")

        if progress_cb:
            progress_cb(100, 100, "回测完成")

        self.trades        = trades
        self.equity_curve  = equity
        self.daily_signals = daily_signals
        self.errors        = errors
        return self._summarize()

    def _summarize(self) -> dict:
        trades   = self.trades
        equity   = self.equity_curve
        p        = self.p

        if not equity:
            return {"error": "回测无数据，请检查策略代码或日期范围"}

        eq_series = pd.Series([e['equity'] for e in equity])
        init_cap  = p.capital
        final_cap = eq_series.iloc[-1]
        total_ret = (final_cap - init_cap) / init_cap

        n_days  = len(equity)
        annual  = (1 + total_ret) ** (252 / max(n_days, 1)) - 1

        peak    = eq_series.cummax()
        dd      = (eq_series - peak) / peak
        max_dd  = dd.min()

        if len(eq_series) > 1:
            daily_rets = eq_series.pct_change().dropna()
            sharpe = (daily_rets.mean() - 0.025 / 252) / (daily_rets.std() + 1e-10) * (252 ** 0.5)
        else:
            sharpe = 0.0

        if trades:
            wins   = [t for t in trades if t['pnl'] > 0]
            losses = [t for t in trades if t['pnl'] < 0]
            win_rate    = len(wins) / len(trades)
            avg_win     = np.mean([t['ret_pct'] for t in wins])  if wins   else 0
            avg_loss    = np.mean([t['ret_pct'] for t in losses]) if losses else 0
            profit_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        else:
            win_rate = avg_win = avg_loss = profit_ratio = 0

        return {
            "init_capital":   init_cap,
            "final_capital":  round(final_cap, 2),
            "total_return":   round(total_ret * 100, 2),
            "annual_return":  round(annual * 100, 2),
            "max_drawdown":   round(max_dd * 100, 2),
            "sharpe":         round(sharpe, 2),
            "total_trades":   len(trades),
            "win_rate":       round(win_rate * 100, 2),
            "avg_win_pct":    round(avg_win, 2),
            "avg_loss_pct":   round(avg_loss, 2),
            "profit_ratio":   round(profit_ratio, 2),
            "equity_curve":   self.equity_curve,
            "trades":         trades,
            "daily_signals":  self.daily_signals,
            "daily_selected": self.daily_signals,  # 兼容UI层字段名
            "errors":         self.errors[:20],  # 只保留前20条错误
        }
