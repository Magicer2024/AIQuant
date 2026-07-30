"""
personal_config.py —— 个人交易配置

针对个人小资金使用的回测和实盘参数。
"""

# ── 资金与仓位 ──────────────────────────────
INITIAL_CAPITAL = 100_000       # 初始资金（元）
DAILY_BUY_COUNT = 2             # 每日买入股票数量（1~3）
POSITION_MODE = "equal_weight"  # 仓位分配: equal_weight | fixed_amount
FIXED_AMOUNT_PER_BUY = 50_000  # fixed_amount 模式下每笔买入金额
MAX_POSITIONS = 5               # 最大同时持仓数
MAX_SINGLE_POSITION_PCT = 0.40  # 单只股票最大仓位占比

# ── 卖出规则 ──────────────────────────────
STOP_LOSS = -0.05               # 固定止损（-5%）
TAKE_PROFIT = 0.15              # 固定止盈（+15%）
TRAILING_STOP_PCT = 0.10        # 移动止损回撤比例（从最高点回撤 10% 触发卖出）
USE_TRAILING_STOP = True        # 是否启用移动止损
HOLDING_MAX_DAYS = 15           # 最大持有天数
HOLDING_MIN_DAYS = 1            # 最小持有天数（T+1 后才可卖）

# ── 买入过滤 ──────────────────────────────
MIN_SCORE = 60.0                # 最低信号分数
MIN_CONFIDENCE = 0.70           # 最低置信度
BUY_AFTER_OPEN = True           # T+1：信号日收盘后，次日开盘买入
SLIPPAGE_RATE = 0.001           # 滑点（0.1%）

# ── 费用 ──────────────────────────────
COMMISSION_RATE = 0.0003        # 佣金费率（万3）
STAMP_TAX_RATE = 0.001          # 印花税（千1，仅卖出）
MIN_COMMISSION = 5.0            # 最低佣金

# ── 信号选择 ──────────────────────────────
SIGNAL_RANK_BY = "fusion_score"  # 排序字段: fusion_score | confidence | score
DEDUP_BY_RULE = True            # 同一规则对同一只股票不重复买入
COOLDOWN_DAYS = 3               # 同一股票卖出后冷却期（天）

# ── 推荐建仓计划（/api/investor/today position_plan）──────────────
POSITION_PLAN_ACCOUNT = 10000    # 建仓计划参考账户规模（元，实际资金约 1 万）
POSITION_PLAN_MAX_PCT = 0.20     # 单股最大仓位占比（max_amount = 账户×占比）

# ── 板块限制（小资金，未开通科创/创业板权限）──────
MAIN_BOARD_ONLY = True           # 每日推荐仅保留主板（沪 60x / 深 00x）
EXCLUDED_BOARD_PREFIXES = ("300", "301", "688", "689")  # 创业板 + 科创板


def get_personal_config() -> dict:
    """返回个人配置字典"""
    return {
        "initial_capital": INITIAL_CAPITAL,
        "daily_buy_count": DAILY_BUY_COUNT,
        "position_mode": POSITION_MODE,
        "fixed_amount_per_buy": FIXED_AMOUNT_PER_BUY,
        "max_positions": MAX_POSITIONS,
        "max_single_position_pct": MAX_SINGLE_POSITION_PCT,
        "stop_loss": STOP_LOSS,
        "take_profit": TAKE_PROFIT,
        "trailing_stop_pct": TRAILING_STOP_PCT,
        "use_trailing_stop": USE_TRAILING_STOP,
        "holding_max_days": HOLDING_MAX_DAYS,
        "holding_min_days": HOLDING_MIN_DAYS,
        "min_score": MIN_SCORE,
        "min_confidence": MIN_CONFIDENCE,
        "buy_after_open": BUY_AFTER_OPEN,
        "slippage_rate": SLIPPAGE_RATE,
        "commission_rate": COMMISSION_RATE,
        "stamp_tax_rate": STAMP_TAX_RATE,
        "min_commission": MIN_COMMISSION,
        "signal_rank_by": SIGNAL_RANK_BY,
        "dedup_by_rule": DEDUP_BY_RULE,
        "cooldown_days": COOLDOWN_DAYS,
        "position_plan_account": POSITION_PLAN_ACCOUNT,
        "position_plan_max_pct": POSITION_PLAN_MAX_PCT,
        "main_board_only": MAIN_BOARD_ONLY,
    }
