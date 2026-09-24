"""
口径修正版实验（v6）—— 干净价源重建
================================================================
问题（tools/_diag_pricesource*.py 已证）：
  daily_price.close 在**行级别混用两个价源**（turnover>0 行自算一致率 99.3%，
  turnover=0/空 行仅 8.8%），任何 close/close.shift(1) 自算收益都会被污染。
  但 **行内 OHLC 同源**（越界 0%、(amount/volume)/close 精确自洽）⇒ 行内比值可用。

修正口径：
  · 涨跌幅     = pct_change 字段（权威）
  · 一致价格序列 adj_close = cumprod(1 + clip(pct_change/100, ±25%))
  · adj_open/high/low = adj_close × (close/open|high|low)   ← 行内比值，与价源无关
  · 量比       = amount / 前 5 日均 amount（amount 为原始成交额，跨行可比）
  · OC 收益    = T+1 行内 close/open - 1（同行同源，安全）
  · 市场基线   = 全市场同日同口径，剔除 T+1 一字板
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 240)

con = sqlite3.connect('core/quant.db')

# ── 0) 机制确认：单只股票的价源交替 ─────────────────────────────────────
print("=" * 110)
print("【机制确认】000001 最近 12 行（turnover 标记价源）")
d0 = pd.read_sql("SELECT trade_date,open,high,low,close,amount,turnover,pct_change "
                 "FROM daily_price WHERE code='000001' AND trade_date>='2026-08-01' "
                 "ORDER BY trade_date", con)
d0['自算%'] = (d0['close'] / d0['close'].shift(1) - 1) * 100
d0['价源'] = np.where(d0['turnover'] > 0, '前复权', '原始/异源')
print(d0.to_string(index=False))

# ── 1) 载入 + 重建 ─────────────────────────────────────────────────────
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,amount,turnover,pct_change "
                 "FROM daily_price WHERE trade_date >= '2023-06-01' ORDER BY code,trade_date", con)
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[(df['close'] > 0) & (df['open'] > 0) & df['pct_change'].notna()].reset_index(drop=True)

g = df.groupby('code', sort=False)
r = (df['pct_change'] / 100).clip(-0.25, 0.25)
df['one_r'] = 1 + r
df['adj'] = df.groupby('code', sort=False)['one_r'].transform('cumprod')
df['adj_open'] = df['adj'] * (df['close'] / df['open'])
df['adj_high'] = df['adj'] * (df['close'] / df['high'])
df['adj_low'] = df['adj'] * (df['close'] / df['low'])
df['ret'] = df['pct_change']                       # 权威日收益 %

# 一致性自检
chk = (df['adj'] / g['adj'].shift(1) - 1) * 100
print("\n【自检】重建序列自算涨幅 vs pct_change 一致率: "
      f"{100 * (chk - df['ret']).abs().lt(0.01).mean():.2f}%")

g = df.groupby('code', sort=False)
df['ma20'] = g['adj'].transform(lambda s: s.rolling(20).mean())
df['ma20_prev'] = g['ma20'].shift(1)
df['adj_prev'] = g['adj'].shift(1)

# MACD on adj
df['ema12'] = g['adj'].transform(lambda s: s.ewm(span=12, adjust=False).mean())
df['ema26'] = g['adj'].transform(lambda s: s.ewm(span=26, adjust=False).mean())
df['dif'] = df['ema12'] - df['ema26']
df['dea'] = df.groupby('code', sort=False)['dif'].transform(lambda s: s.ewm(span=9, adjust=False).mean())

# KDJ on adj high/low/close
low9 = g['adj_low'].transform(lambda s: s.rolling(9).min())
high9 = g['adj_high'].transform(lambda s: s.rolling(9).max())
rsv = (df['adj'] - low9) / (high9 - low9 + 1e-12) * 100
df['k'] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
df['d'] = df.groupby('code', sort=False)['k'].transform(lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
df['k_prev'] = g['k'].shift(1)
df['d_prev'] = g['d'].shift(1)

# 量比（amount 口径）
df['ama5'] = g['amount'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['amount'] / df['ama5'].replace(0, np.nan)

# T+1 出场（行内比值，安全）
df['n_oc'] = (g['close'].shift(-1) / g['open'].shift(-1) - 1) * 100
df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
for k in (3, 5, 10):
    df[f'oc{k}'] = (g['close'].shift(-k) / g['open'].shift(-1) - 1) * 100
df['unbuy'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])
df['ext'] = (df['adj'] / df['ma20'] - 1) * 100

win = ((df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23')
       & df['n_open'].notna() & (~df['unbuy']) & df['ma20'].notna())
mkt = df[win].groupby('trade_date')['n_oc'].mean().rename('mkt')
print(f"\n市场基线（同日等权，剔一字板）: OC {df[win]['n_oc'].mean():+.3f}%  交易日 {len(mkt)}\n")
print("=" * 110)


def rep(name, mask, h='n_oc'):
    m = df[win & mask]
    if len(m) < 40:
        print(f"▌{name:<40} n={len(m)} 过少"); return
    e = m.groupby('trade_date')[h].mean().sub(mkt, axis=0).dropna()
    t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
    m2 = m.assign(y=m['trade_date'].str[:4])
    yr = []
    for y, s in m2.groupby('y'):
        ee = s.groupby('trade_date')[h].mean().sub(mkt, axis=0).dropna()
        yr.append(f"{y}:{s[h].mean():+.2f}/{(s[h]>0).mean()*100:.0f}%/ex{ee.mean():+.2f}")
    exs = [float(x.split('ex')[1]) for x in yr]
    print(f"▌{name:<40} h={h:<5} n={len(m):<6} 日均{len(m)/len(mkt):5.2f}  "
          f"OC {m[h].mean():+.3f}% 胜率{(m[h]>0).mean()*100:4.1f}%  超额{e.mean():+.3f}pp t={t:+.2f} "
          f"{'✅' if all(x > 0 for x in exs) else '❌'}")
    print("      " + " | ".join(yr))


print("【A. 干净口径下重测 W5 主条件】")
F = (df['adj'] > df['ma20']) & (df['adj_prev'] <= df['ma20_prev'])
NOTLIM = df['ret'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)
rep('A0 仅首次站上MA20', F)
rep('A1 首次站上+大阳≥5%', F & (df['ret'] >= 5))
rep('A2 A1+放量(amount量比≥1.5)', F & (df['ret'] >= 5) & (df['vr'] >= 1.5))
rep('A3 A2+非涨停', F & (df['ret'] >= 5) & (df['vr'] >= 1.5) & NOTLIM)

print("\n【B. 用户假设：MACD 上穿 0 值 且在 0 之上 + KDJ K 在 20~40】")
df['dif_prev'] = g['dif'].shift(1)
DIF_UP0 = (df['dif'] > 0) & (df['dif_prev'] <= 0)
rep('B0 MACD DIF 上穿 0 轴（当日）', DIF_UP0)
rep('B1 DIF>0 且 K 在 20~40', (df['dif'] > 0) & df['k'].between(20, 40))
rep('B2 DIF 上穿0 且 K 在 20~40', DIF_UP0 & df['k'].between(20, 40))
rep('B3 DIF>0 & K 20~40 & 金叉(D>K前)', (df['dif'] > 0) & df['k'].between(20, 40)
    & (df['k'] > df['d']) & (df['k_prev'] <= df['d_prev']))
rep('B4 DIF 上穿0 & K 20~40 & 首次站上MA20', DIF_UP0 & df['k'].between(20, 40) & F)
rep('B5 B4 + 放量', DIF_UP0 & df['k'].between(20, 40) & F & (df['vr'] >= 1.5))

print("\n【C. KDJ 分档（对照组：DIF>0 前提下）】")
m = df[win & (df['dif'] > 0)].copy()
m['kb'] = pd.cut(m['k'], [-1, 20, 40, 60, 80, 101], labels=['<20', '20-40', '40-60', '60-80', '>80'])
for b, s in m.groupby('kb', observed=True):
    e = s.groupby('trade_date')['n_oc'].mean().sub(mkt, axis=0).dropna()
    t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
    print(f"  K {str(b):<6} n={len(s):>7} 日均{len(s)/len(mkt):5.2f}  OC {s['n_oc'].mean():+.3f}% "
          f"胜率{(s['n_oc']>0).mean()*100:4.1f}%  超额{e.mean():+.3f}pp t={t:+.2f}")

print("\n【D. 多日持有：最优候选】")
for h in ('oc3', 'oc5', 'oc10'):
    rep('A3 (W5 干净口径)', F & (df['ret'] >= 5) & (df['vr'] >= 1.5) & NOTLIM, h)
