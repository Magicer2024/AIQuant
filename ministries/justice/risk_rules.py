"""
ministries/justice/risk_rules.py —— 风控规则库（配置化版本）

所有规则阈值从 risk_config.yaml 读取，支持热加载。
规则函数签名：
    rule_name(account_state: dict) -> RiskCheckResult
"""

from datetime import datetime
from ministries.justice.risk_models import RiskCheckResult, RiskLevel, RiskCategory
from ministries.justice.risk_config import risk_config


def _make_result(rule_name: str, level: RiskLevel, category: RiskCategory,
                 msg: str, metric: float, threshold: float,
                 suggestion: str = "") -> RiskCheckResult:
    """辅助函数：统一构造 RiskCheckResult"""
    return RiskCheckResult(
        level=level,
        category=category,
        rule_name=rule_name,
        message=msg,
        metric_value=metric,
        threshold=threshold,
        timestamp=datetime.now().isoformat(),
        suggestion=suggestion,
    )


# ─────────────────────────────────────────────
# 规则1: 最大回撤限制
# ─────────────────────────────────────────────

def rule_max_drawdown(account_state: dict) -> RiskCheckResult:
    """账户回撤控制"""
    if not risk_config.is_rule_enabled("max_drawdown"):
        return _make_result("max_drawdown", RiskLevel.PASS, RiskCategory.DRAWDOWN,
                           "规则已禁用", 0, 0)

    drawdown = account_state.get("current_drawdown", 0.0)
    cfg = risk_config.get_rule_config("max_drawdown")
    t = cfg.get("thresholds", {})

    if drawdown >= t.get("block", 0.15):
        return _make_result("max_drawdown", RiskLevel.BLOCK, RiskCategory.DRAWDOWN,
            risk_config.get_message("max_drawdown", "block", drawdown=drawdown * 100),
            drawdown, t.get("block", 0.15),
            risk_config.get_suggestion("max_drawdown", "block"))
    elif drawdown >= t.get("restrict", 0.10):
        return _make_result("max_drawdown", RiskLevel.RESTRICT, RiskCategory.DRAWDOWN,
            risk_config.get_message("max_drawdown", "restrict", drawdown=drawdown * 100),
            drawdown, t.get("restrict", 0.10),
            risk_config.get_suggestion("max_drawdown", "restrict"))
    elif drawdown >= t.get("warning", 0.05):
        return _make_result("max_drawdown", RiskLevel.WARNING, RiskCategory.DRAWDOWN,
            risk_config.get_message("max_drawdown", "warning", drawdown=drawdown * 100),
            drawdown, t.get("warning", 0.05),
            risk_config.get_suggestion("max_drawdown", "warning"))
    else:
        return _make_result("max_drawdown", RiskLevel.PASS, RiskCategory.DRAWDOWN,
                           "回撤正常", drawdown, t.get("warning", 0.05))


# ─────────────────────────────────────────────
# 规则2: 单股集中度限制
# ─────────────────────────────────────────────

def rule_single_stock_limit(account_state: dict) -> RiskCheckResult:
    """单只股票持仓不超过阈值"""
    if not risk_config.is_rule_enabled("single_stock_limit"):
        return _make_result("single_stock_limit", RiskLevel.PASS, RiskCategory.CONCENTRATION,
                           "规则已禁用", 0, 0)

    positions = account_state.get("positions", [])
    total_value = account_state.get("total_value", 1.0)
    cfg = risk_config.get_rule_config("single_stock_limit")
    t = cfg.get("thresholds", {})

    max_ratio = 0.0
    max_stock = ""
    for pos in positions:
        ratio = pos.get("market_value", 0) / total_value if total_value > 0 else 0
        if ratio > max_ratio:
            max_ratio = ratio
            max_stock = pos.get("code", "")

    if max_ratio >= t.get("block", 0.30):
        return _make_result("single_stock_limit", RiskLevel.BLOCK, RiskCategory.CONCENTRATION,
            risk_config.get_message("single_stock_limit", "block", stock=max_stock, ratio=max_ratio * 100),
            max_ratio, t.get("block", 0.30),
            risk_config.get_suggestion("single_stock_limit", "block"))
    elif max_ratio >= t.get("warning", 0.25):
        return _make_result("single_stock_limit", RiskLevel.WARNING, RiskCategory.CONCENTRATION,
            risk_config.get_message("single_stock_limit", "warning", stock=max_stock, ratio=max_ratio * 100),
            max_ratio, t.get("warning", 0.25),
            risk_config.get_suggestion("single_stock_limit", "warning"))
    else:
        return _make_result("single_stock_limit", RiskLevel.PASS, RiskCategory.CONCENTRATION,
                           "集中度正常", max_ratio, t.get("block", 0.30))


# ─────────────────────────────────────────────
# 规则3: 大盘择时（MA5/MA20）
# ─────────────────────────────────────────────

def rule_market_timing(account_state: dict) -> RiskCheckResult:
    """大盘择时：MA5 < MA20 时预警"""
    if not risk_config.is_rule_enabled("market_timing"):
        return _make_result("market_timing", RiskLevel.PASS, RiskCategory.MARKET_TIMING,
                           "规则已禁用", 0, 0)

    ma5 = account_state.get("index_ma5", None)
    ma20 = account_state.get("index_ma20", None)
    cfg = risk_config.get_rule_config("market_timing")

    if ma5 is None or ma20 is None:
        return _make_result("market_timing", RiskLevel.PASS, RiskCategory.MARKET_TIMING,
            risk_config.get_message("market_timing", "skip"),
            0, 0)

    if ma5 < ma20:
        return _make_result("market_timing", RiskLevel.RESTRICT, RiskCategory.MARKET_TIMING,
            risk_config.get_message("market_timing", "restrict", ma5=ma5, ma20=ma20),
            ma5 / ma20 if ma20 > 0 else 0, 1.0,
            risk_config.get_suggestion("market_timing", "restrict"))
    else:
        return _make_result("market_timing", RiskLevel.PASS, RiskCategory.MARKET_TIMING,
            risk_config.get_message("market_timing", "pass", ma5=ma5, ma20=ma20),
            ma5 / ma20 if ma20 > 0 else 0, 1.0)


# ─────────────────────────────────────────────
# 规则4: 连续亏损冷却期
# ─────────────────────────────────────────────

def rule_consecutive_loss(account_state: dict) -> RiskCheckResult:
    """连续亏损触发冷却"""
    if not risk_config.is_rule_enabled("consecutive_loss"):
        return _make_result("consecutive_loss", RiskLevel.PASS, RiskCategory.COOLDOWN,
                           "规则已禁用", 0, 0)

    count = account_state.get("consecutive_losses", 0)
    cfg = risk_config.get_rule_config("consecutive_loss")
    t = cfg.get("thresholds", {})

    if count >= t.get("block", 5):
        return _make_result("consecutive_loss", RiskLevel.BLOCK, RiskCategory.COOLDOWN,
            risk_config.get_message("consecutive_loss", "block", count=count),
            count, t.get("block", 5),
            risk_config.get_suggestion("consecutive_loss", "block"))
    elif count >= t.get("restrict", 3):
        return _make_result("consecutive_loss", RiskLevel.RESTRICT, RiskCategory.COOLDOWN,
            risk_config.get_message("consecutive_loss", "restrict", count=count),
            count, t.get("restrict", 3),
            risk_config.get_suggestion("consecutive_loss", "restrict"))
    else:
        return _make_result("consecutive_loss", RiskLevel.PASS, RiskCategory.COOLDOWN,
                           "连续亏损次数正常", count, t.get("restrict", 3))


# ─────────────────────────────────────────────
# 规则5: 日交易频率限制
# ─────────────────────────────────────────────

def rule_daily_trade_limit(account_state: dict) -> RiskCheckResult:
    """每日开仓次数上限"""
    if not risk_config.is_rule_enabled("daily_trade_limit"):
        return _make_result("daily_trade_limit", RiskLevel.PASS, RiskCategory.POSITION_LIMIT,
                           "规则已禁用", 0, 0)

    count = account_state.get("daily_open_count", 0)
    cfg = risk_config.get_rule_config("daily_trade_limit")
    t = cfg.get("thresholds", {})

    if count >= t.get("block", 5):
        return _make_result("daily_trade_limit", RiskLevel.BLOCK, RiskCategory.POSITION_LIMIT,
            risk_config.get_message("daily_trade_limit", "block", count=count),
            count, t.get("block", 5),
            risk_config.get_suggestion("daily_trade_limit", "block"))
    elif count >= t.get("warning", 3):
        return _make_result("daily_trade_limit", RiskLevel.WARNING, RiskCategory.POSITION_LIMIT,
            risk_config.get_message("daily_trade_limit", "warning", count=count),
            count, t.get("warning", 3),
            risk_config.get_suggestion("daily_trade_limit", "warning"))
    else:
        return _make_result("daily_trade_limit", RiskLevel.PASS, RiskCategory.POSITION_LIMIT,
                           "日交易频率正常", count, t.get("warning", 3))


# ─────────────────────────────────────────────
# 规则6: 个股波动率限制（新增）
# ─────────────────────────────────────────────

def rule_volatility_limit(account_state: dict) -> RiskCheckResult:
    """个股单日涨跌幅限制"""
    if not risk_config.is_rule_enabled("volatility_limit"):
        return _make_result("volatility_limit", RiskLevel.PASS, RiskCategory.VOLATILITY,
                           "规则已禁用", 0, 0)

    # 从持仓中获取每只股票的今日涨跌幅
    positions = account_state.get("positions", [])
    cfg = risk_config.get_rule_config("volatility_limit")
    t = cfg.get("thresholds", {})

    max_change = 0.0
    max_stock = ""
    for pos in positions:
        change = abs(pos.get("daily_change_pct", 0))
        if change > max_change:
            max_change = change
            max_stock = pos.get("code", "")

    if max_change >= t.get("block", 0.07):
        return _make_result("volatility_limit", RiskLevel.BLOCK, RiskCategory.VOLATILITY,
            risk_config.get_message("volatility_limit", "block", stock=max_stock, change=max_change * 100),
            max_change, t.get("block", 0.07),
            risk_config.get_suggestion("volatility_limit", "block"))
    elif max_change >= t.get("warning", 0.05):
        return _make_result("volatility_limit", RiskLevel.WARNING, RiskCategory.VOLATILITY,
            risk_config.get_message("volatility_limit", "warning", stock=max_stock, change=max_change * 100),
            max_change, t.get("warning", 0.05),
            risk_config.get_suggestion("volatility_limit", "warning"))
    else:
        return _make_result("volatility_limit", RiskLevel.PASS, RiskCategory.VOLATILITY,
                           "波动率正常", max_change, t.get("warning", 0.05))


# ─────────────────────────────────────────────
# 规则7: 行业集中度限制（新增）
# ─────────────────────────────────────────────

def rule_sector_concentration(account_state: dict) -> RiskCheckResult:
    """单一行业持仓占比限制"""
    if not risk_config.is_rule_enabled("sector_concentration"):
        return _make_result("sector_concentration", RiskLevel.PASS, RiskCategory.CONCENTRATION,
                           "规则已禁用", 0, 0)

    positions = account_state.get("positions", [])
    total_value = account_state.get("total_value", 1.0)
    cfg = risk_config.get_rule_config("sector_concentration")
    t = cfg.get("thresholds", {})

    # 按行业汇总
    sector_values = {}
    for pos in positions:
        sector = pos.get("sector", "未知")
        sector_values[sector] = sector_values.get(sector, 0) + pos.get("market_value", 0)

    max_ratio = 0.0
    max_sector = ""
    for sector, value in sector_values.items():
        ratio = value / total_value if total_value > 0 else 0
        if ratio > max_ratio:
            max_ratio = ratio
            max_sector = sector

    if max_ratio >= t.get("block", 0.40):
        return _make_result("sector_concentration", RiskLevel.BLOCK, RiskCategory.CONCENTRATION,
            risk_config.get_message("sector_concentration", "block", sector=max_sector, ratio=max_ratio * 100),
            max_ratio, t.get("block", 0.40),
            risk_config.get_suggestion("sector_concentration", "block"))
    elif max_ratio >= t.get("warning", 0.35):
        return _make_result("sector_concentration", RiskLevel.WARNING, RiskCategory.CONCENTRATION,
            risk_config.get_message("sector_concentration", "warning", sector=max_sector, ratio=max_ratio * 100),
            max_ratio, t.get("warning", 0.35),
            risk_config.get_suggestion("sector_concentration", "warning"))
    else:
        return _make_result("sector_concentration", RiskLevel.PASS, RiskCategory.CONCENTRATION,
                           "行业分散度正常", max_ratio, t.get("block", 0.40))


# ─────────────────────────────────────────────
# 规则8: 总仓位限制（新增）
# ─────────────────────────────────────────────

def rule_total_position_limit(account_state: dict) -> RiskCheckResult:
    """总仓位上限"""
    if not risk_config.is_rule_enabled("total_position_limit"):
        return _make_result("total_position_limit", RiskLevel.PASS, RiskCategory.POSITION_LIMIT,
                           "规则已禁用", 0, 0)

    total_value = account_state.get("total_value", 0)
    available = account_state.get("available_capital", 0)
    cfg = risk_config.get_rule_config("total_position_limit")
    t = cfg.get("thresholds", {})

    # 总仓位 = 持仓市值 / (持仓市值 + 可用资金)
    total_capital = total_value + available
    ratio = total_value / total_capital if total_capital > 0 else 0

    if ratio >= t.get("block", 0.90):
        return _make_result("total_position_limit", RiskLevel.BLOCK, RiskCategory.POSITION_LIMIT,
            risk_config.get_message("total_position_limit", "block", ratio=ratio * 100),
            ratio, t.get("block", 0.90),
            risk_config.get_suggestion("total_position_limit", "block"))
    elif ratio >= t.get("warning", 0.80):
        return _make_result("total_position_limit", RiskLevel.WARNING, RiskCategory.POSITION_LIMIT,
            risk_config.get_message("total_position_limit", "warning", ratio=ratio * 100),
            ratio, t.get("warning", 0.80),
            risk_config.get_suggestion("total_position_limit", "warning"))
    else:
        return _make_result("total_position_limit", RiskLevel.PASS, RiskCategory.POSITION_LIMIT,
                           "总仓位正常", ratio, t.get("warning", 0.80))


# ─────────────────────────────────────────────
# 规则9: 节假日/周末持仓提醒（新增）
# ─────────────────────────────────────────────

def rule_weekend_hold(account_state: dict) -> RiskCheckResult:
    """长假期前持仓风险提醒"""
    if not risk_config.is_rule_enabled("weekend_hold"):
        return _make_result("weekend_hold", RiskLevel.PASS, RiskCategory.MARKET_TIMING,
                           "规则已禁用", 0, 0)

    upcoming_holiday_days = account_state.get("upcoming_holiday_days", 0)
    cfg = risk_config.get_rule_config("weekend_hold")
    threshold = cfg.get("threshold_days", 3)

    if upcoming_holiday_days >= threshold:
        return _make_result("weekend_hold", RiskLevel.WARNING, RiskCategory.MARKET_TIMING,
            risk_config.get_message("weekend_hold", "warning", days=upcoming_holiday_days),
            upcoming_holiday_days, threshold,
            risk_config.get_suggestion("weekend_hold", "warning"))
    else:
        return _make_result("weekend_hold", RiskLevel.PASS, RiskCategory.MARKET_TIMING,
                           "无假期风险", upcoming_holiday_days, threshold)


# ─────────────────────────────────────────────
# 规则10: 黑名单检查（新增）
# ─────────────────────────────────────────────

def rule_blacklist_check(account_state: dict) -> RiskCheckResult:
    """检查持仓或交易标的是否在黑名单中"""
    if not risk_config.is_rule_enabled("blacklist"):
        return _make_result("blacklist", RiskLevel.PASS, RiskCategory.BLACKLIST,
                           "规则已禁用", 0, 0)

    # 从数据库加载当前有效的黑名单
    try:
        from core.db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_code, reason FROM blacklist WHERE expiry_date > datetime('now')"
            ).fetchall()
            blacklist = {r["stock_code"]: r["reason"] for r in rows}
    except Exception:
        blacklist = {}

    positions = account_state.get("positions", [])
    trade_target = account_state.get("trade_target", "")

    # 检查持仓中是否有黑名单股票
    for pos in positions:
        code = pos.get("code", "")
        if code in blacklist:
            return _make_result("blacklist", RiskLevel.BLOCK, RiskCategory.BLACKLIST,
                f"{code} 在黑名单中: {blacklist[code]}", 1, 1,
                "请立即卖出或申请移除黑名单")

    # 检查交易标的是否在黑名单
    if trade_target and trade_target in blacklist:
        return _make_result("blacklist", RiskLevel.BLOCK, RiskCategory.BLACKLIST,
            f"交易标的 {trade_target} 在黑名单中: {blacklist[trade_target]}", 1, 1,
            "禁止买入黑名单股票")

    return _make_result("blacklist", RiskLevel.PASS, RiskCategory.BLACKLIST,
                       "无黑名单命中", 0, 0)


# ─────────────────────────────────────────────
# 规则注册表（自动收集所有 rule_ 开头的函数）
# ─────────────────────────────────────────────

DEFAULT_RULES = [
    rule_max_drawdown,
    rule_single_stock_limit,
    rule_market_timing,
    rule_consecutive_loss,
    rule_daily_trade_limit,
    rule_volatility_limit,
    rule_sector_concentration,
    rule_total_position_limit,
    rule_weekend_hold,
    rule_blacklist_check,
]
