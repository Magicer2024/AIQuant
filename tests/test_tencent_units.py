"""tests/test_tencent_units.py —— 腾讯兜底数据源单位换算回归

背景：akshare stock_zh_a_hist_tx 的 amount 列实为成交量(手)，曾被当成
「成交额(万元)」×10000，并用 amount/close 反推 volume，导致 2026-05~06
部分交易日成交量放大约 5 倍（002009 2026-06-26 真实 175,897 手）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import core.db as db
from core.db import get_conn, init_db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "quant_test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    init_db()
    return str(db_file)


def test_tencent_fallback_units(tmp_db, monkeypatch):
    import akshare as ak
    from core.sync import _sync_one_stock_tencent

    # 与真实接口一致：列名 amount，但值是成交量(手)
    raw = pd.DataFrame({
        "date": ["2026-06-25", "2026-06-26"],
        "open": [20.32, 19.87],
        "close": [19.96, 19.09],
        "high": [20.80, 19.91],
        "low": [19.85, 19.09],
        "amount": [182250, 175897],
    })
    monkeypatch.setattr(ak, "stock_zh_a_hist_tx", lambda **kw: raw)

    assert _sync_one_stock_tencent("002009", "20260625", "20260626") is True

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_price WHERE code='002009' ORDER BY trade_date").fetchall()
    by_date = {r["trade_date"]: dict(r) for r in rows}
    assert set(by_date) == {"2026-06-25", "2026-06-26"}

    d = by_date["2026-06-26"]
    assert d["volume"] == pytest.approx(175897 * 100)          # 手 → 股
    assert d["amount"] == pytest.approx(175897 * 100 * 19.09)  # 估算成交额=股数×收盘
    assert d["pct_change"] == pytest.approx((19.09 / 19.96 - 1) * 100, abs=1e-4)
    assert d["turnover"] == 0
