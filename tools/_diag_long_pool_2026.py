"""2026-07~09 窗口：long 的**票池整体**表现 vs 全市场 vs long 实际推荐。

回答一个此前没问对的问题：
  long 推荐 −3.4% 而全市场 +12.1%，到底是
    (a) 排序键差（票池本身还行，只是 top4 选得差），还是
    (b) 票池/选股逻辑本身在这段失效？
判据：在同一批采样日上，计算「票池等权」的同期收益。若票池 ≈ 全市场，是 (a)；
若票池 ≈ long 推荐，是 (b)。
"""

import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")

START, END = "2026-07-01", "2026-09-19"


def main():
    import pandas as pd
    conn = sqlite3.connect(DB)
    # 多取 300 自然日预热，保证 7/20 能算 MA120
    warm = "2025-06-01"
    q = """SELECT code, trade_date, close FROM daily_price
           WHERE trade_date >= ? AND trade_date <= ?
             AND close IS NOT NULL AND close > 0
           ORDER BY code, trade_date ASC"""
    df = pd.read_sql_query(q, conn, params=(warm, END))
    conn.close()
    print(f"载入 {len(df):,} 行 / {df['code'].nunique()} 只")

    # 每只票：算 ma60/ma120/ma20/vol60/high250/dd250/vol250，并保留 7/20 之后各日状态
    pool_by_day = defaultdict(list)     # date -> [code]
    all_by_day = defaultdict(list)
    # 记录每只票在 END 的收盘（用于算区间收益）
    last_close = {}
    close_map = {}                       # (code, date) -> close

    import math
    for code, g in df.groupby("code", sort=False):
        close = pd.Series(g["close"].values.astype("float64"),
                          index=pd.DatetimeIndex(g["trade_date"].values))
        if len(close) < 200:
            continue
        ma60 = close.rolling(60).mean()
        ma120 = close.rolling(120).mean()
        ret = close.pct_change()
        vol60 = ret.rolling(60).std() * math.sqrt(252)
        vol250 = ret.rolling(250).std() * math.sqrt(252)
        high250 = close.rolling(250).max()
        dd = 1.0 - close / high250
        last_close[code] = float(close.iloc[-1])
        for pos, d in enumerate(close.index):
            ds = d.strftime("%Y-%m-%d")
            if ds < START:
                continue
            close_map[(code, ds)] = float(close.iloc[pos])
            all_by_day[ds].append(code)
            c60, c120 = ma60.iloc[pos], ma120.iloc[pos]
            if not (math.isfinite(c60) and math.isfinite(c120)):
                continue
            b0 = c60 > c120 and close.iloc[pos] > c120
            b1 = math.isfinite(ma120.iloc[pos - 21]) and c120 > ma120.iloc[pos - 21] \
                if pos >= 21 else False
            b2 = math.isfinite(vol60.iloc[pos]) and vol60.iloc[pos] < 0.35
            b3 = math.isfinite(dd.iloc[pos]) and dd.iloc[pos] < 0.40
            score = (1.0 if b0 else 0) + (1.0 if b1 else 0) \
                + (0.5 if b2 else 0) + (0.5 if b3 else 0)
            if score >= 2.0:
                pool_by_day[ds].append(code)

    days = sorted(d for d in all_by_day if START <= d <= END)
    print(f"采样日 {len(days)} 个（{days[0]} → {days[-1]}）")

    def ret_since(code, ds):
        a = close_map.get((code, ds))
        b = last_close.get(code)
        if not a or not b or a <= 0:
            return None
        return (b - a) / a * 100

    print()
    print("=" * 92)
    print(f"信号日 → {END} 收盘的区间收益（等权）")
    print("=" * 92)
    print(f"{'信号日':<12}{'票池n':>7}{'票池均值':>10}{'票池中位':>10}"
          f"{'票池胜率':>9}{'全市场n':>8}{'全市场均值':>11}{'超额':>9}")
    diffs = []
    for d in days[::3]:
        pool = [ret_since(c, d) for c in pool_by_day[d]]
        pool = [x for x in pool if x is not None]
        mkt = [ret_since(c, d) for c in all_by_day[d]]
        mkt = [x for x in mkt if x is not None]
        if len(pool) < 20 or len(mkt) < 100:
            continue
        pm, mm = st.mean(pool), st.mean(mkt)
        diffs.append(pm - mm)
        print(f"{d:<12}{len(pool):>7}{pm:>+9.2f}%{st.median(pool):>+9.2f}%"
              f"{sum(1 for x in pool if x>0)/len(pool)*100:>8.1f}%"
              f"{len(mkt):>8}{mm:>+10.2f}%{pm-mm:>+8.2f}%")
    if diffs:
        print(f"{'平均':<12}{'':>7}{'':>10}{'':>10}{'':>9}{'':>8}{'':>11}"
              f"{st.mean(diffs):>+8.2f}%")
        print(f"  票池日均规模 {st.mean([len(pool_by_day[d]) for d in days]):.0f} 只 / "
              f"全市场 {st.mean([len(all_by_day[d]) for d in days]):.0f} 只"
              f"（占比 {st.mean([len(pool_by_day[d])/max(1,len(all_by_day[d])) for d in days])*100:.1f}%）")

    # long 实际推荐同口径
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    recs = [dict(r) for r in conn.execute(
        "SELECT code, scan_date FROM recommend_outcome "
        "WHERE COALESCE(horizon,'short')='long' AND scan_date>=?", (START,))]
    conn.close()
    vals = [ret_since(r["code"], r["scan_date"]) for r in recs]
    vals = [x for x in vals if x is not None]
    if vals:
        print()
        print(f"long 实际推荐（同口径）n={len(vals)}  均值 {st.mean(vals):+.2f}%  "
              f"中位 {st.median(vals):+.2f}%  胜率 "
              f"{sum(1 for x in vals if x>0)/len(vals)*100:.1f}%")


if __name__ == "__main__":
    main()
