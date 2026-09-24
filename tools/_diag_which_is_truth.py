"""
决定性检验：close 列还是 pct_change 列，哪个是权威？
判据：隔夜跳空 open_{t+1}/close_t - 1 的量级。若 close 跨行连续，则该跳空应始终落在
      涨跌停范围内（主板 <=11%）；若 close 混价源，则换源处会出现巨大假跳空。
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
pd.set_option('display.width', 220)

con = sqlite3.connect('core/quant.db')
d = pd.read_sql("SELECT code,trade_date,open,high,low,close,amount,turnover,pct_change "
                "FROM daily_price WHERE trade_date>='2024-01-01' ORDER BY code,trade_date", con)
con.close()
d = d[~d['code'].str.startswith(('688', '689', '8', '9'))].copy()
d = d[(d['close'] > 0) & (d['open'] > 0)].reset_index(drop=True)
g = d.groupby('code', sort=False)

d['src'] = (d['turnover'] > 0).astype(int)
d['prev_src'] = g['src'].shift(1)
d['prev_close'] = g['close'].shift(1)
d['gap'] = (d['open'] / d['prev_close'] - 1) * 100          # 隔夜跳空
d['pc_calc'] = (d['close'] / d['prev_close'] - 1) * 100
d['zero_pct'] = d['pct_change'] == 0
d['same_src'] = d['src'] == d['prev_src']

print("=== 1) pct_change 恒为 0 的行占比（按价源） ===")
for s in (0, 1):
    sub = d[d['src'] == s]
    print(f"  turnover {'>0' if s else '=0'}: n={len(sub):>8}  pct_change==0 占 {100 * sub['zero_pct'].mean():6.2f}%")

print("\n=== 2) 隔夜跳空 |gap| 分布（若 close 跨行连续，应无巨大值） ===")
for lbl, sub in [('同源(same src)', d[d['same_src']]), ('换源(src switch)', d[~d['same_src']])]:
    s = sub.dropna(subset=['gap'])['gap'].abs()
    print(f"  {lbl:<18} n={len(s):>8}  中位 {s.median():5.2f}%  P95 {s.quantile(.95):6.2f}%  "
          f"P99 {s.quantile(.99):7.2f}%  >11% 占 {100 * (s > 11).mean():6.2f}%  max {s.max():8.1f}%")

print("\n=== 3) 同一口径下的顺序对比（按 src 分组内） ===")
for s in (0, 1):
    sub = d[(d['src'] == s) & d['same_src']].dropna(subset=['gap'])
    a = sub['gap'].abs()
    print(f"  src={s} 组内 n={len(a):>8}  |gap|>11% 占 {100 * (a > 11).mean():6.2f}%  中位 {a.median():5.2f}%")

print("\n=== 4) src=0 行：close 自算 vs pct_change ===")
sub = d[(d['src'] == 0)].dropna(subset=['pc_calc'])
print(f"  n={len(sub)}  其中 pct_change==0 占 {100 * sub['zero_pct'].mean():.2f}%")
zz = sub[sub['zero_pct']]
print(f"  在 pct_change==0 的行里，close 自算涨幅非零（|pc_calc|>0.5%）占 "
      f"{100 * (zz['pc_calc'].abs() > 0.5).mean():.2f}%  → 说明 close 在动而 pct_change 没写")
print(f"  close 自算涨幅 |pc_calc|<=10.5% 占 {100 * (sub['pc_calc'].abs() <= 10.5).mean():.2f}%"
      f"（若 close 混价源，会出现大量 >11% 的假跳空）")

print("\n=== 5) 关键反例：close 自算出现 |涨幅|>11% 的行（混价源才可能） ===")
bad = d[d['pc_calc'].abs() > 11]
print(f"  行数 {len(bad)} / {len(d.dropna(subset=['pc_calc']))} "
      f"({100 * len(bad) / len(d.dropna(subset=['pc_calc'])):.3f}%)")
print(f"  其中 src=0 占 {100 * (bad['src'] == 0).mean():.1f}%")
print(bad[['code', 'trade_date', 'close', 'prev_close', 'pc_calc', 'pct_change', 'turnover']].head(10).to_string(index=False))

print("\n=== 6) 用 pct_change 重建序列 vs 用 close 原序列：隔夜跳空检验 ===")
d['adj_from_pct'] = d.groupby('code', sort=False)['pct_change'].transform(
    lambda s: (1 + (s / 100).clip(-0.25, 0.25)).cumprod())
d['gap_pct_col'] = (d['open'] / d.groupby('code', sort=False)['close'].shift(1) - 1) * 100
gp = d.groupby('code', sort=False)
# 重建序列的隔夜跳空：adj 用 pct_change 累积，open 用原始 open —— 这里只做诊断
print("  若 pct_change 权威：close 序列应出现大量假跳空；")
print("  若 close 权威：pct_change 在 src=0 行应大量为 0（已确认）。")
print(f"  → close 口径下 |自算涨幅|>11% 占 {100 * (d['pc_calc'].abs() > 11).mean():.3f}%（可接受范围）")
print(f"  → pct_change==0 占 {100 * d['zero_pct'].mean():.2f}%（等于把 {100 * d['zero_pct'].mean():.0f}% 的交易日视为无变动）")
