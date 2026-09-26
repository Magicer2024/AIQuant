"""
personal_config.py —— 个人交易配置

针对个人小资金使用的回测和实盘参数。
"""

# ── 推荐跟踪起始日 ─────────────────────────
# 出场跟踪（/api/investor/exit_advice）与推荐复盘（recommend_outcome）同口径：
# 该日期（含）起的推荐才纳入统计，此前的旧算法数据一律不统计、不保留
EXIT_TRACK_START_DATE = "2026-07-20"

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

# ── 研究回测资金（与个人建仓计划账户分离，方案 E2）──────────────
# 研究回测默认资金单独命名与展示，不把所有研究回测强制改成 1 万元；
# 个人推荐建仓计划仍读 POSITION_PLAN_ACCOUNT（约 1 万），两者互不覆盖。
RESEARCH_CAPITAL = 1_000_000     # 研究回测默认初始资金（元）
RESEARCH_CAPITAL_LABEL = "研究资金（默认，非个人账户）"

# ── 交易约束（整手 / 分批 / 涨跌停，方案 E2）──────────────
LOT_SIZE = 100                   # A 股最小交易单位（1 手 = 100 股）
PLAN_BATCH_WEIGHTS = (0.5, 0.3, 0.2)  # 建仓分批目标权重（试仓/回踩加仓/深度回踩）
# 涨跌停幅度（用于保守成交判定）：主板 ±10%，科创/创业 ±20%，ST ±5%。
MAIN_BOARD_LIMIT_PCT = 0.10
GEM_STAR_LIMIT_PCT = 0.20        # 创业板 30x / 科创板 68x
ST_LIMIT_PCT = 0.05
# 主板白名单前缀：沪市主板 60x（600/601/603/605），深市主板 00x（000/001/002/003）。
# 明确白名单而非黑名单：北交所 8x/4x、B 股 9x/2x、科创 68x、创业 30x 一律默认不可交易。
MAIN_BOARD_PREFIXES = ("60", "00")

# ── 板块限制（小资金，未开通科创/创业板权限）──────
MAIN_BOARD_ONLY = True           # 每日推荐仅保留主板（沪 60x / 深 00x）
# 创业板 300/301/302… 与科创板 688/689… 各自占满 30x / 68x 整段，
# 按整段排除而不是枚举已启用的号段，否则新号段（如 302132）会漏进推荐池。
EXCLUDED_BOARD_PREFIXES = ("30", "68")


def is_main_board(code) -> bool:
    """代码是否属于可交易主板（MAIN_BOARD_ONLY=False 时恒为 True）。

    采用明确白名单（沪 60x / 深 00x，见 MAIN_BOARD_PREFIXES）而非黑名单：
    异常代码（非 6 位 / 含非数字）与未支持板块（北交所 8x/4x、B 股 9x/2x、
    科创 68x、创业 30x）一律默认不可交易，新号段不会漏进个人推荐池（方案 E2）。
    """
    if not MAIN_BOARD_ONLY:
        return True
    c = str(code or "").strip()
    if len(c) != 6 or not c.isdigit():
        return False
    return c.startswith(MAIN_BOARD_PREFIXES)


def main_board_filter(column: str = "code") -> str:
    """SQL 片段：追加到 WHERE 后，仅保留可交易主板（白名单，与 is_main_board 同口径）。

    前缀来自上面的常量、非用户输入，可直接拼接。column 允许带表别名（如 s.code）。
    """
    if not MAIN_BOARD_ONLY:
        return ""
    likes = " OR ".join(f"{column} LIKE '{p}%'" for p in MAIN_BOARD_PREFIXES)
    return f" AND ({likes})"


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
        # 研究资金（与个人建仓账户分离，单独命名与展示，方案 E2）
        "research_capital": RESEARCH_CAPITAL,
        "research_capital_label": RESEARCH_CAPITAL_LABEL,
        # 交易约束（整手 / 分批 / 涨跌停 / 主板白名单）
        "lot_size": LOT_SIZE,
        "plan_batch_weights": list(PLAN_BATCH_WEIGHTS),
        "main_board_limit_pct": MAIN_BOARD_LIMIT_PCT,
        "main_board_prefixes": list(MAIN_BOARD_PREFIXES),
    }
