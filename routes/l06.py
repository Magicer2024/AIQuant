"""
routes/l06.py —— L06 30 分钟验证闭环 API（T3）
=============================================

spec：shared/specs/L06_spec.md §8；bundle JSON 契约 §8.1（缺字段算后端缺陷）。
原则：判定一律在后端（前端重复校验仅体验）；计时起点由服务端首笔写入且不可变，
      客户端传来的 elapsed/verdict 不作为依据（R3/R4）。
"""
from __future__ import annotations

import json

from flask import Blueprint, request

from utils.api import ok, fail
from core.db import get_conn
from backtest import l06
from backtest.l06 import TriageError
from config.l06_params import L06_CANNOT_PROVE, TRIAGE_BUDGET_SECONDS

l06_bp = Blueprint("l06", __name__, url_prefix="/api/l06")


def _err(exc: TriageError):
    return fail(exc.message, http=409, error_code=exc.error_code)


# ──────────── 想法录入（计时起点） ────────────
@l06_bp.route("/idea", methods=["POST"])
def create_idea():
    p = request.get_json(force=True, silent=True) or {}
    title = str(p.get("title", "")).strip()
    hypothesis = str(p.get("hypothesis", "")).strip()
    conditions = p.get("conditions") or []
    if not (4 <= len(title) <= 120):
        return fail("标题需 4–120 字", http=422, error_code="TITLE_LENGTH")
    if len(hypothesis) < 10:
        return fail("假设必须是一句人话（≥10 字）", http=422, error_code="HYPOTHESIS_REQUIRED")
    if not isinstance(conditions, list) or not conditions:
        return fail("至少一条选股条件", http=422, error_code="CONDITIONS_REQUIRED")
    indicators = sorted({str(c.get("indicator")) for c in conditions if c.get("indicator")})
    implied = p.get("implied_hold_days")
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO idea(title, hypothesis, conditions_json, indicators_json,
                                implied_hold_days, status)
               VALUES (?,?,?,?,?,'triaging')""",
            (title, hypothesis, json.dumps(conditions, ensure_ascii=False),
             json.dumps(indicators, ensure_ascii=False),
             int(implied) if implied is not None else None),
        )
        idea_id = int(cur.lastrowid)
        sess = l06.open_session(c, idea_id, 1)
        c.commit()
    return ok({"idea_id": idea_id, "status": "triaging",
               "triage_started_at": sess["started_at"], "deadline_at": sess["deadline_at"],
               "budget_seconds": TRIAGE_BUDGET_SECONDS})


# ──────────── 开启下一轮分诊（defer → 全新 1800s 预算，§2.1） ────────────
@l06_bp.route("/idea/<int:idea_id>/triage/start", methods=["POST"])
def start_triage(idea_id: int):
    with get_conn() as c:
        row = c.execute("SELECT * FROM idea WHERE id=?", (idea_id,)).fetchone()
        if row is None:
            return fail("想法不存在", http=404, error_code="IDEA_NOT_FOUND")
        try:
            sess = l06.open_next_session(c, idea_id)
            c.execute("UPDATE idea SET status='triaging' WHERE id=?", (idea_id,))
            c.commit()
        except TriageError as exc:
            return _err(exc)
        attempt = l06.next_attempt(c, idea_id)
    return ok({"idea_id": idea_id, "attempt": attempt,
               "triage_started_at": sess["started_at"], "deadline_at": sess["deadline_at"],
               "budget_seconds": TRIAGE_BUDGET_SECONDS})


# ──────────── 分诊闸 ────────────
@l06_bp.route("/idea/<int:idea_id>/triage", methods=["POST"])
def submit_triage(idea_id: int):
    p = request.get_json(force=True, silent=True) or {}
    verdict = str(p.get("verdict", "")).strip()
    if verdict not in ("pass", "fail", "defer"):
        return fail("verdict 必须是 pass/fail/defer", http=422, error_code="VERDICT_REQUIRED")
    with get_conn() as c:
        row = c.execute("SELECT * FROM idea WHERE id=?", (idea_id,)).fetchone()
        if row is None:
            return fail("想法不存在", http=404, error_code="IDEA_NOT_FOUND")
        idea = dict(row)
        if idea["status"] in ("passed", "failed", "run_complete"):
            return fail("该想法分诊已终局", http=409, error_code="IDEA_TRIAGE_CLOSED")

        # 服务端重算快检（T1/T2 为硬门，不接受客户端自述覆盖）
        checks = l06.run_fast_checks(c, idea, buy_timing=p.get("buy_timing"))
        hard_fail = next((ck for ck in checks
                          if ck["result"] == "fail" and ck.get("id") in ("T1", "T2")), None)
        if hard_fail is not None and verdict == "pass":
            return fail(f"服务端重算未过闸：{hard_fail['reason']}",
                        http=409, error_code="TRIAGE_RECOMPUTE_FAILED",
                        failed_check=hard_fail["id"])
        server_verdict, server_death, server_defer = l06.suggest_verdict(checks)
        overlap = next((ck.get("overlap_rule_id") for ck in checks
                        if ck.get("id") == "T6"), None)
        if verdict == "pass" and server_verdict == "fail":
            return fail(f"服务端重算判死：{server_death} —— 需先修条件再提交",
                        http=409, error_code="TRIAGE_RECOMPUTE_FAILED")
        if verdict == "pass" and server_verdict == "defer":
            return fail("数据可得性未过（defer 项未补齐），不能过闸",
                        http=409, error_code="DATA_INSUFFICIENT")
        death_step = p.get("death_step") or server_death
        defer_reason = p.get("defer_reason") or server_defer
        if verdict == "fail" and not death_step:
            return fail("判死必须给出死因码", http=422, error_code="DEATH_STEP_REQUIRED")
        try:
            log = l06.record_triage(
                c, idea_id=idea_id, verdict=verdict, checks=checks,
                death_step=death_step if verdict == "fail" else None,
                defer_reason=defer_reason if verdict == "defer" else None,
                overlap_rule_id=overlap)
            c.commit()
        except TriageError as exc:
            return _err(exc)
        out = dict(log)
        out["checks_json"] = json.loads(log["checks_json"])
        nxt = l06.session_for(c, idea_id, int(log["triage_attempt"]) + 1)
    return ok({"triage_id": out["id"], "verdict": out["verdict"],
               "attempt": out["triage_attempt"], "elapsed_seconds": out["elapsed_seconds"],
               "over_budget": int(out["elapsed_seconds"]) > TRIAGE_BUDGET_SECONDS,
               "death_step": out["death_step"], "checks": out["checks_json"],
               "next_attempt_deadline_at": (nxt or {}).get("deadline_at")})


# ──────────── 一题三跑 ────────────
@l06_bp.route("/idea/<int:idea_id>/run", methods=["POST"])
def submit_run(idea_id: int):
    with get_conn() as c:
        try:
            res = l06.submit_run(c, idea_id)
            c.commit()
        except TriageError as exc:
            return _err(exc)
    return ok(res)


@l06_bp.route("/bundle/<int:run_id>", methods=["GET"])
def get_bundle(run_id: int):
    with get_conn() as c:
        bundle = l06.build_bundle(c, run_id)
        c.commit()
    if bundle is None:
        return fail("运行记录不存在", http=404, error_code="RUN_NOT_FOUND")
    if bundle.get("status") == "running":
        return ok({"status": "running", "run_id": run_id, "combos": bundle["combos"],
                   "task_states": bundle["states"], "cannot_prove": list(L06_CANNOT_PROVE)})
    return ok(bundle)


# ──────────── 队列与周复盘 ────────────
@l06_bp.route("/queue", methods=["GET"])
def queue():
    status = request.args.get("status")
    with get_conn(readonly=True) as c:
        return ok({"items": l06.list_queue(c, status)})


@l06_bp.route("/review", methods=["GET"])
def review():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return fail("需 start/end 日期（YYYY-MM-DD）", http=422, error_code="WINDOW_REQUIRED")
    with get_conn(readonly=True) as c:
        return ok(l06.review_week(c, start, end))
