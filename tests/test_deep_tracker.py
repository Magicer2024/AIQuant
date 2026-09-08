"""tests/test_deep_tracker.py —— 个股深度推荐跟踪引擎测试

覆盖 strategy/deep_tracker.py 的买点/卖点执行与逐笔统计：
 1. 回踩买点成交：次日 low 触及买点 → 按买价成交（不追高）
 2. 跳空低开：开盘已低于买点 → 按开盘价成交
 3. 窗口内未回踩 → expired（不计入胜率统计）
 4. T+1 规则：建仓当日大跌也不触发止损
 5. 止损出场 + 持仓交易日/收益结算
 6. 固定止盈：收盘达推荐止盈价（本股可达位）即离场；盘中冲高不算
 7. 到期了结：持满 max_hold_days 按收盘平掉
 8. 建仓后长期无数据（全市场已前进）→ delisted，不计盈亏
 9. 建仓日即全市场最新交易日 → 保持 holding，不误判
10. 统计：胜率 / 平均持仓 / 盈亏比 / 出场原因分布
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import strategy_params as SP
from strategy import deep_tracker as dt

# 固定参数，隔离 DB 覆盖值，保证测试可重复
SP.DEEP_TRACK.update({
    "enabled": True,
    "daily_top_n": None,        # 测试不裁剪候选
    "entry_window_days": 5,
    "max_hold_days": 10,
    "stop_loss_pct": -0.06,     # 止损 -6%
})

# 交易日序列（2026-01-05 起连续 20 个自然日，全部视作交易日，便于算 hold_days）
DATES = [f"2026-01-{d:02d}" for d in range(5, 25)]


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE daily_price (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL, trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        UNIQUE(code, trade_date)
    );
    CREATE TABLE stock_deep_signal (
        scan_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
        level TEXT, label TEXT, emoji TEXT, rhythm_leg TEXT, rhythm_hint TEXT,
        pct_above_ma20 REAL, entry_price REAL, stop_loss REAL, take_profit REAL,
        reasons TEXT, created_at TEXT, PRIMARY KEY (scan_date, code)
    );
    """)
    dt.ensure_table(c)
    return c


def _bars(c, code, rows, start=0):
    """rows: [(open, high, low, close), ...] 从 DATES[start] 开始逐日写入。"""
    for i, (o, h, l, cl) in enumerate(rows):
        c.execute(
            "INSERT OR REPLACE INTO daily_price (code, trade_date, open, high, low, close)"
            " VALUES (?,?,?,?,?,?)", (code, DATES[start + i], o, h, l, cl))
    c.commit()


def _signal(c, code, scan_date, entry=10.0, stop=9.4, tp=11.5, level="buy"):
    c.execute(
        """INSERT OR REPLACE INTO stock_deep_signal
           (scan_date, code, name, level, label, entry_price, stop_loss, take_profit, reasons)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (scan_date, code, "T" + code, level, "可建仓", entry, stop, tp, "[]"))
    c.commit()


def _one(c, code):
    return c.execute("SELECT * FROM deep_track WHERE code = ?", (code,)).fetchone()


# ── 1 & 2. 建仓价口径 ─────────────────────────────────────────────
def test_fill_on_pullback():
    """回踩买点成交：low 触及买点 → 按买价 10.00 成交（不是按收盘 10.2 追）"""
    c = _conn()
    _bars(c, "000001", [(10.2, 10.3, 10.1, 10.2)])          # DATES[0] = 信号日，收 10.2
    _bars(c, "000001", [(10.5, 10.6, 9.80, 10.2)], start=1)  # 次日冲高回落，low 触及 10.0
    _signal(c, "000001", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000001")
    assert r["status"] == "holding", r["status"]
    assert r["entry_date"] == DATES[1]
    assert abs(r["entry_price"] - 10.0) < 1e-6, r["entry_price"]
    print("✓ 回踩买点成交 → 按买价 10.00")


def test_fill_on_gap_down():
    """跳空低开：开盘已低于买点 → 按开盘价 9.50 成交"""
    c = _conn()
    _bars(c, "000002", [(10.2, 10.3, 10.1, 10.2)])
    _bars(c, "000002", [(9.50, 9.70, 9.40, 9.60)], start=1)
    _signal(c, "000002", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000002")
    assert r["status"] == "holding"
    assert abs(r["entry_price"] - 9.50) < 1e-6, r["entry_price"]
    print("✓ 跳空低开 → 按开盘价 9.50 成交")


# ── 3. 窗口内未回踩 → 失效 ─────────────────────────────────────────
def test_expire_when_no_pullback():
    """一路涨不回踩 → 5 个交易日后判失效，且不计入胜率统计"""
    c = _conn()
    _bars(c, "000003", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000003", [(10.5, 10.8, 10.4, 10.7)] * 6, start=1)  # 连续 6 天不回踩
    _signal(c, "000003", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000003")
    assert r["status"] == "expired", r["status"]
    assert r["entry_date"] is None
    st = dt.get_stats(c)
    assert st["expired"] == 1 and st["total_closed"] == 0, st
    assert st["win_rate"] is None   # 失效不计入胜率
    print("✓ 窗口内未回踩 → expired，不污染胜率统计")


# ── 4. T+1 规则 ───────────────────────────────────────────────────
def test_t1_no_exit():
    """建仓当日（T+1 买入日）即使跌破止损也不出场"""
    c = _conn()
    _bars(c, "000004", [(10.0, 10.1, 9.9, 10.0)])
    # 次日建仓日：直接砸到 8.8（远低于止损 9.4），但当日不判出场
    _bars(c, "000004", [(9.6, 9.7, 8.80, 8.80)], start=1)
    _bars(c, "000004", [(9.0, 9.2, 8.9, 9.10)] * 3, start=2)  # 之后一直在止损下方
    _signal(c, "000004", DATES[0], entry=10.0, stop=9.4)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000004")
    assert r["entry_date"] == DATES[1]
    assert r["status"] == "closed"
    assert r["exit_date"] == DATES[2], r["exit_date"]   # T+1 当天不出，次日才止损
    assert r["exit_reason"] == "stop_loss"
    print(f"✓ T+1 不判出场 → 建仓日 {DATES[1]} 不出，{DATES[2]} 止损")


# ── 5. 止损出场 + 结算 ────────────────────────────────────────────
def test_stop_loss_and_settlement():
    """止损出场：收益/持仓交易日/最大浮亏结算正确"""
    c = _conn()
    _bars(c, "000005", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000005", [(10.1, 10.2, 10.0, 10.1)], start=1)   # 建仓 @10.00，收 10.1
    _bars(c, "000005", [(10.0, 10.0, 9.30, 9.30)], start=2)   # 收 9.30 ≤ 止损 9.4
    _signal(c, "000005", DATES[0], entry=10.0, stop=9.4)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000005")
    assert r["status"] == "closed" and r["exit_reason"] == "stop_loss"
    assert abs(r["exit_price"] - 9.30) < 1e-6
    assert abs(r["return_pct"] - (-7.0)) < 0.01, r["return_pct"]     # (9.3-10)/10
    assert r["hold_tdays"] == 1, r["hold_tdays"]                     # 建仓日到出场日 1 个交易日
    assert r["hold_days"] == 1                                       # 自然日也是 1
    assert abs(r["min_return"] - (-7.0)) < 0.01, r["min_return"]
    print(f"✓ 止损出场：{r['return_pct']}% · 持仓 {r['hold_tdays']} 个交易日")


# ── 6. 固定止盈（本股可达位）──────────────────────────────────────
def test_take_profit():
    """收盘达到推荐止盈价 sig_tp 即离场；T+1 建仓日不判"""
    c = _conn()
    _bars(c, "000006", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000006", [(10.0, 10.2, 9.95, 10.1)], start=1)   # 建仓 @10.00，T+1 不判
    _bars(c, "000006", [(10.2, 11.8, 10.2, 11.60)], start=2)  # 收 11.60 ≥ 止盈 11.5 → 出场
    _signal(c, "000006", DATES[0], entry=10.0, stop=9.4, tp=11.5)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000006")
    assert r["status"] == "closed"
    assert r["exit_reason"] == "take_profit", r["exit_reason"]
    assert abs(r["exit_price"] - 11.60) < 1e-6
    assert abs(r["return_pct"] - 16.0) < 0.01, r["return_pct"]
    assert r["hold_tdays"] == 1, r["hold_tdays"]
    assert abs(r["max_return"] - 18.0) < 0.01, r["max_return"]   # 盘中最高 11.8 → +18%
    print(f"✓ 固定止盈：+{r['return_pct']}% · 持仓 {r['hold_tdays']} 日 · 最大浮盈 {r['max_return']}%")


def test_no_take_on_intraday_spike():
    """盘中 high 冲过止盈价但收盘没站上 → 不出场（收盘价口径，不做插针触发）"""
    c = _conn()
    _bars(c, "000007", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000007", [(10.0, 10.3, 9.95, 10.2)], start=1)
    _bars(c, "000007", [(10.2, 11.7, 10.1, 11.0)], start=2)   # high 11.7 > 11.5 但收 11.0
    _bars(c, "000007", [(11.0, 11.2, 10.8, 11.1)], start=3)
    _signal(c, "000007", DATES[0], entry=10.0, stop=9.4, tp=11.5)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000007")
    assert r["status"] == "holding", (r["status"], r["exit_reason"])
    print("✓ 盘中冲高未收盘确认 → 继续持有")


# ── 7. 到期了结 ───────────────────────────────────────────────────
def test_max_hold_exit():
    """持满 max_hold_days(10) 个交易日 → 按当日收盘了结"""
    c = _conn()
    _bars(c, "000008", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000008", [(10.0, 10.05, 9.95, 10.0)] * 12, start=1)  # 横盘，不触发任何出场
    _signal(c, "000008", DATES[0], entry=10.0, stop=9.4)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000008")
    assert r["status"] == "closed"
    assert r["exit_reason"] == "max_hold_days", r["exit_reason"]
    assert r["hold_tdays"] == 9, r["hold_tdays"]      # j 从 1 数到 10，建仓日不算
    assert r["exit_date"] == DATES[10]
    assert abs(r["return_pct"]) < 0.01
    print(f"✓ 到期了结：持仓 {r['hold_tdays']} 个交易日后按收盘平掉")


# ── 8 & 9. 停牌 / 最新交易日 ──────────────────────────────────────
def test_delisted():
    """建仓后长期无数据（全市场已前进）→ delisted，不计盈亏"""
    c = _conn()
    _bars(c, "000009", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000009", [(10.0, 10.05, 9.95, 10.0)], start=1)
    _bars(c, "999999", [(10.0, 10.05, 9.95, 10.0)] * 15, start=0)  # 别的票把市场推到 DATES[14]
    _signal(c, "000009", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000009")
    assert r["status"] == "closed"
    assert r["exit_reason"] == "delisted", r["exit_reason"]
    assert r["return_pct"] == 0
    print("✓ 建仓后无数据（全市场已前进）→ delisted，不计盈亏")


def test_hold_when_entry_is_last_bar():
    """建仓日就是全市场最新交易日 → 保持 holding，不误判 delisted"""
    c = _conn()
    _bars(c, "000010", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000010", [(10.0, 10.05, 9.95, 10.0)], start=1)   # 次日建仓，也是最后一根 K
    _signal(c, "000010", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    r = _one(c, "000010")
    assert r["status"] == "holding", (r["status"], r["exit_reason"])
    assert r["entry_date"] == DATES[1]
    print("✓ 建仓日即最新交易日 → 保持 holding")


# ── 10. 统计 ──────────────────────────────────────────────────────
def test_stats():
    """胜率 / 平均持仓 / 盈亏比 / 出场原因分布"""
    c = _conn()
    # 两笔：A 止盈 +10%（收盘 11.0 达止盈价 11.0，持 2 交易日），B 止损 -7%（1 日）
    _bars(c, "100001", [(10.0, 10.1, 9.9, 10.0), (10.0, 10.2, 9.95, 10.1),
                        (10.2, 11.0, 10.2, 10.9), (10.9, 11.5, 10.8, 11.0)])
    _signal(c, "100001", DATES[0], entry=10.0, stop=9.4, tp=11.0)
    _bars(c, "100002", [(10.0, 10.1, 9.9, 10.0), (10.1, 10.2, 10.0, 10.1),
                        (10.0, 10.0, 9.30, 9.30)])
    _signal(c, "100002", DATES[0], entry=10.0, stop=9.4)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    st = dt.get_stats(c)
    assert st["total_closed"] == 2, st
    assert st["win_rate"] == 50.0, st["win_rate"]
    assert abs(st["avg_return"] - 1.5) < 0.01, st["avg_return"]      # (10 - 7) / 2
    assert st["avg_hold_tdays"] == 1.5, st["avg_hold_tdays"]         # (2 + 1) / 2
    assert abs(st["profit_factor"] - 10 / 7) < 0.01, st["profit_factor"]
    assert st["by_reason"] == {"止盈": 1, "止损": 1}, st["by_reason"]
    print(f"✓ 统计：胜率 {st['win_rate']}% · 平均持仓 {st['avg_hold_tdays']} 日 · "
          f"盈亏比 {st['profit_factor']}")


def test_tracks_filter_and_order():
    """列表过滤：只取 holding/closed；排序：按推荐日 scan_date 从近到远。

    旧版按状态分组（holding → watching → closed）会把新推荐的票压到老单后面，
    用户要的是"最近推荐的先看"，故排序只看 scan_date（同日按代码升序）。
    """
    c = _conn()
    # A：DATES[0] 推荐 → 次日回踩建仓 → 持仓中
    _bars(c, "000001", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000001", [(10.2, 10.3, 9.90, 10.1)], start=1)
    _bars(c, "000001", [(10.1, 10.2, 10.0, 10.1)] * 5, start=2)
    _signal(c, "000001", DATES[0], entry=10.0, stop=9.4)
    # B：DATES[3] 推荐（比 A 新）→ 建仓后次日止损 → 已清仓
    _bars(c, "000002", [(10.0, 10.1, 9.9, 10.0)] * 4)
    _bars(c, "000002", [(10.2, 10.3, 9.90, 10.1)], start=4)
    _bars(c, "000002", [(10.0, 10.0, 9.30, 9.30)], start=5)
    _signal(c, "000002", DATES[3], entry=10.0, stop=9.4)
    # C：DATES[0] 推荐 → 一路涨不回踩 → 已失效
    _bars(c, "000003", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "000003", [(10.5, 10.8, 10.4, 10.7)] * 6, start=1)
    _signal(c, "000003", DATES[0], entry=10.0)
    # D：DATES[6] 推荐 → 之后无行情 → 待回踩
    _bars(c, "000004", [(10.0, 10.1, 9.9, 10.0)] * 7)
    _signal(c, "000004", DATES[6], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.sync_from_scan(c, DATES[3])
    dt.sync_from_scan(c, DATES[6])
    dt.update_open_tracks(c)

    # 只取 holding / closed
    items = dt.get_tracks(c, status="holding,closed")
    codes = [it["code"] for it in items]
    assert codes == ["000002", "000001"], codes      # B(DATES[3]) 新于 A(DATES[0])
    assert "000003" not in codes and "000004" not in codes

    # 单状态过滤仍兼容
    assert [it["code"] for it in dt.get_tracks(c, status="holding")] == ["000001"]
    assert [it["code"] for it in dt.get_tracks(c, status="closed")] == ["000002"]

    # 全量：按 scan_date 从近到远；同一天按代码升序
    all_codes = [it["code"] for it in dt.get_tracks(c)]
    assert all_codes == ["000004", "000002", "000001", "000003"], all_codes
    print(f"✓ 过滤 holding,closed → {codes}（推荐日从近到远）")


def test_sync_idempotent():
    """重复 sync 不产生重复跟踪单"""
    c = _conn()
    _bars(c, "100003", [(10.0, 10.1, 9.9, 10.0)])
    _signal(c, "100003", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.sync_from_scan(c, DATES[0])
    n = c.execute("SELECT COUNT(*) AS c FROM deep_track WHERE code='100003'").fetchone()["c"]
    assert n == 1, n
    print("✓ 重复同步不产生重复单")


def test_close_manual():
    """手动平仓：按指定价结算，收益与持仓天数正确"""
    c = _conn()
    _bars(c, "100004", [(10.0, 10.1, 9.9, 10.0)])
    _bars(c, "100004", [(10.0, 10.05, 9.95, 10.0)] * 5, start=1)
    _signal(c, "100004", DATES[0], entry=10.0)
    dt.sync_from_scan(c, DATES[0])
    dt.update_open_tracks(c)
    tid = _one(c, "100004")["id"]
    res = dt.close_track(c, tid, price=11.0, reason="manual")
    assert abs(res["return_pct"] - 10.0) < 0.01, res
    r = _one(c, "100004")
    assert r["status"] == "closed" and r["exit_reason"] == "manual"
    assert r["hold_tdays"] == 4, r["hold_tdays"]   # 建仓 DATES[1] → 平仓 DATES[5]
    print(f"✓ 手动平仓：+{res['return_pct']}% · 持仓 {r['hold_tdays']} 个交易日")


if __name__ == "__main__":
    for fn in (
        test_fill_on_pullback, test_fill_on_gap_down, test_expire_when_no_pullback,
        test_t1_no_exit, test_stop_loss_and_settlement, test_take_profit,
        test_no_take_on_intraday_spike, test_max_hold_exit, test_delisted,
        test_hold_when_entry_is_last_bar, test_stats, test_tracks_filter_and_order,
        test_sync_idempotent, test_close_manual,
    ):
        fn()
    print("\n全部通过 ✅")
