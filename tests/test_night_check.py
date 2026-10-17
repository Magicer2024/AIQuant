"""tests/test_night_check.py —— L05 双源夜检（T1）

验收红线（管理A 派单）：清单可揪出注入的假差异；干净数据零误报；只读不写业务表。
全部离线：比对层为纯函数；编排层注入 fake fetcher + monkeypatch 交易日历。
"""
from __future__ import annotations

import pandas as pd
import pytest

from core.db import get_conn
from tools.night_check import (Diff, collect_universe, compare_stock,
                               run_night_check)


def _frame(dates, closes):
    return pd.DataFrame({"close": closes}, index=[str(d) for d in dates])


DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
DB_CLOSES = [10.00, 10.20, 10.10, 10.30, 10.50]


# ─────────────────────────────────────────────
# 比对层（纯函数）
# ─────────────────────────────────────────────
class TestCompareStock:
    def test_clean_data_no_findings(self):
        """两源一致 + 因子无漂移 → 零差异（无误报）"""
        db = _frame(DAYS, DB_CLOSES)
        tx = _frame(DAYS, DB_CLOSES)
        emq = _frame(DAYS, DB_CLOSES)
        emn = _frame(DAYS, DB_CLOSES)
        rows = compare_stock("600001", "测试", db, tx, emq, emn)
        assert rows == []

    def test_injected_close_divergence_detected(self):
        """注入假差异①：腾讯某日收盘高 5% → 红 close"""
        db = _frame(DAYS, DB_CLOSES)
        bad_tx = list(DB_CLOSES)
        bad_tx[2] = DB_CLOSES[2] * 1.05
        rows = compare_stock("600001", "测试", db, _frame(DAYS, bad_tx),
                             _frame(DAYS, DB_CLOSES), _frame(DAYS, DB_CLOSES))
        closes = [r for r in rows if r.item == "close"]
        assert len(closes) == 1
        assert closes[0].level == "red"
        assert closes[0].date == "2026-10-14"

    def test_injected_db_gap_detected(self):
        """注入假差异②：外部源有 10-16、DB 缺该日 → 红 db_gap"""
        db = _frame(DAYS[:-1], DB_CLOSES[:-1])
        tx = _frame(DAYS, DB_CLOSES)
        rows = compare_stock("600001", "测试", db, tx, _frame(DAYS, DB_CLOSES),
                             _frame(DAYS, DB_CLOSES))
        gaps = [r for r in rows if r.item == "db_gap"]
        assert len(gaps) == 1
        assert gaps[0].level == "red"
        assert gaps[0].date == "2026-10-16"

    def test_injected_factor_drift_detected(self):
        """注入假差异③：DB 隐含因子与东财口径偏 1% → 红 factor（+黄 anchor）"""
        db = _frame(DAYS, DB_CLOSES)                 # DB 按旧因子存
        emn = _frame(DAYS, DB_CLOSES)                # 不复权
        emq = _frame(DAYS, [c * 1.01 for c in DB_CLOSES])  # 东财当前前复权整体偏 1%
        rows = compare_stock("600001", "测试", db, _frame(DAYS, DB_CLOSES), emq, emn)
        factors = [r for r in rows if r.item == "factor"]
        assert len(factors) == 1
        assert factors[0].level == "red"
        assert factors[0].rel == "1.00%"
        # 锚点漂移应同时被标黄（qfq 最新日 ≠ 原始价）
        assert any(r.item == "anchor" and r.level == "yellow" for r in rows)

    def test_solo_row_flagged_yellow(self):
        """DB 单边有行、两外源均无 → 黄 solo_row（疑似停牌，不自动定性）"""
        db = _frame(DAYS, DB_CLOSES)
        tx = _frame(DAYS[:-1], DB_CLOSES[:-1])
        emq = _frame(DAYS[:-1], DB_CLOSES[:-1])
        emn = _frame(DAYS[:-1], DB_CLOSES[:-1])
        rows = compare_stock("600001", "测试", db, tx, emq, emn)
        solo = [r for r in rows if r.item == "solo_row"]
        assert len(solo) == 1
        assert solo[0].level == "yellow"
        assert solo[0].date == "2026-10-16"

    def test_tx_gap_yellow_not_db(self):
        """东财有、腾讯缺 → 黄 tx_gap，不判主源错"""
        db = _frame(DAYS, DB_CLOSES)
        tx = _frame(DAYS[:-1], DB_CLOSES[:-1])
        rows = compare_stock("600001", "测试", db, tx, _frame(DAYS, DB_CLOSES),
                             _frame(DAYS, DB_CLOSES))
        assert not any(r.item == "db_gap" for r in rows)
        tgap = [r for r in rows if r.item == "tx_gap"]
        assert len(tgap) == 1 and tgap[0].level == "yellow"

    def test_empty_db_red(self):
        rows = compare_stock("600001", "测试", _frame([], []), _frame(DAYS, DB_CLOSES),
                             None, None)
        assert len(rows) == 1 and rows[0].item == "no_db" and rows[0].level == "red"

    def test_fetch_notes_degrade_info(self):
        """拉取失败降级 info，不算红"""
        db = _frame(DAYS, DB_CLOSES)
        rows = compare_stock("600001", "测试", db, _frame(DAYS, DB_CLOSES),
                             None, None, fetch_notes=["东财口径参照拉取失败"])
        assert any(r.level == "info" and r.item == "fetch_fail" for r in rows)
        assert not any(r.level == "red" for r in rows)


# ─────────────────────────────────────────────
# 标的范围收集（隔离库 seed）
# ─────────────────────────────────────────────
class TestCollectUniverse:
    def test_union_positions_signals_watchlist(self):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO personal_position (code, name, shares, cost_price, status) "
                "VALUES ('600001','持仓A',100,10.0,'holding')")
            conn.execute(
                "INSERT INTO personal_position (code, name, shares, cost_price, status) "
                "VALUES ('600002','已平B',100,10.0,'closed')")   # 不应进池
            conn.execute(
                "INSERT INTO stock_signal (scan_date, trade_date, code, name) VALUES ('2026-10-16','2026-10-16','000003','信号C')")
            # stock_deep_signal 由 strategy.stock_deep 运行时建表，隔离库补最小 DDL（含排序列）
            conn.execute("CREATE TABLE IF NOT EXISTS stock_deep_signal "
                         "(scan_date TEXT, code TEXT, name TEXT, level TEXT, score REAL)")
            conn.execute(
                "INSERT INTO stock_deep_signal (scan_date, code, name) VALUES ('2026-10-16','000004','深析D')")
            conn.execute("INSERT INTO personal_watchlist (code) VALUES ('000005')")
        with get_conn(readonly=True) as conn:
            uni = collect_universe(conn, pool_days=5)
        assert set(uni) == {"600001", "000003", "000004", "000005"}
        assert uni["600001"] == "持仓A"


# ─────────────────────────────────────────────
# 编排层：注入假差异端到端可揪出 + 报告落盘
# ─────────────────────────────────────────────
class TestRunNightCheck:
    @pytest.fixture()
    def seeded(self):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO personal_position (code, name, shares, cost_price, status) "
                "VALUES ('600001','假差A',100,10.0,'holding')")
            conn.execute(
                "INSERT INTO stock_signal (scan_date, trade_date, code, name) VALUES ('2026-10-16','2026-10-16','600002','缺日B')")
            conn.execute(
                "INSERT INTO stock_signal (scan_date, trade_date, code, name) VALUES ('2026-10-16','2026-10-16','600003','干净C')")
            for i, d in enumerate(DAYS):
                conn.execute(
                    "INSERT INTO daily_price (code, trade_date, close) VALUES ('600001', ?, ?)",
                    (d, DB_CLOSES[i]))
                conn.execute(
                    "INSERT INTO daily_price (code, trade_date, close) VALUES ('600002', ?, ?)",
                    (d, DB_CLOSES[i]))
                conn.execute(
                    "INSERT INTO daily_price (code, trade_date, close) VALUES ('600003', ?, ?)",
                    (d, DB_CLOSES[i]))

    def _fetchers(self):
        tx_map = {
            "600001": [c * (1.05 if d == "2026-10-14" else 1.0)
                       for c, d in zip(DB_CLOSES, DAYS)],   # 假差异①：收盘偏 5%
            "600002": DB_CLOSES,
            "600003": DB_CLOSES,
        }
        emq_map = {
            "600001": DB_CLOSES,
            # 假差异②：外源有 10-16、DB 缺 → db 侧缺日（DB 数据在 seeded 里造 4 天即可，
            # 这里外源给满 5 天）
            "600002": DB_CLOSES,
            "600003": DB_CLOSES,
        }
        none_map = {c: v for c, v in emq_map.items()}

        def tx_fetcher(code, start, end):
            return _frame(DAYS, tx_map[code])

        def em_fetcher(code, adjust, start, end):
            return _frame(DAYS, (emq_map if adjust == "qfq" else none_map)[code])

        return tx_fetcher, em_fetcher

    def test_injected_fake_diffs_surface_in_report(self, seeded, tmp_path, monkeypatch):
        # 600002 制造 DB 缺日：删掉其 10-16 行（外源仍有 → 应报 db_gap）
        with get_conn() as conn:
            conn.execute("DELETE FROM daily_price WHERE code='600002' AND trade_date='2026-10-16'")
        monkeypatch.setattr("core.trade_calendar.is_trading_day", lambda d: True)
        tx_fetcher, em_fetcher = self._fetchers()

        res = run_night_check(check_date="2026-10-16", out_dir=str(tmp_path),
                              fetcher_tx=tx_fetcher, fetcher_em=em_fetcher,
                              sleep_s=0, now="2026-10-17 21:30:00")

        assert res["red"] == 2           # 600001 close 假差 + 600002 db_gap
        items = {(r.code, r.item) for r in res["rows"] if r.level == "red"}
        assert ("600001", "close") in items
        assert ("600002", "db_gap") in items
        assert "600003" not in {r.code for r in res["rows"]}   # 干净标的零误报

        report = tmp_path / "night_check_20261016.md"
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert "600001" in text and "close" in text
        assert "600002" in text and "db_gap" in text
        assert "2 红" in text

    def test_clean_universe_no_reds(self, seeded, tmp_path, monkeypatch):
        monkeypatch.setattr("core.trade_calendar.is_trading_day", lambda d: True)

        def tx_fetcher(code, start, end):
            return _frame(DAYS, DB_CLOSES)

        def em_fetcher(code, adjust, start, end):
            return _frame(DAYS, DB_CLOSES)

        res = run_night_check(check_date="2026-10-16", out_dir=str(tmp_path),
                              fetcher_tx=tx_fetcher, fetcher_em=em_fetcher,
                              sleep_s=0, now="2026-10-17 21:30:00")
        assert res["red"] == 0 and res["yellow"] == 0
        text = (tmp_path / "night_check_20261016.md").read_text(encoding="utf-8")
        assert "0 红" in text

    def test_max_pool_truncation_reported(self, seeded, tmp_path, monkeypatch):
        """限流截断：max_pool=2 → universe 总数 3、核查 2、截断 1，报告有标注"""
        monkeypatch.setattr("core.trade_calendar.is_trading_day", lambda d: True)

        def tx_fetcher(code, start, end):
            return _frame(DAYS, DB_CLOSES)

        def em_fetcher(code, adjust, start, end):
            return _frame(DAYS, DB_CLOSES)

        res = run_night_check(check_date="2026-10-16", out_dir=str(tmp_path),
                              max_pool=2, sleep_s=0,
                              fetcher_tx=tx_fetcher, fetcher_em=em_fetcher)
        assert res["universe"] == 3 and res["checked"] == 2 and res["truncated"] == 1
        text = (tmp_path / "night_check_20261016.md").read_text(encoding="utf-8")
        assert "截断" in text

    def test_read_only_no_db_mutation(self, seeded, tmp_path, monkeypatch):
        """红线：夜检不写任何业务表——运行前后 daily_price 行数一致"""
        monkeypatch.setattr("core.trade_calendar.is_trading_day", lambda d: True)
        with get_conn(readonly=True) as conn:
            before = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]

        def tx_fetcher(code, start, end):
            raise RuntimeError("网络不可用")   # 顺带覆盖拉取失败降级路径

        def em_fetcher(code, adjust, start, end):
            raise RuntimeError("网络不可用")

        res = run_night_check(check_date="2026-10-16", out_dir=str(tmp_path),
                              fetcher_tx=tx_fetcher, fetcher_em=em_fetcher, sleep_s=0)
        with get_conn(readonly=True) as conn:
            after = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        assert before == after
        assert res["red"] == 0
        assert res["info"] >= 1   # 每标的记录不可核查，不阻断
