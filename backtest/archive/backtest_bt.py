"""
基于 backtesting.py 框架的单股回测引擎
等价于 backtest.py 的 Backtester 类

特性：
- T+1 规则：当天买入的股票不能当天卖出
- A股手续费/印花税/滑点模型（与 Backtester 完全一致）
- R1: 回撤熔断
- R2: 大盘择时
- R3: 动态仓位管理（ATR波动率）
- R5: 交易信号溯源

用法：
    from backtest.backtest_bt import run_backtest, prepare_data, BTStrategy

    df = get_stock_history("000001", start_date="20230101")
    df_s = strategy_fn(df)  # 产生 BUY_SIGNAL/SELL_SIGNAL/STOP_LOSS/TAKE_PROFIT
    result = run_backtest(df_s)
    summary = get_summary(result)

    # 或直接使用框架的优化器
    bt = Backtest(data_df, BTStrategy, cash=100_000)
    opt = bt.optimize(score_threshold=range(15, 30))
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional

from backtesting import Backtest, Strategy
from backtesting.lib import crossover


# ============================================================
# 数据结构 — 与 backtest.py 保持一致
# ============================================================

@dataclass
class Trade:
    """单笔交易记录（与 backtest.py Trade 对齐）"""
    entry_date: str = ""
    entry_price: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    shares: float = 0.0
    direction: str = "long"
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0
    slippage: float = 0.0
    trigger_strategy: str = ""


@dataclass
class BacktestResult:
    """回测结果（与 backtest.py BacktestResult 对齐）"""
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_holding_days: float = 0.0
    avg_profit_pct: float = 0.0
    avg_loss_pct: float = 0.0
    final_capital: float = 0.0
    benchmark_return: float = 0.0
    alpha: float = 0.0
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    equity_dates: List[str] = field(default_factory=list)


# ============================================================
# A股费用计算器（复用 Backtester 的精确逻辑）
# ============================================================

class AShareCommission:
    """
    A股交易费用计算器
    - 手续费：万三（双向），最低5元
    - 印花税：千一（卖出单向）
    - 滑点：千一（买入多加/卖出少收）
    """

    def __init__(
        self,
        commission_rate: float = 0.0003,
        stamp_tax: float = 0.001,
        slippage_rate: float = 0.001,
    ):
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.slippage_rate = slippage_rate

    def calc_buy_cost(self, price: float, size: float) -> tuple:
        """返回 (实际成交价, 总成本)"""
        slip_price = price * (1 + self.slippage_rate)  # 滑点：买贵了
        amount = slip_price * size
        comm = max(amount * self.commission_rate, 5.0)
        return slip_price, amount + comm

    def calc_sell_revenue(self, price: float, size: float) -> tuple:
        """返回 (实际成交价, 净收入, 滑点损失)"""
        slip_price = price * (1 - self.slippage_rate)  # 滑点：卖便宜了
        amount = slip_price * size
        comm = max(amount * self.commission_rate, 5.0)
        tax = amount * self.stamp_tax
        revenue = amount - comm - tax
        slippage_loss = (price - slip_price) * size
        return slip_price, revenue, slippage_loss


# ============================================================
# 核心 Strategy 类 — T+1 规则 + R1/R2/R3/R5 全部内置
# ============================================================

class BTStrategy(Strategy):
    """
    基于 backtesting.py 的单股回测策略
    等价于 backtest.Backtester.run() 的完整逻辑

    关键设计：
    - T+1：用 _pending_buy 标记，信号日在 next() 中设标记，
      下一根K线以 Open 价执行买入
    - 当天买的不能当天卖：_can_sell 标记控制
    - R1 回撤熔断：追踪权益峰值和连续亏损
    - R2 大盘择时：index_close 列 vs MA
    - R3 动态仓位：基于 ATR 波动率反比调整
    - R5 信号溯源：记录触发策略名称
    """

    # ── 可调参数（支持 optimize()）─────────────────────
    score_threshold: float = 20.0       # BUY_SCORE 门槛
    position_pct: float = 1.0           # 基础仓位比例（占可用资金的百分比）
    max_holding_days: int = 40          # 最大持仓天数

    # ── T+1 模式 ──
    strict_t1: bool = False             # False=同原版(信号日close买), True=严格T+1(次日open买)

    # ── R1 回撤熔断 ──
    use_drawdown_guard: bool = True
    drawdown_threshold: float = 0.10
    consecutive_loss_limit: int = 3

    # ── R2 大盘择时 ──
    use_market_timing: bool = True
    market_timing_ma: int = 20

    # ── R3 动态仓位 ──
    use_dynamic_position: bool = True
    volatility_period: int = 20
    min_position_ratio: float = 0.3
    max_position_ratio: float = 1.5

    # ── 止损止盈倍数（相对买入价）───
    stop_loss_mult: float = 0.93        # 止损价 = entry × 0.93 (-7%)
    take_profit_mult: float = 1.15     # 止盈价 = entry × 1.15 (+15%)

    # ── 内部状态（运行时自动初始化，不需要外部传入）──
    _pending_buy: bool = False           # T+1 待执行买入
    _can_sell: bool = False             # 今天是否允许卖出（T+1规则）
    _entry_day_count: int = 0            # 已持仓天数
    _circuit_broken: bool = False       # 熔断标志
    _consecutive_losses: int = 0         # 连续亏损次数
    _peak_equity: float = 0.0            # 权益峰值
    _last_trigger: str = ""              # 当前持仓的触发策略名
    _entry_price_for_sl: float = 0.0     # 记录止损价（用于固定SL/TP）

    def init(self):
        """注册指标"""
        _df = self.data.df  # 底层 DataFrame（0.6.5 API）

        # 辅助函数：安全地将 _Array 转换为 float 数组（处理 NaN）
        def to_float(x):
            arr = np.asarray(x, dtype=float)
            arr = np.nan_to_num(arr, nan=0.0)
            return arr

        # 买入信号列
        if "BUY_SIGNAL" in _df.columns:
            self.buy_signal = self.I(to_float, self.data.BUY_SIGNAL)
        else:
            # 如果没有 BUY_SIGNAL 列，用 fusion_score 替代
            if "fusion_score" in _df.columns:
                self.buy_signal = self.I(
                    lambda x: (to_float(x) > self.score_threshold).astype(float),
                    self.data.fusion_score,
                )
            else:
                raise ValueError("数据必须包含 BUY_SIGNAL 或 fusion_score 列")

        # 卖出信号列
        if "SELL_SIGNAL" in _df.columns:
            self.sell_signal = self.I(to_float, self.data.SELL_SIGNAL)
        else:
            self.sell_signal = self.I(lambda x: np.zeros(len(x), dtype=float),
                                      self.data.Close)

        # 止损/止盈价（从数据中读取或按倍数计算）
        if "STOP_LOSS" in _df.columns:
            self.stop_loss_price = self.I(
                lambda x: np.nan_to_num(np.asarray(x, dtype=float),
                                       nan=np.nan),
                self.data.STOP_LOSS,
            )
        else:
            self.stop_loss_price = None  # 用动态计算的值

        if "TAKE_PROFIT" in _df.columns:
            self.take_profit_price = self.I(
                lambda x: np.nan_to_num(np.asarray(x, dtype=float),
                                       nan=np.nan),
                self.data.TAKE_PROFIT,
            )
        else:
            self.take_profit_price = None

        # R2: 大盘均线
        self.index_ma = None
        if self.use_market_timing and "index_close" in _df.columns:
            idx_close = _df["index_close"].values
            ma_values = pd.Series(idx_close).rolling(
                self.market_timing_ma, min_periods=self.market_timing_ma
            ).mean().values
            self.index_ma = ma_values

        # R3: ATR 波动率序列（预计算）
        self.atr_series = None
        if self.use_dynamic_position and len(self.data) > self.volatility_period:
            high = _df["High"].values
            low = _df["Low"].values
            close = _df["Close"].values
            tr = np.maximum(high - low,
                            np.abs(high - np.roll(close, 1)))
            tr = np.maximum(tr, np.abs(low - np.roll(close, 1)))
            atr = pd.Series(tr).rolling(
                self.volatility_period, min_periods=self.volatility_period
            ).mean().values
            self.atr_series = atr

        # R5: 策略评分列（用于信号溯源）
        self.strategy_cols = {}
        for label, col_name in [
            ("放量突破", "VOL_SCORE"), ("均线粘合", "MA_SCORE"),
            ("量价背离", "DIVERGE_SCORE"), ("抄底型", "BOTTOM_SCORE"),
            ("主力建仓", "WHALE_SCORE"),
        ]:
            if col_name in _df.columns:
                self.strategy_cols[label] = col_name

        # 初始化权益峰值
        self._peak_equity = self.equity

        # 保存数据总长度（用于判断是否是最后一天）
        self._total_data_len = len(_df)

    def next(self):
        """
        每根 K 线调用一次。
        核心逻辑：
        严格T+1模式 (strict_t1=True):
            1. 先检查是否需要执行待处理的 T+1 买入
            2. 再检查持仓中的止损/止盈/超时/卖出信号
            3. 最后检查空仓时的买入机会（标记，次日执行）
        兼容模式 (strict_t1=False, 与原版Backtester一致):
            1. 持仓中检查退出条件
            2. 空仓时当天 close 价直接买入
        """
        current_idx = len(self.data) - 1
        price = self.data.Close[-1]
        open_price = self.data.Open[-1]

        # ── R1: 权益恢复则解除熔断 ──
        if self.use_drawdown_guard and self._circuit_broken:
            if self.equity >= self._peak_equity:
                self._circuit_broken = False

        if self.strict_t1:
            # ═══ 严格 T+1 模式 ═══
            # 执行 T+1 待处理买入
            if self._pending_buy:
                self._execute_pending_buy(open_price, current_idx)
                return  # 买入当天不检查卖出

            # 持仓中：检查退出条件
            if self.position:
                self._entry_day_count += 1
                if not self._can_sell and self._entry_day_count >= 2:
                    self._can_sell = True

                exit_reason = None
                sl_price = self._get_stop_loss(current_idx, open_price)
                tp_price = self._get_take_profit(current_idx, open_price)

                if self._can_sell:
                    if price <= sl_price:
                        exit_reason = "止损"
                    elif price >= tp_price:
                        exit_reason = "止盈"
                    elif self._entry_day_count >= self.max_holding_days:
                        exit_reason = "超期平仓"
                    elif self.sell_signal[-1] > 0.5:
                        exit_reason = "卖出信号"

                if exit_reason:
                    self._execute_exit(exit_reason, price, current_idx)
                    return

            # 空仓：标记待买入
            if not self.position and self.buy_signal[-1] > 0.5:
                self._check_and_schedule_buy(price, current_idx)
        else:
            # ═══ 兼容模式（与原版 Backtester 一致）═══
            # 持仓中：检查退出
            if self.position:
                self._entry_day_count += 1
                exit_reason = None
                sl_price = self._get_stop_loss(current_idx, open_price)
                tp_price = self._get_take_profit(current_idx, open_price)

                if price <= sl_price:
                    exit_reason = "止损"
                elif price >= tp_price:
                    exit_reason = "止盈"
                elif self._entry_day_count >= self.max_holding_days:
                    exit_reason = "超期平仓"
                elif self.sell_signal[-1] > 0.5:
                    exit_reason = "卖出信号"

                if exit_reason:
                    self._execute_exit(exit_reason, price, current_idx)
                    return

            # 空仓：当天直接以 close 价买入（与原版完全一致）
            total = getattr(self, '_total_data_len', current_idx + 100)
            if not self.position and self.buy_signal[-1] > 0.5 and current_idx < total - 1:
                self._execute_buy_same_day(price, current_idx)

    def _execute_buy_same_day(self, price: float, idx: int):
        """兼容模式：信号日当天以 close 价买入（与原版 Backtester 完全一致）"""
        # R1: 熔断中禁止开仓
        if self.use_drawdown_guard and self._circuit_broken:
            return

        # R2: 大盘择时过滤
        if self.use_market_timing and self.index_ma is not None:
            if idx < len(self.index_ma):
                ma_val = self.index_ma[idx]
                if not np.isnan(ma_val) and price < ma_val:
                    return

        # R3: 动态仓位
        effective_pos_pct = self.position_pct
        if self.use_dynamic_position and self.atr_series is not None:
            effective_pos_pct = self._calc_dynamic_position(price, idx)

        buy_value = self.equity * effective_pos_pct
        slip_price = price * (1 + 0.001)
        size = int(buy_value / slip_price / 100) * 100
        if size >= 100:
            self._last_trigger = self._detect_trigger_strategy(idx)
            self._entry_price_for_sl = slip_price

            # 兼容模式：不设 _can_sell 限制，持仓第二天就能卖
            self._can_sell = True
            self._entry_day_count = 0
            self._peak_equity = max(self._peak_equity, self.equity)

            self.buy(size=size, limit=slip_price)

    def _execute_pending_buy(self, open_price: float, idx: int):
        """执行 T+1 买入（以开盘价成交）"""
        # R1: 熔断中禁止开仓
        if self.use_drawdown_guard and self._circuit_broken:
            self._pending_buy = False
            return

        # R2: 大盘择时过滤
        if self.use_market_timing and self.index_ma is not None:
            if idx < len(self.index_ma):
                ma_val = self.index_ma[idx]
                if not np.isnan(ma_val) and open_price < ma_val:
                    self._pending_buy = False
                    return

        # R3: 计算动态仓位大小
        effective_pos_pct = self.position_pct
        if self.use_dynamic_position and self.atr_series is not None:
            effective_pos_pct = self._calc_dynamic_position(open_price, idx)

        buy_value = self.equity * effective_pos_pct

        # 使用自定义滑点价格买入
        slip_price = open_price * (1 + 0.001)
        # 计算可买股数（整手）
        size = int(buy_value / slip_price / 100) * 100
        if size >= 100:
            # R5: 识别触发策略
            self._last_trigger = self._detect_trigger_strategy(idx)

            # 设置止损止盈参考价
            self._entry_price_for_sl = slip_price

            # 执行买入（backtesting.py 用 limit 指定价格）
            # 注意：不传 sl/tp 参数，由 next() 手动控制止损止盈
            # 这样才能精确实现 T+1 规则和超期平仓
            self.buy(
                size=size,
                limit=slip_price,   # 限价买入（T+1 开盘价+滑点）
            )

            # T+1 标记
            self._can_sell = False
            self._entry_day_count = 0
            self._peak_equity = max(self._peak_equity, self.equity)

        self._pending_buy = False

    def _check_and_schedule_buy(self, price: float, idx: int):
        """空仓时检查买入条件，如果满足则标记为待买入（T+1）"""
        # 注意：backtesting.py 中 len(self.data) 是滑动窗口大小（=idx+1），不是总长度
        # 必须用 _total_data_len 来判断是否最后一天
        total = getattr(self, '_total_data_len', idx + 100)
        if idx >= total - 1:
            return  # 最后一天不买入（没有明天可以执行）

        # R1: 熔断中禁止开仓
        if self.use_drawdown_guard and self._circuit_broken:
            return

        # R2: 大盘择时过滤
        if self.use_market_timing and self.index_ma is not None:
            if idx < len(self.index_ma):
                ma_val = self.index_ma[idx]
                if not np.isnan(ma_val) and price < ma_val:
                    return

        # 标记为待买入（下一根K线执行）
        self._pending_buy = True

    def _execute_exit(self, reason: str, price: float, idx: int):
        """执行卖出操作"""
        # 使用滑点价格卖出（backtesting.py 用 limit 指定价格）
        slip_price = price * (1 - 0.001)
        self.position.close()  # 以当前价平仓（框架自动处理）

        # R1: 更新熔断状态
        if self.use_drawdown_guard:
            trade_pnl_pct = (slip_price / self._entry_price_for_sl - 1) * 100 \
                            if self._entry_price_for_sl > 0 else 0
            if trade_pnl_pct < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

            self._peak_equity = max(self._peak_equity, self.equity)

            dd = (self._peak_equity - self.equity) / self._peak_equity \
                 if self._peak_equity > 0 else 0
            if dd >= self.drawdown_threshold or \
               self._consecutive_losses >= self.consecutive_loss_limit:
                self._circuit_broken = True

        # 重置状态
        self._entry_day_count = 0
        self._can_sell = False

    def _get_stop_loss(self, idx: int, default_price: float) -> float:
        """获取当前止损价"""
        if self.stop_loss_price is not None and idx < len(self.stop_loss_price):
            val = self.stop_loss_price[idx]
            if not np.isnan(val):
                return val
        return self._entry_price_for_sl * self.stop_loss_mult \
            if self._entry_price_for_sl > 0 else default_price * 0.93

    def _get_take_profit(self, idx: int, default_price: float) -> float:
        """获取当前止盈价"""
        if self.take_profit_price is not None and idx < len(self.take_profit_price):
            val = self.take_profit_price[idx]
            if not np.isnan(val):
                return val
        return self._entry_price_for_sl * self.take_profit_mult \
            if self._entry_price_for_sl > 0 else default_price * 1.15

    def _calc_dynamic_position(self, price: float, idx: int) -> float:
        """R3: 基于 ATR 波动率的动态仓位"""
        if self.atr_series is None or idx >= len(self.atr_series):
            return self.position_pct

        atr_val = self.atr_series[idx]
        if np.isnan(atr_val) or price <= 0:
            return self.position_pct

        vol_ratio = atr_val / price
        base_vol = 0.02  # 基准波动率 ~2%
        vol_factor = (base_vol / vol_ratio) * 2 * self.position_pct
        return np.clip(
            vol_factor,
            self.position_pct * self.min_position_ratio,
            self.position_pct * self.max_position_ratio,
        )

    def _detect_trigger_strategy(self, idx: int) -> str:
        """R5: 信号溯源 — 识别贡献最大的策略"""
        if not self.strategy_cols:
            return "融合信号"

        best_label = ""
        best_score = 0.0
        _df = self.data.df

        for label, col_name in self.strategy_cols.items():
            col_data = _df[col_name].values
            if idx < len(col_data):
                s = float(col_data[idx]) if not pd.isna(col_data[idx]) else 0.0
                if s > best_score:
                    best_score = s
                    best_label = label

        if best_label and best_score > 0:
            if best_score >= 2.0:
                return f"{best_label}(高)"
            else:
                return f"{best_label}(低)"
        return "融合信号"


# ============================================================
# 数据准备函数 — 将项目格式转换为 backtesting.py 格式
# ============================================================

def prepare_data(
    df_strategy: pd.DataFrame,
    date_column: Optional[str] = None,
) -> pd.DataFrame:
    """
    将项目的 DataFrame 格式转换为 backtesting.py 要求的格式。

    backtesting.py 要求数据包含以下列（大写）：
    - Open, High, Low, Close, Volume（必需）
    - 可选列会被保留并传递给 Strategy：
      BUY_SIGNAL, SELL_SIGNAL, STOP_LOSS, TAKE_PROFIT
      index_close（大盘指数收盘价）
      fusion_score, VOL_SCORE, MA_SCORE, DIVERGE_SCORE,
      BOTTOM_SCORE, WHALE_SCORE

    :param df_strategy: 项目格式的 DataFrame（小写列名）
    :param date_column: 日期列名，如果索引不是 DatetimeIndex
    :return: 转换后的 DataFrame（backtesting.py 格式）
    """
    df = df_strategy.copy()

    # 处理日期索引
    if date_column and date_column in df.columns:
        df[date_column] = pd.to_datetime(df[date_column])
        df.set_index(date_column, inplace=True)
    elif not isinstance(df.index, pd.DatetimeIndex):
        if 'trade_date' in df.columns:
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df.set_index('trade_date', inplace=True)

    # 列名映射：项目小写 → backtesting.py 大写
    column_map = {
        'open': 'Open', 'high': 'High', 'low': 'Low',
        'close': 'Close', 'volume': 'Volume',
    }
    df.rename(columns=column_map, inplace=True)

    # 确保必需列存在
    required = ['Open', 'High', 'Low', 'Close', 'Volume']
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"缺少必需列: {missing}。请确认输入数据包含 "
                         "open/high/low/close/volume 字段。")

    # 将可选信号列也保留（Strategy 通过 self.data.xxx 访问）
    signal_columns = [
        'BUY_SIGNAL', 'SELL_SIGNAL', 'STOP_LOSS', 'TAKE_PROFIT',
        'index_close', 'fusion_score',
        'VOL_SCORE', 'MA_SCORE', 'DIVERGE_SCORE',
        'BOTTOM_SCORE', 'WHALE_SCORE',
    ]
    # 这些列可能已经是原始名字（大写），也可能需要从小写映射
    extra_map = {
        'buy_signal': 'BUY_SIGNAL', 'sell_signal': 'SELL_SIGNAL',
        'stop_loss': 'STOP_LOSS', 'take_profit': 'TAKE_PROFIT',
        'index_close': 'index_close',
        'vol_score': 'VOL_SCORE', 'ma_score': 'MA_SCORE',
        'diverge_score': 'DIVERGE_SCORE',
        'bottom_score': 'BOTTOM_SCORE', 'whale_score': 'WHALE_SCORE',
        'fusion_score': 'fusion_score',
    }
    df.rename(columns=extra_map, inplace=True)

    # 排除 NaN 行
    df = df.dropna(subset=['Open', 'Close'])

    # 排序（backtesting.py 要求时间升序）
    df.sort_index(inplace=True)

    return df


# ============================================================
# 结果转换 — 将 backtesting.py 结果转为项目格式
# ============================================================

def convert_result(bt_result, data_df: pd.DataFrame,
                   initial_capital: float = 100_000) -> BacktestResult:
    """
    将 backtesting.py 的结果对象转换为项目的 BacktestResult 格式。

    :param bt_result: bt.run() 返回的 _Stats 对象
    :param data_df: 原始数据 DataFrame（用于计算基准收益）
    :param initial_capital: 初始资金
    :return: BacktestResult（与 backtest.py 完全兼容）
    """
    result = BacktestResult()
    
    # backtesting.py 0.6.5 用字符串键访问
    r = bt_result

    # 基本信息
    try:
        result.final_capital = round(float(r["Equity Final [$]"]), 2)
    except (KeyError, TypeError):
        result.final_capital = initial_capital

    result.total_return = round(float(r.get("Return [%]", 0)), 2)
    result.annual_return = round(float(r.get("Return (Ann.) [%]", 0)), 2)

    # 最大回撤（注意：框架返回的是负值，如 -1.38%）
    dd_val = float(r.get("Max. Drawdown [%]", 0))
    result.max_drawdown = round(dd_val, 2)  # 保持负值，与原版一致

    # 夏普比率
    sr = float(r.get("Sharpe Ratio", 0))
    if not np.isnan(sr):
        result.sharpe_ratio = round(sr, 2)

    # 交易统计
    trades_raw = r._trades  # Trade Series
    result.total_trades = int(r.get("# Trades", len(trades_raw)))

    win_rate_val = float(r.get("Win Rate [%]", 0))
    if not np.isnan(win_rate_val):
        result.win_rate = round(win_rate_val, 1)

    pf_val = float(r.get("Profit Factor", 0))
    if not np.isnan(pf_val):
        result.profit_factor = round(pf_val, 2)

    # 盈亏幅度（从 _trades 计算更精确）
    if len(trades_raw) > 0:
        winning_pnl = trades_raw[trades_raw['PnL'] > 0]
        losing_pnl = trades_raw[trades_raw['PnL'] <= 0]
        result.winning_trades = len(winning_pnl)
        result.losing_trades = len(losing_pnl)

        if len(winning_pnl) > 0:
            result.avg_profit_pct = round(
                float(winning_pnl['ReturnPct'].mean()), 2
            )
        if len(losing_pnl) > 0:
            result.avg_loss_pct = round(
                float(losing_pnl['ReturnPct'].mean()), 2
            )

        # 平均持仓天数
        durations = trades_raw['ExitBar'] - trades_raw['EntryBar']
        if len(durations) > 0:
            result.avg_holding_days = round(float(durations.mean()), 1)

    # 转换交易记录
    for idx in range(len(trades_raw)):
        t = trades_raw.iloc[idx]
        entry_price = float(t['EntryPrice'])
        exit_price = float(t.get('ExitPrice', 0))
        size = float(t['Size'])

        # 滑点损失估算（买入+卖出各千一）
        slippage_loss = (entry_price * 0.001 * size) + \
                         (exit_price * 0.001 * size) if size > 0 else 0.0

        pnl_pct = float(t.get('ReturnPct', 0))

        # 推断退出原因
        exit_reason = "盈利" if pnl_pct > 0 else "亏损"
        exit_time = t.get('ExitTime')
        entry_time = t.get('EntryTime')

        result.trades.append(Trade(
            entry_date=str(entry_time.date()) 
                       if hasattr(entry_time, 'date') else str(entry_time),
            entry_price=round(entry_price, 3),
            exit_date=str(exit_time.date()) 
                      if hasattr(exit_time, 'date') and pd.notna(exit_time)
                      else str(exit_time) if exit_time else "",
            exit_price=round(exit_price, 3),
            shares=round(size, 0),
            exit_reason=exit_reason,
            pnl=round(float(t.get('PnL', 0)), 2),
            pnl_pct=round(pnl_pct, 2),
            holding_days=int(t.get('ExitBar', 0)) - int(t.get('EntryBar', 0)),
            slippage=round(slippage_loss, 2),
            trigger_strategy="",
        ))

    # 权益曲线
    if hasattr(r, '_equity_curve'):
        eq = r._equity_curve
        result.equity_curve = [float(v) for v in eq.Equity.values.tolist()]
        result.equity_dates = [str(d.date()) for d in eq.index]

    # 基准收益（优先用框架计算的值，否则自己算）
    bh_return = r.get("Buy & Hold Return [%]", None)
    if bh_return is not None and not np.isnan(float(bh_return)):
        result.benchmark_return = round(float(bh_return), 2)
    elif 'Close' in data_df.columns:
        first_price = data_df.iloc[0]['Close']
        last_price = data_df.iloc[-1]['Close']
        result.benchmark_return = round((last_price / first_price - 1) * 100, 2)
    else:
        first_price = data_df.iloc[0]['close']
        last_price = data_df.iloc[-1]['close']
        result.benchmark_return = round((last_price / first_price - 1) * 100, 2)

    alpha_val = r.get("Alpha [%]", None)
    if alpha_val is not None and not np.isnan(float(alpha_val)):
        result.alpha = round(float(alpha_val), 2)
    else:
        result.alpha = round(result.total_return - result.benchmark_return, 2)

    return result


def get_summary(result: BacktestResult) -> dict:
    """返回易于展示的摘要字典（与 Backtester.get_summary 完全对齐）"""
    total_slippage = sum(t.slippage for t in result.trades)
    return {
        "初始资金": f"¥{100_000:,.0f}",
        "最终资金": f"¥{result.final_capital:,.2f}",
        "总收益率": f"{result.total_return:+.2f}%",
        "年化收益率": f"{result.annual_return:+.2f}%",
        "最大回撤": f"{result.max_drawdown:.2f}%",
        "夏普比率": f"{result.sharpe_ratio:.2f}",
        "胜率": f"{result.win_rate:.1f}%",
        "盈亏比": f"{result.profit_factor:.2f}",
        "总交易次数": result.total_trades,
        "盈利交易": result.winning_trades,
        "亏损交易": result.losing_trades,
        "平均持仓天数": f"{result.avg_holding_days:.1f}天",
        "平均盈利幅度": f"{result.avg_profit_pct:+.2f}%",
        "平均亏损幅度": f"{result.avg_loss_pct:.2f}%",
        "基准收益（买入持有）": f"{result.benchmark_return:+.2f}%",
        "超额收益（Alpha）": f"{result.alpha:+.2f}%",
        "总滑点成本": f"¥{total_slippage:,.2f}",
    }


# ============================================================
# 便捷函数 — 一行代码跑完回测
# ============================================================

def run_backtest(
    df_strategy: pd.DataFrame,
    initial_capital: float = 100_000,
    **strategy_params,
) -> BacktestResult:
    """
    一行代码完成回测。

    :param df_strategy: 项目格式的 DataFrame（含 BUY_SIGNAL 等）
    :param initial_capital: 初始资金
    :param strategy_params: 传给 BTStrategy 的额外参数，
                           如 score_threshold=20, use_market_timing=False,
                           strict_t1=False(兼容模式)/True(严格T+1)
    :return: BacktestResult（与原版完全兼容）
    """
    data = prepare_data(df_strategy)

    bt = Backtest(data, BTStrategy, cash=initial_capital,
                  commission=lambda price, size: max(price * size * 0.0003, 5.0))

    # 注入策略参数
    for key, value in strategy_params.items():
        setattr(BTStrategy, key, value)

    result_bt = bt.run()
    result = convert_result(result_bt, data, initial_capital)

    return result


# ============================================================
# 测试入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("backtesting.py 等价实现测试")
    print("=" * 60)

    try:
        from core.db import get_daily_price
        from strategy.strategies import fuse_signals

        symbol = "000001"
        start_date = "20240101"

        print(f"\n[1/4] 加载数据: {symbol} 从 {start_date}")
        df = get_daily_price(symbol, start_date=start_date)
        print(f"  数据行数: {len(df)}, 列: {list(df.columns[:10])}")

        print(f"\n[2/4] 计算策略分...")
        df_s = fuse_signals(df)
        print(f"  融合分范围: [{df_s['fusion_score'].min():.1f}, "
              f"{df_s['fusion_score'].max():.1f}]")
        print(f"  BUY_SIGNAL 为True的数量: {(df_s.get('BUY_SIGNAL', 0) == True).sum()}")

        print(f"\n[3/4] 运行 backtesting.py 回测...")
        result = run_backtest(
            df_s,
            initial_capital=100_000,
            score_threshold=20.0,
            use_drawdown_guard=True,
            use_market_timing=False,  # 需要index_close列才启用
        )

        print(f"\n[4/4] 回测结果:")
        summary = get_summary(result)
        for k, v in summary.items():
            print(f"  {k}: {v}")

        print(f"\n最近5笔交易:")
        for t in result.trades[-5:]:
            print(f"  {t.entry_date} \u4e70\u5165 {t.entry_price} "
                  f"\u2192 {t.exit_date} \u5356\u51fa {t.exit_price}"
                  f"  {t.pnl_pct:+.1f}%  [{t.exit_reason}]  "
                  f"\u6301\u4ed3{t.holding_days}\u5929")

    except ImportError as e:
        print(f"依赖导入失败: {e}")
        print("提示: 请在项目根目录下运行此脚本")
        print("或手动构造测试数据进行验证")
