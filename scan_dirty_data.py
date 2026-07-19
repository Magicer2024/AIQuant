"""
scan_dirty_data.py —— 扫描数据库中的异常价格数据
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import get_conn

print("=" * 70)
print("  数据库脏数据扫描")
print("=" * 70)

# 先看 daily_price 表结构
with get_conn() as conn:
    cols = conn.execute("PRAGMA table_info(daily_price)").fetchall()
    print("\n[表结构] daily_price 字段:")
    for c in cols:
        print(f"  - {c['name']} ({c['type']})")

with get_conn() as conn:
    # 1) 价格异常（<=0 或 > 2000 或 NaN）
    print("\n[1] 价格异常（<=0 或 > 2000）")
    rows = conn.execute("""
        SELECT code, trade_date, open, high, low, close, volume
        FROM daily_price
        WHERE close <= 0 OR close > 2000
           OR open  <= 0 OR high <= 0 OR low  <= 0
        ORDER BY close DESC
        LIMIT 20
    """).fetchall()
    print(f"  共 {len(rows)} 条（只看前 20）")
    for r in rows:
        print(f"  {r['code']} {r['trade_date']} "
              f"O={r['open']} H={r['high']} L={r['low']} C={r['close']}")

    # 2) 单日涨跌幅异常（直接看 pct_change）
    print("\n[2] 单日涨跌幅异常（|pct_change| > 30%）")
    rows = conn.execute("""
        SELECT code, trade_date, close, pct_change
        FROM daily_price
        WHERE ABS(pct_change) > 30
        ORDER BY ABS(pct_change) DESC
        LIMIT 20
    """).fetchall()
    print(f"  共 {len(rows)} 条（只看前 20）")
    for r in rows:
        print(f"  {r['code']} {r['trade_date']} "
              f"close={r['close']} pct={r['pct_change']}%")

    # 3) OHLC 不合规（high < low 等）
    print("\n[3] OHLC 不合规")
    rows = conn.execute("""
        SELECT code, trade_date, open, high, low, close
        FROM daily_price
        WHERE high < low OR high < open OR high < close
           OR low  > open OR low  > close
        LIMIT 20
    """).fetchall()
    print(f"  共 {len(rows)} 条（只看前 20）")
    for r in rows:
        print(f"  {r['code']} {r['trade_date']} "
              f"O={r['open']} H={r['high']} L={r['low']} C={r['close']}")

    # 4) 与前一日价格断崖式跳变
    print("\n[4] 跳价异常（与前一日 close 偏离 > 50%）")
    rows = conn.execute("""
        WITH t AS (
          SELECT code, trade_date, close,
                 LAG(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev_close
          FROM daily_price
        )
        SELECT code, trade_date, close, prev_close,
               ROUND(100.0 * (close - prev_close) / prev_close, 2) AS jump_pct
        FROM t
        WHERE prev_close > 0
          AND ABS(close - prev_close) / prev_close > 0.5
        ORDER BY ABS(close - prev_close) / prev_close DESC
        LIMIT 20
    """).fetchall()
    print(f"  共 {len(rows)} 条（只看前 20）")
    for r in rows:
        print(f"  {r['code']} {r['trade_date']} "
              f"prev={r['prev_close']} now={r['close']} jump={r['jump_pct']}%")

    # 5) 复权不连续（与历史均价差异 > 5 倍）
    print("\n[5] 复权不连续（close/历史 20 日均价 > 5 倍）")
    rows = conn.execute("""
        WITH t AS (
          SELECT code, trade_date, close,
                 AVG(close) OVER (
                   PARTITION BY code ORDER BY trade_date
                   ROWS BETWEEN 20 PRECEDING AND 6 PRECEDING
                 ) AS hist_avg
          FROM daily_price
        )
        SELECT code, trade_date, close, hist_avg,
               ROUND(close / hist_avg, 2) AS ratio
        FROM t
        WHERE hist_avg > 0
          AND (close / hist_avg > 5 OR close / hist_avg < 0.2)
        ORDER BY ABS(close / hist_avg - 1) DESC
        LIMIT 20
    """).fetchall()
    print(f"  共 {len(rows)} 条（只看前 20）")
    for r in rows:
        print(f"  {r['code']} {r['trade_date']} "
              f"close={r['close']} hist_avg={r['hist_avg']:.2f} ratio={r['ratio']}")

    # 6) 总览
    print("\n[总览] daily_price 数据量")
    row = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT code) AS c FROM daily_price").fetchone()
    print(f"  总行数: {row['n']:,}, 股票数: {row['c']}")

print("\n" + "=" * 70)
print("  扫描完成")
print("=" * 70)
