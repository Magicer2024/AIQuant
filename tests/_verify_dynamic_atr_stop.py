"""回归：手动持仓「每日动态 ATR 止损」口径校验（只读，不写库）。

覆盖点：
  1) atr_dynamic_stop_pct == clamp(k×ATR14%, floor, cap)，且 ATR 取最新交易日（每日会变）
  2) 回退：数据不足 / 开关关闭 / 无 high-low 列 → None（调用方回退固定 short_stop_loss）
  3) 假阳性防护：动态止损不得用「持仓期最低价」判定 —— 波动率下行、止损线上移时，
     "当初没破线、现在还赚"的仓位不得被判为已止损
  4) _diagnose_position 端到端：short 持仓的 advice 带 stop_mode=atr 与 ATR 止损价
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from strategy.exit_advisor import atr_dynamic_stop_pct, evaluate_exit
from strategy.indicators import calc_atr
from core.db import get_conn
from config.strategy_params import get_param

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def synthetic(dip_to=90.0, now=105.0, atr_low=True):
    """构造：建仓 100 → 中途下探 dip_to → 现在 now；当前 ATR 很小（止损线会上移）。"""
    n = 40
    idx = pd.date_range("2026-06-01", periods=n, freq="B").strftime("%Y-%m-%d")
    close = np.full(n, 100.0)
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)
    close[5], high[5], low[5] = 95.0, 96.0, dip_to        # 中途探底
    close[-1], high[-1], low[-1] = now, now + 0.5, now - 0.5
    if atr_low:
        for i in range(n - 20, n):                        # 近期振幅压极小 → ATR 很小
            high[i], low[i] = close[i] + 0.2, close[i] - 0.2
    return pd.DataFrame({"close": close, "high": high, "low": low}, index=idx)


def main():
    print("=== 1) 公式一致性 ===")
    df = synthetic()
    pct = atr_dynamic_stop_pct(df)
    close = float(df["close"].iloc[-1])
    atr = float(calc_atr(df["high"].astype(float), df["low"].astype(float),
                         df["close"].astype(float), 14).iloc[-1])
    k, fl, cp = (get_param("short_atr_stop_k"), get_param("short_atr_stop_floor"),
                 get_param("short_atr_stop_cap"))
    exp = -round(min(max(atr / close * k, min(fl, cp)), max(fl, cp)), 4)
    check("pct == -clamp(k×ATR%/close, floor, cap)", pct == exp,
          f"pct={pct} exp={exp} (k={k} floor={fl} cap={cp})")

    print("\n=== 2) 「每天动态」：ATR 取最新交易日，逐日会变 ===")
    from core.repository.price_repo import get_daily_price
    real = get_daily_price("601611")
    seq = [atr_dynamic_stop_pct(real.iloc[:len(real) - i] if i else real) for i in range(0, 6)]
    check("最近 6 个交易日止损比例不全相同（确实动态）",
          len(set(seq)) > 1, f"{seq}")
    check("每日取值落在 [floor, cap] 内",
          all(p is not None and -cp - 1e-9 <= p <= -fl + 1e-9 for p in seq))

    print("\n=== 3) 回退路径 ===")
    check("数据不足(<15 行) → None", atr_dynamic_stop_pct(real.tail(10)) is None)
    check("缺 high/low 列 → None",
          atr_dynamic_stop_pct(real[["close"]]) is None)
    import config.strategy_params as sp
    orig = sp.TUNABLE_PARAMS["short_atr_stop_enabled"]["default"]
    sp.TUNABLE_PARAMS["short_atr_stop_enabled"]["default"] = 0
    sp.invalidate_param_cache()
    check("开关关闭 → None", atr_dynamic_stop_pct(real) is None)
    sp.TUNABLE_PARAMS["short_atr_stop_enabled"]["default"] = orig
    sp.invalidate_param_cache()
    check("开关恢复 → 非 None", atr_dynamic_stop_pct(real) is not None)

    print("\n=== 4) 假阳性防护（波动率下行 → 止损线上移）===")
    d = synthetic(dip_to=90.0, now=105.0, atr_low=True)
    p = atr_dynamic_stop_pct(d)
    stop = 100.0 * (1 + p)
    print(f"    当前止损价 = 100×(1{p:+.4f}) = {stop:.2f}；持仓期最低 90.00；现价 105.00")
    if stop >= 90.0:      # 止损线高于历史低点 → 旧口径会误报
        # ⚠ 关掉 max_hold_days：本段只考察「硬止损」这一条规则的判定口径。
        # 构造帧持仓 39 日 > 默认上限 10 日，超期规则会先于/独立于止损触发，
        # 若不禁用，动态路径会因「超期」而非「止损」返回 clear，掩盖真实结论。
        legacy = evaluate_exit(100.0, "2026-06-01", d, stop_loss_pct=p,
                               stop_is_dynamic=False, max_hold_days=None)
        fixed = evaluate_exit(100.0, "2026-06-01", d, stop_loss_pct=p,
                              stop_is_dynamic=True, max_hold_days=None)
        check("静态判定会误报 clear（复现旧缺陷）", legacy["status"] == "clear",
              legacy["reason"])
        check("动态判定不误报（现价 105 高于止损线）", fixed["status"] == "hold",
              fixed["reason"])
        check("动态路径的 clear 不含「硬止损」（口径隔离自检）",
              "硬止损" not in fixed["reason"], fixed["reason"])
    else:
        check("构造未触发阈值（跳过）", True, f"stop={stop:.2f} < 90")

    print("\n=== 5) _diagnose_position 端到端（short，ATR 动态）===")
    import routes.investor as inv
    with get_conn() as conn:
        adv = inv._diagnose_position(conn, "601611", 12.12, "2026-08-05", "short")
    check("advice 非空", bool(adv))
    if adv:
        det = adv.get("detail") or {}
        check("stop_mode == 'atr'", det.get("stop_mode") == "atr", str(det.get("stop_mode")))
        check("stop_price == 成本×(1+pct)",
              det.get("stop_price") is not None
              and abs(det["stop_price"] - round(12.12 * (1 + det["stop_loss_pct"]), 2)) <= 0.02,
              f"stop={det.get('stop_price')} pct={det.get('stop_loss_pct')}")
        print(f"    诊断：{adv['status']} · {adv['reason']}")

    print("\n结论:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
