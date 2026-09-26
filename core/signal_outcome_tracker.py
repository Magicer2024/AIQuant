"""signal_outcome_tracker.py —— 诊断推进器：从 signal_event 冻结参考价计算 T+n 收益。

方案 C2：诊断输入来自 signal_event（signal_reference_price），与模拟成交（exec_entry_price）
彻底分离，两者不再从同一个 entry_price 字段猜含义。旧诊断参考价原样保存，收益基准不变。

口径（沿用 legacy 诊断定义，definition_version 冻结）：
  T+n 收益 = (第 n 个 scan_date 之后交易日收盘 - signal_reference_price) / signal_reference_price × 100
  - 缺行情（scan_date 之后无任何日线）→ status='data_pending'，收益保持 NULL，不等同亏损。
  - 随交易日推进逐日补全 T1/T2/T3/T5/T10；不足 n 日的 T+n 保持 NULL。
  - 幂等：重复推进覆盖为更完整的值，不改参考价基准。
"""
from datetime import date

from core.db import get_conn
from core.execution_config import DIAGNOSIS_DEFINITION_VERSION
from core.repository import outcome_repo

# T+n 字段 → 天数
_TN = (("t1", 1), ("t2", 2), ("t3", 3), ("t5", 5), ("t10", 10))
_MAX_WINDOW = 10


def _todays(as_of):
    return as_of or date.today().strftime("%Y-%m-%d")


def advance_signal_outcomes(*, run_id=None, scan_date=None, since=None, as_of=None,
                            definition_version=DIAGNOSIS_DEFINITION_VERSION):
    """推进诊断结果。三种模式（择一）：
      - run_id：只推进该运行的信号；
      - scan_date：只推进该信号日的信号；
      - since：推进 [since, as_of] 区间内全部信号（跨日累积补全 T+n，供每日前向推进）。
    as_of 截断行情（不含未来数据）。返回 {"signals": N, "evaluated": M, "data_pending": K}。
    """
    if run_id is None and scan_date is None and since is None:
        raise ValueError("必须提供 run_id、scan_date 或 since 之一")
    as_of = _todays(as_of)
    with get_conn(readonly=True) as conn:
        if run_id is not None:
            events = conn.execute(
                "SELECT id, code, scan_date, signal_reference_price FROM signal_event WHERE run_id=?",
                (run_id,)).fetchall()
        elif scan_date is not None:
            events = conn.execute(
                "SELECT id, code, scan_date, signal_reference_price FROM signal_event WHERE scan_date=?",
                (scan_date,)).fetchall()
        else:
            events = conn.execute(
                """SELECT id, code, scan_date, signal_reference_price FROM signal_event
                   WHERE scan_date>=? AND scan_date<=?""", (since, as_of)).fetchall()
        events = [dict(e) for e in events]

    evaluated = pending = 0
    for ev in events:
        ref = ev["signal_reference_price"]
        if not ref or ref <= 0:
            # 无参考价无法计算诊断收益，标记缺数据（不伪造 0 收益）。
            outcome_repo.upsert_signal_outcome(
                signal_id=ev["id"], definition_version=definition_version,
                reference_price=ref, returns={}, evaluated_as_of=as_of, status="data_pending")
            pending += 1
            continue
        with get_conn(readonly=True) as conn:
            prices = conn.execute(
                """SELECT trade_date, close FROM daily_price
                   WHERE code=? AND trade_date>? AND trade_date<=?
                   ORDER BY trade_date ASC LIMIT ?""",
                (ev["code"], ev["scan_date"], as_of, _MAX_WINDOW)).fetchall()
        if not prices:
            outcome_repo.upsert_signal_outcome(
                signal_id=ev["id"], definition_version=definition_version,
                reference_price=ref, returns={}, evaluated_as_of=as_of, status="data_pending")
            pending += 1
            continue
        returns = {}
        for key, n in _TN:
            if len(prices) >= n:
                close = prices[n - 1]["close"]
                if close:
                    returns[key] = round((close - ref) / ref * 100, 2)
        outcome_repo.upsert_signal_outcome(
            signal_id=ev["id"], definition_version=definition_version,
            reference_price=ref, returns=returns, evaluated_as_of=as_of, status="evaluated")
        evaluated += 1
    return {"signals": len(events), "evaluated": evaluated, "data_pending": pending}
