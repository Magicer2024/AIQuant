"""决定性检验：
A) 行内 OHLC 是否同源（决定 OC 口径可否使用）
B) 连续同类行的自算一致率（判断 turnover 分组是否等价于价源分组）
C) volume / amount 是否也随价源切换（决定量比口径）
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
pd.set_option('display.width', 220)

con = sqlite3.connect('core/quant.db')
d = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,amount,turnover,pct_change "
                "FROM daily_price WHERE trade_date>='2024-01-01' ORDER BY code,trade_date", con)
con.close()
d = d[~d['code'].str.startswith(('688', '689', '8', '9'))].copy()
d = d[d['close'] > 0].reset_index(drop=True)

d['src'] = (d['turnover'] > 0).astype(int)
d['code_id'] = d['code']
d['prev_src'] = d.groupby('code_id', sort=False)['src'].shift(1)
d['pc_calc'] = (d['close'] / d.groupby('code_id', sort=False)['close'].shift(1) - 1) * 100
d['diff'] = (d['pc_calc'] - d['pct_change']).abs()
d['same_src'] = d['src'] == d['prev_src']

print("=== A/B) 同类连续行（价源未切换）自算一致率 ===")
for s in (0, 1):
    sub = d[(d['src'] == s) & d['same_src']].dropna(subset=['diff'])
    print(f"  src={s} 且前一行同源  n={len(sub):>8}  |自算-字段|<=0.05: {100 * (sub['diff'] <= 0.05).mean():6.2f}%"
          f"   >1: {100 * (sub['diff'] > 1).mean():5.2f}%")
sub = d[d['same_src']].dropna(subset=['diff'])
print(f"  全部同源切换 n={len(sub):>8}  |自算-字段|<=0.05: {100 * (sub['diff'] <= 0.05).mean():6.2f}%"
      f"   >1: {100 * (sub['diff'] > 1).mean():5.2f}%")
sub = d[~d['same_src']].dropna(subset=['diff'])
print(f"  价源切换处   n={len(sub):>8}  |自算-字段|<=0.05: {100 * (sub['diff'] <= 0.05).mean():6.2f}%"
      f"   >1: {100 * (sub['diff'] > 1).mean():5.2f}%")

print("\n=== C) 行内 OHLC 关系（是否自洽：low<=open,close<=high） ===")
bad = ((d['open'] > d['high']) | (d['open'] < d['low'])
       | (d['close'] > d['high']) | (d['close'] < d['low'])).mean()
print(f"  OHLC 越界行占比: {100 * bad:.3f}%")

print("\n=== D) volume 是否随价源切换（同源 vs 切换处的 volume 比 / amount 比） ===")
d['vr'] = d['volume'] / d.groupby('code_id', sort=False)['volume'].shift(1)
d['ar'] = d['amount'] / d.groupby('code_id', sort=False)['amount'].shift(1)
for lbl, sub in [('同源', d[d['same_src']]), ('切换处', d[~d['same_src']])]:
    s = sub.dropna(subset=['vr', 'ar'])
    print(f"  {lbl:<6} n={len(s):>8}  volume比中位 {s['vr'].median():6.3f}  "
          f"P99 {s['vr'].quantile(.99):8.2f}   |  amount比中位 {s['ar'].median():6.3f}  "
          f"P99 {s['ar'].quantile(.99):8.2f}")

print("\n=== E) amount/volume vs close 的量级关系（判断 amount 是否为原始值） ===")
d['vwap'] = d['amount'] / d['volume'].replace(0, np.nan)
d['ratio'] = d['vwap'] / d['close']
for s in (0, 1):
    sub = d[d['src'] == s].dropna(subset=['ratio'])
    print(f"  src={s}  n={len(sub):>8}  (amount/volume)/close 中位 {sub['ratio'].median():.4f}  "
          f"P5 {sub['ratio'].quantile(.05):.4f}  P95 {sub['ratio'].quantile(.95):.4f}")

print("\n=== F) 用 pct_change 重建序列后，自算涨幅与字段一致率 ===")
d['r'] = d['pct_change'] / 100
d['r_c'] = d.groupby('code_id', sort=False)['r'].transform(lambda s: (1 + s).cumprod())
chk = d.groupby('code_id', sort=False)['r'].transform(lambda s: s.shift(1))
print(f"  重建序列自算涨幅 vs pct_change 一致率: {100 * ((d['r_c'] / (1 + chk) - 1) * 100 - d['pct_change']).abs().lt(0.01).mean():.2f}%")
print(f"  pct_change 异常值(|r|>0.25)行数: {int((d['r'].abs() > 0.25).sum())} "
      f"({100 * (d['r'].abs() > 0.25).mean():.4f}%)")
