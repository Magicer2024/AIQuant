"""tests/test_db_write.py —— 验证 _batch_write_daily_price 写库逻辑

使用 monkeypatch 将 core.db.DB_PATH 指向临时库，隔离运行，
不会像旧版脚本那样写入生产 quant.db。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import core.db as db
from core.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把数据库句柄重定向到临时文件，返回临时库路径"""
    db_file = tmp_path / "quant_test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    init_db()
    return str(db_file)


def _mock_df() -> pd.DataFrame:
    return pd.DataFrame({
        "code": ["000001", "600519", "300750", "601318", "000858"],
        "name": ["平安银行", "贵州茅台", "宁德时代", "中国平安", "五粮液"],
        "trade_date": ["2026-06-25"] * 5,
        "open":   [10.5, 1400.0, 250.0, 45.0, 150.0],
        "close":  [10.7, 1410.0, 252.0, 45.5, 151.0],
        "high":   [10.8, 1415.0, 253.0, 46.0, 152.0],
        "low":    [10.4, 1395.0, 249.0, 44.5, 149.0],
        "volume": [1000000, 500000, 2000000, 3000000, 800000],
        "amount": [10000000, 700000000, 500000000, 140000000, 120000000],
        "pct_change": [1.9, 0.71, 0.8, 1.11, 0.67],
        "turnover": [0.5, 0.3, 1.2, 0.4, 0.6],
    })


def test_batch_write_daily_price(tmp_db):
    from core.sync import _batch_write_daily_price

    n = _batch_write_daily_price(_mock_df())
    assert n == 5, f"应写入 5 行，实际 {n}"

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, trade_date, close, pct_change, turnover "
            "FROM daily_price WHERE trade_date='2026-06-25' ORDER BY code"
        ).fetchall()

    assert len(rows) == 5, f"库中应查到 5 行，实际 {len(rows)}"
    by_code = {r["code"]: dict(r) for r in rows}
    assert by_code["000001"]["close"] == pytest.approx(10.7)
    assert by_code["600519"]["pct_change"] == pytest.approx(0.71)
    # upsert 语义：重复写入同一天数据应更新而非新增
    n2 = _batch_write_daily_price(_mock_df())
    assert n2 == 5
    with get_conn() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM daily_price WHERE trade_date='2026-06-25'"
        ).fetchone()["c"]
    assert cnt == 5, f"重复 upsert 后仍应为 5 行，实际 {cnt}"
