"""execution_config.py —— 从冻结参数解析各策略的执行配置（入场/出场/合并）。

方案 C2 要求：执行配置区分短线回踩、动量/反转次日开盘、中长线、个股深度固定止盈，
保留不同策略各自的出场数学，不强行改成一套止损方式；且计算必须由「可传入冻结参数
的适配器」驱动，不在推进时读取当前 get_param（否则会用今日参数重算历史）。

本模块只负责「把冻结参数解析成一份不可变的执行配置」，不含任何行情读取或状态机；
出场数学复用现有引擎：
  - 短线：core.outcome_tracker._short_exit_sim（收盘判定 + 移动止盈状态机）
  - 中长线：strategy.exit_advisor.evaluate_exit_by_prices
诊断/成交口径分离（C2 核心）：
  - signal_reference_price → 诊断收益（signal_outcome），沿用既有诊断定义。
  - exec_entry_price       → 真实模拟成交价（simulated_trade），按 entry_rule 决定。
"""
from dataclasses import dataclass
from typing import Optional

# 执行/诊断定义版本：出场数学或口径变更时递增，signal_outcome 与 simulated_trade
# 按 (signal_id/trade, definition_version) 冻结，历史结果不被新定义改写。
EXECUTION_DEFINITION_VERSION = "exec-v1"
DIAGNOSIS_DEFINITION_VERSION = "diag-v1"

# 入场规则
ENTRY_NEXT_OPEN = "next_open"            # 次日开盘无条件建仓（动量/反转/突破）
ENTRY_PULLBACK_CONFIRM = "pullback_confirm"  # 回踩确认建仓（抄底类）

# 出场引擎
EXIT_SHORT_MACHINE = "short_state_machine"   # 短线收盘判定状态机
EXIT_BY_PRICES = "exit_by_prices"            # 中长线逐日出场纪律
EXIT_DEEP_FIXED_TP = "deep_fixed_tp"         # 个股深度固定止盈

# 合并策略（命名，互不借用）
MERGE_CONSECUTIVE_SEGMENT = "consecutive_segment"    # 正式线：连续推荐分段 + 卖出后拆段
MERGE_DEEP_SAME_STOCK = "deep_same_stock_merge"      # 深度：未了结期间同股合并

# 次日开盘入场豁免回踩的短线线（与 legacy _evaluate_short 同口径）：
# 隔日动量全部价值＝次日开盘建仓；反转首日＝首日/次日开盘入场，套回踩等于变成另一条线。
_NEXT_OPEN_SHORT_KEYS = {"next_day_momentum", "first_reversal"}


@dataclass(frozen=True)
class ExecutionConfig:
    """一份不可变的执行配置；由冻结参数解析而来，随批次/交易冻结保存。"""
    horizon: str
    strategy_key: str
    entry_rule: str
    entry_window: int                 # 回踩窗口交易日数（next_open 为 0）
    exit_engine: str
    merge_policy: str
    stop_loss_pct: Optional[float]    # 止损比例（负小数），推荐自带止损价缺失时兜底
    take_profit_launch: Optional[float]   # 短线移动止盈启动线比例（short_take_profit）
    trailing_pct: Optional[float]     # 移动止盈回撤比例
    partial_tp: Optional[float]       # 中长线移动止盈启动线（<1 比例 / >=1 绝对价）
    stop_cap_pct: Optional[float]     # 止损宽度上限（仅中线叠加）
    max_hold_days: Optional[int]      # 持仓上限交易日（None 不限）
    definition_version: str

    def to_json(self) -> dict:
        return {
            "horizon": self.horizon, "strategy_key": self.strategy_key,
            "entry_rule": self.entry_rule, "entry_window": self.entry_window,
            "exit_engine": self.exit_engine, "merge_policy": self.merge_policy,
            "stop_loss_pct": self.stop_loss_pct, "take_profit_launch": self.take_profit_launch,
            "trailing_pct": self.trailing_pct, "partial_tp": self.partial_tp,
            "stop_cap_pct": self.stop_cap_pct, "max_hold_days": self.max_hold_days,
            "definition_version": self.definition_version,
        }


def _eff(params: dict, key: str, default=None):
    return (params.get("effective") or {}).get(key, default)


def _fnum(value, default=None):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _max_hold(horizon: str, params: dict) -> Optional[int]:
    """短线读冻结的 short_max_hold_days；中长线用 HORIZON_MAX_HOLD 常量。

    中长线持仓上限是代码常量（随 code_version 冻结），不在可调参数内，故按当前
    常量解析——与 run 的 code_version_json 共同锁定时点，运行期不再变动。
    """
    if horizon == "short":
        value = _eff(params, "short_max_hold_days")
        return int(value) if value is not None else None
    from strategy.exit_advisor import HORIZON_MAX_HOLD
    return HORIZON_MAX_HOLD.get(horizon)


def resolve_execution(horizon: str, strategy_key: str, params: dict) -> ExecutionConfig:
    """按周期与稳定策略键，从冻结参数解析执行配置。

    params 为运行冻结的 {"effective","strategy_config","account"} 快照，绝不读 get_param。
    """
    params = params or {}
    if horizon == "short":
        pullback_enabled = bool(int(_eff(params, "short_pullback_entry", 0) or 0))
        use_pullback = pullback_enabled and strategy_key not in _NEXT_OPEN_SHORT_KEYS
        entry_rule = ENTRY_PULLBACK_CONFIRM if use_pullback else ENTRY_NEXT_OPEN
        entry_window = max(1, int(_eff(params, "short_entry_window_days", 1) or 1)) if use_pullback else 0
        return ExecutionConfig(
            horizon="short", strategy_key=strategy_key,
            entry_rule=entry_rule, entry_window=entry_window,
            exit_engine=EXIT_SHORT_MACHINE, merge_policy=MERGE_CONSECUTIVE_SEGMENT,
            stop_loss_pct=_fnum(_eff(params, "short_stop_loss")),
            take_profit_launch=_fnum(_eff(params, "short_take_profit")),
            trailing_pct=_fnum(_eff(params, "short_trailing_pct")),
            partial_tp=None, stop_cap_pct=None,
            max_hold_days=_max_hold("short", params),
            definition_version=EXECUTION_DEFINITION_VERSION)
    if horizon in ("mid", "long"):
        prefix = "mid" if horizon == "mid" else "long"
        return ExecutionConfig(
            horizon=horizon, strategy_key=strategy_key,
            entry_rule=ENTRY_NEXT_OPEN, entry_window=0,
            exit_engine=EXIT_BY_PRICES, merge_policy=MERGE_CONSECUTIVE_SEGMENT,
            stop_loss_pct=_fnum(_eff(params, f"{prefix}_stop_loss")),
            take_profit_launch=None,
            trailing_pct=_fnum(_eff(params, f"{prefix}_trailing_pct")),
            partial_tp=_fnum(_eff(params, f"{prefix}_partial_tp")),
            # 止损宽度上限仅中线叠加（与 legacy _evaluate_mid_long 同口径）
            stop_cap_pct=_fnum(_eff(params, "mid_stop_max_width")) if horizon == "mid" else None,
            max_hold_days=_max_hold(horizon, params),
            definition_version=EXECUTION_DEFINITION_VERSION)
    if horizon == "deep":
        # 个股深度：固定止盈 + 未了结期间同股合并；止损/止盈价来自冻结计划。
        return ExecutionConfig(
            horizon="deep", strategy_key=strategy_key,
            entry_rule=ENTRY_NEXT_OPEN, entry_window=0,
            exit_engine=EXIT_DEEP_FIXED_TP, merge_policy=MERGE_DEEP_SAME_STOCK,
            stop_loss_pct=None, take_profit_launch=None, trailing_pct=None,
            partial_tp=None, stop_cap_pct=None,
            max_hold_days=None,
            definition_version=EXECUTION_DEFINITION_VERSION)
    raise ValueError(f"未支持的周期：{horizon}")
