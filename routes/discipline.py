"""
routes/discipline.py —— JH4 纪律装置包 + L09' 封存盲核 API（T2/T4）
====================================================================

spec：shared/specs/JH4_spec.md §8（管理A 已批准三条裁定）。
原则：
  - 校验以后端为准（前端重复做一遍仅体验）；错误码用 spec 建议枚举。
  - 盲核顺序约束在后端：blind 响应绝不含封存字段，reveal 必须有作答（R10）。
  - 不接券商、不物理拦截；本 API 只生产「不可逆证据件」。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from flask import Blueprint, request

from utils.api import ok, fail
from core.db import get_conn
from core import discipline as disc
from core.discipline_calibration import run_calibration

discipline_bp = Blueprint("discipline", __name__, url_prefix="/api")

_DEFAULT_RISK_PARAMS = {"stop_loss_pct": -0.05, "trailing_pct": 0.10, "max_hold_days": 15}


def _row(r):
    return dict(r) if r is not None else None


def _current_calibration(c):
    row = c.execute("SELECT * FROM risk_param_calibration ORDER BY id DESC LIMIT 1").fetchone()
    return row


# ──────────── 交易计划（理由卡） ────────────
@discipline_bp.route("/plan", methods=["POST"])
def create_plan():
    p = request.get_json(force=True, silent=True) or {}
    errs = []
    code = str(p.get("code", "")).zfill(6)
    with get_conn(readonly=True) as c:
        if not c.execute("SELECT 1 FROM stock_info WHERE code=?", (code,)).fetchone():
            return fail(f"标的 {code} 不在股票列表", http=422, error_code="CODE_UNKNOWN")
    anchors = p.get("anchors") or []
    errs += disc.validate_anchors(anchors)
    thesis = str(p.get("thesis", ""))
    if not (15 <= len(thesis) <= 500):
        errs.append("THESIS_LENGTH")
    conf = p.get("confidence")
    if conf is None or not (0.05 <= float(conf) <= 0.95):
        errs.append("CONFIDENCE_RANGE")
    horizon = p.get("expected_horizon_days")
    if horizon is None or not (1 <= int(horizon) <= 60):
        errs.append("HORIZON_RANGE")
    if p.get("expected_return_pct") is None:
        errs.append("EXPECTED_RETURN_REQUIRED")
    if errs:
        return fail("理由卡校验未通过", http=422, failed=errs)

    with get_conn() as c:
        if disc.thesis_duplicate(c, code, thesis):
            if not str(p.get("delta_note", "")).strip():
                return fail("与近期卡片雷同，需 delta_note 写明这次与上次不同在哪",
                            http=422, error_code="THESIS_DUPLICATE_NEEDS_DELTA")
        # 仓位上限：min(校准建议, MAX_SINGLE_POSITION_PCT)
        from config.personal_config import MAX_SINGLE_POSITION_PCT
        cal = _current_calibration(c)
        cap = MAX_SINGLE_POSITION_PCT
        suggested_stop = _DEFAULT_RISK_PARAMS["stop_loss_pct"]
        params_source = "default"
        calibration_id = None
        if cal is not None and cal["status"] in ("calibrated", "pooled", "low_sample"):
            if cal["max_single_position_pct"]:
                cap = min(cap, float(cal["max_single_position_pct"]))
            if cal["stop_loss_pct"]:
                suggested_stop = float(cal["stop_loss_pct"])
                params_source = "calibrated"
                calibration_id = cal["id"]
        pos_pct = float(p.get("position_pct") or 0)
        if pos_pct <= 0:
            return fail("position_pct 必填且 >0", http=422, error_code="POSITION_REQUIRED")
        if pos_pct > cap:
            return fail(f"仓位超上限 {cap:.2f}（校准建议与硬上限取小）", http=422,
                        error_code="POSITION_OVER_CAP")
        rp = dict(_DEFAULT_RISK_PARAMS)
        given = p.get("risk_params") or {}
        manual_keys = {k for k in rp if given.get(k) is not None}
        rp.update({k: given[k] for k in manual_keys})
        if manual_keys:
            params_source = "manual"
        elif params_source != "calibrated":
            params_source = "default"
        try:
            from strategy.market_regime import compute_market_regime
            regime = compute_market_regime(c)
        except Exception:
            regime = None
        atr_val = None
        try:
            import pandas as pd
            from strategy.exit_advisor import atr_dynamic_stop_pct
            df = pd.read_sql_query(
                "SELECT trade_date, high, low, close FROM daily_price WHERE code=? "
                "ORDER BY trade_date", c, params=(code,)).set_index("trade_date")
            atr_val = atr_dynamic_stop_pct(df)
        except Exception:
            atr_val = None
        entry = p.get("entry_trigger") or {}
        if "max_buy_price" not in entry:
            return fail("entry_trigger.max_buy_price 必填（追价上限）", http=422,
                        error_code="ENTRY_TRIGGER_REQUIRED")
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status0 = "alive" if p.get("initial_status", "alive") == "alive" else "draft"
        pid, h = disc.insert_hashed(c, "trade_plan", {
            "parent_id": None, "revision": 1, "code": code, "name": _stock_name(c, code),
            "status": status0, "thesis": thesis, "delta_note": p.get("delta_note"),
            "entry_trigger_json": json.dumps(entry, ensure_ascii=False),
            "expected_return_pct": float(p["expected_return_pct"]),
            "expected_horizon_days": int(horizon), "confidence": float(conf),
            "position_pct": pos_pct,
            "risk_params_json": json.dumps(rp, ensure_ascii=False),
            "params_source": params_source, "calibration_id": calibration_id,
            "regime_at_plan": str(regime) if regime is not None else None,
            "atr_stop_at_plan": atr_val, "created_at": now_s})
        for a in anchors:
            c.execute(
                """INSERT INTO plan_anchor (plan_id, anchor_kind, indicator, comparator,
                   threshold_num, unit, check_expr_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, a["anchor_kind"], a.get("indicator"), a.get("comparator"),
                 float(a["threshold_num"]), a.get("unit"),
                 json.dumps(a.get("check_expr") or {}, ensure_ascii=False)))
    return ok({"plan_id": pid, "content_hash": h, "anchors": len(anchors),
               "params_source": params_source})


def _stock_name(c, code):
    r = c.execute("SELECT name FROM stock_info WHERE code=?", (code,)).fetchone()
    return r["name"] if r else None


# 注：trade_plan 状态机 draft→alive 由修订/激活走 revise；建卡默认直接 alive（自用承诺件）。
# trigger 中 status 不在 payload 哈希集（_HASH_EXCLUDE 语义），状态推进不影响链。


@discipline_bp.route("/plan/list", methods=["GET"])
def plan_list():
    status = request.args.get("status", "alive")
    with get_conn(readonly=True) as c:
        if status == "all":
            plans = c.execute("SELECT * FROM trade_plan ORDER BY id DESC LIMIT 200").fetchall()
        else:
            plans = c.execute("SELECT * FROM trade_plan WHERE status=? ORDER BY id DESC LIMIT 200",
                              (status,)).fetchall()
        out = []
        for p_ in plans:
            anchors = c.execute("SELECT * FROM plan_anchor WHERE plan_id=?", (p_["id"],)).fetchall()
            d = dict(p_)
            d["anchors"] = [dict(a) for a in anchors]
            d["anchor_dead"] = sum(1 for a in anchors if a["state"] == "dead")
            out.append(d)
    return ok({"plans": out, "status_filter": status})


@discipline_bp.route("/plan/<int:plan_id>/revise", methods=["POST"])
def revise_plan(plan_id: int):
    p = request.get_json(force=True, silent=True) or {}
    with get_conn() as c:
        old = c.execute("SELECT * FROM trade_plan WHERE id=?", (plan_id,)).fetchone()
        if old is None:
            return fail("计划不存在", http=404)
        # 修订=新行；原件 status 不动（invalidated 等历史留痕）
        thesis = str(p.get("thesis", old["thesis"]))
        errs = disc.validate_anchors(p["anchors"]) if "anchors" in p else []
        if errs:
            return fail("锚校验未通过", http=422, failed=errs)
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_id, _h = disc.insert_hashed(c, "trade_plan", {
            "parent_id": old["id"], "revision": old["revision"] + 1,
            "code": old["code"], "name": old["name"], "status": "alive",
            "thesis": thesis, "delta_note": p.get("delta_note") or old["delta_note"],
            "entry_trigger_json": json.dumps(
                p.get("entry_trigger") or json.loads(old["entry_trigger_json"]),
                ensure_ascii=False),
            "expected_return_pct": float(p.get("expected_return_pct", old["expected_return_pct"])),
            "expected_horizon_days": int(p.get("expected_horizon_days", old["expected_horizon_days"])),
            "confidence": float(p.get("confidence", old["confidence"])),
            "position_pct": float(p.get("position_pct", old["position_pct"])),
            "risk_params_json": json.dumps(
                p.get("risk_params") or json.loads(old["risk_params_json"]), ensure_ascii=False),
            "params_source": old["params_source"], "calibration_id": old["calibration_id"],
            "regime_at_plan": old["regime_at_plan"], "atr_stop_at_plan": old["atr_stop_at_plan"],
            "created_at": now_s})
        anchors = p.get("anchors")
        if anchors is None:
            for a in c.execute("SELECT * FROM plan_anchor WHERE plan_id=?", (plan_id,)).fetchall():
                c.execute("""INSERT INTO plan_anchor (plan_id, anchor_kind, indicator,
                    comparator, threshold_num, unit, check_expr_json)
                    VALUES (?,?,?,?,?,?,?)""",
                    (new_id, a["anchor_kind"], a["indicator"], a["comparator"],
                     a["threshold_num"], a["unit"], a["check_expr_json"]))
        else:
            for a in anchors:
                c.execute("""INSERT INTO plan_anchor (plan_id, anchor_kind, indicator,
                    comparator, threshold_num, unit, check_expr_json)
                    VALUES (?,?,?,?,?,?,?)""",
                    (new_id, a["anchor_kind"], a.get("indicator"), a.get("comparator"),
                     float(a["threshold_num"]), a.get("unit"),
                     json.dumps(a.get("check_expr") or {}, ensure_ascii=False)))
        # 原 alive 卡作废为 stale（状态机 alive→stale 合法；修订承接后旧卡不再存活）
        if old["status"] == "alive":
            c.execute("UPDATE trade_plan SET status='stale', updated_at=? WHERE id=?",
                      (now_s, plan_id))
    return ok({"new_plan_id": new_id, "parent_id": plan_id, "revision": old["revision"] + 1})


@discipline_bp.route("/plan/check-anchors", methods=["POST"])
def check_anchors():
    with get_conn() as c:
        res = disc.check_all_anchors(c)
    return ok(res)


# ──────────── 进场偏离单 ────────────
@discipline_bp.route("/deviation", methods=["POST"])
def submit_deviation():
    p = request.get_json(force=True, silent=True) or {}
    code = str(p.get("code", "")).zfill(6)
    direction = p.get("direction")
    if direction not in ("buy", "sell", "stop_change"):
        return fail("direction 必须为 buy/sell/stop_change", http=422)
    qty = float(p.get("qty", 0))
    price = float(p.get("price", 0))
    tier = p.get("tier", "24h")
    with get_conn() as c:
        res = disc.evaluate_deviation(
            c, code=code, direction=direction,
            qty=qty if direction != "sell" else -abs(qty),
            price=price, stop_old=p.get("stop_old"), stop_new=p.get("stop_new"))
        if not res["is_deviation"]:
            return ok({"is_deviation": False, "action_class": res["action_class"],
                       "exempt": res["action_class"] == disc.EXIT_RISK_REDUCING,
                       "delay_seconds": 0.0,   # 计划内单直接放行（spec §4.1），无延迟语义
                       "required_seal": res["required_seal"],
                       "seal_triggers": res["seal_triggers"]})
        if not str(p.get("intent_reason", "")).strip():
            return fail("偏离单必须当场写理由（不许事后补）", http=422,
                        error_code="INTENT_REASON_REQUIRED")
        if res["action_class"] == disc.ENTRY and res["required_seal"]:
            # 高风险须事前封存：后端只认库内近 24h 未对照的封存件，不认客户端旗标
            fresh_seal = c.execute(
                "SELECT 1 FROM sealed_decision WHERE code=? AND status!='reconciled' "
                "AND sealed_at>=datetime('now','localtime','-1 day') LIMIT 1", (code,)).fetchone()
            if fresh_seal is None:
                return fail("高风险意向：先封存（POST /api/l09/seal）再提交偏离单", http=422,
                            error_code="SEAL_REQUIRED", seal_triggers=res["seal_triggers"])
        info = disc.insert_deviation_ticket(
            c, plan_id=res["plan_id"], code=code, action=direction, qty=int(abs(qty)),
            price_ref=price, amount_ref=abs(qty) * price, rules=res["rules"],
            tier=tier, intent_reason=str(p.get("intent_reason")))
    return ok({"is_deviation": True, "rules": res["rules"],
               "action_class": res["action_class"], "tier": tier, **info,
               "required_seal": res["required_seal"], "seal_triggers": res["seal_triggers"]})


def _get_ticket(c, tid):
    return c.execute("SELECT * FROM deviation_ticket WHERE id=?", (tid,)).fetchone()


@discipline_bp.route("/deviation/<int:tid>/confirm", methods=["POST"])
def confirm_deviation(tid: int):
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        t = _get_ticket(c, tid)
        if t is None:
            return fail("偏离单不存在", http=404)
        if t["status"] != "pending":
            return fail(f"状态 {t['status']} 不可复核", http=409)
        if now_s > t["expire_at"]:
            c.execute("UPDATE deviation_ticket SET status='expired' WHERE id=?", (tid,))
            disc.archive_case(c, dict(t) | {"status": "expired"}, now_s)
            return fail("已过放行窗口，本单逾期失效不可复活；要重买请新建偏离单",
                        http=409, error_code="EXPIRED")
        if now_s < t["release_at"]:
            return fail("未到放行时间（24h+下一交易日开盘较晚者）", http=409,
                        error_code="NOT_IN_RELEASE_WINDOW")
        last = c.execute("SELECT close FROM daily_price WHERE code=? "
                         "ORDER BY trade_date DESC LIMIT 1", (t["code"],)).fetchone()
        if last and t["price_ref"] and float(last["close"]) > float(t["price_ref"]) * (
                1 + disc.DRIFT_UP_PCT):
            c.execute("UPDATE deviation_ticket SET status='voided_drift', void_reason=? WHERE id=?",
                      (f"复核时收盘 {float(last['close']):.3f} 已偏离参考价超 {disc.DRIFT_UP_PCT:.0%}",
                       tid))
            t2 = _get_ticket(c, tid)
            disc.archive_case(c, t2, now_s)
            return fail("价格漂移守卫触发：冷静期不是追高授权期", http=409,
                        error_code="PRICE_DRIFT_VOID")
        c.execute("UPDATE deviation_ticket SET status='released' WHERE id=?", (tid,))
    return ok({"ticket_id": tid, "status": "released",
               "expire_at": t["expire_at"], "note": "放行后当日 15:00 前未执行将自动作废"})


@discipline_bp.route("/deviation/<int:tid>/execute", methods=["POST"])
def execute_deviation(tid: int):
    p = request.get_json(force=True, silent=True) or {}
    trade_id = p.get("executed_trade_id")
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        t = _get_ticket(c, tid)
        if t is None:
            return fail("偏离单不存在", http=404)
        if t["status"] != "released":
            return fail(f"状态 {t['status']} 不可标记执行", http=409)
        if trade_id is not None and not c.execute(
                "SELECT 1 FROM personal_trade WHERE id=?", (trade_id,)).fetchone():
            return fail("executed_trade_id 不存在", http=422)
        c.execute("UPDATE deviation_ticket SET status='executed', executed_trade_id=? WHERE id=?",
                  (trade_id, tid))
        disc.archive_case(c, dict(t) | {"status": "executed"}, now_s)
        if trade_id is not None:
            # 执行且该单在 seal 前未封存 → 违规留痕（偏离执行留证）
            sealed = c.execute("SELECT 1 FROM sealed_decision WHERE ticket_id=? LIMIT 1",
                               (tid,)).fetchone()
            if not sealed:
                hi = c.execute("SELECT 1 FROM personal_trade WHERE id=? AND pnl_pct<0 LIMIT 1",
                               (trade_id,)).fetchone()
                c.execute("INSERT INTO discipline_violation (plan_id,ticket_id,kind,detected_at,note) "
                          "VALUES (?,?,?,?,?)",
                          (t["plan_id"], tid, "deviation_executed", now_s,
                           "偏离单执行" + ("且首记亏损" if hi else "")))
    return ok({"ticket_id": tid, "status": "executed"})


@discipline_bp.route("/deviation/<int:tid>/withdraw", methods=["POST"])
def withdraw_deviation(tid: int):
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        t = _get_ticket(c, tid)
        if t is None:
            return fail("偏离单不存在", http=404)
        if t["status"] != "pending":
            return fail(f"状态 {t['status']} 不可放弃", http=409)
        c.execute("UPDATE deviation_ticket SET status='withdrawn' WHERE id=?", (tid,))
        disc.archive_case(c, dict(t) | {"status": "withdrawn"}, now_s)
    return ok({"ticket_id": tid, "status": "withdrawn"})


@discipline_bp.route("/deviation/pending", methods=["GET"])
def pending_deviation():
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn(readonly=True) as c:
        rows = c.execute("SELECT * FROM deviation_ticket WHERE status IN ('pending','released') "
                         "ORDER BY release_at").fetchall()
        out = []
        for t in rows:
            d = dict(t)
            d["seconds_to_release"] = max(0, int((_dt(t["release_at"]) -
                                                  datetime.now()).total_seconds()))
            d["seconds_to_expire"] = max(0, int((_dt(t["expire_at"]) -
                                                 datetime.now()).total_seconds()))
            out.append(d)
    return ok({"tickets": out, "now": now_s})


def _dt(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


@discipline_bp.route("/case-file", methods=["GET"])
def case_file():
    week = request.args.get("week")  # 2026-42
    with get_conn(readonly=True) as c:
        if week:
            iso_start = datetime.fromisocalendar(int(week[:4]), int(week.split("-")[1]), 1)
            iso_end = datetime.fromisocalendar(int(week[:4]), int(week.split("-")[1]), 7)
            tickets = c.execute(
                "SELECT * FROM deviation_ticket WHERE submitted_at>=? AND submitted_at<=? "
                "AND tier='24h' ORDER BY id",
                (iso_start.strftime("%Y-%m-%d"), iso_end.strftime("%Y-%m-%d 23:59:59"))).fetchall()
        else:
            tickets = c.execute("SELECT * FROM deviation_ticket WHERE tier='24h' "
                                "ORDER BY id DESC LIMIT 500").fetchall()
        by_status: dict[str, int] = {}
        for t in tickets:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        denom = sum(by_status.get(s, 0) for s in ("expired", "released", "executed",
                                                  "abandoned", "voided_drift"))
        expire_rate = (by_status.get("expired", 0) / denom) if denom else None
        cases = c.execute(
            "SELECT cf.*, t.submitted_at FROM case_file cf "
            "JOIN deviation_ticket t ON t.id=cf.ticket_id ORDER BY cf.id DESC LIMIT 200").fetchall()
    return ok({"week": week, "ticket_count": len(tickets), "by_status": by_status,
               "auto_expire_rate": round(expire_rate, 4) if expire_rate is not None else None,
               "cases": [dict(r) for r in cases]})


# ──────────── 校准 ────────────
@discipline_bp.route("/calibration/run", methods=["POST"])
def calibration_run():
    with get_conn() as c:
        res = run_calibration(c)
    return ok(res)


@discipline_bp.route("/calibration/current", methods=["GET"])
def calibration_current():
    with get_conn(readonly=True) as c:
        cal = _current_calibration(c)
    if cal is None:
        return ok({"status": "uncalibrated", "stop_loss_pct": None,
                   "max_single_position_pct": None,
                   "note": "尚无校准记录，沿用默认风控参数"})
    d = dict(cal)
    if d["status"] == "uncalibrated":
        # R8：冷启动不给数字，前端不得显示建议值
        d["stop_loss_pct"] = None
        d["max_single_position_pct"] = None
        d["note"] = "无个人样本，沿用默认"
    return ok(d)


# ──────────── T4｜L09' 封存与盲核 ────────────
@discipline_bp.route("/l09/seal", methods=["POST"])
def seal_decision():
    p = request.get_json(force=True, silent=True) or {}
    code = str(p.get("code", "")).zfill(6)
    falsifier = str(p.get("falsifier", ""))
    if len(falsifier) < 15:
        return fail("falsifier 必填且 ≥15 字：这个判断可能错在哪", http=422,
                    error_code="FALSIFIER_REQUIRED")
    conf = p.get("confidence")
    if conf is None or not (0.05 <= float(conf) <= 0.95):
        return fail("confidence 必须 0.05–0.95（禁止 1.0）", http=422,
                    error_code="CONFIDENCE_RANGE")
    with get_conn() as c:
        plan = None
        if p.get("plan_id"):
            plan = c.execute("SELECT * FROM trade_plan WHERE id=?", (p["plan_id"],)).fetchone()
        else:
            plan = disc.alive_plan(c, code)
        thesis = str(p.get("thesis_snapshot") or (plan["thesis"] if plan else "") or "")
        if not thesis:
            return fail("无可用的 thesis（请附 plan_id 或 thesis_snapshot）", http=422)
        # 反复制：与该 code 上次封存件查重
        prevs = c.execute("SELECT falsifier FROM sealed_decision WHERE code=? ORDER BY id",
                          (code,)).fetchall()
        from difflib import SequenceMatcher
        for r in prevs:
            if SequenceMatcher(None, r["falsifier"], falsifier).ratio() >= 0.9:
                return fail("反证与上次封存件雷同（套话检测）", http=422,
                            error_code="FALSIFIER_DUPLICATE")
        horizon = int(p.get("expected_horizon_days") or (plan["expected_horizon_days"]
                                                          if plan else 10))
        ticket_id = p.get("ticket_id")
        triggers = list(p.get("triggers") or [])
        if ticket_id is not None and "H6" not in triggers:
            t = _get_ticket(c, int(ticket_id))
            if t and t["status"] == "released":
                triggers.append("H6")
        auto = disc.evaluate_high_risk(
            c, code=code, amount=float(p.get("qty", 0)) * float(p.get("ref_price", 0)),
            direction=str(p.get("action", "buy")),
            stop_old=p.get("stop_old"), stop_new=p.get("stop_new"),
            ticket_id=int(ticket_id) if ticket_id else None)
        triggers = sorted(set(triggers) | set(auto))
        sealed_at = datetime.now()
        sealed_s = sealed_at.strftime("%Y-%m-%d %H:%M:%S")
        due_on = disc.trading_days_after(sealed_at.strftime("%Y-%m-%d"),
                                         min(max(horizon, 1), 20))
        payload = {"code": code, "action": p.get("action", "buy"),
                   "qty": p.get("qty"), "ref_price": p.get("ref_price"),
                   "triggers": triggers, "thesis_snapshot": thesis,
                   "expected_return_pct": p.get("expected_return_pct")
                   or (plan["expected_return_pct"] if plan else 0),
                   "expected_horizon_days": horizon, "confidence": float(conf),
                   "worst_case_pct": p.get("worst_case_pct"), "falsifier": falsifier,
                   "seal_note": p.get("seal_note"), "due_on": due_on,
                   "sealed_at": sealed_s}
        if payload["worst_case_pct"] is None:
            return fail("worst_case_pct 必填（事前想象的最坏情形）", http=422,
                        error_code="WORST_CASE_REQUIRED")
        sid, h = disc.insert_hashed(c, "sealed_decision", {
            "plan_id": plan["id"] if plan else None, "ticket_id": ticket_id,
            "code": code, "action": payload["action"], "qty": int(payload["qty"]),
            "ref_price": float(payload["ref_price"]),
            "triggers_json": json.dumps(triggers), "thesis_snapshot": thesis,
            "expected_return_pct": float(payload["expected_return_pct"]),
            "expected_horizon_days": horizon, "confidence": float(conf),
            "worst_case_pct": float(p["worst_case_pct"]), "falsifier": falsifier,
            "seal_note": p.get("seal_note"), "status": "sealed",
            "due_on": due_on, "sealed_at": sealed_s})
    return ok({"sealed_id": sid, "triggers": triggers, "due_on": due_on,
               "content_hash": h})


@discipline_bp.route("/l09/due", methods=["GET"])
def l09_due():
    with get_conn() as c:
        disc.sweep_sealed(c)
        rows = c.execute("SELECT id, code, status, due_on FROM sealed_decision "
                         "WHERE status IN ('due','blinding') ORDER BY due_on").fetchall()
    # R10：只给 id/code——不含任何封存字段
    return ok({"due": [dict(r) for r in rows]})


@discipline_bp.route("/l09/<int:sid>/blind", methods=["GET"])
def l09_blind(sid: int):
    with get_conn() as c:
        s = c.execute("SELECT * FROM sealed_decision WHERE id=?", (sid,)).fetchone()
        if s is None:
            return fail("封存件不存在", http=404)
        if s["status"] == "sealed":
            return fail("未到期，无可核对内容", http=409, error_code="NOT_DUE_YET")
        if s["status"] == "due":
            c.execute("UPDATE sealed_decision SET status='blinding' WHERE id=?", (sid,))
        answered = c.execute("SELECT * FROM sealed_blind_check WHERE sealed_id=?",
                             (sid,)).fetchall()
        metrics = disc.compute_result_metrics(c, s)
    if answered:
        return ok({"sealed_id": sid, "already_answered": True,
                   "result_metrics": json.loads(answered[0]["result_metrics_json"]),
                   "answers": [{"verdict": a["recall_verdict"],
                                "recall_expected_return_pct": a["recall_expected_return_pct"],
                                "answered_at": a["answered_at"]} for a in answered]})
    return ok({"sealed_id": sid, "already_answered": False, "result_metrics": metrics,
               "note": "先作答（POST blind/answer），再揭示原件"})


@discipline_bp.route("/l09/<int:sid>/blind", methods=["POST"])
def l09_blind_submit(sid: int):
    """别名：前端A 契约「POST /blind 带答案」与 /blind/answer 等价（seq 235 对齐点②）。"""
    return l09_blind_answer(sid)


@discipline_bp.route("/l09/<int:sid>/blind/answer", methods=["POST"])
def l09_blind_answer(sid: int):
    p = request.get_json(force=True, silent=True) or {}
    if p.get("recall_verdict") not in ("yes", "no", "partial"):
        return fail("recall_verdict 必须 yes/no/partial", http=422)
    if p.get("recall_expected_return_pct") is None or not str(p.get("recall_reason", "")).strip():
        return fail("三字段全部必填（我现在记得当时写的预期是多少 + 我的解释）", http=422)
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        s = c.execute("SELECT * FROM sealed_decision WHERE id=?", (sid,)).fetchone()
        if s is None:
            return fail("封存件不存在", http=404)
        if s["status"] == "sealed":
            return fail("未到期", http=409, error_code="NOT_DUE_YET")
        dup = c.execute("SELECT 1 FROM sealed_blind_check WHERE sealed_id=? LIMIT 1",
                        (sid,)).fetchone()
        if dup is not None or s["status"] == "reconciled":
            return fail("已作答，不可重复提交（盲评一次定稿）", http=409,
                        error_code="BLIND_ALREADY_ANSWERED")
        if s["status"] == "due":
            c.execute("UPDATE sealed_decision SET status='blinding' WHERE id=?", (sid,))
        metrics = disc.compute_result_metrics(c, s)
        c.execute(
            """INSERT INTO sealed_blind_check (sealed_id, result_metrics_json, recall_verdict,
               recall_expected_return_pct, recall_reason, answered_at)
               VALUES (?,?,?,?,?,?)""",
            (sid, json.dumps(metrics), p["recall_verdict"],
             float(p["recall_expected_return_pct"]), str(p["recall_reason"])[:200], now_s))
    return ok({"sealed_id": sid, "answered_at": now_s})


@discipline_bp.route("/l09/<int:sid>/reveal", methods=["POST"])
def l09_reveal(sid: int):
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        s = c.execute("SELECT * FROM sealed_decision WHERE id=?", (sid,)).fetchone()
        if s is None:
            return fail("封存件不存在", http=404)
        blind = c.execute("SELECT * FROM sealed_blind_check WHERE sealed_id=? "
                          "AND revealed_at IS NULL ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if blind is None:
            return fail("必须盲评（先 blind/answer 后 reveal）", http=409,
                        error_code="BLIND_ANSWER_REQUIRED")
        cmp_ = disc.reveal_compare(c, s, blind)
        c.execute(
            """UPDATE sealed_blind_check SET gap_expected=?, confidence_hit=?,
               worst_case_hit=?, explanation_diverged=?, revealed_at=? WHERE id=?""",
            (cmp_["gap_expected"], cmp_["confidence_hit"], cmp_["worst_case_hit"],
             cmp_["explanation_diverged"], now_s, blind["id"]))
        c.execute("UPDATE sealed_decision SET status='reconciled' WHERE id=?", (sid,))
        out = {"sealed": dict(s), "my_recall": dict(blind), "comparison": cmp_,
               "content_hash": s["content_hash"]}
    return ok(out)


# ──────────── 链完整性（R11） ────────────
@discipline_bp.route("/discipline/chain/verify", methods=["GET"])
def chain_verify():
    table = request.args.get("table", "trade_plan")
    if table not in disc._HASH_TABLES:
        return fail(f"仅支持 {list(disc._HASH_TABLES)}", http=422)
    with get_conn(readonly=True) as c:
        breaks = disc.verify_chain(c, table)
    return ok({"table": table, "breaks": breaks, "intact": not breaks})


@discipline_bp.route("/discipline/sweep", methods=["POST"])
def discipline_sweep():
    """手动触发状态 sweep（scheduler 每 5 分钟自动做同一件事）。"""
    with get_conn() as c:
        t = disc.sweep_ticket_states(c)
        n_due = disc.sweep_sealed(c)
        bf = disc.backfill_case_files(c)
    return ok({"tickets": t, "sealed_due": n_due, "backfill": bf})
