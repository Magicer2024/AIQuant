"""
config/l06_params.py —— L06 30 分钟验证闭环参数（T3，唯一来源）

spec：shared/specs/L06_spec.md §2/§5/§6。
本文件**独立于** config/strategy_params.py 与 config/personal_config.py：
L06 只读取它们的默认值，绝不写回、绝不覆盖（T5 红线：历史数字零变更）。
"""
from __future__ import annotations

# ── 分诊闸计时（§2）──
TRIAGE_BUDGET_SECONDS = 1800          # 30 分钟，只压在分诊闸上
TRIAGE_UNCLOSED_GRACE_SECONDS = 24 * 3600   # timeout 后 24h 未收口 → 自动 defer
MAX_TRIAGE_ATTEMPTS = 2               # attempt=2 有全新的 1800s 预算

# ── 一题三跑（§4）：L 形 3 组，不跑全格 4 组 ──
L06_WINDOW_DAYS = {"recent": 250, "full": 750}    # 单位=交易日
L06_COMBOS = (
    {"code": "A", "window": "recent", "cost_tier": "real"},
    {"code": "B", "window": "full",   "cost_tier": "real"},
    {"code": "C", "window": "recent", "cost_tier": "ideal"},
)
L06_OPTIONAL_COMBO = {"code": "D", "window": "full", "cost_tier": "ideal"}   # §6.3 补跑，默认不跑

# ── 固定执行配置（§5）：三跑唯一不变的变量集，全部取现网默认，L06 不新造口径 ──
L06_MID_FREQ_EXEC = {
    "buy_timing": "next_day_open",
    "max_holdings": 5,
    "max_buy_per_day": 3,
    "stop_loss_pct": -0.06,        # exit_advisor.DEFAULT_STOP_LOSS 同源
    "take_profit_pct": None,
    "max_hold_days": 10,           # exit_advisor.DEFAULT_MAX_HOLD_DAYS —— 中频定义锚点
    "exclude_st": True,
    "exclude_kcb": True,
    "exclude_cyb": False,
    "risk_free_rate": 0.02,
}

# ── 成本两档（§5）──
# real 档从 config.strategy_params.BACKTEST_COST **拷贝读取**，不引用同一对象、不修改它。
# ideal 档只把三项费率置 0。
#
# ⚠ 最低佣金 `min_commission` 保持原值，**不是遗漏而是被迫的诚实边界**：
#   backtest/engine.py:37 把它读成模块常量 DEFAULT_MIN_COMMISSION 并在 :591/599/648/718/786
#   直接使用，不走 BacktestParams 字段 → 置 0 必须改 engine.py，而 engine.py 是 T5 红线禁区。
#   影响评估：L06 用 RESEARCH_CAPITAL=100 万、max_holdings=5 → 单笔约 20 万，
#   万三佣金 ≈60 元 > 5 元最低佣金，**该项在研究资金规模下非绑定**，
#   对理想/现实两版差值的贡献为 0。若将来用小资金规模跑 L06，需在报告注明此项未被理想化。
def cost_tier(tier: str) -> dict:
    from config.strategy_params import BACKTEST_COST
    if tier == "ideal":
        return {"commission_rate": 0.0, "stamp_tax_rate": 0.0, "slippage_rate": 0.0,
                "min_commission": BACKTEST_COST["min_commission"]}
    if tier == "real":
        return {"commission_rate": BACKTEST_COST["commission_rate"],
                "stamp_tax_rate": BACKTEST_COST["stamp_tax_rate"],
                "slippage_rate": BACKTEST_COST["slippage_rate"],
                "min_commission": BACKTEST_COST["min_commission"]}
    raise ValueError(f"未知成本档: {tier}")


# ── 死因判定阈值（§6.2，量纲=带符号小数）──
L06_DEATH_THRESHOLDS = {
    "min_trades": 30,              # A.total_trades < 30 → SAMPLE
    "fill_shrink_ratio": 0.5,      # A.total_trades 相对预计信号数萎缩 >50% → LIQUIDITY
    "ideal_min_annual": 0.05,      # COST 前提：理想档仍有年化 5%
    "real_dead_negative": 0.0,     # COST 前提：现实档年化 ≤ 0
    "cost_gap_min": 0.05,          # 同窗收益被成本吃掉 >5pp → COST
    "regime_gap_min": 0.10,        # A/B 年化差 >10pp 或反号 → REGIME
    "weak_min_annual": 0.05,
    "weak_max_drawdown": -0.25,    # 回撤是**负小数**：A.max_drawdown < -0.25 判弱
    "weak_min_sharpe": 0.5,
    "min_window_days": 120,        # 实际交易日 <120 → DATA（窗口不足不许冒充长窗结论）
    "liquidity_block_max": 0.30,   # 分诊期抽样不可成交占比 >30% → LIQUIDITY
    "data_coverage_min": 0.80,     # 行情覆盖率
    "indicator_presence_min": 0.70,  # 可落库字段非空率
    "overlap_jaccard_max": 0.80,   # 与已有规则条件重合度
}

# 报告固定段落（§7.3）——结构化版，前端与 md 共用；非空是硬要求（R7）
L06_CANNOT_PROVE = [
    "这是海选快测，不是完整验证：未做样本外/walk-forward（回测引擎本身不支持），"
    "未做参数敏感性扫描 → 过拟合风险未评估。",
    "成本是固定假设（佣金/印花/滑点写死在 config/l06_params.py），不是实测滑点；"
    "成交可行性用涨跌停与一字板规则近似，不等于真实流动性。",
    "理想档**未理想化最低佣金 5 元/笔**：引擎把它读成模块常量而非参数（改动属红线禁区）。"
    "研究资金规模（100 万、5 只持仓）下该项非绑定、对两版差值贡献为 0；"
    "若改用小资金规模跑 L06，COST 死因会略微低估。",
    "行情为前复权且无复权因子表，历史价会随后续除权回改 → 存在 point-in-time 风险。",
    "退市股覆盖依赖 stock_info.is_active，幸存者偏差未排除（待 L05 夜检定性与数据侧确认）。",
    "仅测中频档（max_hold_days=10）；实际持有周期不是 10 日时本报告结论不适用。",
    "分诊 T2 只校验执行时点口径（T+1/次日开盘），**不证明指标计算本身无前视**；"
    "自定义指标需人工核对取数日期。",
    "结论保鲜期未实现（依赖未验证的 regime 距离度量，纪要 D-6 未关闭）："
    "本报告不随市场变化自动失效，读的时候自己看日期。",
]
