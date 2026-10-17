"""tests/test_l06.py —— L06 30 分钟验证闭环红线（L06_spec §10 R2–R8）

R1（历史数字零变更的 git 侧红线）属测试A/管理A 验收面；本文件用「配置未被修改 +
payload 只覆盖费率」两条断言从实现侧配合。
模式沿用 test_api_identity / test_discipline：conftest isolated_database + init_db
触发 005/006/007 迁移。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timedelta

import pytest

from core.db import get_conn, init_db
from backtest import l06
from backtest.l06 import TriageError
from config.l06_params import (
    L06_CANNOT_PROVE, L06_COMBOS, L06_MID_FREQ_EXEC, TRIAGE_BUDGET_SECONDS, cost_tier,
)

FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture(autouse=True)
def _migrated(isolated_database):
    init_db()


def _conn():
    return get_conn()


def _seed_prices(days: int, codes=("600001", "600002"), start="2024-01-02"):
    """灌 N 个交易日的最小行情（周末跳过，close 递增，全部可成交）。"""
    d = datetime.strptime(start, "%Y-%m-%d")
    dates = []
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    with _conn() as c:
        for code in codes:
            c.execute("INSERT OR IGNORE INTO stock_info(code,name,is_active) VALUES (?,?,1)",
                      (code, code))
            for i, dt in enumerate(dates):
                px = 10.0 + i * 0.01
                c.execute(
                    """INSERT OR IGNORE INTO daily_price
                       (code, trade_date, open, high, low, close, volume, fusion_score)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (code, dt, px, px * 1.01, px * 0.99, px, 1_000_000, 40 + i % 30))
        c.commit()
    return dates


def _new_idea(title="超跌放量反转测试想法", hypothesis="连续下跌后放量首日买入，10 日内止盈离场",
              conditions=None, hold=8):
    """录入想法但**不开计时会话**：会话由测试显式 open_session(at=…) 固定时钟。
    （会话起点服务端首笔写入后不可改，重复 open 是幂等 no-op。）"""
    conditions = conditions or [{"indicator": "fusion_score", "operator": "gt",
                                 "params": {"value": 60}}]
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO idea(title, hypothesis, conditions_json, indicators_json,
                                implied_hold_days, status)
               VALUES (?,?,?,?,?,'triaging')""",
            (title, hypothesis, json.dumps(conditions, ensure_ascii=False),
             json.dumps(sorted({x["indicator"] for x in conditions}), ensure_ascii=False),
             hold))
        idea_id = int(cur.lastrowid)
        c.commit()
    return idea_id


def _idea_row(idea_id):
    with _conn() as c:
        return dict(c.execute("SELECT * FROM idea WHERE id=?", (idea_id,)).fetchone())


def _pass_gate(idea_id, at=None):
    checks = []
    with _conn() as c:
        checks = l06.run_fast_checks(c, _idea_row(idea_id))
        log = l06.record_triage(c, idea_id=idea_id, verdict="pass", checks=checks, at=at)
        c.commit()
    return log


# ════════════════ R2 / R8：三跑参数一致性与理想档隔离 ════════════════
class TestRunPayloads:
    def test_r2_real_tier_equals_BACKTEST_COST_and_config_untouched(self):
        from config.strategy_params import BACKTEST_COST
        before = dict(BACKTEST_COST)
        tier = cost_tier("real")
        assert tier["commission_rate"] == before["commission_rate"]
        assert tier["stamp_tax_rate"] == before["stamp_tax_rate"]
        assert tier["slippage_rate"] == before["slippage_rate"]
        assert BACKTEST_COST == before                 # 拷贝读，不改全局默认
        ideal = cost_tier("ideal")
        assert (ideal["commission_rate"], ideal["stamp_tax_rate"], ideal["slippage_rate"]) == (0.0, 0.0, 0.0)

    def test_r8_combos_differ_only_in_window_and_cost(self):
        dates = _seed_prices(300)
        windows = {"recent": dates[-250:], "full": dates}
        combos = l06.build_run_payloads(
            [{"indicator": "fusion_score", "operator": "gt", "params": {"value": 60}}],
            "测试想法", windows)
        assert [c["code"] for c in combos] == ["A", "B", "C"]
        p = {c["code"]: c["payload"]["params"] for c in combos}
        COST_KEYS = {"commission_rate", "stamp_tax_rate", "slippage_rate"}
        # A vs C：同窗同条件，只差成本三项（COST 死因必须是同窗对比）
        assert p["A"]["start_date"] == p["C"]["start_date"]
        assert p["A"]["end_date"] == p["C"]["end_date"]
        diff_ac = {k for k in set(p["A"]) | set(p["C"]) if p["A"].get(k) != p["C"].get(k)}
        assert diff_ac <= COST_KEYS
        # A vs B：同成本档，只差 start_date（REGIME 死因必须是同成本对比）
        diff_ab = {k for k in set(p["A"]) | set(p["B"]) if p["A"].get(k) != p["B"].get(k)}
        assert diff_ab <= {"start_date", "_l06_trading_days"}
        assert p["A"]["commission_rate"] == p["B"]["commission_rate"]
        # 其余执行口径逐项等于 L06 固定中频快照
        for key, val in L06_MID_FREQ_EXEC.items():
            for code in ("A", "B", "C"):
                assert p[code][key] == val

    def test_ideal_tier_does_not_carry_min_commission_override(self):
        """min_commission 不下发（engine 走模块常量，属禁区）；研究资金规模下非绑定。"""
        dates = _seed_prices(260)
        combos = l06.build_run_payloads(
            [{"indicator": "fusion_score", "operator": "gt", "params": {"value": 60}}],
            "x" * 6, {"recent": dates, "full": dates})
        assert all("min_commission" not in c["payload"]["params"] for c in combos)


# ════════════════ R3：深跑门禁不可绕 ════════════════
class TestGate:
    def test_run_requires_pass_verdict(self):
        idea_id = _new_idea()
        with _conn() as c, pytest.raises(TriageError) as ei:
            l06.submit_run(c, idea_id)
        assert ei.value.error_code == "GATE_NOT_PASSED"

    def test_unexpressible_indicator_fails_T1(self):
        idea_id = _new_idea(conditions=[{"indicator": "not_a_real_indicator",
                                         "operator": "gt", "params": {"value": 1}}])
        with _conn() as c:
            checks = l06.run_fast_checks(c, _idea_row(idea_id))
        t1 = checks[0]
        assert t1["id"] == "T1" and t1["result"] == "fail"
        assert l06.suggest_verdict(checks)[0] == "fail"
        assert l06.suggest_verdict(checks)[1] == "LOGIC"

    def test_same_day_entry_fails_T2(self):
        """当日信号当日成交 → A股 T+1 不成立，硬门判死。"""
        with _conn() as c:
            checks = l06.run_fast_checks(c, _idea_row(_new_idea()), buy_timing="current_close")
        t2 = next(x for x in checks if x["id"] == "T2")
        assert t2["result"] == "fail" and t2["death_step"] == "LOGIC"

    def test_T2_declares_it_only_checks_execution_timing(self):
        """诚实边界：T2 通过不代表指标本身无前视（写进 checks 与 cannot_prove）。"""
        with _conn() as c:
            checks = l06.run_fast_checks(c, _idea_row(_new_idea()))
        t2 = next(x for x in checks if x["id"] == "T2")
        assert "无前视" in t2.get("caveat", "")
        assert any("不证明指标计算本身无前视" in s for s in L06_CANNOT_PROVE)


# ════════════════ R4：计时在服务端，超时不得补 pass ════════════════
class TestTriageClock:
    def test_client_elapsed_is_ignored_and_timeout_forbids_pass(self):
        idea_id = _new_idea()
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=t0)
            c.commit()
        # 预算内提交 pass → 正常
        log = _pass_gate(idea_id, at=t0 + timedelta(seconds=600))
        assert log["verdict"] == "pass" and log["elapsed_seconds"] == 600

    def test_pass_after_timeout_forbidden(self):
        """已超预算的 attempt 不得补交 pass；defer/fail 仍可（收口入口不关）。"""
        idea_id = _new_idea()
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=t0)
            c.commit()
        with _conn() as c, pytest.raises(TriageError) as ei:
            l06.record_triage(c, idea_id=idea_id, verdict="pass", checks=[],
                              at=t0 + timedelta(seconds=1900))
        assert ei.value.error_code == "PASS_AFTER_TIMEOUT_FORBIDDEN"
        # 同一 attempt 仍可收口为 defer/fail
        with _conn() as c:
            log = l06.record_triage(c, idea_id=idea_id, verdict="defer", checks=[],
                                    defer_reason="缺龙虎榜字段", at=t0 + timedelta(seconds=1900))
            c.commit()
        assert log["verdict"] == "defer" and log["elapsed_seconds"] == 1900

    def test_over_budget_pass_rejected_even_without_timeout_event(self):
        idea_id = _new_idea()
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=t0)
            c.commit()
        with _conn() as c, pytest.raises(TriageError) as ei:
            l06.record_triage(c, idea_id=idea_id, verdict="pass", checks=[],
                              at=t0 + timedelta(seconds=TRIAGE_BUDGET_SECONDS + 5))
        assert ei.value.error_code == "PASS_AFTER_TIMEOUT_FORBIDDEN"

    def test_attempt2_gets_fresh_budget_and_can_pass(self):
        """关掉超时 pass 不埋掉好想法：defer → 下一轮全新 1800s → 预算内可 pass。"""
        idea_id = _new_idea()
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=t0)
            c.commit()
        with _conn() as c:
            l06.record_triage(c, idea_id=idea_id, verdict="defer", checks=[],
                              defer_reason="缺数据", at=t0 + timedelta(seconds=1801))
            c.commit()
        t2 = t0 + timedelta(days=3)
        with _conn() as c:
            sess = l06.open_next_session(c, idea_id, at=t2)
            c.execute("UPDATE idea SET status='triaging' WHERE id=?", (idea_id,))
            log = l06.record_triage(c, idea_id=idea_id, verdict="pass", checks=[], at=t2)
            c.commit()
        assert log["triage_attempt"] == 2
        assert log["elapsed_seconds"] == 0
        assert sess["deadline_at"] == (t2 + timedelta(seconds=TRIAGE_BUDGET_SECONDS)).strftime(FMT)

    def test_two_real_decisions_exhaust_the_budget(self):
        """两次真人结论用尽后不再给第三次预算；timeout 事件不占名额（见 sweep 用例）。"""
        idea_id = _new_idea()
        _pass_gate(idea_id)                       # decision #1
        with _conn() as c:
            l06.open_next_session(c, idea_id)
            c.commit()
        _pass_gate(idea_id)                       # decision #2
        with _conn() as c, pytest.raises(TriageError) as ei:
            l06.open_next_session(c, idea_id)
        assert ei.value.error_code == "IDEA_TRIAGE_CLOSED"

    def test_sweep_records_timeout_event_then_auto_defer_not_fail(self):
        """timeout 只记事件、不写 triage_log、不占第二次预算名额，也不自动判死。"""
        idea_id = _new_idea()
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=t0)
            c.commit()
        with _conn() as c:
            res = l06.sweep_timeouts(c, at=t0 + timedelta(seconds=1801))
            c.commit()
        assert res["timeout_recorded"] == 1 and res["final_fail"] == 0
        with _conn() as c:
            st = c.execute("SELECT status FROM idea WHERE id=?", (idea_id,)).fetchone()["status"]
            logs = c.execute("SELECT COUNT(*) n FROM triage_log WHERE idea_id=?",
                             (idea_id,)).fetchone()["n"]
            nxt = l06.next_attempt(c, idea_id)
        assert st == "timeout" and logs == 0 and nxt == 1     # 名额未被消耗
        with _conn() as c:
            res2 = l06.sweep_timeouts(c, at=t0 + timedelta(seconds=1801 + 90_000))
            c.commit()
        assert res2["auto_deferred"] == 1
        with _conn() as c:
            st2 = c.execute("SELECT status FROM idea WHERE id=?", (idea_id,)).fetchone()["status"]
        assert st2 == "deferred"

    def test_second_attempt_timeout_is_final_fail_with_TIMEOUT_death(self):
        idea_id = _new_idea()
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=t0)
            c.commit()
        with _conn() as c:
            l06.record_triage(c, idea_id=idea_id, verdict="defer", checks=[],
                              defer_reason="缺数据", at=t0 + timedelta(seconds=600))
            c.commit()
        t2 = t0 + timedelta(hours=1)
        with _conn() as c:
            l06.open_next_session(c, idea_id, at=t2)
            c.commit()
        with _conn() as c:
            res = l06.sweep_timeouts(c, at=t2 + timedelta(seconds=1801))
            c.commit()
        assert res["final_fail"] == 1
        with _conn() as c:
            row = dict(c.execute("SELECT verdict, death_step FROM triage_log "
                                 "WHERE idea_id=? AND triage_attempt=2", (idea_id,)).fetchone())
            st = c.execute("SELECT status FROM idea WHERE id=?", (idea_id,)).fetchone()["status"]
        assert row == {"verdict": "fail", "death_step": "TIMEOUT"}
        assert st == "failed"


# ════════════════ R5：量纲与符号（COST 判定）════════════════
def _m(annual, cum, mdd=-0.20, trades=80, sharpe=1.0, win=0.55):
    return {"cumulative_return": cum, "annual_return": annual, "win_rate": win,
            "sharpe_ratio": sharpe, "max_drawdown": mdd, "total_trades": trades,
            "win_trades": int(trades * win)}


class TestUnitsAndDeathStep:
    def test_normalize_rejects_percent_and_positive_drawdown(self):
        with pytest.raises(TriageError) as ei:
            l06.normalize_metrics({"total_return": 30.0, "annual_return": 12.0,
                                   "max_drawdown": -8.0, "total_trades": 50})
        assert ei.value.error_code == "UNIT_MISMATCH"
        with pytest.raises(TriageError) as ei2:
            l06.normalize_metrics({"total_return": 0.3, "annual_return": 0.12,
                                   "max_drawdown": 0.08, "total_trades": 50})
        assert ei2.value.error_code == "SIGN_MISMATCH"
        m = l06.normalize_metrics({"total_return": 0.30, "annual_return": 0.12,
                                   "max_drawdown": -0.08, "total_trades": 50,
                                   "win_trades": 30, "win_rate": 0.6, "sharpe_ratio": 1.2})
        assert m["cumulative_return"] == 0.30        # 引擎 total_return → bundle cumulative_return

    def test_cost_death_with_decimal_magnitudes(self):
        step, text = l06.decide_death_step({"A": _m(-0.02, -0.02, trades=80),
                                            "B": _m(-0.01, -0.01, trades=120),
                                            "C": _m(0.30, 0.32, trades=80)})
        assert step == "COST"
        assert "成本" in text
        # 小数口径自证：成本差 34 个百分点写作 0.34，不是 34
        gap = 0.32 - (-0.02)
        assert 0.05 < gap < 1.0

    def test_drawdown_sign_used_for_weak(self):
        """回撤阈值是 < -0.25：把符号写反时该用例必须失败（R5 后半段）。"""
        step, _ = l06.decide_death_step({"A": _m(0.20, 0.20, mdd=-0.40),
                                         "B": _m(0.18, 0.18, mdd=-0.40),
                                         "C": _m(0.22, 0.22, mdd=-0.40)})
        assert step == "WEAK"

    def test_regime_death_needs_same_cost_comparison(self):
        step, _ = l06.decide_death_step({"A": _m(0.25, 0.25),
                                         "B": _m(-0.05, -0.05),
                                         "C": _m(0.28, 0.28)})
        assert step == "REGIME"

    def test_sample_death_below_min_trades(self):
        step, text = l06.decide_death_step({"A": _m(0.20, 0.20, trades=12),
                                            "B": _m(0.20, 0.20, trades=20),
                                            "C": _m(0.25, 0.25, trades=12)})
        assert step == "SAMPLE" and "12" in text

    def test_none_when_reaches_all_bars(self):
        step, _ = l06.decide_death_step({"A": _m(0.20, 0.20, mdd=-0.15, sharpe=1.4),
                                         "B": _m(0.18, 0.18, mdd=-0.18, sharpe=1.2),
                                         "C": _m(0.23, 0.23, mdd=-0.15, sharpe=1.6)})
        assert step == "NONE"


# ════════════════ R6：窗口不足必须诚实 ════════════════
class TestWindowHonesty:
    def test_window_clamped_flag_set(self):
        dates = _seed_prices(200)
        windows = {"recent": dates[-200:], "full": dates}
        combos = l06.build_run_payloads(
            [{"indicator": "fusion_score", "operator": "gt", "params": {"value": 60}}],
            "x" * 6, windows)
        b = l06.assemble_bundle(
            run_id=1, idea={"id": 1, "title": "x"},
            triage=None,
            combos=[{"code": c["code"], "task_id": "t", "window": c["window"],
                     "cost_tier": c["cost_tier"], "window_days": c["window_days"]}
                    for c in combos],
            results={"A": {"total_return": 0.2, "annual_return": 0.2, "max_drawdown": -0.1,
                           "total_trades": 60, "win_trades": 30, "win_rate": 0.5,
                           "sharpe_ratio": 1.0},
                     "B": None, "C": None})
        assert b["window_clamped"] is True
        assert b["window_days_actual"]["A"] == 200

    def test_short_long_window_yields_DATA(self):
        metrics = {"A": _m(0.2, 0.2), "B": _m(0.1, 0.1), "C": _m(0.25, 0.25)}
        step, text = l06.decide_death_step(metrics, {"window_days_actual": {"B": 90},
                                                     "est_signals": None})
        assert step == "DATA" and "90" in text


# ════════════════ R7：报告必备「不能证明什么」段 ════════════════
class TestReportContract:
    def _bundle(self):
        return l06.assemble_bundle(
            run_id=7, idea={"id": 7, "title": "想法七号"},
            triage={"elapsed_seconds": 743, "verdict": "pass", "triage_attempt": 1,
                    "checks_json": json.dumps([{"id": "T1", "result": "pass"},
                                               {"id": "T4", "result": "pass",
                                                "est_signals": 118}]),
                    "death_step": None, "defer_reason": None, "overlap_rule_id": None},
            combos=[{"code": "A", "task_id": "a", "window": "recent", "cost_tier": "real",
                     "window_days": 250},
                    {"code": "B", "task_id": "b", "window": "full", "cost_tier": "real",
                     "window_days": 750},
                    {"code": "C", "task_id": "c", "window": "recent", "cost_tier": "ideal",
                     "window_days": 250}],
            results={"A": {"total_return": -0.02, "annual_return": -0.02,
                           "max_drawdown": -0.18, "total_trades": 80, "win_trades": 40,
                           "win_rate": 0.5, "sharpe_ratio": 0.2,
                           "equity_curve": [{"date": "2025-01-01", "value": 1.0}]},
                     "B": {"total_return": -0.01, "annual_return": -0.01,
                           "max_drawdown": -0.22, "total_trades": 150, "win_trades": 75,
                           "win_rate": 0.5, "sharpe_ratio": 0.3},
                     "C": {"total_return": 0.32, "annual_return": 0.30,
                           "max_drawdown": -0.15, "total_trades": 82, "win_trades": 45,
                           "win_rate": 0.55, "sharpe_ratio": 1.1}})

    def test_r7_cannot_prove_present_and_nonempty(self):
        b = self._bundle()
        assert b["cannot_prove"] and len(b["cannot_prove"]) >= 5
        md = l06.render_markdown(b)
        assert "## 本报告不能证明什么" in md
        for line in b["cannot_prove"]:
            assert line in md

    def test_bundle_contract_fields_all_present(self):
        b = self._bundle()
        for k in ("bundle_id", "idea_id", "combos", "metrics", "death_step",
                  "conclusion_text", "execution_config", "data_version", "window_clamped",
                  "window_days_actual", "cannot_prove", "triage"):
            assert k in b, k
        for k in ("A", "B", "C", "cost_gap"):
            assert k in b["metrics"], k
        for c in b["combos"]:
            assert {"code", "task_id", "window", "cost_tier", "window_days"} <= set(c)
        assert b["death_step"] in l06.DEATH_STEPS
        assert b["death_step"] == "COST"
        assert b["metrics"]["cost_gap"] == pytest.approx(0.34, abs=1e-4)
        for code in ("A", "B", "C"):
            assert b["metrics"][code]["max_drawdown"] < 0
        assert b["triage"]["over_budget"] == 0 and b["triage"]["attempt"] == 1

    def test_missing_metrics_degrade_to_DATA_not_crash(self):
        b = self._bundle()
        b2 = l06.assemble_bundle(
            run_id=8, idea={"id": 8, "title": "想法八号"}, triage=None,
            combos=[{"code": "A", "task_id": "a", "window": "recent", "cost_tier": "real",
                     "window_days": 250},
                    {"code": "B", "task_id": "b", "window": "full", "cost_tier": "real",
                     "window_days": 750},
                    {"code": "C", "task_id": "c", "window": "recent", "cost_tier": "ideal",
                     "window_days": 250}],
            results={"A": None, "B": None, "C": None})
        assert b2["death_step"] == "DATA"
        assert b2["metrics"]["cost_gap"] is None

    def test_write_report_creates_md_and_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(l06, "report_root", lambda: str(tmp_path))
        b = self._bundle()
        md, js = l06.write_report(None, b, at=datetime(2026, 10, 19, 21, 0, 0))
        assert os.path.isfile(md) and os.path.isfile(js)
        with open(js, encoding="utf-8") as f:
            assert json.load(f)["death_step"] == "COST"


# ════════════════ 数据快检与周复盘 ════════════════
class TestChecksAndReview:
    def test_T3_defers_when_no_data(self):
        with _conn() as c:
            checks = l06.run_fast_checks(c, _idea_row(_new_idea()))
        assert checks[2]["id"] == "T3" and checks[2]["result"] == "defer"
        assert l06.suggest_verdict(checks)[0] == "defer"

    def test_T4_T5_not_evaluated_for_rolling_indicators(self):
        """含需历史滚动的技术指标时，快检不假装算得出 → not_evaluated（样本交深跑实测）。"""
        idea_id = _new_idea(conditions=[{"indicator": "ma_cross", "operator": "golden_cross",
                                         "params": {}}])
        _seed_prices(120)
        with _conn() as c:
            checks = l06.run_fast_checks(c, _idea_row(idea_id))
        ids = {x["id"]: x["result"] for x in checks}
        assert ids["T4"] == "not_evaluated" and ids["T5"] == "not_evaluated"

    def test_review_week_success_criterion(self):
        _seed_prices(260)
        t0 = datetime(2026, 10, 19, 20, 0, 0)
        outcomes = ["pass", "pass", "pass", "fail", "timeout"]
        for i, v in enumerate(outcomes):
            idea_id = _new_idea(title=f"想法{i}_队列测试标题")
            with _conn() as c:
                l06.open_session(c, idea_id, 1, at=t0)
                checks = l06.run_fast_checks(c, _idea_row(idea_id))
                if v == "timeout":
                    res = l06.sweep_timeouts(c, at=t0 + timedelta(seconds=1801))
                else:
                    l06.record_triage(c, idea_id=idea_id, verdict=v, checks=checks,
                                      death_step="SAMPLE" if v == "fail" else None,
                                      at=t0 + timedelta(seconds=600))
                c.commit()
        with _conn() as c:
            rev = l06.review_week(c, "2026-10-19", "2026-10-25")
        assert rev["ideas_triaged"] == 5
        assert rev["by_verdict"]["pass"] == 3 and rev["by_verdict"]["timeout"] == 1
        assert rev["actionable_within_budget"] == 4      # 3 pass + 1 fail(带死因)
        assert rev["pass"] is True
        assert rev["timeout_rate"] == 0.2

    def test_triage_session_and_run_are_append_only(self):
        """计时起点写库后不可改（R4「计时不可绕」的存储层保证）。"""
        idea_id = _new_idea()
        with _conn() as c:
            l06.open_session(c, idea_id, 1, at=datetime(2026, 10, 19, 20, 0, 0))
            c.commit()
        with _conn() as c, pytest.raises(Exception):
            c.execute("UPDATE l06_triage_session SET started_at='1999-01-01 00:00:00' "
                      "WHERE idea_id=?", (idea_id,))


# ════════════════ API 表面（前端契约可达性）════════════════
@pytest.fixture()
def client():
    init_db()
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


class TestApiSurface:
    def test_idea_then_triage_then_queue(self, client):
        _seed_prices(260)
        r = client.post("/api/l06/idea", json={
            "title": "API 链路测试想法", "hypothesis": "放量首日介入，10 日内离场",
            "conditions": [{"indicator": "fusion_score", "operator": "gt",
                            "params": {"value": 60}}], "implied_hold_days": 8})
        assert r.status_code == 200
        d = r.get_json()["data"]
        assert d["deadline_at"] and d["budget_seconds"] == TRIAGE_BUDGET_SECONDS
        idea_id = d["idea_id"]

        q = client.get("/api/l06/queue").get_json()["data"]["items"]
        assert any(i["id"] == idea_id and i["deadline_at"] for i in q)   # 前端A 要求的 deadline_at

        t = client.post(f"/api/l06/idea/{idea_id}/triage", json={"verdict": "pass"})
        assert t.status_code == 200
        body = t.get_json()["data"]
        assert body["verdict"] == "pass" and "checks" in body
        assert {k for k in ("elapsed_seconds", "attempt", "over_budget", "death_step")} <= set(body)

    def test_triage_rejects_unexpressible_pass(self, client):
        _seed_prices(200)
        r = client.post("/api/l06/idea", json={
            "title": "不可表达想法", "hypothesis": "用不存在的指标选股试试看",
            "conditions": [{"indicator": "no_such_indicator", "operator": "gt",
                            "params": {"value": 1}}]}).get_json()["data"]
        bad = client.post(f"/api/l06/idea/{r['idea_id']}/triage", json={"verdict": "pass"})
        assert bad.status_code == 409
        assert bad.get_json()["error_code"] == "TRIAGE_RECOMPUTE_FAILED"

    def test_run_blocked_before_gate(self, client):
        _seed_prices(200)
        r = client.post("/api/l06/idea", json={
            "title": "未过闸就想深跑", "hypothesis": "直接跑回测看看会怎样呢",
            "conditions": [{"indicator": "fusion_score", "operator": "gt",
                            "params": {"value": 60}}]}).get_json()["data"]
        bad = client.post(f"/api/l06/idea/{r['idea_id']}/run")
        assert bad.status_code == 409
        assert bad.get_json()["error_code"] == "GATE_NOT_PASSED"

    def test_review_requires_window(self, client):
        bad = client.get("/api/l06/review")
        assert bad.status_code == 422
