"""
反转首日线 —— 第三轮：强反转参数 + KDJ + 入场价位优势
用户观察的 600641 形态 = 前期深跌 + 首次站上 MA20 + 大阳(+6.8%) + 放量。
本轮测「大阳」这一维度能否救活这条线，并量化「入场时 ext 有多低」。
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 220)

DB = 'core/quant.db'
con = sqlite3.connect(DB)
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,pct_change FROM daily_price "
                 "WHERE trade_date >= '2023-06-01' ORDER BY code, trade_date", con)
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[df['close'] > 0]

g = df.groupby('code', sort=False)
df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
df['ma20_prev'] = g['ma20'].shift(1)
df['close_prev'] = g['close'].shift(1)
df['pct'] = (df['close'] / df['close_prev'] - 1) * 100
df['ret20_prev'] = (df['close_prev'] / g['close'].shift(21) - 1) * 100
df['vma5_prev'] = g['volume'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['volume'] / df['vma5_prev'].replace(0, np.nan)
df['ext'] = (df['close'] / df['ma20'] - 1) * 100          # 入场时相对 MA20 偏离

# KDJ
low_n = g['low'].transform(lambda s: s.rolling(9).min())
high_n = g['high'].transform(lambda s: s.rolling(9).max())
rsv = (df['close'] - low_n) / (high_n - low_n + 1e-10) * 100
df['k'] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
df['d'] = df.groupby('code', sort=False)['k'].transform(lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
df['k_prev'] = g['k'].shift(1)
df['d_prev'] = g['d'].shift(1)

df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
for k in (1, 3, 5):
    df[f'oc{k}'] = (g['close'].shift(-k) / df['n_open'] - 1) * 100
df['unbuyable'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])
df['limit_thr'] = np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)
df['notlimit'] = df['pct'] < df['limit_thr']

F = (df['close'] > df['ma20']) & (df['close_prev'] <= df['ma20_prev'])   # 首次站上
DEEP = df['ret20_prev'] <= -10
KDJX = (df['k'] > df['d']) & (df['k_prev'] <= df['d_prev']) & (df['k'] < 50)

VAR = {
    'W0 首次站上+大阳≥5%':            F & (df['pct'] >= 5),
    'W1 W0+放量≥1.5':                 F & (df['pct'] >= 5) & (df['vr'] >= 1.5),
    'W2 W1+放量≥2':                   F & (df['pct'] >= 5) & (df['vr'] >= 2),
    'W3 W1+前期跌≥10%':               F & (df['pct'] >= 5) & (df['vr'] >= 1.5) & DEEP,
    'W4 W1+KDJ低位金叉':              F & (df['pct'] >= 5) & (df['vr'] >= 1.5) & KDJX,
    'W5 W1+非涨停':                   F & (df['pct'] >= 5) & (df['vr'] >= 1.5) & df['notlimit'],
    'W6 W5+前期跌≥10%(600641形态)':   F & (df['pct'] >= 5) & (df['vr'] >= 1.5) & df['notlimit'] & DEEP,
    'W7 首次站上+放量2+大阳7%':       F & (df['pct'] >= 7) & (df['vr'] >= 2) & df['notlimit'],
    'W8 前期深跌15%+大阳5%+放量2':    F & (df['pct'] >= 5) & (df['vr'] >= 2) & df['notlimit'] & (df['ret20_prev'] <= -15),
    'W9 W5(即最优基础版)':            F & (df['pct'] >= 5) & (df['vr'] >= 1.5) & df['notlimit'],
}

win = (df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23') & df['n_open'].notna() & (~df['unbuyable'])
mkt = df[win].groupby('trade_date')['oc1'].mean().rename('mkt')
ndays = len(mkt)
print(f"市场基线 OC1 {df[win]['oc1'].mean():+.2f}%  交易日 {ndays}\n")
print("=" * 118)


def rep(name, mask, h='oc1'):
    m = df[win & mask].copy()
    if len(m) < 30:
        print(f"{name:<34} n={len(m)} 过少"); return
    m['year'] = m['trade_date'].str[:4]
    dm = m.groupby('trade_date')[h].mean().rename('sig')
    ex = dm.sub(mkt, axis=0).dropna()
    t = ex.mean() / (ex.std() / np.sqrt(len(ex))) if ex.std() > 0 else 0
    yr = []
    for y, s in m.groupby('year'):
        e = s.groupby('trade_date')[h].mean().sub(mkt, axis=0).dropna()
        yr.append((y, len(s), s[h].mean(), (s[h] > 0).mean() * 100, e.mean()))
    exs = [r[4] for r in yr]
    print(f"▌{name:<34} h={h} n={len(m):<5} 日均{len(m)/ndays:5.2f}  "
          f"OC {m[h].mean():+.2f}% 胜率{(m[h]>0).mean()*100:4.1f}%  超额{ex.mean():+.3f}pp t={t:+.2f}  "
          f"中位ext {m['ext'].median():+.1f}%  {'✅' if all(x > 0 for x in exs) else '❌'}")
    print(f"      " + " | ".join(f"{y}: {o:+.2f}%/{(w):.0f}%/ex{x:+.2f}" for y, n2, o, w, x in yr))


for k, v in VAR.items():
    rep(k, v)
print("\n" + "=" * 118)
print("【多日持有】")
for k in ('W5 W1+非涨停', 'W6 W5+前期跌≥10%(600641形态)', 'W1 W0+放量≥1.5'):
    for h in ('oc1', 'oc3', 'oc5'):
        rep(k, VAR[k], h)
