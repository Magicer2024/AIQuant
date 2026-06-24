"""test_db_write.py —— 验证新写库逻辑（不实际调网络）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from core.db import get_conn, init_db

init_db()
print("[OK] 数据库连接")

mock = pd.DataFrame({
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
print(f"[OK] 模拟 {len(mock)} 行")

from core.sync import _batch_write_daily_price
n = _batch_write_daily_price(mock)
print(f"[OK] _batch_write_daily_price 写入 {n} 行")

with get_conn() as conn:
    rows = conn.execute(
        "SELECT code, trade_date, close, pct_change, turnover "
        "FROM daily_price WHERE trade_date='2026-06-25' "
        "AND code IN ('000001','600519','300750','601318','000858') "
        "ORDER BY code"
    ).fetchall()
    print(f"\n[DB 验证] 数据库中查询到 {len(rows)} 行:")
    for r in rows:
        print(f"  {dict(r)}")
