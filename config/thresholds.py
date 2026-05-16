"""
config/thresholds.py —— 扫描阈值、回测默认参数、风控阈值
"""

# ── 5策略融合评分阈值 ──────────────────────────
FUSION_THRESHOLD = 28.0       # 融合分触发门槛（与 signal_agent.py/quant.py 对齐）
FUSION_THRESHOLD_MIN = 15.0   # 最低有效门槛（信号回填用）
V4_SCORE_THRESHOLD = 1.8      # v4 超跌反弹信号阈值

# ── 信号扫描参数 ───────────────────────────────
START_CAPITAL = 10000         # 单笔交易初始资金
POSITION_PER_STOCK = 0.5      # 单股仓位比例
POSITION_NUM = 3              # 每日最多推荐数
SIG_BACKFILL_THRESHOLD = 15.0 # 信号回填最低分

# ── 单股止损止盈 ───────────────────────────────
STOP_LOSS = -0.06             # 默认止损 -6%
TAKE_PROFIT = 0.20            # 默认止盈 +20%

# ── 回测默认参数 ───────────────────────────────
BACKTEST_DEFAULTS = {
    "initial_capital": 100000.0,
    "commission_rate": 0.0003,    # 万三手续费（双向）
    "stamp_tax": 0.001,            # 印花税（卖出单向）
    "slippage_rate": 0.001,        # 滑点率千分之一
    "position_pct": 1.0,           # 单笔仓位比例
    "max_holding_days": 40,        # 最大持仓天数
}

# ── 风控阈值 ───────────────────────────────────
RISK_GUARDS = {
    "drawdown_threshold": 0.10,    # 回撤熔断 10%
    "consecutive_loss_limit": 3,   # 连续亏损 3 次暂停开仓
    "max_positions": 5,            # 最大同时持仓数
}

# ── 大盘择时 ───────────────────────────────────
MARKET_TIMING = {
    "index_code": "000001",        # 上证指数
    "ma_short": 5,                 # 短期均线
    "ma_long": 20,                 # 长期均线
}

# ── 动态仓位（ATR） ────────────────────────────
DYNAMIC_POSITION = {
    "atr_period": 14,
    "atr_multiplier": 2.0,
}
