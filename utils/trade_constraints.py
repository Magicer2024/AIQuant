"""
utils/trade_constraints.py —— A 股交易约束与账户模型（方案 E2）

统一「建仓计划构造」「两个回测引擎」「推荐资格」三处的成交约束口径，
保证各入口在相同执行配置下通过同一组黄金样例。全部为纯函数，无状态：

  - 主板资格：明确沪 60x / 深 00x 白名单；异常代码与未支持板块默认不可交易。
  - 最大可买：按预算 / 100 股整手 / 配置的最低佣金求最大可买股数；
    1 手也买不起 → executable=False + 原因，不生成虚构仓位。
  - 分批：50/30/20 目标权重，最大余数法分配整手，删除 0 股批次，逐批复核最低佣金。
  - 保守成交：涨跌停 / 一字板 / 停牌 / 缺数据 → blocked / suspended / unverifiable，
    缺少可靠限价元数据时标记不可验证，不假定成功成交。
  - 执行配置快照：T+1 / 入场方式 / 止损判定时点 / 费用 / 滑点 / 整手规则。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.personal_config import (
    LOT_SIZE, MIN_COMMISSION, COMMISSION_RATE, STAMP_TAX_RATE, SLIPPAGE_RATE,
    PLAN_BATCH_WEIGHTS, MAIN_BOARD_PREFIXES, MAIN_BOARD_LIMIT_PCT,
)

# ──────────── 成交障碍状态 ────────────
FILL_OK = "ok"                    # 可正常成交
FILL_SUSPENDED = "suspended"      # 停牌 / 缺数据（无当日有效行情）
FILL_BLOCKED = "blocked"          # 一字板 / 开盘封板：确定无法成交
FILL_UNVERIFIABLE = "unverifiable"  # 涨跌停但缺可靠限价元数据：不可验证，保守视作不成交

_TRADABLE_BUY = (FILL_OK,)
_TRADABLE_SELL = (FILL_OK,)


# ──────────── 主板资格（明确白名单）────────────

def is_tradable_main_board(code: Any) -> bool:
    """代码是否属于可交易主板（明确白名单：沪 60x / 深 00x）。

    异常代码（非 6 位 / 含非数字 / 空）与未支持板块（北交所 8x/4x、B 股 9x/2x、
    科创板 68x、创业板 30x）一律默认不可交易 —— 白名单比黑名单更安全，
    新号段（如 302xxx）不会漏进个人推荐池。
    """
    c = str(code or "").strip()
    if len(c) != 6 or not c.isdigit():
        return False
    return c.startswith(MAIN_BOARD_PREFIXES)


def board_limit_pct(code: Any, *, is_st: bool = False) -> float:
    """按板块返回涨跌停幅度：ST ±5%、科创/创业 ±20%、主板 ±10%。"""
    if is_st:
        return 0.05
    c = str(code or "").strip()
    if c.startswith(("30", "68")):
        return 0.20
    return MAIN_BOARD_LIMIT_PCT


# ──────────── 费用 ────────────

def buy_commission(amount: float, commission_rate: float = COMMISSION_RATE,
                   min_commission: float = MIN_COMMISSION) -> float:
    """买入佣金：max(金额 × 费率, 最低佣金)。买入不收印花税。"""
    return max(amount * commission_rate, min_commission)


def sell_costs(amount: float, commission_rate: float = COMMISSION_RATE,
               min_commission: float = MIN_COMMISSION,
               stamp_tax_rate: float = STAMP_TAX_RATE) -> Tuple[float, float]:
    """卖出费用：返回 (佣金, 印花税)。印花税仅卖出收取。"""
    commission = max(amount * commission_rate, min_commission)
    stamp = amount * stamp_tax_rate
    return commission, stamp


# ──────────── 最大可买股数（整手 + 最低佣金）────────────

def max_affordable_shares(
    budget: float,
    price: float,
    *,
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
    lot_size: int = LOT_SIZE,
) -> Dict[str, Any]:
    """按预算 / 整手 / 最低佣金求最大可买股数。

    返回 {executable, shares, lots, cost, commission, total_cost, reason}：
      - executable=False 时 shares=0 且给出中文原因（价格无效 / 预算不足 / 1 手买不起）；
      - 逐手回退保证「成交额 + 佣金」不超过预算，绝不生成虚构仓位。
    """
    def _no(reason: str) -> Dict[str, Any]:
        return {"executable": False, "shares": 0, "lots": 0, "cost": 0.0,
                "commission": 0.0, "total_cost": 0.0, "reason": reason}

    if price is None or price <= 0 or math.isnan(price):
        return _no("价格无效")
    if budget is None or budget <= 0:
        return _no("预算不足")

    lot_cost = price * lot_size
    lot_commission = buy_commission(lot_cost, commission_rate, min_commission)
    if lot_cost + lot_commission > budget:
        return _no(
            f"1 手（{lot_size} 股）含佣金需 {lot_cost + lot_commission:.2f} 元，"
            f"超过预算 {budget:.2f} 元")

    lots = int(budget // lot_cost)
    while lots > 0:
        cost = lots * lot_cost
        commission = buy_commission(cost, commission_rate, min_commission)
        if cost + commission <= budget:
            return {"executable": True, "shares": lots * lot_size, "lots": lots,
                    "cost": round(cost, 2), "commission": round(commission, 2),
                    "total_cost": round(cost + commission, 2), "reason": None}
        lots -= 1
    return _no("计入佣金后预算不足 1 手")


# ──────────── 分批分配（最大余数法）────────────

def allocate_batches(
    total_shares: int,
    weights: Sequence[float] = PLAN_BATCH_WEIGHTS,
    *,
    lot_size: int = LOT_SIZE,
    keep_zeros: bool = False,
) -> List[int]:
    """按目标权重（默认 50/30/20）用最大余数法把整手股数分配到各批，默认删除 0 股批次。

    最大余数法：先按比例向下取整到「整手」，再把剩余手数按小数余数从大到小逐批 +1 手，
    保证各批之和恰为 total_shares 且每批为整手。余数相同按批次先后，结果确定。
    例：total=1000 → [500, 300, 200]（精确 50/30/20，而非旧实现的 500/250/250）。

    keep_zeros=True 时保留与 weights 等长的列表（含 0 股批次），供调用方按原始
    权重索引对齐价格/备注后再自行删除 0 股批次。
    """
    if total_shares is None or total_shares <= 0:
        return [0] * len(weights) if (keep_zeros and weights) else []
    total_lots = int(total_shares) // lot_size
    if total_lots <= 0 or not weights:
        return [0] * len(weights) if (keep_zeros and weights) else []
    wsum = float(sum(weights)) or 1.0
    norm = [float(w) / wsum for w in weights]
    exact = [total_lots * w for w in norm]
    lots = [int(math.floor(e)) for e in exact]
    leftover = total_lots - sum(lots)
    if leftover > 0:
        order = sorted(range(len(norm)), key=lambda i: (-(exact[i] - lots[i]), i))
        for k in range(leftover):
            lots[order[k % len(order)]] += 1
    shares = [l * lot_size for l in lots]
    return shares if keep_zeros else [s for s in shares if s > 0]


def batch_costs(
    shares_list: Sequence[int],
    prices: Sequence[float],
    *,
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
) -> Dict[str, Any]:
    """逐批计算含最低佣金的买入金额，返回 {batches, total_amount, total_commission, total_cost}。

    每批 total = 成交额 + max(成交额 × 费率, 最低佣金)；合计为各批 total 之和
    （即「重新检查各批最低佣金后的总金额」）。
    """
    batches: List[Dict[str, Any]] = []
    total_amount = 0.0
    total_commission = 0.0
    for shares, price in zip(shares_list, prices):
        if shares <= 0 or price is None or price <= 0:
            continue
        amount = shares * price
        commission = buy_commission(amount, commission_rate, min_commission)
        batches.append({"shares": int(shares), "price": round(float(price), 4),
                        "amount": round(amount, 2), "commission": round(commission, 2),
                        "total": round(amount + commission, 2)})
        total_amount += amount
        total_commission += commission
    return {"batches": batches,
            "total_amount": round(total_amount, 2),
            "total_commission": round(total_commission, 2),
            "total_cost": round(total_amount + total_commission, 2)}


# ──────────── 保守成交判定（涨跌停 / 一字板 / 停牌 / 缺数据）────────────

def _valid_price(*vals: Any) -> bool:
    for v in vals:
        if v is None:
            return False
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        if math.isnan(fv) or fv <= 0:
            return False
    return True


def is_one_price_board(high: Any, low: Any, prev_close: Any, *, tol: float = 0.002) -> bool:
    """一字板判定：最高价 ≈ 最低价（全天单一价），无法在盘中择价成交。"""
    if not _valid_price(high, low, prev_close):
        return False
    return abs(float(high) - float(low)) <= float(prev_close) * tol


def classify_fill_bar(
    direction: str,
    *,
    open_: Any = None,
    high: Any = None,
    low: Any = None,
    close: Any = None,
    volume: Any = None,
    prev_close: Any = None,
    limit_pct: float = MAIN_BOARD_LIMIT_PCT,
    limit_reliable: bool = True,
    tol: float = 0.002,
    price_basis: str = "close",
) -> Tuple[str, Optional[str]]:
    """保守成交判定。返回 (status, reason)，status ∈ {ok, suspended, blocked, unverifiable}。

    direction="buy"：一字涨停 / 开盘封涨停 → blocked（无卖盘，买不进）；
    direction="sell"：一字跌停 / 开盘封跌停 → blocked（无买盘，卖不出）。
    停牌、缺当日有效行情、量为 0 → suspended。
    涨跌停但非一字/非开盘封板，或缺可靠限价元数据 → unverifiable（不假定成交）。
    调用方对非 ok 状态一律保守跳过该笔成交。

    price_basis：本笔成交所依据的价格口径。
      - "close"（默认）：按收盘价成交，收盘触及涨/跌停即保守标记不可验证；
      - "open"：按开盘价成交（回测次日开盘买入），当日尾盘是否涨跌停不影响
        开盘那一刻能否成交，故跳过「收盘涨跌停」判定，仅保留一字板与开盘封板判定。
    """
    # 1) 停牌 / 缺数据
    if not _valid_price(close):
        return FILL_SUSPENDED, "缺当日有效收盘价（停牌/缺数据）"
    if volume is not None:
        try:
            if float(volume) <= 0:
                return FILL_SUSPENDED, "成交量为 0（停牌）"
        except (TypeError, ValueError):
            return FILL_SUSPENDED, "成交量缺失（停牌/缺数据）"
    if not _valid_price(prev_close):
        # 无前收盘无法核定涨跌停：一字板仍可判（high==low），其余标记不可验证
        if _valid_price(high, low) and is_one_price_board(high, low, close, tol=tol):
            return _one_price_result(direction, close, None, tol)
        return FILL_UNVERIFIABLE, "缺前收盘价，无法核定涨跌停"

    pc = float(prev_close)
    limit_up = pc * (1 + limit_pct)
    limit_down = pc * (1 - limit_pct)
    one_price = is_one_price_board(high, low, pc, tol=tol) if _valid_price(high, low) else False

    if direction == "buy":
        if one_price and float(close) >= limit_up * (1 - tol):
            return FILL_BLOCKED, "一字涨停，无卖盘不可买入"
        if _valid_price(open_) and float(open_) >= limit_up * (1 - tol):
            return FILL_BLOCKED, "开盘即封涨停，不可买入"
        # 收盘涨停：仅对「以收盘价成交」的买入保守拦截。开盘买入时当日尾盘是否
        # 涨停不影响开盘那一刻能否成交，故 price_basis="open" 跳过此判。
        if price_basis != "open" and float(close) >= limit_up * (1 - tol):
            if not limit_reliable:
                return FILL_UNVERIFIABLE, "涨停但缺可靠限价元数据，不可验证能否成交"
            return FILL_UNVERIFIABLE, "收盘涨停，盘中封板时点未知，保守不假定成交"
        return FILL_OK, None

    if direction == "sell":
        if one_price and float(close) <= limit_down * (1 + tol):
            return FILL_BLOCKED, "一字跌停，无买盘不可卖出"
        if _valid_price(open_) and float(open_) <= limit_down * (1 + tol):
            return FILL_BLOCKED, "开盘即封跌停，不可卖出"
        # 收盘跌停：仅对「以收盘价成交」的卖出保守拦截（回测卖出均按收盘价）。
        if price_basis != "open" and float(close) <= limit_down * (1 + tol):
            if not limit_reliable:
                return FILL_UNVERIFIABLE, "跌停但缺可靠限价元数据，不可验证能否成交"
            return FILL_UNVERIFIABLE, "收盘跌停，盘中封板时点未知，保守不假定成交"
        return FILL_OK, None

    raise ValueError(f"direction 必须是 buy 或 sell，收到: {direction}")


def _one_price_result(direction: str, close: Any, prev_close: Any,
                      tol: float) -> Tuple[str, Optional[str]]:
    """无前收盘时的一字板兜底：无法判方向涨跌，标记不可验证。"""
    return FILL_UNVERIFIABLE, "一字板但缺前收盘，无法判定涨跌停方向"


def can_fill(status: str) -> bool:
    """成交障碍状态是否允许成交（仅 ok 可成交，其余一律保守跳过）。"""
    return status == FILL_OK


# ──────────── 执行配置快照 ────────────

def execution_config_snapshot(
    *,
    settlement: str = "T+1",
    buy_timing: str = "next_day_open",
    exit_check_timing: str = "close",
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    max_hold_days: Optional[int] = None,
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
    stamp_tax_rate: float = STAMP_TAX_RATE,
    slippage_rate: float = SLIPPAGE_RATE,
    lot_size: int = LOT_SIZE,
    risk_free_rate: float = 0.02,
    board_restriction: str = "main_board_only",
    fill_constraint: str = "conservative",
) -> Dict[str, Any]:
    """执行配置快照：不同配置分别比较，不能把日内触价回测与收盘止损推荐称为相同实验。

    记录 T+1、入场方式、止损判定时点、费用、滑点、整手规则、板块限制与成交约束口径。
    """
    return {
        "settlement": settlement,
        "buy_timing": buy_timing,
        "entry_price_basis": "next_day_open" if buy_timing == "next_day_open" else "current_close",
        "exit_check_timing": exit_check_timing,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "max_hold_days": max_hold_days,
        "commission_rate": commission_rate,
        "min_commission": min_commission,
        "stamp_tax_rate": stamp_tax_rate,
        "slippage_rate": slippage_rate,
        "lot_size": lot_size,
        "risk_free_rate": risk_free_rate,
        "board_restriction": board_restriction,
        "fill_constraint": fill_constraint,
    }
