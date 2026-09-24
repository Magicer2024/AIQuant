"""量化 daily_price 的完整性：行覆盖、日期间隔、价源混用对自算指标的影响。"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
pd.set_option('display.width', 220)

con = sqlite3.connect('core/quant.db')

print("=== 1) 每只股票在 2024-01-01~2026-09-23 的行数分布 ===")
n = pd.read_sql("SELECT code, COUNT(*) n, MIN(trade_date) mn, MAX(trade_date) mx "
                "FROM daily_price WHERE trade_date>='2024-01-01' GROUP BY code", con)
print(n['n'].describe(percentiles=[.05, .25, .5, .75, .95]).to_string())
_ndays = con.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_price "
                     "WHERE trade_date>='2024-01-01' AND trade_date<='2026-09-23'").fetchone()[0]
print(f"\n  交易日总数（全市场去重）: {_ndays}")
print(f"  行数 >= 600 的股票数: {(n['n'] >= 600).sum()} / {len(n)}")
print(f"  行数 <  300 的股票数: {(n['n'] < 300).sum()} / {len(n)}")

print("\n=== 2) 每只股票的日期间隔分布（2024 起） ===")
d = pd.read_sql("SELECT code, trade_date FROM daily_price WHERE trade_date>='2024-01-01' "
                "ORDER BY code, trade_date", con)
d['gap'] = pd.to_datetime(d['trade_date']).groupby(d['code']).diff().dt.days
g = d['gap'].dropna()
print(f"  间隔=1~3天(相邻交易日): {100 * (g <= 3).mean():.2f}%   "
      f"4~10天: {100 * ((g > 3) & (g <= 10)).mean():.2f}%   >10天: {100 * (g > 10).mean():.2f}%")

print("\n=== 3) 按日期看：每日股票数（抽样近 20 个交易日） ===")
dd = d.groupby('trade_date')['code'].count()
print(dd.tail(20).to_string())

print("\n=== 4) turnover>0 行占比（按年） ===")
t = pd.read_sql("SELECT substr(trade_date,1,4) y, "
                "SUM(CASE WHEN turnover>0 THEN 1 ELSE 0 END) pos, COUNT(*) tot "
                "FROM daily_price WHERE trade_date>='2024-01-01' GROUP BY y", con)
t['ratio%'] = (100 * t['pos'] / t['tot']).round(1)
print(t.to_string(index=False))

print("\n=== 5) 关键：W5 命中样本落在哪类行？ ===")
x = pd.read_sql("SELECT code,trade_date,close,volume,turnover,pct_change FROM daily_price "
                "WHERE trade_date>='2023-06-01' ORDER BY code,trade_date", con)
x = x[~x['code'].str.startswith(('688', '689', '8', '9'))].copy()
x['pc_calc'] = (x['close'] / x.groupby('code', sort=False)['close'].shift(1) - 1) * 100
x['gap'] = pd.to_datetime(x['trade_date']).groupby(x['code']).diff().dt.days
ma20_c = x.groupby('code', sort=False)['close'].transform(lambda s: s.rolling(20).mean())
x['ma20_prev_c'] = ma20_c.groupby(x['code'], sort=False).shift(1)
x['close_prev'] = x.groupby('code', sort=False)['close'].shift(1)
w = x[(x['trade_date'] >= '2024-07-01') & (x['trade_date'] <= '2026-09-23')]

w5_calc = ((w['close'] > ma20_c.loc[w.index]) & (w['close_prev'] <= w['ma20_prev_c'])
           & (w['pc_calc'] >= 5) & (w['pc_calc'] < 9.8))
print(f"  自算口径 W5 命中 n={int(w5_calc.sum())}")
sub = w[w5_calc]
print(f"    其中 turnover>0 行: {int((sub['turnover'] > 0).sum())} "
      f"({100 * (sub['turnover'] > 0).mean():.1f}%)")
print(f"    其中 与前一行间隔>3天: {int((sub['gap'] > 3).sum())} "
      f"({100 * (sub['gap'] > 3).mean():.1f}%)")
print(f"    其中 |自算-字段|>1%: {int(((sub['pc_calc'] - sub['pct_change']).abs() > 1).sum())} "
      f"({100 * ((sub['pc_calc'] - sub['pct_change']).abs() > 1).mean():.1f}%)")
print(f"    用 pct_change 字段判定>=5% 的: {int((sub['pct_change'] >= 5).sum())} "
      f"({100 * (sub['pct_change'] >= 5).mean():.1f}%)")
print(f"\n  自算 W5 的 pc_calc 分布: 均值 {sub['pc_calc'].mean():.2f}% 中位 {sub['pc_calc'].median():.2f}%")
print(f"  同批样本 pct_change 分布: 均值 {sub['pct_change'].mean():.2f}% 中位 {sub['pct_change'].median():.2f}%")
con.close()
