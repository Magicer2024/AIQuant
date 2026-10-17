"""
backtest/l06.py —— L06 30 分钟验证闭环的编排与判定层（T3）
==========================================================

spec：shared/specs/L06_spec.md（管理A 已批准三条裁定 + 超时禁 pass 修订）。

职责边界：
  - 本模块**只做编排与判定**：分诊 7 项快检、一题三跑参数展开、bundle 指标汇总、
    死因判定、报告落盘。回测计算 100% 委托现成 `backtest.service.create_task`。
  - 不修改 engine/rule_engine 的任何计算路径，不改 BACKTEST_COST 默认值：
    理想档只是新任务实例的 payload.params 覆盖（T5 红线：历史数字零变更）。
  - 计时判定在服务端：`l06_triage_session.started_at` 由后端首笔写入且不可变，
    客户端传来的 elapsed 一律忽略（R4）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config.l06_params import (
    L06_CANNOT_PROVE,
    L06_COMBOS,
    L06_DEATH_THRESHOLDS as TH,
    L06_MID_FREQ_EXEC,
    L06_WINDOW_DAYS,
    MAX_TRIAGE_ATTEMPTS,
    TRIAGE_BUDGET_SECONDS,
    TRIAGE_UNCLOSED_GRACE_SECONDS,
    cost_tier,
)

TS_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"

# ── 指标 → daily_price 可直接落库判定的列（T4/T5 预筛用）──
# 名单外的技术指标需要历史窗口滚动计算，**不在快检里假装算得出**：
# 预筛一律标 not_evaluated，交由深跑真实成交数判 SAMPLE。
DB_COLUMN_INDICATORS = {
    "fusion_score": "fusion_score",
    "pct_change": "pct_change",
    "turnover_rate": "turnover",
    "volume": "volume",
}

# T2 未来函数白名单：可作为「当日决策」依据的只有当日收盘前已确定的量价字段。
SAME_DAY_DECISION_OK_FIELDS = {"open", "high", "low", "close", "volume"}
# 盘后才生成的派生分：只能按 trade_date < 决策日使用（次日开盘买入合法）。
POST_CLOSE_DERIVED_FIELDS = {"fusion_score", "vol_ratio", "chip_conc", "ma_score",
                             "diverge_score", "bottom_score", "whale_score"}

# 死因枚举（§8.1）
DEATH_STEPS = ("LOGIC", "DATA", "SAMPLE", "LIQUIDITY", "COST", "REGIME", "WEAK",
               "NONE", "TIMEOUT")


# ════════════════ 时间工具 ════════════════
def now_local() -> datetime:
    return datetime.now()


def _parse(ts: str) -> datetime:
    return datetime.strptime(str(ts)[:19], TS_FMT)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


# ════════════════ 分诊计时（§2 / §2.1）════════════════
def open_session(conn: sqlite3.Connection, idea_id: int, attempt: int,
                 at: Optional[datetime] = None) -> Dict[str, Any]:
    """记录某 attempt 的计时起点（服务端唯一权威，写后不可变）。幂等。"""
    start = at or now_local()
    deadline = start + timedelta(seconds=TRIAGE_BUDGET_SECONDS)
    conn.execute(
        """INSERT INTO l06_triage_session(idea_id, attempt, started_at, deadline_at)
           VALUES (?,?,?,?) ON CONFLICT(idea_id,attempt) DO NOTHING""",
        (idea_id, attempt, _fmt(start), _fmt(deadline)),
    )
    row = conn.execute(
        "SELECT started_at, deadline_at FROM l06_triage_session WHERE idea_id=? AND attempt=?",
        (idea_id, attempt),
    ).fetchone()
    return {"started_at": row["started_at"], "deadline_at": row["deadline_at"]}


def session_for(conn: sqlite3.Connection, idea_id: int, attempt: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM l06_triage_session WHERE idea_id=? AND attempt=?",
        (idea_id, attempt),
    ).fetchone()
    return dict(row) if row else None


def next_attempt(conn: sqlite3.Connection, idea_id: int) -> int:
    """下一次分诊的 attempt 编号。**只数真人结论**（pass/fail/defer）：
    timeout / auto_defer 是系统推进的状态，记在事件流里，不占预算名额。"""
    row = conn.execute(
        "SELECT COUNT(*) n FROM triage_log WHERE idea_id=? AND verdict IN ('pass','fail','defer')",
        (idea_id,),
    ).fetchone()
    return int(row["n"] or 0) + 1


def open_next_session(conn: sqlite3.Connection, idea_id: int,
                      at: Optional[datetime] = None) -> Dict[str, Any]:
    """defer 后开启下一轮分诊的全新预算（§2.1）。已用尽两次机会则拒绝。"""
    attempt = next_attempt(conn, idea_id)
    if attempt > MAX_TRIAGE_ATTEMPTS:
        raise TriageError("IDEA_TRIAGE_CLOSED", "两次分诊机会已用尽，该想法已终局")
    return open_session(conn, idea_id, attempt, at=at)


def record_triage(conn: sqlite3.Connection, *, idea_id: int, verdict: str,
                  checks: List[Dict[str, Any]], death_step: Optional[str] = None,
                  defer_reason: Optional[str] = None, overlap_rule_id: Optional[int] = None,
                  at: Optional[datetime] = None) -> Dict[str, Any]:
    """写分诊结论（R3/R4 服务端口径）。

    - 计时起点取 `l06_triage_session`（服务端首笔写入、不可变），客户端 elapsed 一律忽略
    - 本 attempt 已超预算 → 不得补交 pass（§2.1：超时补 pass 就是沉没成本绑架）
    - 最后一个 attempt 上仍超时 → 由 sweep 落终局 fail + death_step='TIMEOUT'
    """
    decided = at or now_local()
    attempt = next_attempt(conn, idea_id)
    if attempt > MAX_TRIAGE_ATTEMPTS:
        raise TriageError("IDEA_TRIAGE_CLOSED", "两次分诊机会已用尽，该想法已终局")
    sess = session_for(conn, idea_id, attempt)
    if sess is None:
        sess = open_session(conn, idea_id, attempt, at=decided)
    started = _parse(sess["started_at"])
    elapsed = int((decided - started).total_seconds())
    over_budget = elapsed > TRIAGE_BUDGET_SECONDS

    if verdict not in ("pass", "fail", "defer"):
        raise TriageError("VERDICT_REQUIRED", "verdict 必须是 pass/fail/defer")
    if verdict == "pass" and over_budget:
        raise TriageError("PASS_AFTER_TIMEOUT_FORBIDDEN",
                          "本 attempt 已超 30 分钟预算，不能再补交 pass；请提交 defer 或 fail，"
                          "或用 defer 开启下一轮全新预算")
    if verdict == "fail" and not death_step:
        raise TriageError("DEATH_STEP_REQUIRED", "判死必须给出死因码")

    conn.execute(
        """INSERT INTO triage_log(idea_id, triage_attempt, started_at, decided_at,
                                  elapsed_seconds, verdict, death_step, checks_json,
                                  defer_reason, overlap_rule_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (idea_id, attempt, sess["started_at"], _fmt(decided), elapsed, verdict,
         death_step, json.dumps(checks, ensure_ascii=False), defer_reason, overlap_rule_id),
    )
    idea_status = {"pass": "passed", "fail": "failed", "defer": "deferred"}[verdict]
    conn.execute("UPDATE idea SET status=? WHERE id=?", (idea_status, idea_id))
    log = conn.execute("SELECT * FROM triage_log WHERE id=last_insert_rowid()").fetchone()
    return dict(log)


def _has_event(conn: sqlite3.Connection, idea_id: int, attempt: int, event: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM l06_idea_event WHERE idea_id=? AND attempt=? AND event=?",
        (idea_id, attempt, event)).fetchone())


def _add_event(conn: sqlite3.Connection, idea_id: int, attempt: int, event: str,
               occurred: datetime, reason: str) -> None:
    conn.execute(
        """INSERT INTO l06_idea_event(idea_id, attempt, event, occurred_at, reason)
           VALUES (?,?,?,?,?) ON CONFLICT(idea_id,attempt,event) DO NOTHING""",
        (idea_id, attempt, event, _fmt(occurred), reason))


def sweep_timeouts(conn: sqlite3.Connection, at: Optional[datetime] = None) -> Dict[str, int]:
    """scheduler 每分钟调用：
      ① 预算耗尽仍无结论 → 记 `timeout` 事件（不写 triage_log，不占预算名额、不自动判死）
      ② 最后一个 attempt（attempt=2）超时 → 终局 fail + death_step='TIMEOUT'
      ③ timeout 后 24h 仍无结论 → 记 `auto_defer` 事件，idea.status='deferred'（进欠账列表）
    """
    now = at or now_local()
    out = {"timeout_recorded": 0, "auto_deferred": 0, "final_fail": 0}
    rows = conn.execute(
        """SELECT s.idea_id, s.attempt, s.deadline_at FROM l06_triage_session s
           WHERE s.deadline_at < ?
             AND NOT EXISTS (SELECT 1 FROM triage_log t
                             WHERE t.idea_id = s.idea_id AND t.triage_attempt = s.attempt)
             AND NOT EXISTS (SELECT 1 FROM idea i WHERE i.id = s.idea_id
                             AND i.status IN ('passed','failed','run_complete'))
           ORDER BY s.idea_id, s.attempt""",
        (_fmt(now),),
    ).fetchall()
    for r in rows:
        idea_id, attempt = int(r["idea_id"]), int(r["attempt"])
        if attempt >= MAX_TRIAGE_ATTEMPTS:
            # 第二次机会也超时 → 落一条终局结论，语义「当前不可验证」，不是惩罚
            record_triage(conn, idea_id=idea_id, verdict="fail",
                          checks=[{"id": "T0", "result": "fail", "reason": "二次分诊仍超时"}],
                          death_step="TIMEOUT", at=now)
            out["final_fail"] += 1
            continue
        if _has_event(conn, idea_id, attempt, "timeout"):
            continue
        _add_event(conn, idea_id, attempt, "timeout", now, "30 分钟预算耗尽，未提交结论")
        conn.execute("UPDATE idea SET status='timeout' WHERE id=?", (idea_id,))
        out["timeout_recorded"] += 1

    stale = conn.execute(
        """SELECT e.idea_id, e.attempt, e.occurred_at FROM l06_idea_event e
           WHERE e.event='timeout'
             AND e.occurred_at < ?
             AND NOT EXISTS (SELECT 1 FROM l06_idea_event x
                             WHERE x.idea_id=e.idea_id AND x.attempt=e.attempt
                               AND x.event='auto_defer')
             AND NOT EXISTS (SELECT 1 FROM triage_log t
                             WHERE t.idea_id=e.idea_id AND t.triage_attempt=e.attempt)""",
        (_fmt(now - timedelta(seconds=TRIAGE_UNCLOSED_GRACE_SECONDS)),),
    ).fetchall()
    for s in stale:
        _add_event(conn, int(s["idea_id"]), int(s["attempt"]), "auto_defer", now,
                   "超时后 24h 未收口，自动转入欠账列表")
        conn.execute("UPDATE idea SET status='deferred' WHERE id=?", (int(s["idea_id"]),))
        out["auto_deferred"] += 1
    return out


class TriageError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# ════════════════ 分诊 7 项快检（§3）════════════════
def _catalog_ids() -> set:
    from backtest.conditions import get_catalog
    ids = set()
    for cat in get_catalog():
        for item in cat.get("items", []):
            ids.add(item["id"])
    return ids


def check_T1_expressionable(conditions: List[Dict[str, Any]]) -> Dict[str, Any]:
    known = _catalog_ids()
    used = {str(c.get("indicator") or "") for c in conditions or []}
    unknown = sorted(u for u in used if u and u not in known)
    if not used or not [u for u in used if u]:
        return {"id": "T1", "result": "fail", "death_step": "LOGIC",
                "reason": "没有任何可识别的选股条件", "unknown": []}
    if unknown:
        return {"id": "T1", "result": "fail", "death_step": "LOGIC",
                "reason": f"条件无法被回测引擎表达：{','.join(unknown)}", "unknown": unknown}
    return {"id": "T1", "result": "pass", "reason": "全部指标在目录内", "unknown": []}


def check_T2_lookahead(conditions: List[Dict[str, Any]],
                       buy_timing: str) -> Dict[str, Any]:
    """当日决策只能用收盘前已确定的量价；派生分只能取 trade_date < 决策日。"""
    if buy_timing != "next_day_open":
        return {"id": "T2", "result": "fail", "death_step": "LOGIC",
                "reason": f"A股 T+1：`buy_timing={buy_timing}` 要求当日信号当日成交，口径不成立"}
    offenders = sorted({
        str(c.get("indicator")) for c in conditions or []
        if str(c.get("indicator")) in ("current_close", "intraday", "same_day_close")
    })
    if offenders:
        return {"id": "T2", "result": "fail", "death_step": "LOGIC",
                "reason": f"条件引用了当日未收盘不可得字段：{','.join(offenders)}",
                "fields": offenders}
    derived = sorted({str(c.get("indicator")) for c in conditions or []
                      if str(c.get("indicator")) in POST_CLOSE_DERIVED_FIELDS})
    return {"id": "T2", "result": "pass",
            "reason": "按次日开盘成交，派生分取上一交易日，无当日成交前视",
            "derived_used": derived,
            "caveat": "本项只校验执行时点口径，不证明指标计算本身无前视"}


def _window_dates(conn: sqlite3.Connection, days: int) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT ?",
        (days,),
    ).fetchall()
    return sorted(str(r["trade_date"])[:10] for r in rows)


def _prescan_signals(conn: sqlite3.Connection, conditions: List[Dict[str, Any]],
                     dates: List[str]) -> Tuple[Optional[int], Optional[float], str]:
    """用可落库字段做一阶截面估计。返回 (预计信号数, 不可成交占比, 状态)。

    不可落库的指标（需历史滚动的技术指标）→ 状态 not_evaluated，**不假装算得出**。
    """
    mappable, unmapped = [], []
    for c in conditions or []:
        ind = str(c.get("indicator"))
        if ind in DB_COLUMN_INDICATORS:
            mappable.append((DB_COLUMN_INDICATORS[ind], c))
        else:
            unmapped.append(ind)
    if not mappable:
        return None, None, "not_evaluated"

    lo, hi = dates[0], dates[-1]
    sql = "SELECT code, trade_date, open, high, low, close, volume FROM daily_price " \
          "WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL"
    args: List[Any] = [lo, hi]
    for col, cond in mappable:
        params = cond.get("params") or {}
        op = str(cond.get("operator") or "gt")
        thr = params.get("min", params.get("value", params.get("max")))
        if thr is None:
            return None, None, "not_evaluated"
        if op in ("gt", "above", "breakout_high", "golden_cross"):
            sql += f" AND {col} > ?"
            args.append(float(thr))
        elif op in ("lt", "below", "breakout_low", "death_cross"):
            sql += f" AND {col} < ?"
            args.append(float(thr))
        elif op == "between":
            sql += f" AND {col} BETWEEN ? AND ?"
            args.extend([float(params.get("min", thr)), float(params.get("max", thr))])
        else:
            return None, None, "not_evaluated"

    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return 0, None, "evaluated"
    # 次日开盘可成交性：只对抽样信号日取「下一交易日开盘」判一字板/开盘封板不可买。
    # 抽样上限 120 笔（每笔 2 次索引查询），保证快检单项 ≤60s 的预算（§3）。
    from utils.trade_constraints import classify_fill_bar, can_fill
    sample = rows[:120]
    judged = blocked = 0
    for r in sample:
        nxt = _next_bar_open(conn, r["code"], str(r["trade_date"])[:10])
        if nxt is None:
            continue
        judged += 1
        status, _ = classify_fill_bar(
            "buy", open_=nxt["open"], high=nxt["high"], low=nxt["low"],
            close=nxt["close"], volume=nxt["volume"], prev_close=nxt["prev_close"],
            price_basis="open")
        if not can_fill(status):
            blocked += 1
    ratio = (blocked / judged) if judged else None
    return len(rows), ratio, "evaluated"


def _next_bar_open(conn: sqlite3.Connection, code: str, trade_date: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """SELECT open, high, low, close, volume FROM daily_price
           WHERE code=? AND trade_date>? ORDER BY trade_date LIMIT 1""",
        (code, trade_date)).fetchone()
    if not row:
        return None
    prev = conn.execute("SELECT close FROM daily_price WHERE code=? AND trade_date=?",
                        (code, trade_date)).fetchone()
    return {"open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"],
            "prev_close": float(prev["close"]) if prev and prev["close"] else None}


def check_T3_data(conn: sqlite3.Connection, dates: List[str]) -> Dict[str, Any]:
    if not dates:
        return {"id": "T3", "result": "defer", "reason": "窗口内无任何行情数据",
                "coverage": 0.0}
    lo, hi = dates[0], dates[-1]
    tot = conn.execute(
        "SELECT COUNT(*) n FROM daily_price WHERE trade_date BETWEEN ? AND ?",
        (lo, hi)).fetchone()["n"]
    good = conn.execute(
        "SELECT COUNT(*) n FROM daily_price WHERE trade_date BETWEEN ? AND ? "
        "AND close IS NOT NULL AND volume IS NOT NULL", (lo, hi)).fetchone()["n"]
    coverage = (good / tot) if tot else 0.0
    if coverage < TH["data_coverage_min"]:
        return {"id": "T3", "result": "defer", "coverage": round(coverage, 4),
                "reason": f"行情覆盖率 {coverage:.1%} < {TH['data_coverage_min']:.0%}，先补数据"}
    return {"id": "T3", "result": "pass", "coverage": round(coverage, 4),
            "reason": f"覆盖率 {coverage:.1%}"}


def check_T4_sample(prescan: Tuple[Optional[int], Optional[float], str]) -> Dict[str, Any]:
    est, _ratio, state = prescan
    if state != "evaluated":
        return {"id": "T4", "result": "not_evaluated",
                "reason": "条件含需历史滚动的技术指标，快检不做截面假装；样本量交由深跑实测",
                "est_signals": None}
    if (est or 0) < TH["min_trades"]:
        return {"id": "T4", "result": "fail", "death_step": "SAMPLE", "est_signals": est,
                "reason": f"预计信号 {est} 笔 < {TH['min_trades']}，统计上无意义"}
    return {"id": "T4", "result": "pass", "est_signals": est,
            "reason": f"预计信号 {est} 笔"}


def check_T5_liquidity(prescan: Tuple[Optional[int], Optional[float], str]) -> Dict[str, Any]:
    _est, ratio, state = prescan
    if state != "evaluated" or ratio is None:
        return {"id": "T5", "result": "not_evaluated", "blocked_ratio": None,
                "reason": "无法快检成交可行性，深跑后按 total_trades 萎缩比再判"}
    if ratio > TH["liquidity_block_max"]:
        return {"id": "T5", "result": "fail", "death_step": "LIQUIDITY",
                "blocked_ratio": round(ratio, 4),
                "reason": f"次日开盘不可成交占比 {ratio:.1%} > {TH['liquidity_block_max']:.0%}"}
    return {"id": "T5", "result": "pass", "blocked_ratio": round(ratio, 4),
            "reason": f"不可成交占比 {ratio:.1%}"}


def check_T6_overlap(conn: sqlite3.Connection, indicators: List[str]) -> Dict[str, Any]:
    used = set(indicators or [])
    if not used:
        return {"id": "T6", "result": "pass", "reason": "无指标可查重", "overlap_rule_id": None}
    try:
        rows = conn.execute("SELECT id, name, params_json FROM strategy_rules").fetchall()
    except sqlite3.Error:
        return {"id": "T6", "result": "not_evaluated", "reason": "规则表不可用",
                "overlap_rule_id": None}
    best, best_id = 0.0, None
    for r in rows:
        try:
            other = {str(x.get("indicator")) for x in json.loads(r["params_json"] or "[]")}
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        other.discard("")
        if not other:
            continue
        jac = len(used & other) / max(1, len(used | other))
        if jac > best:
            best, best_id = jac, int(r["id"])
    if best >= TH["overlap_jaccard_max"]:
        return {"id": "T6", "result": "needs_delta", "overlap": round(best, 3),
                "overlap_rule_id": best_id,
                "reason": "与已有规则高度重合，必须写明差异才能过闸"}
    return {"id": "T6", "result": "pass", "overlap": round(best, 3),
            "overlap_rule_id": best_id, "reason": "无高度重合的既有规则"}


def check_T7_frequency(implied_hold_days: Optional[int]) -> Dict[str, Any]:
    if implied_hold_days is None:
        return {"id": "T7", "result": "pass", "implied_hold_days": None,
                "reason": "未声明持有期，按中频默认 10 日测"}
    if 3 <= int(implied_hold_days) <= 20:
        return {"id": "T7", "result": "pass", "implied_hold_days": int(implied_hold_days),
                "reason": f"隐含持有 {implied_hold_days} 日，落在中频档"}
    return {"id": "T7", "result": "fail", "death_step": "REGIME",
            "implied_hold_days": int(implied_hold_days),
            "reason": f"隐含持有 {implied_hold_days} 日不在中频 [3,20] 日，本闭环不测超频/低频"}


def run_fast_checks(conn: sqlite3.Connection, idea: Dict[str, Any],
                    buy_timing: Optional[str] = None) -> List[Dict[str, Any]]:
    conditions = json.loads(idea["conditions_json"] or "[]")
    indicators = json.loads(idea["indicators_json"] or "[]")
    dates = _window_dates(conn, L06_WINDOW_DAYS["recent"])
    timing = buy_timing or L06_MID_FREQ_EXEC["buy_timing"]
    prescan = _prescan_signals(conn, conditions, dates) if dates else (None, None, "not_evaluated")
    checks = [
        check_T1_expressionable(conditions),
        check_T2_lookahead(conditions, timing),
    ]
    for c in checks:
        if c["result"] == "fail":
            c["short_circuited"] = True
            return checks
    checks.append(check_T3_data(conn, dates))
    if checks[-1]["result"] == "defer":
        return checks
    checks.append(check_T4_sample(prescan))
    if checks[-1]["result"] == "fail":
        return checks
    checks.append(check_T5_liquidity(prescan))
    if checks[-1]["result"] == "fail":
        return checks
    checks.append(check_T6_overlap(conn, indicators))
    checks.append(check_T7_frequency(idea.get("implied_hold_days")))
    return checks


def suggest_verdict(checks: List[Dict[str, Any]]) -> Tuple[str, Optional[str], Optional[str]]:
    """由检查项推出建议结论：(verdict, death_step, defer_reason)。"""
    for c in checks:
        if c["result"] == "fail":
            return "fail", c.get("death_step") or "LOGIC", None
        if c["result"] == "defer":
            return "defer", None, c.get("reason", "数据不足")
    if any(c["result"] == "needs_delta" for c in checks):
        return "defer", None, "与已有规则高度重合，需先写明差异（delta）"
    return "pass", None, None


# ════════════════ 一题三跑编排（§4 / §5）════════════════
def build_run_payloads(conditions: List[Dict[str, Any]], rule_name: str,
                       windows: Dict[str, List[str]],
                       combos: Tuple[Dict[str, str], ...] = L06_COMBOS) -> List[Dict[str, Any]]:
    """展开 3 组 payload：除 start_date 与成本三项外，**其余参数逐字相同**（R8）。"""
    out = []
    for cb in combos:
        dates = windows.get(cb["window"]) or []
        if not dates:
            raise TriageError("WINDOW_UNAVAILABLE", f"{cb['window']} 窗口无可用行情数据")
        tier = cost_tier(cb["cost_tier"])
        params: Dict[str, Any] = {
            "start_date": dates[0],
            "end_date": dates[-1],
            "max_holdings": L06_MID_FREQ_EXEC["max_holdings"],
            "max_buy_per_day": L06_MID_FREQ_EXEC["max_buy_per_day"],
            "buy_timing": L06_MID_FREQ_EXEC["buy_timing"],
            "stop_loss_pct": L06_MID_FREQ_EXEC["stop_loss_pct"],
            "take_profit_pct": L06_MID_FREQ_EXEC["take_profit_pct"],
            "max_hold_days": L06_MID_FREQ_EXEC["max_hold_days"],
            "exclude_st": L06_MID_FREQ_EXEC["exclude_st"],
            "exclude_kcb": L06_MID_FREQ_EXEC["exclude_kcb"],
            "exclude_cyb": L06_MID_FREQ_EXEC["exclude_cyb"],
            "risk_free_rate": L06_MID_FREQ_EXEC["risk_free_rate"],
            "commission_rate": tier["commission_rate"],
            "stamp_tax_rate": tier["stamp_tax_rate"],
            "slippage_rate": tier["slippage_rate"],
            # min_commission 不传：engine.py:37 把它读成模块常量、不走 params（禁区不动），
            # 且在研究资金规模下非绑定，详见 config/l06_params.py::cost_tier 注释。
            "_l06_trading_days": len(dates),
        }
        out.append({"code": cb["code"], "window": cb["window"], "cost_tier": cb["cost_tier"],
                    "window_days": len(dates),
                    "payload": {"rule_name": f"L06·{cb['code']}·{rule_name}",
                                "conditions": conditions, "params": params}})
    return out


def submit_run(conn: sqlite3.Connection, idea_id: int, at: Optional[datetime] = None) -> Dict[str, Any]:
    """门禁：最新分诊结论必须是 pass；否则 409 语义（§2 深跑门禁）。"""
    idea = conn.execute("SELECT * FROM idea WHERE id=?", (idea_id,)).fetchone()
    if idea is None:
        raise TriageError("IDEA_NOT_FOUND", "想法不存在")
    idea = dict(idea)
    last = conn.execute(
        """SELECT * FROM triage_log WHERE idea_id=? AND verdict IN ('pass','fail','defer')
           ORDER BY decided_at DESC, id DESC LIMIT 1""",
        (idea_id,)).fetchone()
    if last is None or last["verdict"] != "pass":
        raise TriageError("GATE_NOT_PASSED", "分诊结论不是 pass，禁止深跑")
    windows = {w: _window_dates(conn, d) for w, d in L06_WINDOW_DAYS.items()}
    combos = build_run_payloads(json.loads(idea["conditions_json"]), idea["title"], windows)
    from backtest import service as bt_service
    task_ids = []
    for c in combos:
        tid = bt_service.create_task(c["payload"])
        task_ids.append(tid)
    combo_rows = [{"code": c["code"], "task_id": t, "window": c["window"],
                   "cost_tier": c["cost_tier"], "window_days": c["window_days"]}
                  for c, t in zip(combos, task_ids)]
    cur = conn.execute(
        "INSERT INTO l06_run(idea_id, triage_id, combos_json) VALUES (?,?,?)",
        (idea_id, int(last["id"]), json.dumps(combo_rows, ensure_ascii=False)))
    conn.execute("UPDATE idea SET status='run_complete' WHERE id=?", (idea_id,))
    return {"run_id": cur.lastrowid, "combos": combo_rows}


# ════════════════ bundle 指标与死因（§6，纯函数，可单测）════════════════
METRIC_KEYS = ("cumulative_return", "annual_return", "win_rate", "sharpe_ratio",
               "max_drawdown", "total_trades", "win_trades")


def normalize_metrics(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """引擎 result → bundle 指标（**一律带符号小数**，回撤为负）。

    坑位（§6.2）：引擎返回 total_return/annual_return/max_drawdown 已是小数，
    而 strategy_rules 回写列是百分数。bundle 只取引擎小数口径，绝不去规则表取数。
    """
    if not result:
        return None
    out = {
        "cumulative_return": float(result.get("total_return") or 0.0),
        "annual_return": float(result.get("annual_return") or 0.0),
        "win_rate": float(result.get("win_rate") or 0.0),
        "sharpe_ratio": float(result.get("sharpe_ratio") or 0.0),
        "max_drawdown": float(result.get("max_drawdown") or 0.0),
        "total_trades": int(result.get("total_trades") or 0),
        "win_trades": int(result.get("win_trades") or 0),
        "equity_curve": result.get("equity_curve") or [],
    }
    # 单位自证：收益类字段落在 [-1, 5] 视为小数，否则大概率被谁乘了 100
    for k in ("cumulative_return", "annual_return", "max_drawdown"):
        v = out[k]
        if abs(v) > 5:
            raise TriageError("UNIT_MISMATCH", f"{k}={v} 量图像百分数，bundle 只收小数")
    if out["max_drawdown"] > 0:
        raise TriageError("SIGN_MISMATCH", f"max_drawdown={out['max_drawdown']} 应为负小数")
    return out


def decide_death_step(metrics: Dict[str, Optional[Dict[str, Any]]],
                      triage: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """§6.2 顺序短路判定。返回 (death_step, conclusion_text)。"""
    A, B, C = metrics.get("A"), metrics.get("B"), metrics.get("C")
    if triage and triage.get("death_step") in ("LOGIC", "DATA"):
        return str(triage["death_step"]), "分诊阶段已判死，深跑结果仅存档"
    if A is None or B is None or C is None:
        return "DATA", "三跑未全部就绪或窗口不足，无法给出可信对比结论"
    days = (triage or {}).get("window_days_actual") or {}
    if days.get("B") and days["B"] < TH["min_window_days"]:
        return "DATA", f"长窗实际仅 {days['B']} 交易日，不足 {TH['min_window_days']} 日，结论不可用"
    if A["total_trades"] < TH["min_trades"]:
        return "SAMPLE", (f"现实档仅成交 {A['total_trades']} 笔 < {TH['min_trades']}，"
                          f"样本不足以支撑任何结论")
    est = (triage or {}).get("est_signals")
    if est and A["total_trades"] < est * (1 - TH["fill_shrink_ratio"]):
        return "LIQUIDITY", (f"预计 {est} 笔信号但仅成交 {A['total_trades']} 笔："
                             f"想法死于涨跌停/停牌下买不进去的流动性")
    if (C["annual_return"] > TH["ideal_min_annual"]
            and A["annual_return"] <= TH["real_dead_negative"]):
        gap = C["cumulative_return"] - A["cumulative_return"]
        if gap > TH["cost_gap_min"]:
            return "COST", (f"想法本身有信号，死于成本：同期收益被交易成本吃掉 {gap:.1%}"
                            f"（佣金/印花/滑点假设见报告头部快照）")
    same_sign = (A["annual_return"] > 0) == (B["annual_return"] > 0)
    if not same_sign or abs(A["annual_return"] - B["annual_return"]) > TH["regime_gap_min"]:
        return "REGIME", (f"近窗年化 {A['annual_return']:.1%} vs 全窗 {B['annual_return']:.1%}："
                          f"依赖特定市场环境，不是普适 edge")
    if (A["annual_return"] < TH["weak_min_annual"]
            or A["max_drawdown"] < TH["weak_max_drawdown"]
            or A["sharpe_ratio"] < TH["weak_min_sharpe"]):
        return "WEAK", (f"三项都过但强度不足（年化 {A['annual_return']:.1%}、"
                        f"回撤 {A['max_drawdown']:.1%}、Sharpe {A['sharpe_ratio']:.2f}），"
                        f"不值得占用下单额度")
    return "NONE", (f"现实档年化 {A['annual_return']:.1%}、回撤 {A['max_drawdown']:.1%}、"
                    f"Sharpe {A['sharpe_ratio']:.2f} 同框达标 → 进完整体检或小仓试验")


def assemble_bundle(*, run_id: int, idea: Dict[str, Any], triage: Optional[Dict[str, Any]],
                    combos: List[Dict[str, Any]],
                    results: Dict[str, Optional[Dict[str, Any]]],
                    execution_config: Optional[Dict[str, Any]] = None,
                    data_version: Optional[str] = None,
                    cannot_prove: Optional[List[str]] = None) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for code in ("A", "B", "C"):
        m = normalize_metrics(results.get(code))
        metrics[code] = {k: m[k] for k in METRIC_KEYS} if m else None
    cost_gap = None
    if metrics.get("A") and metrics.get("C"):
        cost_gap = round(metrics["C"]["cumulative_return"] - metrics["A"]["cumulative_return"], 4)
    metrics["cost_gap"] = cost_gap
    window_days_actual = {c["code"]: c.get("window_days") for c in combos}
    triage_view = {
        "elapsed_seconds": triage["elapsed_seconds"] if triage else None,
        "verdict": triage["verdict"] if triage else None,
        "attempt": triage["triage_attempt"] if triage else None,
        "over_budget": 1 if triage and triage["elapsed_seconds"] > TRIAGE_BUDGET_SECONDS else 0,
        "checks_json": json.loads(triage["checks_json"]) if triage and triage.get("checks_json") else [],
        "window_days_actual": window_days_actual,
        "est_signals": next((c.get("est_signals") for c in
                             (json.loads(triage["checks_json"]) if triage else [])
                             if c.get("id") == "T4"), None),
    }
    death_step, conclusion = decide_death_step(
        {k: metrics.get(k) for k in ("A", "B", "C")}, triage_view)
    return {
        "bundle_id": run_id, "idea_id": idea["id"], "idea_title": idea["title"],
        "combos": [{k: c[k] for k in ("code", "task_id", "window", "cost_tier", "window_days")}
                   for c in combos],
        "metrics": metrics,
        "death_step": death_step, "conclusion_text": conclusion,
        "execution_config": execution_config or {}, "data_version": data_version,
        "window_clamped": any(
            (window_days_actual.get(c["code"]) or 0) < L06_WINDOW_DAYS[c["window"]]
            for c in combos),
        "window_days_actual": window_days_actual,
        "cannot_prove": cannot_prove if cannot_prove is not None else list(L06_CANNOT_PROVE),
        "triage": triage_view,
    }


# ════════════════ bundle 就绪与落盘 ════════════════
def build_bundle(conn: sqlite3.Connection, run_id: int, *,
                 at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """三跑都到终态才组装并落盘（bundle 行只在就绪时插入，见 007 说明）。"""
    run = conn.execute("SELECT * FROM l06_run WHERE id=?", (run_id,)).fetchone()
    if run is None:
        return None
    run = dict(run)
    combos = json.loads(run["combos_json"])
    existing = conn.execute(
        "SELECT * FROM idea_run_bundle WHERE idea_id=? AND combos_json=? LIMIT 1",
        (run["idea_id"], run["combos_json"])).fetchone()
    if existing:
        # 终稿已存在：直接回放已落盘的 bundle，保证历史结论可复现且不被重算改写
        return _bundle_from_row(dict(existing))

    from backtest import service as bt_service
    results: Dict[str, Optional[Dict[str, Any]]] = {}
    states: Dict[str, str] = {}
    for c in combos:
        t = bt_service.get_task(c["task_id"])
        states[c["code"]] = (t or {}).get("status", "unknown")
        if states[c["code"]] == "done":
            results[c["code"]] = bt_service.get_task_result(c["task_id"])
        elif states[c["code"]] in ("failed", "cancelled"):
            results[c["code"]] = None
        else:
            results[c["code"]] = None
    if any(s not in ("done", "failed", "cancelled") for s in states.values()):
        return {"status": "running", "states": states, "combos": combos,
                "run_id": run_id, "idea_id": run["idea_id"]}

    idea = dict(conn.execute("SELECT * FROM idea WHERE id=?", (run["idea_id"],)).fetchone())
    triage = conn.execute("SELECT * FROM triage_log WHERE id=?", (run["triage_id"],)).fetchone()
    exec_cfg = None
    dv = None
    r = results.get("A")
    if isinstance(r, dict):
        exec_cfg = r.get("_execution_config")
        dv = str(r.get("data_version") or "") or None
    bundle = assemble_bundle(
        run_id=run_id, idea=idea, triage=dict(triage) if triage else None,
        combos=combos, results={k: (v if isinstance(v, dict) else None)
                                for k, v in results.items()},
        execution_config=exec_cfg, data_version=dv)
    bundle["task_states"] = states
    md_path, json_path = write_report(conn, bundle, at=at)
    conn.execute(
        """INSERT INTO idea_run_bundle(idea_id, triage_id, combos_json, death_step,
                                       conclusion_text, metrics_json,
                                       report_md_path, report_json_path)
           VALUES (?,?,?,?,?,?,?,?)""",
        (run["idea_id"], run["triage_id"], json.dumps(combos, ensure_ascii=False),
         bundle["death_step"], bundle["conclusion_text"],
         json.dumps({k: v for k, v in bundle["metrics"].items()}, ensure_ascii=False),
         md_path, json_path))
    conn.execute("UPDATE l06_run SET status='ready', finished_at=? WHERE id=?",
                 (_fmt(at or now_local()), run_id))
    bundle["status"] = "ready"
    return bundle


def _bundle_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(row["report_json_path"], "r", encoding="utf-8") as f:
            b = json.load(f)
        b["status"] = "ready"
        return b
    except (OSError, json.JSONDecodeError):
        return {"status": "ready", "bundle_id": row["id"], "idea_id": row["idea_id"],
                "death_step": row["death_step"], "conclusion_text": row["conclusion_text"],
                "combos": json.loads(row["combos_json"]),
                "metrics": json.loads(row["metrics_json"]), "cannot_prove": list(L06_CANNOT_PROVE)}


def report_root() -> str:
    from config.settings import BASE_DIR
    return os.path.join(str(BASE_DIR), "reports", "l06")


def render_markdown(bundle: Dict[str, Any]) -> str:
    m = bundle["metrics"]
    tri = bundle.get("triage") or {}
    lines: List[str] = [f"# 想法验证报告 · {bundle.get('idea_title', '')}", ""]
    lines.append(f"**结论一句话**：{bundle['conclusion_text']}　"
                 f"**死因**：`{bundle['death_step']}`")
    lines.append("")
    lines.append(f"- 分诊：用时 {tri.get('elapsed_seconds')}s / 预算 {TRIAGE_BUDGET_SECONDS}s，"
                 f"verdict={tri.get('verdict')}，attempt={tri.get('attempt')}，"
                 f"over_budget={tri.get('over_budget')}")
    checks = tri.get("checks_json") or []
    if checks:
        lines.append("- 检查项：" + " ".join(
            f"{c.get('id')}={c.get('result')}"
            + (f"({c.get('coverage'):.0%})" if isinstance(c.get("coverage"), float) else "")
            + (f"({c.get('est_signals')}笔)" if c.get("est_signals") else "")
            for c in checks))
    lines += ["", "## 执行配置与数据版本（同框可比的前提）"]
    wd = bundle.get("window_days_actual") or {}
    lines.append(f"- 窗口：A={wd.get('A')}、B={wd.get('B')}、C={wd.get('C')} 交易日，"
                 f"window_clamped={bundle.get('window_clamped')}")
    ec = bundle.get("execution_config") or {}
    if ec:
        lines.append(f"- 执行快照：settlement={ec.get('settlement')}，buy_timing="
                     f"{ec.get('buy_timing')}，exit_check_timing={ec.get('exit_check_timing')}，"
                     f"stop={ec.get('stop_loss_pct')}，max_hold_days={ec.get('max_hold_days')}，"
                     f"lot_size={ec.get('lot_size')}")
    lines.append(f"- 成本假设：现实档=佣金 {cost_tier('real')['commission_rate']} / 印花 "
                 f"{cost_tier('real')['stamp_tax_rate']} / 滑点 {cost_tier('real')['slippage_rate']}"
                 f"；理想档=三项全 0 且最低佣金 0")
    lines.append(f"- 数据版本：{bundle.get('data_version') or '未记录'}")
    lines += ["", "## 两版同框（近窗：理想 vs 现实）"]
    lines.append("| 指标 | 理想 C | 现实 A | 差 |")
    lines.append("|---|---|---|---|")
    for k in METRIC_KEYS:
        a = (m.get("A") or {}).get(k)
        c = (m.get("C") or {}).get(k)
        gap = (round(c - a, 4) if isinstance(a, (int, float)) and isinstance(c, (int, float))
               else None)
        lines.append(f"| {k} | {c} | {a} | {gap} |")
    lines.append(f"\n`cost_gap = C.cumulative_return − A.cumulative_return = {m.get('cost_gap')}`")
    lines += ["", "## 环境依赖（同成本：近窗 A vs 全窗 B）", "| 指标 | A | B |", "|---|---|---|"]
    for k in METRIC_KEYS:
        lines.append(f"| {k} | {(m.get('A') or {}).get(k)} | {(m.get('B') or {}).get(k)} |")
    lines += ["", "## 本报告不能证明什么"]
    for cp in bundle.get("cannot_prove") or []:
        lines.append(f"- {cp}")
    if not (bundle.get("cannot_prove") or []):
        lines.append("- （缺失：本段为硬要求，报告不合格）")
    return "\n".join(lines) + "\n"


def write_report(conn: sqlite3.Connection, bundle: Dict[str, Any],
                 at: Optional[datetime] = None) -> Tuple[str, str]:
    when = at or now_local()
    slug = "".join(ch if ch.isalnum() else "_" for ch in str(bundle.get("idea_title", "idea")))[:40]
    day = when.strftime("%Y%m%d_%H%M%S")
    root = os.path.join(report_root(), f"{day}_{slug}")
    os.makedirs(root, exist_ok=True)
    md_path = os.path.join(root, "bundle.md")
    json_path = os.path.join(root, "bundle.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(bundle))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2, default=str)
    return md_path, json_path


# ════════════════ 队列与周复盘（§9）════════════════
def list_queue(conn: sqlite3.Connection, status: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM idea"
    args: List[Any] = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT 200"
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        attempt = next_attempt(conn, d["id"])
        open_attempt = min(attempt, MAX_TRIAGE_ATTEMPTS)
        sess = session_for(conn, d["id"], open_attempt)
        events = conn.execute(
            "SELECT attempt, event, occurred_at, reason FROM l06_idea_event "
            "WHERE idea_id=? ORDER BY id", (d["id"],)).fetchall()
        d["triage_attempt_open"] = attempt if attempt <= MAX_TRIAGE_ATTEMPTS else None
        d["deadline_at"] = (sess or {}).get("deadline_at")   # §2.1：每轮全新预算的前端显示口径
        d["events"] = [dict(e) for e in events]
        out.append(d)
    return out


def review_week(conn: sqlite3.Connection, week_start: str, week_end: str) -> Dict[str, Any]:
    """纪要成功判据：≥3/5 想法在 30 分钟内出可行动结论且死因被明说。

    口径：真人结论（pass/fail/defer）取自 triage_log；timeout 取自事件流（不占名额）。
    """
    lo, hi = f"{week_start} 00:00:00", f"{week_end} 23:59:59"
    rows = conn.execute(
        """SELECT t.*, i.title FROM triage_log t JOIN idea i ON i.id=t.idea_id
           WHERE t.started_at BETWEEN ? AND ? AND t.verdict IN ('pass','fail','defer')
           ORDER BY t.id""", (lo, hi)).fetchall()
    timeouts = conn.execute(
        """SELECT e.* FROM l06_idea_event e JOIN idea i ON i.id=e.idea_id
           WHERE e.event='timeout' AND e.occurred_at BETWEEN ? AND ?""", (lo, hi)).fetchall()
    actionable = sum(1 for r in rows if r["verdict"] == "pass"
                     or (r["verdict"] == "fail" and r["death_step"]))
    n = len(rows) + len(timeouts)
    by_verdict: Dict[str, int] = {}
    for r in rows:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
    if timeouts:
        by_verdict["timeout"] = len(timeouts)
    decisions = [r for r in rows if r["verdict"] in ("pass", "fail")]
    return {
        "window": [week_start, week_end], "ideas_triaged": n,
        "by_verdict": by_verdict,
        "actionable_within_budget": actionable,
        "success_criterion": "≥3/5 在 30 分钟内出可行动结论且死因被明说",
        "pass": bool(n) and actionable >= max(1, int(round(n * 0.6))),
        "timeout_rate": round(len(timeouts) / n, 3) if n else None,
        "mean_elapsed_seconds": (round(sum(r["elapsed_seconds"] for r in decisions)
                                       / len(decisions)) if decisions else None),
        "defer_rate": round(by_verdict.get("defer", 0) / n, 3) if n else None,
    }
