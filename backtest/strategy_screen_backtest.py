"""
strategy_screen_backtest.py —— 策略选股回测引擎
================================================
类似同花顺策略回测：
  1. 每个交易日用选股条件筛出候选股
  2. 按排序字段取 Top N
  3. 模拟开盘买入（或次日开盘买入）
  4. 按止盈 / 止损 / 最大持仓天数 / 止损反弹 退出
  5. 统计资金曲线、胜率、最大回撤等
"""

import pandas as pd
import numpy as np

def _f(v):
    """安全转 float，NaN→0"""
    try:
        return 0.0 if pd.isna(v) else float(v)
    except (ValueError, TypeError):
        return 0.0

def _i(v):
    """安全转 int，NaN→0"""
    return int(_f(v))
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn


# ──────────────────────────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────────────────────────

def load_all_daily(start_date: str, end_date: str, progress_cb=None) -> pd.DataFrame:
    """一次性加载回测区间内所有股票的日线数据（含基本指标）"""
    if progress_cb:
        progress_cb(0, 100, "🔄 正在加载数据...")
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT dp.code, dp.trade_date,
                   dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.pct_change, dp.turnover,
                   si.name, si.market,
                   dp.vol_score, dp.ma_score, dp.diverge_score,
                   dp.bottom_score, dp.whale_score, dp.fusion_score
            FROM daily_price dp
            LEFT JOIN stock_info si ON dp.code = si.code
            WHERE dp.trade_date BETWEEN ? AND ?
              AND dp.close IS NOT NULL AND dp.close > 0
              AND dp.code NOT LIKE '688%'
              AND dp.code NOT LIKE '300%'
              AND dp.code NOT LIKE '301%'
            ORDER BY dp.trade_date, dp.code
            """,
            conn,
            params=(start_date, end_date)
        )
    if progress_cb:
        progress_cb(10, 100, f"✅ 已加载 {len(df)} 条数据")
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    return df


def load_stock_meta() -> pd.DataFrame:
    """加载股票基本信息（市值/PB等需要用到时从此处扩展）"""
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT code, name, market FROM stock_info WHERE is_active=1",
            conn
        )


# ──────────────────────────────────────────────────────────────────────────────
# 技术指标计算
# ──────────────────────────────────────────────────────────────────────────────

def add_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """对单只股票的历史 DataFrame 计算技术指标（用于选股条件判断）"""
    g = g.sort_values('trade_date').copy()
    c = g['close']
    v = g['volume']

    # 均线（同时保存小写 ma5 和大写 MA5 两种别名）
    # 小写供内部逻辑使用，大写与 indicator_engine 注册的 key 一致
    # 生成的策略代码用 df['MA5']，必须有大写列名才能找到
    for n in [5, 10, 20, 30, 60, 120]:
        g[f'ma{n}'] = c.rolling(n).mean()
        g[f'MA{n}'] = g[f'ma{n}']   # 大写别名，供 _gen_helper 生成的代码使用

    # 成交量均量
    g['vol_ma5']  = v.rolling(5).mean()
    g['vol_ma10'] = v.rolling(10).mean()
    g['vol_ma20'] = v.rolling(20).mean()

    # 涨跌幅（回看 N 日收益率）
    for n in [1, 2, 3, 5, 10]:
        g[f'ret_{n}d'] = c.pct_change(n)

    # 振幅（当日）
    g['amplitude'] = (g['high'] - g['low']) / g['close'].shift(1)

    # 换手率已在 daily_price.turnover 中（如有）
    # 周成交量（5日累计量 / 5日均量）
    g['vol_ratio_5d'] = v.rolling(5).sum() / g['vol_ma5'] / 5

    # ── 新增排序辅助指标 ────────────────────────────────────────
    # 近5日成交总量（固定5日窗口）
    g['vol_sum_5d'] = v.rolling(5).sum()

    # 上周成交量 —— 按日历周（周一~周五的交易日合计）
    # 思路：用 isocalendar 拿到 (year, week)，每周一组求和，再合并回来
    iso = g['trade_date'].dt.isocalendar()
    g['_year'] = iso['year'].astype(int)
    g['_week'] = iso['week'].astype(int)

    weekly_vol = g.groupby(['_year', '_week'])['volume'].sum().reset_index()
    weekly_vol.columns = ['_year', '_week', '_week_vol']

    # 合并本周周量
    merged = g.merge(weekly_vol[['_year', '_week', '_week_vol']], on=['_year', '_week'], how='left')
    g['vol_curr_week'] = merged['_week_vol'].values

    # ── 上周周量（关键：正确映射！）───────────────────────────────
    # weekly_vol 的 key 是 (_year, _week)，值是该周总成交量
    # 对任意一天 (yr, wk)，其 vol_prev_week = weekly_vol[(yr, wk-1)]（上一完整周的量）
    # 不需要 merge+ffill，直接建字典查：merge 的 ffill 会因周初不完整而出错
    wv_dict = {(int(r['_year']), int(r['_week'])): float(r['_week_vol'])
               for _, r in weekly_vol.iterrows()}

    prev_vols = []
    for _, row in g.iterrows():
        yr, wk = int(row['_year']), int(row['_week'])
        pwk = wk - 1
        pyr = yr
        if pwk <= 0:
            pyr = yr - 1
            pwk = 53  # 跨年 fallback
        prev_vols.append(wv_dict.get((pyr, pwk)))
    g['vol_prev_week'] = prev_vols

    # 本周/上周量比（>1=本周放量，<1=缩量）
    g['vol_vs_prev_ratio'] = g['vol_curr_week'] / g['vol_prev_week'].replace(0, float('nan'))

    # 近5日量/上周量比（缩量选股用：值越小=相对近5日越缩量）
    g['vol_5d_vs_prev_ratio'] = g['vol_sum_5d'] / g['vol_prev_week'].replace(0, float('nan'))

    # 清理临时列
    for col in ['_year', '_week']:
        if col in g.columns:
            g.drop(columns=[col], inplace=True)
    # 成交额（已有 amount 列，直接透传）
    # ─────────────────────────────────────────────────────────────

    # ATR（14日）
    hl = g['high'] - g['low']
    hc = (g['high'] - c.shift(1)).abs()
    lc = (g['low']  - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    g['atr14'] = tr.rolling(14).mean()

    return g


# ──────────────────────────────────────────────────────────────────────────────
# 选股条件定义
# ──────────────────────────────────────────────────────────────────────────────

class ScreenCondition:
    """
    选股条件集合，每个方法接收一行（Series）并返回 bool。
    支持链式 AND 组合。
    """

    @staticmethod
    def close_above_ma(row, ma_n: int) -> bool:
        """收盘在 N 日线上方"""
        return row['close'] >= row.get(f'ma{ma_n}', float('nan'))

    @staticmethod
    def ma_above_ma(row, fast: int, slow: int) -> bool:
        """快线在慢线上方（多头排列）"""
        return row.get(f'ma{fast}', float('nan')) >= row.get(f'ma{slow}', float('nan'))

    @staticmethod
    def ret_range(row, n_days: int, lo: float, hi: float) -> bool:
        """N日涨跌幅在 [lo, hi] 范围内"""
        r = row.get(f'ret_{n_days}d', float('nan'))
        if pd.isna(r): return False
        return lo <= r <= hi

    @staticmethod
    def amplitude_lt(row, threshold: float) -> bool:
        """振幅 < threshold"""
        a = row.get('amplitude', float('nan'))
        if pd.isna(a): return False
        return a < threshold

    @staticmethod
    def turnover_range(row, lo: float, hi: float) -> bool:
        """换手率在 [lo, hi] 范围内"""
        t = row.get('turnover', float('nan'))
        if pd.isna(t): return False
        return lo <= t <= hi

    @staticmethod
    def vol_gt_ma(row, multiplier: float = 1.0, ma_n: int = 5) -> bool:
        """成交量 > N日均量 × multiplier"""
        v = row.get('volume', float('nan'))
        vm = row.get(f'vol_ma{ma_n}', float('nan'))
        if pd.isna(v) or pd.isna(vm) or vm == 0: return False
        return v > multiplier * vm

    @staticmethod
    def not_st(row) -> bool:
        """非ST股（名称不含ST）"""
        name = str(row.get('name', ''))
        return 'ST' not in name.upper()

    @staticmethod
    def market_filter(row, markets: list) -> bool:
        """市场过滤（'SH'/'SZ' 等）"""
        return row.get('market', '') in markets

    @staticmethod
    def fusion_score_gt(row, threshold: float) -> bool:
        """融合信号分 > threshold"""
        s = row.get('fusion_score', 0)
        return (s or 0) >= threshold

    @staticmethod
    def not_new_stock(row, min_history: int = 60) -> bool:
        """非次新股（需在外部判断上市天数，此处仅作占位）"""
        return True  # 实际由数据量保证


# ──────────────────────────────────────────────────────────────────────────────
# 回测参数
# ──────────────────────────────────────────────────────────────────────────────

class BacktestParams:
    def __init__(
        self,
        start_date: str          = "2025-01-01",
        end_date: str            = "2026-04-10",
        capital: float           = 100_000,
        hold_days: int           = 2,          # 最大持仓天数
        max_positions: int       = 1,          # 同时最多持有股票数
        buy_num_per_day: int     = 1,          # 每日最多买入数量
        stop_loss: float         = -0.05,      # 止损比例（负数）
        take_profit: float       = 0.05,       # 止盈比例（正数）
        trailing_stop: float     = 0.0,        # 跟踪止损回撤（0=禁用）
        buy_at_open: bool        = True,       # True=开盘价买入，False=收盘价买入
        sell_at_open: bool       = True,       # True=次日开盘价卖出（市价），False=收盘价
        # 选股条件（lambda/bool函数列表，每个接收一行 Series 返回 bool）
        screen_filters: list     = None,
        # 排序字段（用于候选股排序，默认融合分）
        sort_by: str             = "fusion_score",
        sort_ascending: bool     = False,
        # 非科创/创业板过滤
        exclude_kcb: bool        = True,
        exclude_cyb: bool        = True,
        exclude_st: bool         = True,
        # 市值过滤（亿元，None=不过滤）
        market_cap_max: Optional[float] = None,
        # 上市天数要求
        min_list_days: int       = 60,
        # 交易成本
        commission_rate: float   = 0.0003,   # 买入佣金率（万三）
        stamp_duty: float        = 0.001,    # 卖出印花税率（千一）
        min_commission: float    = 5.0,      # 最低佣金（元）
    ):
        self.start_date    = start_date
        self.end_date      = end_date
        self.capital       = capital
        self.hold_days     = hold_days
        self.max_positions = max_positions
        self.buy_num_per_day = buy_num_per_day
        self.stop_loss     = stop_loss
        self.take_profit   = take_profit
        self.trailing_stop = trailing_stop
        self.buy_at_open   = buy_at_open
        self.sell_at_open  = sell_at_open
        self.screen_filters = screen_filters or []
        self.sort_by       = sort_by
        self.sort_ascending = sort_ascending
        self.exclude_kcb   = exclude_kcb
        self.exclude_cyb   = exclude_cyb
        self.exclude_st    = exclude_st
        self.market_cap_max = market_cap_max
        self.min_list_days = min_list_days
        self.commission_rate = commission_rate
        self.stamp_duty = stamp_duty
        self.min_commission = min_commission


# ──────────────────────────────────────────────────────────────────────────────
# 主回测引擎
# ──────────────────────────────────────────────────────────────────────────────

class StrategyScreenBacktest:

    def __init__(self, params: BacktestParams):
        self.p = params
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []

    def _calc_buy_cost(self, shares: int, price: float) -> float:
        """计算买入总成本（含手续费）"""
        amount = shares * price
        comm = max(amount * self.p.commission_rate, self.p.min_commission)
        return amount + comm

    def _calc_sell_net(self, shares: int, price: float) -> float:
        """计算卖出净收入（扣除佣金+印花税）"""
        amount = shares * price
        comm = max(amount * self.p.commission_rate, self.p.min_commission)
        stamp = amount * self.p.stamp_duty
        return amount - comm - stamp

    # ── 股票过滤（板块/ST/市值等静态过滤）──
    def _static_filter(self, code: str, name: str) -> bool:
        name = name or ''
        if self.p.exclude_st and 'ST' in name.upper():
            return False
        if self.p.exclude_kcb and code.startswith('688'):
            return False
        if self.p.exclude_cyb and (code.startswith('300') or code.startswith('301')):
            return False
        return True

    # ── 对某交易日的候选股应用动态选股条件 ──
    def _screen(self, day_df: pd.DataFrame) -> pd.DataFrame:
        if day_df.empty:
            return day_df
        mask = pd.Series([True] * len(day_df), index=day_df.index)
        for fn in self.p.screen_filters:
            mask = mask & day_df.apply(fn, axis=1)
        result = day_df[mask].copy()

        # 截面排名（每日候选股池内的相对排名，1=最大，数值越小排名越靠前）
        for col, suffix in [
            ('volume',             'vol_rank'),
            ('amount',             'amount_rank'),
            ('vol_sum_5d',         'vol5d_rank'),
            ('vol_vs_prev_ratio',  'volprev_rank'),
        ]:
            if col in result.columns:
                result[suffix] = result[col].rank(ascending=False, method='min').fillna(0).astype(int)

        # 排序（优先使用排序参数，若参数为排名列则直接使用）
        sort_col = self.p.sort_by
        if sort_col in result.columns:
            result = result.sort_values(sort_col, ascending=self.p.sort_ascending)
        return result

    # ── 主回测循环 ──
    def run(self, progress_cb=None) -> dict:
        p = self.p
        # ── 加载数据 ──
        # 为了计算指标，需要多加载 60 天历史
        extra_start = (datetime.strptime(p.start_date, '%Y-%m-%d') - timedelta(days=90)).strftime('%Y-%m-%d')
        raw = load_all_daily(extra_start, p.end_date, progress_cb=progress_cb)
        if raw.empty:
            return {"error": "数据库无数据，请先同步"}

        # ── 按股票分组计算技术指标（用 pandas groupby 避免 O(n²) 扫描）──
        groups = {}
        for code, g in raw.groupby('code', sort=False):
            name = str(g['name'].iloc[0]) if not g.empty else ''
            if not self._static_filter(code, name):
                continue
            if len(g) < p.min_list_days:
                continue
            groups[code] = add_indicators(g)

        if progress_cb:
            progress_cb(20, 100, f"✅ 指标计算完成，共 {len(groups)} 只股票")

        # ── 获取交易日列表 ──
        start_dt = datetime.strptime(p.start_date, '%Y-%m-%d')
        end_dt   = datetime.strptime(p.end_date, '%Y-%m-%d')
        all_dates = sorted(raw['trade_date'].unique())
        trade_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d).to_pydatetime() <= end_dt]

        if not trade_dates:
            return {"error": f"回测区间 {p.start_date}~{p.end_date} 内无交易日数据"}

        # ── 预建日期索引：{code: {trade_date: row_dict}}，实现 O(1) 查询 ──
        code_date_idx = {}   # code -> {date -> row_dict}
        for code, g in groups.items():
            code_date_idx[code] = {
                row['trade_date']: row
                for row in g.to_dict('records')
            }

        # 预建全局日期集合（用于快速判断某日是否有数据）
        all_date_set = set(all_dates)

        # ── 状态 ──
        capital   = float(p.capital)
        positions: list[dict] = []   # 当前持仓
        trades    = []
        equity    = []
        daily_selected: list[dict] = []   # 每日选股结果
        n_trade_dates = len(trade_dates)

        # ── 逐日模拟 ──
        for day_i, today in enumerate(trade_dates):
            today_ts = pd.Timestamp(today)
            # ── Step1: 检查已有持仓是否需要卖出 ──
            # T+1 卖出逻辑：收盘时判断卖出条件 → 标记为次日执行
            # 当日卖出仅用于到期退出（持股天数自然到期，无可争议）
            still_holding = []
            for pos in positions:
                code    = pos['code']
                cd_idx  = code_date_idx.get(code)
                row     = cd_idx.get(today_ts) if cd_idx else None

                if row is None:
                    still_holding.append(pos)
                    continue

                # 用收盘价判断卖出条件（盘中无法预知收盘价，但这是最合理的近似）
                close_price = row['close']
                open_price  = row.get('open', close_price)
                if pd.isna(close_price) or close_price <= 0:
                    still_holding.append(pos)
                    continue

                entry    = pos['entry_price']
                entry_ts = pos.get('buy_date', today_ts)  # 买入日期（真实交易日）
                ret      = (close_price - entry) / entry

                if p.trailing_stop > 0:
                    pos['peak'] = max(pos.get('peak', entry), close_price)
                    trail_ret   = (close_price - pos['peak']) / pos['peak']
                else:
                    trail_ret = 0

                # T+1 检查：A股当日买入，次日及之后才能卖出
                t1_ready = (today_ts - entry_ts).days >= 1

                should_sell = False
                sell_reason = ''
                if t1_ready:
                    if ret <= p.stop_loss:
                        should_sell, sell_reason = True, '止损'
                    elif ret >= p.take_profit:
                        should_sell, sell_reason = True, '止盈'
                    elif p.trailing_stop > 0 and trail_ret <= -p.trailing_stop:
                        should_sell, sell_reason = True, '跟踪止损'
                if not should_sell and (today_ts - entry_ts).days >= p.hold_days:
                    should_sell, sell_reason = True, f'持股{p.hold_days}天到期'

                if should_sell:
                    # T+1 延迟执行：卖出标记在收盘时做出 → 次日开盘价执行
                    # 到期退出也用次日开盘价（保守但无未来数据泄露）
                    # 找次日数据
                    next_sell_ts = next_day_ts  # 下方 Step2 中会计算
                    # 先用 today 的 next_day_ts（需要提前计算）
                    # 由于 next_day_ts 还没算，用 all_dates 查找
                    cur_idx_local = next((i for i, d in enumerate(all_dates) if d == today_ts), None)
                    next_sell_day = all_dates[cur_idx_local + 1] if (cur_idx_local is not None and cur_idx_local + 1 < len(all_dates)) else None

                    if next_sell_day is not None:
                        next_row = cd_idx.get(next_sell_day) if cd_idx else None
                        if next_row is not None and not pd.isna(next_row.get('open')) and next_row.get('open', 0) > 0:
                            sell_price = next_row['open']
                            sell_date_actual = next_sell_day
                        else:
                            # 次日无数据（停牌），用当日收盘价 fallback
                            sell_price = close_price
                            sell_date_actual = today_ts
                    else:
                        # 已是最后一个交易日，无法次日执行，用收盘价
                        sell_price = close_price
                        sell_date_actual = today_ts

                    shares       = pos['shares']
                    net_proceeds = self._calc_sell_net(shares, sell_price)
                    buy_cost     = pos.get('cost', entry * shares)
                    pnl          = net_proceeds - buy_cost
                    capital     += net_proceeds
                    trades.append({
                        'buy_date':   pos['buy_date'].strftime('%Y-%m-%d'),
                        'sell_date':  pd.Timestamp(sell_date_actual).strftime('%Y-%m-%d'),
                        'code':       code,
                        'name':       pos['name'],
                        'entry_price':round(entry, 3),
                        'sell_price': round(sell_price, 3),
                        'shares':     shares,
                        'pnl':        round(pnl, 2),
                        'ret_pct':    round((pnl / buy_cost) * 100, 2) if buy_cost else 0,
                        'reason':     sell_reason,
                        'hold_days':  (pd.Timestamp(sell_date_actual) - entry_ts).days,
                    })
                else:
                    still_holding.append(pos)

            positions = still_holding

            # ── Step2: 选股（每天都要记录候选股，与能否买入无关）──
            # 用 date_idx 找次日（O(1) 替代列表遍历）
            cur_idx = next((i for i, d in enumerate(all_dates) if d == today_ts), None)
            next_day_ts = all_dates[cur_idx + 1] if (cur_idx is not None and cur_idx + 1 < len(all_dates)) else None

            # 用 held_codes set 做排除（已持仓股票不重复买）
            held_codes = {pos['code'] for pos in positions}

            # 一次性收集今日有数据的股票（O(n) 遍历，而非每日 O(T×n)）
            today_rows = []
            for code, cd_idx in code_date_idx.items():
                if code in held_codes:
                    continue
                row = cd_idx.get(today_ts)
                if row is not None:
                    today_rows.append(row)

            # ── 每天都记录选股结果（哪怕当天没有候选股也要记录空列表）──
            if today_rows:
                today_df = pd.DataFrame(today_rows)
                candidates = self._screen(today_df)

                # ── 记录今日所有候选股──
                cand_dicts = candidates.to_dict('records')
                for cand_row in cand_dicts:
                    code = cand_row.get('code', '')
                    c_idx = code_date_idx.get(code, {})

                    daily_selected.append({
                        'date':              today_ts.strftime('%Y-%m-%d'),
                        'code':              code,
                        'name':              cand_row.get('name', ''),
                        'close':             round(_f(cand_row.get('close')), 3),
                        'pct_change':        round(_f(cand_row.get('pct_change')), 2),
                        'turnover':          round(_f(cand_row.get('turnover')) * 100, 3),
                        'amplitude':         round(_f(cand_row.get('amplitude')) * 100, 2),
                        'volume':            _i(cand_row.get('volume')),
                        'vol_sum_5d':        _i(cand_row.get('vol_sum_5d')),
                        'vol_curr_week':    _i(cand_row.get('vol_curr_week')),
                        'vol_prev_week':    _i(cand_row.get('vol_prev_week')),
                        'vol_vs_prev_ratio': round(_f(cand_row.get('vol_vs_prev_ratio')), 2),
                        'vol_5d_vs_prev_ratio': round(_f(cand_row.get('vol_5d_vs_prev_ratio')), 3),
                        'fusion_score':      round(_f(cand_row.get('fusion_score')), 1),
                        'ret_5d':            round(_f(cand_row.get('ret_5d')) * 100, 2),
                        'sort_val':          round(_f(cand_row.get(self.p.sort_by)), 3),
                        'vol_rank':         _i(cand_row.get('vol_rank')),
                        'amount_rank':      _i(cand_row.get('amount_rank')),
                        'vol5d_rank':       _i(cand_row.get('vol5d_rank')),
                        'volprev_rank':     _i(cand_row.get('volprev_rank')),
                    })

                # ── 实际买入（只有当天还有仓位空位时才执行）──
                had_sell = (len(positions) != len(still_holding))
                can_buy = 0 if had_sell else p.max_positions - len(positions)
                if can_buy > 0:
                    n_buy = min(can_buy, p.buy_num_per_day, len(candidates))
                    for cand_row in candidates.head(n_buy).to_dict('records'):
                        code = cand_row['code']
                        c_idx = code_date_idx.get(code, {})

                        # 次日开盘买入→次日开盘价，收盘买入→次日收盘价（T+1：当天选股，次日执行）
                        buy_price = c_idx.get(next_day_ts, {}).get('open' if p.buy_at_open else 'close')
                        if buy_price is None or pd.isna(buy_price) or buy_price <= 0:
                            continue

                        buy_date = next_day_ts

                        per_pos = capital / max(can_buy, 1)
                        shares  = int(per_pos / buy_price / 100) * 100
                        if shares < 100: continue

                        cost = self._calc_buy_cost(shares, buy_price)
                        if cost > capital: continue
                        capital -= cost

                        positions.append({
                            'code':        code,
                            'name':        cand_row.get('name', ''),
                            'buy_date':    pd.Timestamp(buy_date),
                            'entry_price': buy_price,
                            'shares':      shares,
                            'hold_days':   0,  # 从0开始，T+1由日期差判断
                            'peak':        buy_price,
                            'cost':        cost,  # 含手续费的实际买入成本
                        })
            else:
                # 当天没有任何股票有数据（全部停牌），记录空候选
                daily_selected.append({
                    'date':   today_ts.strftime('%Y-%m-%d'),
                    'code':   '',
                    'name':   '(无候选)',
                    'close':  0, 'pct_change': 0, 'sort_val': 0,
                    'vol_rank': 0, 'amount_rank': 0,
                    'vol5d_rank': 0, 'volprev_rank': 0,
                    'fusion_score': 0, 'volume': 0,
                })

            # ── Step3: 记录资金曲线 ──
            pos_value = 0.0
            for pos in positions:
                cd_idx = code_date_idx.get(pos['code'])
                if not cd_idx:
                    continue
                row = cd_idx.get(today_ts)
                if row is None:
                    # 当日无数据（停牌/缺失）→ 用最近可用收盘价延续估值
                    for d in reversed(all_dates):
                        if d > today_ts:
                            continue
                        r = cd_idx.get(d)
                        if r and not pd.isna(r.get('close')) and r.get('close', 0) > 0:
                            row = r
                            break
                if row is not None:
                    pos_value += row['close'] * pos['shares']
            total_equity = capital + pos_value
            equity.append({
                'date':   today_ts.strftime('%Y-%m-%d'),
                'equity': round(total_equity, 2),
                'cash':   round(capital, 2),
                'pos_value': round(pos_value, 2),
            })

            if progress_cb and day_i % 10 == 0:
                progress_cb(20 + int(day_i / max(n_trade_dates, 1) * 75),
                             100, f"⏳ 回测进度 {today_ts.strftime('%Y-%m-%d')} ({day_i+1}/{n_trade_dates})")

        # ── 回测结束后，修正最终权益记录（若有未平仓持仓则用收盘价重新估值）──
        if trade_dates and positions:
            last_date_ts = pd.Timestamp(trade_dates[-1])
            # 检查是否已存在最后一天的记录
            last_eq = equity[-1] if equity else None
            # 用最后一天的收盘价估值未平仓持仓
            pos_value_final = 0.0
            for pos in positions:
                cd_idx = code_date_idx.get(pos['code'])
                if cd_idx:
                    row = cd_idx.get(last_date_ts)
                    if row is None:
                        # 取最后一条可用记录
                        for d in reversed(all_dates):
                            r = cd_idx.get(d)
                            if r and r.get('close') and r['close'] > 0:
                                row = r
                                break
                    if row:
                        pos_value_final += row['close'] * pos['shares']
            final_equity = capital + pos_value_final

            if last_eq and last_eq['date'] == last_date_ts.strftime('%Y-%m-%d'):
                # 已有当天记录 → 更新它（覆盖循环中用 open 价算的不准估值）
                last_eq['equity']    = round(final_equity, 2)
                last_eq['cash']      = round(capital, 2)
                last_eq['pos_value'] = round(pos_value_final, 2)
            else:
                # 无当天记录（异常情况）→ 追加
                equity.append({
                    'date':       last_date_ts.strftime('%Y-%m-%d'),
                    'equity':     round(final_equity, 2),
                    'cash':       round(capital, 2),
                    'pos_value':  round(pos_value_final, 2),
                })

        self.trades       = trades
        self.equity_curve = equity
        self.daily_selected = daily_selected
        self.final_positions = positions  # 最终未平仓持仓（用于UI展示）
        return self._summarize()

    # ── 统计汇总 ──
    def _summarize(self) -> dict:
        trades    = self.trades
        equity    = self.equity_curve
        p         = self.p

        if not equity:
            return {"error": "回测无数据"}

        eq_series = pd.Series([e['equity'] for e in equity])
        init_cap  = p.capital
        final_cap = eq_series.iloc[-1]
        total_ret = (final_cap - init_cap) / init_cap

        # 年化收益：total_ret 是小数 (10.74=1074%)，标准公式 (1+r)^(252/n) - 1
        n_days   = len(equity)
        annual   = (1 + total_ret) ** (252 / max(n_days, 1)) - 1

        # 最大回撤
        peak     = eq_series.cummax()
        drawdown = (eq_series - peak) / peak
        max_dd   = drawdown.min()

        # Sharpe（简化，假设无风险利率 2.5%）
        if len(eq_series) > 1:
            daily_rets = eq_series.pct_change().dropna()
            sharpe = (daily_rets.mean() - 0.025 / 252) / (daily_rets.std() + 1e-10) * (252 ** 0.5)
        else:
            sharpe = 0.0

        # 胜率、盈亏比
        if trades:
            wins   = [t for t in trades if t['pnl'] > 0]
            losses = [t for t in trades if t['pnl'] < 0]
            win_rate   = len(wins) / len(trades)
            avg_win    = np.mean([t['ret_pct'] for t in wins])  if wins   else 0
            avg_loss   = np.mean([t['ret_pct'] for t in losses]) if losses else 0
            profit_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        else:
            win_rate = avg_win = avg_loss = profit_ratio = 0

        # 序列化最终持仓（供UI展示）
        final_pos_list = []
        for pos in self.final_positions:
            final_pos_list.append({
                "code":        pos.get("code", ""),
                "name":        pos.get("name", ""),
                "buy_date":    str(pos.get("buy_date", "")),
                "entry_price": round(pos.get("entry_price", 0), 3),
                "shares":      int(pos.get("shares", 0)),
            })

        return {
            "init_capital":     init_cap,
            "final_capital":    round(final_cap, 2),
            "total_return":     round((final_cap - init_cap) / init_cap * 100, 2),
            "annual_return":    round(annual * 100, 2),
            "max_drawdown":     round(abs(max_dd) * 100, 2),  # 转为正数显示，如 11.2
            "sharpe":           round(sharpe, 2),
            "total_trades":     len(trades),
            "win_rate":         round(win_rate * 100, 2),
            "avg_win_pct":      round(avg_win, 2),
            "avg_loss_pct":     round(avg_loss, 2),
            "profit_ratio":     round(profit_ratio, 2),
            "equity_curve":     self.equity_curve,
            "trades":           trades,
            "daily_selected":   self.daily_selected,
            "final_positions":  final_pos_list,
            "sort_by":          self.p.sort_by,
            "sort_ascending":   self.p.sort_ascending,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 预设策略模板（对应同花顺类似的条件）
# ──────────────────────────────────────────────────────────────────────────────

def make_preset_strategy(name: str) -> tuple[list, str, bool]:
    """
    返回 (screen_filters, sort_by, sort_ascending)
    可在 UI 下拉框中选择
    """
    if name == "5日线突破（3天内收在5日线上）":
        filters = [
            lambda row: row.get('close', 0) > row.get('ma5', float('nan')),
            lambda row: row.get('ma5', float('nan')) > row.get('ma10', float('nan')),
            lambda row: row.get('ret_2d', float('nan')) is not None
                        and abs(row.get('ret_2d', 0)) < 0.05,
            lambda row: (row.get('amplitude', 1) or 1) < 0.08,
            lambda row: 0.01 <= (row.get('turnover', 0) or 0) <= 0.06,
        ]
        return filters, "vol_ratio_5d", False

    elif name == "均线多头 + 放量":
        filters = [
            lambda row: row.get('close', 0) > row.get('ma5', float('nan')),
            lambda row: row.get('ma5', float('nan')) > row.get('ma10', float('nan')),
            lambda row: row.get('ma10', float('nan')) > row.get('ma20', float('nan')),
            lambda row: row.get('volume', 0) > 1.5 * (row.get('vol_ma5', 0) or 1),
        ]
        return filters, "fusion_score", False

    elif name == "超跌反弹（5日跌幅 > 8%）":
        filters = [
            lambda row: (row.get('ret_5d', 0) or 0) < -0.08,
            lambda row: row.get('close', 0) > row.get('low', 0),
            lambda row: (row.get('turnover', 0) or 0) > 0.01,
            lambda row: (row.get('amplitude', 1) or 1) < 0.10,
        ]
        return filters, "ret_5d", True  # 跌最多的排前面

    elif name == "量价背离（量减价稳）":
        filters = [
            lambda row: abs(row.get('ret_2d', 0) or 0) < 0.03,
            lambda row: (row.get('vol_ratio_5d', 1) or 1) < 0.8,
            lambda row: row.get('close', 0) > row.get('ma20', float('nan')),
        ]
        return filters, "fusion_score", False

    elif name == "融合信号高分选股":
        filters = [
            lambda row: (row.get('fusion_score', 0) or 0) >= 15,
        ]
        return filters, "fusion_score", False

    else:
        return [], "fusion_score", False
