"""任务1c 数据层修复包验收（2026-10-17，管理A派单①）。

覆盖：
  1. schema 迁移 004 幂等落地（source/exit_code 列、archive 表、默认值）
  2. source 显式赋值：stock_signal 分支=live、first_reversal_hist 分支=backfill
  3. archive：窗口重建 DELETE 前同事务快照，跨 policy_version 保留
  4. exit_code：evaluate_exit_by_prices 结构化枚举 + _evaluate_short/_evaluate_mid_long 落库
  5. 长线滚动窗口：出场发生在第 130 交易日之后也能结算（原 LIMIT 冻结回归测试）
  6. _exit_code_from_text 前缀归类（与 db.py SQL 规则同源）
"""
import pandas as pd
import pytest

from core import db
from core.outcome_tracker import (
    insert_new_outcomes, evaluate_outcomes, _exit_code_from_text,
)
from strategy.exit_advisor import evaluate_exit_by_prices

SCAN_DATE = "2026-09-10"  # 处于 60 天复盘窗内（相对仓库时钟 2026-10）


def _dates(start, n):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def _put_prices(code, rows):
    with db.get_conn() as conn:
        conn.executemany(
            """INSERT INTO daily_price(code,trade_date,open,high,low,close,
                                        volume,amount,pct_change,turnover)
               VALUES (?,?,?,?,?,?,0,0,0,0)""",
            [(code, d, o, h, l, c) for d, o, h, l, c in rows])


def _put_signal(code, *, horizon, strategy, scan_date=SCAN_DATE,
                buy=10.0, stop=8.5, tp=15.0, fusion=30.0, ext=0.01,
                long_mask=15, rank_key=0.1):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO stock_signal(code,name,scan_date,trade_date,horizon,strategy,
                                        buy_price,price,stop_loss,take_profit,
                                        fusion_score,pct_above_ma20,long_mask,long_rank_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, "测试股", scan_date, scan_date, horizon, strategy,
             buy, buy, stop, tp, fusion, ext, long_mask, rank_key))


# ─────────────────────────────────────────────
def test_migration_004_columns_and_archive_table():
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM schema_migration WHERE version='004_task1c_outcome_columns'"
        ).fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recommend_outcome)")}
        assert {"source", "exit_code"} <= cols
        scols = {r[1] for r in conn.execute("PRAGMA table_info(recommend_outcome_shadow)")}
        assert "exit_code" in scols
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='recommend_outcome_archive'"
        ).fetchone()


def test_mid_signal_source_live_and_archive_on_rebuild():
    _put_signal("600001", horizon="mid", strategy="中线综合")
    rows = [(SCAN_DATE, 10, 10, 9.8, 9.9)]
    rows += [(d, 10, 10.2, 9.8, 10) for d in _dates("2026-09-11", 20)]
    _put_prices("600001", rows)

    insert_new_outcomes()
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT source, exit_code FROM recommend_outcome "
            "WHERE code='600001' AND horizon='mid'").fetchone()
        assert r is not None and r["source"] == "live"
        # 模拟已结算状态后重建：归档必须保住被删前的评估进度
        conn.execute(
            "UPDATE recommend_outcome SET exit_code='stop_loss', exit_return=-5.0 "
            "WHERE code='600001' AND horizon='mid'")
    insert_new_outcomes()  # 第二次：先清后重写，DELETE 前应已快照
    with db.get_conn() as conn:
        a = conn.execute(
            "SELECT policy_version, exit_code, exit_return, source "
            "FROM recommend_outcome_archive WHERE code='600001'").fetchall()
        assert len(a) == 1
        assert a[0]["policy_version"].startswith("pv")
        assert a[0]["exit_code"] == "stop_loss" and a[0]["exit_return"] == -5.0
        assert a[0]["source"] == "live"
        # 重建后的新行回到未评估态（口径：当前过滤链快照，不继承旧值）
        n = conn.execute(
            "SELECT exit_code FROM recommend_outcome WHERE code='600001'").fetchone()
        assert n["exit_code"] is None


def test_first_reversal_source_backfill_vs_live():
    """hist 回填分支=backfill；stock_signal 当日链路=live。"""
    insert_new_outcomes()  # 先跑一次：建出 first_reversal_hist（与生产同懒建路径）
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO first_reversal_hist(code,scan_date,name,buy_price,
                    stop_loss,take_profit,fusion_score)
               VALUES ('600002',?,'回填股',10.0,9.5,11.0,50.0)""",
            (SCAN_DATE,))
        conn.execute(
            """INSERT INTO stock_signal(code,name,scan_date,trade_date,horizon,strategy,
                    buy_price,price,stop_loss,take_profit,fusion_score,pct_above_ma20)
               VALUES ('600003','实盘股','2026-10-10','2026-10-10','short','反转首日',
                       10.0,10.0,9.5,11.0,50.0,0.02)""")
    insert_new_outcomes()
    with db.get_conn() as conn:
        h = conn.execute(
            "SELECT source FROM recommend_outcome WHERE code='600002'").fetchone()
        s = conn.execute(
            "SELECT source FROM recommend_outcome WHERE code='600003'").fetchone()
        assert h["source"] == "backfill"
        assert s["source"] == "live"


# ── exit_advisor 结构化 code ──
def _df(rows):
    return pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close"]
                        ).set_index("trade_date")


def test_exit_codes_from_evaluate_exit_by_prices():
    # 止损
    df = _df([("2026-01-06", 10, 10, 10, 10), ("2026-01-07", 10, 10, 8, 8.4)])
    adv = evaluate_exit_by_prices(10.0, "2026-01-05", df, stop_loss=9.0,
                                  trailing_pct=0.05, partial_tp=0.10)
    assert adv["detail"]["exit_code"] == "stop_loss"
    # 移动止盈：day2 收盘过启动线 11，day3 收盘破 高点×0.95
    df = _df([("2026-01-06", 10, 10, 10, 10), ("2026-01-07", 10, 11, 10, 11.0),
              ("2026-01-08", 11, 11, 10, 10.4)])
    adv = evaluate_exit_by_prices(10.0, "2026-01-05", df, stop_loss=8.0,
                                  trailing_pct=0.05, partial_tp=0.10)
    assert adv["detail"]["exit_code"] == "trailing_stop"
    # 固定止盈（trailing 关闭；T+1：建仓日不判出场，第 2 日收盘达止盈价）
    df = _df([("2026-01-06", 10, 10.5, 10, 10.5), ("2026-01-07", 10.5, 11.2, 10.5, 11.2)])
    adv = evaluate_exit_by_prices(10.0, "2026-01-05", df, take_profit=11.0,
                                  trailing_pct=None)
    assert adv["detail"]["exit_code"] == "fixed_tp"
    # 到期
    df = _df([("2026-01-06", 10, 10, 10, 10), ("2026-01-07", 10, 10, 10, 10)])
    adv = evaluate_exit_by_prices(10.0, "2026-01-05", df, stop_loss=8.0,
                                  trailing_pct=0.05, partial_tp=0.10, max_hold_days=2)
    assert adv["detail"]["exit_code"] == "max_hold_days"


def test_evaluate_short_writes_exit_code():
    """隔日动量（豁免回踩）次日开盘建仓、第 2 日收盘破止损 → exit_code 落库。"""
    _put_signal("600004", horizon="short", strategy="隔日动量",
                buy=10.0, stop=9.5, tp=11.0)
    rows = [(SCAN_DATE, 10, 10, 9.9, 10)]
    rows += [(d, 10, 10, 9.0, 9.2) for d in _dates("2026-09-11", 3)]
    _put_prices("600004", rows)
    insert_new_outcomes()
    evaluate_outcomes()
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT exit_reason, exit_code, exit_return FROM recommend_outcome "
            "WHERE code='600004'").fetchone()
        assert r["exit_code"] == r["exit_reason"] == "stop_loss"
        assert r["exit_return"] < 0


def test_long_exit_after_day_130_rolls_forward():
    """原 LIMIT 130 冻结回归：止损发生在第 135 个交易日，滚动窗口必须能结算。"""
    code = "600005"
    scan = "2026-07-21"
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO recommend_outcome(code,scan_date,horizon,strategy,
                    entry_price,stop_loss,take_profit)
               VALUES (?,?,'long','长线趋势',10.0,8.5,15.0)""", (code, scan))
    days = _dates("2026-07-22", 140)
    rows = [(d, 10, 10.1, 9.9, 10.0) for d in days[:134]]
    rows += [(days[134], 10, 10, 8.0, 8.4)]          # 第 135 个交易日破止损
    rows += [(d, 8.4, 8.4, 8.0, 8.2) for d in days[135:]]
    _put_prices(code, rows)
    n = evaluate_outcomes()
    assert n >= 1
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT exit_code, exit_date, exit_return FROM recommend_outcome "
            "WHERE code=?", (code,)).fetchone()
        assert r["exit_code"] == "stop_loss"
        assert r["exit_date"] == days[134]
        # stop_fill_at_stop=1：收盘 8.4 穿线按止损价 8.5 成交
        assert r["exit_return"] == -15.0


def test_exit_code_from_text_rules():
    assert _exit_code_from_text("收盘跌破止损价 12.34") == "stop_loss"
    assert _exit_code_from_text("移动止盈：自高点 13.70 回撤 8.5% ≥ 5%，清仓") == "trailing_stop"
    assert _exit_code_from_text("收盘达到止盈价 11.00") == "fixed_tp"
    assert _exit_code_from_text("持仓满 60 个交易日，到期卖出") == "max_hold_days"
    assert _exit_code_from_text("no_fill") == "no_fill"
    assert _exit_code_from_text("未知文本") == "other"
