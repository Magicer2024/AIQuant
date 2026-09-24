"""验证 daily_price 的 close 列是否在行级别混用两个价源。"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
pd.set_option('display.width', 220)

con = sqlite3.connect('core/quant.db')

print("=== 000001 平安银行 2024-03-15~2024-03-26 ===")
d = pd.read_sql("SELECT trade_date,open,high,low,close,volume,turnover,pct_change "
                "FROM daily_price WHERE code='000001' "
                "AND trade_date BETWEEN '2024-03-15' AND '2024-03-26' ORDER BY trade_date", con)
print(d.to_string(index=False))

print("\n=== 600641 2026-09-10~2026-09-23 ===")
d2 = pd.read_sql("SELECT trade_date,open,high,low,close,volume,turnover,pct_change "
                 "FROM daily_price WHERE code='600641' AND trade_date>='2026-09-10' "
                 "ORDER BY trade_date", con)
print(d2.to_string(index=False))

print("\n=== turnover 分组：|自算-字段| 一致率（2024 起，剔科创/北交） ===")
df = pd.read_sql("SELECT code,trade_date,close,turnover,pct_change FROM daily_price "
                 "WHERE trade_date>='2024-01-01' ORDER BY code,trade_date", con)
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df['pc_calc'] = (df['close'] / df.groupby('code', sort=False)['close'].shift(1) - 1) * 100
d = df.dropna(subset=['pc_calc', 'pct_change'])
d = d[d['close'] > 0]
d['diff'] = (d['pc_calc'] - d['pct_change']).abs()
for name, sub in [('turnover>0', d[d['turnover'] > 0]),
                  ('turnover 空/0', d[~(d['turnover'] > 0)])]:
    print(f"  {name:<14} n={len(sub):>9}  一致(<=0.05) {100 * (sub['diff'] <= 0.05).mean():6.2f}%"
          f"   >2 {100 * (sub['diff'] > 2).mean():6.2f}%")

print("\n=== turnover>0 行占比（按月，2024 起） ===")
df['ym'] = df['trade_date'].str[:7]
tt = df.groupby('ym').apply(lambda s: pd.Series({
    'rows': len(s), 'pos_ratio': 100 * (s['turnover'] > 0).mean()}))
print(tt.to_string())

print("\n=== 000001 行级价源切换：turnover>0 段内 close 是否连续 ===")
s = pd.read_sql("SELECT trade_date,close,turnover,pct_change FROM daily_price "
                "WHERE code='000001' AND trade_date>='2024-01-01' ORDER BY trade_date", con)
s['grp'] = (s['turnover'] > 0).astype(int)
print(s.groupby('grp')['close'].agg(['count', 'min', 'max', 'mean']).to_string())
s['pc'] = (s['close'] / s['close'].shift(1) - 1) * 100
s['ok'] = (s['pc'] - s['pct_change']).abs() <= 0.05
print("\n  turnover>0 行内自算一致的占比: %.2f%%" % (100 * s[s['grp'] == 1]['ok'].mean()))
print("  turnover=0 行内自算一致的占比: %.2f%%" % (100 * s[s['grp'] == 0]['ok'].mean()))
con.close()
