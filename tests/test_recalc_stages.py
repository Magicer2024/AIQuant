"""tests/test_recalc_stages.py —— 增量重算拆段测量与幂等（2026-10 优化计划 T5）

T5 验收：增量重算耗时 P95 可量化下降 + 同日重算两次存量信号 diff=0。
先立护栏再优化：
1. `recalc_incremental_signals` 返回值带 StageTimer 拆段（prepare/load_price/
   build_signal/write_signals），热点归因只认真实计时，不猜；
2. 同日重算两次 stock_signal 结果完全一致（DELETE+INSERT 写库路径幂等）；
3. `_build_signal_records` 同输入两次输出一致（排除 created_at 时间戳）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import core.sync as sync

SCAN_DATE = "2026-01-05"


@pytest.fixture
def seeded_prices(isolated_database):
    """320 个交易日的单调行情（与 test_unified_signals 同规格），可走完整重算链路"""
    from core.db import get_conn
    days = pd.bdate_range(end=SCAN_DATE, periods=320)
    with get_conn() as conn:
        conn.execute("INSERT INTO stock_info(code,name) VALUES ('600001','测试股票')")
        conn.executemany(
            "INSERT INTO daily_price(code,trade_date,open,high,low,close,volume,"
            "amount,pct_change,turnover) VALUES ('600001',?,?,?,?,?,10000000,"
            "100000000,0.1,2)",
            [(d.date().isoformat(), 10 + i * 0.01, 10.1 + i * 0.01,
              9.9 + i * 0.01, 10 + i * 0.01) for i, d in enumerate(days)])
    return SCAN_DATE


def _fixed_record(scan_date: str) -> dict:
    """确定性信号记录（覆盖 _SIGNAL_INSERT_SQL 全部命名键，写库绑定不缺键）"""
    return {
        "scan_date": scan_date, "trade_date": scan_date, "code": "600001",
        "name": "测试股票", "price": 10.0, "fusion_score": 30.0,
        "vol_score": 5, "ma_score": 5, "diverge_score": 5, "bottom_score": 5,
        "whale_score": 5, "trigger_list": "[]", "buy_price": 10.0,
        "stop_loss": 9.5, "take_profit": 10.8, "buy_volume": 100,
        "buy_money": 1000, "sent_wechat": 0, "created_at": "2026-01-05 16:00:00",
        "horizon": "short", "strategy": "超跌反弹v3", "pct_above_ma20": 0.0,
        "chip_conc": None, "vol_ratio": None, "long_mask": None,
        "vol60": None, "dd250": None, "long_rank_key": None,
    }


# ─────────────────────────────────────────────
# 1. 拆段可见性：热点归因只认真实计时
# ─────────────────────────────────────────────
def test_recalc_returns_stage_breakdown(seeded_prices, monkeypatch):
    """返回值必须带 prepare/load_price/build_signal/write_signals 拆段计时"""
    monkeypatch.setattr(sync, "_build_signal_records", lambda *a, **k: [])
    result = sync.recalc_incremental_signals(trade_dates=[seeded_prices], verbose=False)

    assert "stages" in result, "recalc 结果缺少 stages 拆段（无测量不优化）"
    stages = result["stages"]["stages"]
    for key in ("prepare", "load_price", "build_signal", "write_signals"):
        assert key in stages, f"缺少阶段 {key}"
        assert stages[key]["ms"] >= 0
    # 各段之和不应超过总墙钟（含循环外杂项，允许小于）
    assert result["stages"]["total_ms"] >= sum(s["ms"] for s in stages.values())


# ─────────────────────────────────────────────
# 2. 同日重算两次 diff=0（写库路径幂等）
# ─────────────────────────────────────────────
def test_recalc_same_day_twice_diff_zero(seeded_prices, monkeypatch):
    """同日两次增量重算后 stock_signal 全行一致，且不产生重复行"""
    from core.db import get_conn
    rec = _fixed_record(SCAN_DATE)
    monkeypatch.setattr(sync, "_build_signal_records", lambda *a, **k: [dict(rec)])

    def _dump():
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM stock_signal WHERE scan_date=? ORDER BY code",
                (SCAN_DATE,)).fetchall()
        # diff=0 比对**信号内容**：id 是 REPLACE 语义下的物理行号、
        # created_at 是写入时间戳，二者天然随重写变化，不属信号口径
        return [{k: v for k, v in dict(r).items()
                 if k not in ("id", "created_at")} for r in rows]

    sync.recalc_incremental_signals(trade_dates=[seeded_prices], verbose=False)
    first = _dump()
    sync.recalc_incremental_signals(trade_dates=[seeded_prices], verbose=False)
    second = _dump()

    assert first and second, "替身应产出至少 1 条信号"
    assert first == second, "同日重算两次 stock_signal 不一致（diff≠0）"
    assert len(second) == 1, "重复执行不得产生重复行"


# ─────────────────────────────────────────────
# 3. 计算确定性：同输入两次输出一致
# ─────────────────────────────────────────────
def test_build_signal_records_deterministic(isolated_database):
    """真实 _build_signal_records 同输入两次输出逐键一致（created_at 时间戳除外）"""
    days = pd.bdate_range(end=SCAN_DATE, periods=320)
    df = pd.DataFrame({
        "open": [10 + i * 0.01 for i in range(len(days))],
        "high": [10.1 + i * 0.01 for i in range(len(days))],
        "low": [9.9 + i * 0.01 for i in range(len(days))],
        "close": [10 + i * 0.01 for i in range(len(days))],
        "volume": [1e7] * len(days),
        "amount": [1e8] * len(days),
        "pct_change": [0.1] * len(days),
        "turnover": [2.0] * len(days),
    }, index=days)

    def _run():
        recs = sync._build_signal_records(
            df, "600001", "测试股票", 1e9, 15.0, -0.05, 0.08,
            scan_date=SCAN_DATE)
        for r in recs:
            r.pop("created_at", None)   # 时间戳天然不同，不参与确定性比较
        return recs

    assert _run() == _run()
