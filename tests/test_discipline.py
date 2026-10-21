"""tests/test_discipline.py —— JH4 纪律装置包红线（JH4_spec §10 R1–R8/R10/R11）

R9（历史数字零变更 git 红线）属测试A/管理A 验收面，不在本文件。
模式沿用 test_api_identity：conftest isolated_database + init_db 触发 005/006 迁移。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

from core.db import get_conn, init_db
from core import discipline as disc


@pytest.fixture(autouse=True)
def _migrated(isolated_database):
    init_db()


def _conn():
    return get_conn()


# ─────────────────────────────────────────────
# R1/R2/R3：豁免与硬门
# ─────────────────────────────────────────────
class TestExemptions:
    def test_r1_all_exempt_kinds_zero_delay(self):
        for kind in disc.EXIT_EXEMPT:
            assert disc.delay_seconds_for(disc.ENTRY, exempt_kind=kind) == 0.0

    def test_r1_no_config_key_for_exempt(self, monkeypatch):
        """豁免来自 frozenset 常量：config 层不存在可关开关；
        monkeypatch 任意参数键都不改变出场零延迟。"""
        from config import strategy_params
        monkeypatch.setattr(strategy_params, "get_param", lambda k: 999999)
        assert "EXIT_EXEMPT" not in json.dumps(
            getattr(strategy_params, "TUNABLE_PARAMS", {}), default=str)
        assert disc.delay_seconds_for(disc.EXIT_RISK_REDUCING) == 0.0
        with pytest.raises(ValueError):
            disc.delay_seconds_for(disc.ENTRY, exempt_kind="hand_feel_override")

    def test_r2_intent_is_symbolic_not_text(self):
        """classify_intent 不接收 reason 文本参数：伪装文本无法改变分类。"""
        import inspect
        params = inspect.signature(disc.classify_intent).parameters
        assert "reason" not in params
        # 文本写「止损」但符号是买入加仓 → 仍 ENTRY（延迟 24h）
        assert disc.classify_intent(direction="buy", qty_delta=100) == disc.ENTRY
        assert disc.delay_seconds_for(disc.ENTRY) == disc.RELEASE_DELAY_SECONDS

    def test_r2_stop_widen_is_entry_stop_tighten_is_exit(self):
        assert disc.classify_intent(direction="stop_change", stop_old=9.0,
                                    stop_new=8.0) == disc.ENTRY      # 放宽
        assert disc.classify_intent(direction="stop_change", stop_old=9.0,
                                    stop_new=None) == disc.ENTRY     # 撤销
        assert disc.classify_intent(direction="stop_change", stop_old=9.0,
                                    stop_new=9.5) == disc.EXIT_RISK_REDUCING  # 收紧

    def test_r3_anchor_numeric_hard_gate(self):
        assert "ANCHOR_NO_NUMBER" in disc.validate_anchors([])
        assert "ANCHOR_NO_NUMBER" in disc.validate_anchors(
            [{"anchor_kind": "price_below", "threshold_num": None}])
        assert "INDICATOR_NOT_AVAILABLE" in disc.validate_anchors(
            [{"anchor_kind": "indicator_below", "threshold_num": 1, "indicator": "moon_magic"}])
        assert disc.validate_anchors(
            [{"anchor_kind": "price_below", "threshold_num": 9.5}]) == []

    def test_r3_ddl_threshold_not_null(self):
        with _conn() as c:
            c.execute("""INSERT INTO trade_plan (code,status,thesis,entry_trigger_json,
                expected_return_pct,expected_horizon_days,confidence,position_pct,
                risk_params_json,content_hash) VALUES
                ('600001','alive','这是一段用于触发器测试的足够长的一字不差的理由文本一二三',
                 '{"max_buy_price": 10.5}',0.1,10,0.7,0.1,'{}','h')""")
            pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO plan_anchor (plan_id,anchor_kind,check_expr_json) "
                          "VALUES (?,'price_below','{}')", (pid,))   # 无数字锚 → DDL 拒


# ─────────────────────────────────────────────
# R4：append-only 触发器
# ─────────────────────────────────────────────
class TestAppendOnly:
    def _plan_id(self, c):
        c.execute("""INSERT INTO trade_plan (code,status,thesis,entry_trigger_json,
            expected_return_pct,expected_horizon_days,confidence,position_pct,
            risk_params_json,content_hash) VALUES
            ('600001','alive','这是一段用于触发器测试的足够长的一字不差的理由文本一二三',
             '{"max_buy_price": 10.5}',0.1,10,0.7,0.1,'{}','h')""")
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_r4_plan_payload_immutable_status_advances(self):
        with _conn() as c:
            pid = self._plan_id(c)
            c.execute("UPDATE trade_plan SET status='invalidated' WHERE id=?", (pid,))  # 合法推进
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("UPDATE trade_plan SET thesis='事后改写' WHERE id=?", (pid,))
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("DELETE FROM trade_plan WHERE id=?", (pid,))

    def test_r4_sealed_business_frozen(self):
        with _conn() as c:
            c.execute("""INSERT INTO sealed_decision (code,action,qty,ref_price,triggers_json,
                thesis_snapshot,expected_return_pct,expected_horizon_days,confidence,
                worst_case_pct,falsifier,due_on,content_hash)
                VALUES ('600001','buy',100,10.0,'[]','t',0.1,10,0.7,-0.08,
                        '如果放量跌破年线并且板块资金流出则这个判断是错的','2026-11-01','s')""")
            sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("UPDATE sealed_decision SET status='due' WHERE id=?", (sid,))
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("UPDATE sealed_decision SET falsifier='换个说法' WHERE id=?", (sid,))


# ─────────────────────────────────────────────
# R5/R6：逾期失效、retry 链、漂移守卫
# ─────────────────────────────────────────────
def _seed_price(c, code, dates_closes):
    for d, cl in dates_closes:
        c.execute("INSERT OR IGNORE INTO daily_price (code, trade_date, close, high, low) "
                  "VALUES (?,?,?,?,?)", (code, d, cl, cl * 1.01, cl * 0.99))


class TestTicketLifecycle:
    def _old_ticket(self, c, code="600002", days_ago=3):
        t0 = datetime.now() - timedelta(days=days_ago)
        return disc.insert_deviation_ticket(
            c, plan_id=None, code=code, action="buy", qty=100, price_ref=10.0,
            amount_ref=1000, rules=["D1"], tier="24h",
            intent_reason="手痒想追", submitted_at=t0)["ticket_id"]

    def test_r5_expired_after_window_and_archived(self):
        with _conn() as c:
            tid = self._old_ticket(c)
            res = disc.sweep_ticket_states(c)
            assert res["expired"] >= 1
            row = c.execute("SELECT status FROM deviation_ticket WHERE id=?", (tid,)).fetchone()
            assert row["status"] == "expired"
            cf = c.execute("SELECT * FROM case_file WHERE ticket_id=?", (tid,)).fetchone()
            assert cf is not None and cf["outcome"] == "expired"

    def test_r5_retry_of_chains_expired(self):
        with _conn() as c:
            t1 = self._old_ticket(c, days_ago=5)
            disc.sweep_ticket_states(c)
            t2 = self._old_ticket(c, days_ago=3)
            disc.sweep_ticket_states(c)
            t3 = self._old_ticket(c, days_ago=1)
            r = c.execute("SELECT retry_of FROM deviation_ticket WHERE id=?", (t3,)).fetchone()
            assert r["retry_of"] in (t1, t2)

    def test_r6_drift_guard_voids_released(self):
        with _conn() as c:
            tid = disc.insert_deviation_ticket(
                c, plan_id=None, code="600003", action="buy", qty=100, price_ref=10.0,
                amount_ref=1000, rules=["D1"], tier="24h", intent_reason="手痒",
                submitted_at=datetime.now())["ticket_id"]
            # 已过 release 语义：置 released（模拟已复核，等待执行）
            c.execute("UPDATE deviation_ticket SET status='released' WHERE id=?", (tid,))
            # expire_at 未到（sweep 走漂移分支而非 abandoned）；现价 +5% > 3% 阈值
            today = datetime.now().strftime("%Y-%m-%d")
            _seed_price(c, "600003", [(today, 10.5)])
            res = disc.sweep_ticket_states(c)
            assert res["voided_drift"] >= 1
            row = c.execute("SELECT status, void_reason FROM deviation_ticket WHERE id=?",
                            (tid,)).fetchone()
            assert row["status"] == "voided_drift"


# ─────────────────────────────────────────────
# R7/R8：校准单位与冷启动
# ─────────────────────────────────────────────
class TestCalibration:
    def _seed_cut(self, c, i, cut_pct=-5.0, loss_yuan=-50.0):
        code = f"60010{i}"
        start_d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        exit_day = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        c.execute("""INSERT INTO personal_position (code,name,shares,cost_price,status,opened_at)
                     VALUES (?, ?, 100, 10.0, 'closed', ?)""", (code, f"割{i}", start_d))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        # ≥16 个交易日价格序列（ATR14 需要窗口）：从 11 缓跌至出场 9.5
        dates = pd.bdate_range(start=start_d, end=exit_day).strftime("%Y-%m-%d").tolist()
        n = len(dates)
        closes = [11.0 - 1.5 * k / max(n - 1, 1) for k in range(n)]
        _seed_price(c, code, list(zip(dates, closes)))
        c.execute("""INSERT INTO personal_trade (position_id,code,name,action,shares,price,
                     cost_price,pnl,pnl_pct,traded_at)
                     VALUES (?,?,?,?,100,9.5,10.0,?,?,?)""",
                  (pid, code, f"割{i}", "卖出", loss_yuan, cut_pct, dates[-1]))

    def test_r7_units_percent_to_decimal(self):
        from core.discipline_calibration import run_calibration
        with _conn() as c:
            for i in range(3):
                self._seed_cut(c, i, cut_pct=-5.0)   # -5.0 百分数
            res = run_calibration(c)
        assert res["status"] == "pooled" and res["n"] == 3
        assert abs(res["stop_loss_pct"]) == pytest.approx(0.04, abs=0.005)
        assert res["max_single_position_pct"] is not None
        with _conn() as c2:
            row = c2.execute("SELECT m_p50 FROM risk_param_calibration WHERE id=?",
                             (res["calibration_id"],)).fetchone()
        assert abs(row["m_p50"] - 0.05) < 1e-9      # /100 不遗漏

    def test_r8_cold_start_uncalibrated_no_numbers(self):
        from core.discipline_calibration import run_calibration
        with _conn() as c:
            res = run_calibration(c)
        assert res["status"] == "uncalibrated"
        assert res["stop_loss_pct"] is None and res["max_single_position_pct"] is None


# ─────────────────────────────────────────────
# R10/R11：盲核顺序与哈希链
# ─────────────────────────────────────────────
class TestBlindOrderAndChain:
    @pytest.fixture()
    def client(self, monkeypatch):
        from app import app
        app.config["TESTING"] = True
        # 封存到期日直接落在今天（模拟已过观察窗），其余流程走真实实现
        monkeypatch.setattr(disc, "trading_days_after",
                            lambda d, n: datetime.now().strftime("%Y-%m-%d"))
        return app.test_client()

    def _seal(self, client):
        r = client.post("/api/l09/seal", json={
            "code": "600519", "action": "buy", "qty": 100, "ref_price": 10.0,
            "thesis_snapshot": "这是一段用于封存测试的买入理由文本内容足够长一点",
            "expected_return_pct": 0.10, "expected_horizon_days": 3,
            "confidence": 0.6, "worst_case_pct": -0.08,
            "falsifier": "如果缩量阴跌且跌破二十日线仍未反弹则本判断被证伪成立",
        })
        assert r.status_code == 200, r.get_json()
        return r.get_json()["data"]["sealed_id"]

    def test_r10_blind_hides_sealed_content(self, client):
        sid = self._seal(client)
        due = client.get("/api/l09/due").get_json()["data"]["due"]
        assert any(d["id"] == sid for d in due)
        body = client.get(f"/api/l09/{sid}/blind")
        assert body.status_code == 200
        text = json.dumps(body.get_json(), ensure_ascii=False)
        assert "thesis" not in text and "falsifier" not in text   # 不含封存字段
        # R10：未作答 reveal → 409
        reveal = client.post(f"/api/l09/{sid}/reveal")
        assert reveal.status_code == 409
        ans = client.post(f"/api/l09/{sid}/blind/answer", json={
            "recall_verdict": "partial", "recall_expected_return_pct": 0.05,
            "recall_reason": "我当时大概预期五个点"})
        assert ans.status_code == 200
        r2 = client.post(f"/api/l09/{sid}/reveal")
        assert r2.status_code == 200
        cmp_ = r2.get_json()["data"]["comparison"]
        assert abs(cmp_["gap_expected"] - (-0.05)) < 1e-9

    def test_r11_chain_detects_first_break(self):
        base = {"status": "alive", "parent_id": None, "revision": 1,
                "thesis": "这是一段用于哈希链测试的足够长的理由文本内容一二三",
                "entry_trigger_json": "{}", "expected_return_pct": 0.1,
                "expected_horizon_days": 10, "confidence": 0.7, "position_pct": 0.1,
                "risk_params_json": "{}", "created_at":
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        ids = []
        with _conn() as c:
            for code in ("600201", "600202"):
                iid, _h = disc.insert_hashed(c, "trade_plan", dict(base, code=code))
                ids.append(iid)
            assert disc.verify_chain(c, "trade_plan") == []
            # 合法操作也不断链：状态推进 + 锚回填
            c.execute("UPDATE trade_plan SET status='invalidated' WHERE id=?", (ids[0],))
            assert disc.verify_chain(c, "trade_plan") == []
        # 模拟「绕过 trigger 的离线篡改」：trigger 拦 UPDATE，坏行只能经 INSERT 复现
        with _conn() as c:
            prev = disc.chain_prev_hash(c, "trade_plan")
            c.execute("""INSERT INTO trade_plan (parent_id,revision,code,status,thesis,
                entry_trigger_json,expected_return_pct,expected_horizon_days,confidence,
                position_pct,risk_params_json,created_at,content_hash,prev_hash)
                VALUES (NULL,1,'600203','alive',?, '{}',0.1,10,0.7,0.1,'{}',?,'deadbeef',?)""",
                (base["thesis"], base["created_at"], prev))
            bad_id = c.execute("SELECT id FROM trade_plan ORDER BY id DESC LIMIT 1").fetchone()[0]
            breaks = disc.verify_chain(c, "trade_plan")
        assert breaks and breaks[0]["id"] == bad_id


# ─────────────────────────────────────────────
# 建卡/偏离 API 冒烟（含 422 结构化错误码）
# ─────────────────────────────────────────────
class TestPlanAndDeviationApi:
    @pytest.fixture()
    def client(self):
        from app import app
        app.config["TESTING"] = True
        with _conn() as c:
            c.execute("INSERT OR IGNORE INTO stock_info (code,name,market) VALUES ('600519','贵州茅台','sh')")
        return app.test_client()

    def test_plan_missing_anchor_422(self, client):
        r = client.post("/api/plan", json={
            "code": "600519", "thesis": "这是一段长度超过十五个字符的理由文本用于通过长度校验",
            "entry_trigger": {"max_buy_price": 10.5}, "expected_return_pct": 0.1,
            "expected_horizon_days": 10, "confidence": 0.7, "position_pct": 0.1,
            "anchors": []})
        assert r.status_code == 422
        assert "ANCHOR_NO_NUMBER" in r.get_json()["failed"]

    def test_plan_happy_then_deviation_d1(self, client):
        r = client.post("/api/plan", json={
            "code": "600519", "thesis": "这是一段长度超过十五个字符的理由文本用于通过长度校验",
            "entry_trigger": {"max_buy_price": 10.5}, "expected_return_pct": 0.1,
            "expected_horizon_days": 10, "confidence": 0.7, "position_pct": 0.1,
            "anchors": [{"anchor_kind": "price_below", "threshold_num": 9.2}]})
        assert r.status_code == 200, r.get_json()
        # 计划内小单：金额占比 1%（<position_pct），价 10.4 ≤ max_buy×1.02 → 无偏离
        d = client.post("/api/deviation", json={
            "code": "600519", "direction": "buy", "qty": 10, "price": 10.4,
            "intent_reason": "按计划在触发价内"})
        j = d.get_json()["data"]
        assert j["is_deviation"] is False
        assert j["delay_seconds"] == 0
        # 无 plan 的票 → D1 偏离；且 H4 手痒票 → 须先封存（事前不可逆件），封后再提交
        with _conn() as c:
            c.execute("INSERT OR IGNORE INTO stock_info (code,name,market) VALUES ('600000','测试票','sh')")
        need_seal = client.post("/api/deviation", json={
            "code": "600000", "direction": "buy", "qty": 100, "price": 10.0,
            "intent_reason": "手痒想追"})
        assert need_seal.status_code == 422
        assert need_seal.get_json()["error_code"] == "SEAL_REQUIRED"
        sealed = client.post("/api/l09/seal", json={
            "code": "600000", "action": "buy", "qty": 100, "ref_price": 10.0,
            "thesis_snapshot": "测试票的封存理由文本需要满足十五个字符的最小长度要求",
            "expected_return_pct": 0.08, "expected_horizon_days": 5,
            "confidence": 0.6, "worst_case_pct": -0.06,
            "falsifier": "若三日内缩量回落至前低下方则此入场判断即告失效"})
        assert sealed.status_code == 200, sealed.get_json()
        d2 = client.post("/api/deviation", json={
            "code": "600000", "direction": "buy", "qty": 100, "price": 10.0,
            "intent_reason": "手痒想追"})
        j2 = d2.get_json()["data"]
        assert j2["is_deviation"] is True and "D1" in j2["rules"]
        assert datetime.strptime(j2["release_at"], "%Y-%m-%d %H:%M:%S") >= \
            datetime.now() + timedelta(hours=23)

    def test_sell_never_delayed(self, client):
        """出场全豁免 API 面：卖出意向恒 is_deviation=False、delay=0。"""
        d = client.post("/api/deviation", json={
            "code": "600519", "direction": "sell", "qty": 100, "price": 9.0,
            "intent_reason": "破位了"})
        j = d.get_json()["data"]
        assert j["is_deviation"] is False and j["exempt"] is True and j["delay_seconds"] == 0


# ─────────────────────────────────────────────
# 判例回填（_job_discipline_night 夜检消费）
# ─────────────────────────────────────────────
class TestCaseFileBackfill:
    def _archive_case(self, c, *, code="600002", status="executed"):
        t0 = datetime.now() - timedelta(days=40)
        tid = disc.insert_deviation_ticket(
            c, plan_id=None, code=code, action="buy", qty=100, price_ref=10.0,
            amount_ref=1000, rules=["D1"], tier="24h",
            intent_reason="回填测试", submitted_at=t0)["ticket_id"]
        c.execute("UPDATE deviation_ticket SET status='released' WHERE id=?", (tid,))
        if status == "executed":
            c.execute("UPDATE deviation_ticket SET status='executed' WHERE id=?", (tid,))
        c.execute("""INSERT INTO case_file (ticket_id, code, outcome, ref_price, ref_amount,
                     qty, archived_at, content_hash) VALUES (?,?,?,?,?,?,?,?)""",
                  (tid, code, status, 10.0, 1000, 100,
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "h"))
        return tid, t0

    @staticmethod
    def _seed_days(c, code, start, n, low_override=None):
        days = pd.bdate_range(start=start + timedelta(days=1), periods=n).strftime("%Y-%m-%d")
        for i, d in enumerate(days):
            low = (low_override or {}).get(i, 9.5)
            c.execute("INSERT OR IGNORE INTO daily_price (code, trade_date, close, high, low) "
                      "VALUES (?,?,?,?,?)", (code, d, 10.5, 10.6, low))

    def test_executed_fills_ret_and_worst_then_idempotent(self):
        with _conn() as c:
            tid, t0 = self._archive_case(c)
            self._seed_days(c, "600002", t0, 25, low_override={12: 8.0})
        with _conn() as c:
            r = disc.backfill_case_files(c)
        assert r == {"scanned": 1, "filled_cells": 4}   # 3 个收益格 + 最大逆行
        with _conn() as c:
            cf = c.execute("SELECT * FROM case_file WHERE ticket_id=?", (tid,)).fetchone()
        for n in (5, 10, 20):
            assert cf[f"exec_ret_{n}"] == pytest.approx(0.05, abs=1e-6)
            assert cf[f"counterfactual_ret_{n}"] is None
        assert cf["worst_adverse_ret"] == pytest.approx(-0.2, abs=1e-6)
        with _conn() as c:   # 二次跑不应触发「NULL→值一次写入」触发器
            assert disc.backfill_case_files(c) == {"scanned": 0, "filled_cells": 0}

    def test_insufficient_days_then_worst_waits_for_20d_window(self):
        with _conn() as c:
            tid, t0 = self._archive_case(c, code="600003")
            self._seed_days(c, "600003", t0, 12, low_override={8: 8.0})
        with _conn() as c:
            r = disc.backfill_case_files(c)
        assert r["filled_cells"] == 2                     # 只有 5/10 日可算
        with _conn() as c:
            cf = c.execute("SELECT * FROM case_file WHERE ticket_id=?", (tid,)).fetchone()
        assert cf["exec_ret_20"] is None and cf["worst_adverse_ret"] is None
        with _conn() as c:                                 # 补足 20 个交易日后再跑
            self._seed_days(c, "600003", t0, 25)
        with _conn() as c:
            r = disc.backfill_case_files(c)
        assert r["filled_cells"] == 2                     # 20 日收益 + 最大逆行各一次
        with _conn() as c:
            cf = c.execute("SELECT * FROM case_file WHERE ticket_id=?", (tid,)).fetchone()
        assert cf["exec_ret_20"] == pytest.approx(0.05, abs=1e-6)
        assert cf["worst_adverse_ret"] == pytest.approx(-0.2, abs=1e-6)

    def test_counterfactual_branch_fills_without_exec_cols(self):
        with _conn() as c:
            tid, t0 = self._archive_case(c, code="600004", status="expired")
            self._seed_days(c, "600004", t0, 25)
        with _conn() as c:
            assert disc.backfill_case_files(c)["filled_cells"] == 4
        with _conn() as c:
            cf = c.execute("SELECT * FROM case_file WHERE ticket_id=?", (tid,)).fetchone()
        for n in (5, 10, 20):
            assert cf[f"counterfactual_ret_{n}"] == pytest.approx(0.05, abs=1e-6)
            assert cf[f"exec_ret_{n}"] is None
        assert cf["worst_adverse_ret"] == pytest.approx(-0.05, abs=1e-6)
        with _conn() as c:
            assert disc.backfill_case_files(c) == {"scanned": 0, "filled_cells": 0}
