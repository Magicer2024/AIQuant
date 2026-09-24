"""v9：KDJ K>80 是否可作为「禁止追高」硬过滤？—— 用同日配对 + 分年 + 分市场强弱判定。"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
pd.set_option('display.width', 240)
con = sqlite3.connect('core/quant.db')
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,amount,turnover,turnover "
                 "FROM daily_price WHERE trade_date>='2023-06-01' ORDER BY code,trade_date", con)
df.columns = ['code', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'amount',
              'turnover', '_t2']
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[(df['close'] > 0) & (df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0)].reset_index(drop=True)
g = df.groupby('code', sort=False)
df['cp'] = g['close'].shift(1)
df['ret'] = (df['close'] / df['cp'] - 1) * 100
df['bad'] = df['ret'].abs() > 25
df['bad_n'] = g['bad'].shift(-1).astype('boolean').fillna(False)
low9 = g['low'].transform(lambda s: s.rolling(9).min())
high9 = g['high'].transform(lambda s: s.rolling(9).max())
rsv = (df['close'] - low9) / (high9 - low9 + 1e-12) * 100
df['k'] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
df['n_open'] = g['open'].shift(-1)
df['n_oc'] = (g['close'].shift(-1) / df['n_open'] - 1) * 100
df['unbuy'] = (df['n_open'] == g['high'].shift(-1)) & (df['n_open'] == g['low'].shift(-1))
win = ((df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23')
       & df['n_open'].notna() & (~df['unbuy']) & (~df['bad']) & (~df['bad_n'])
       & df['k'].notna())
mkt = df[win].groupby('trade_date')['n_oc'].mean().rename('mkt')
MKTPCT = df[win].groupby('trade_date')['ret'].mean().rename('mkp')
ND = len(mkt)


def rep(name, mask, tag=''):
    m = df[win & mask]
    if len(m) < 40:
        print(f"  {name:<40} n={len(m)} 过少"); return
    e = m.groupby('trade_date')['n_oc'].mean().sub(mkt, axis=0).dropna()
    t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
    m2 = m.assign(y=m['trade_date'].str[:4])
    ys = [(y, s['n_oc'].mean(), (s['n_oc'] > 0).mean() * 100,
           s.groupby('trade_date')['n_oc'].mean().sub(mkt, axis=0).dropna().mean())
          for y, s in m2.groupby('y')]
    print(f"  {name:<40} n={len(m):<6} OC{m['n_oc'].mean():+.3f}% 胜率{(m['n_oc']>0).mean()*100:4.1f}% "
          f"超额{e.mean():+.3f}pp t={t:+.2f} {'✅' if all(x[3] > 0 for x in ys) else '❌'} {tag}")
    print("      " + " | ".join(f"{y}:{o:+.2f}/{(w):.0f}%/ex{x:+.2f}" for y, o, w, x in ys))


print("=" * 118)
print("【一、K>80 硬过滤在「反转首日」线上的边际（A3 = 首日站上MA20+大阳5%+放量+非涨停）】")
g2 = df.groupby('code', sort=False)
df['ma20'] = g2['close'].transform(lambda s: s.rolling(20).mean())
df['ma20p'] = g2['ma20'].shift(1)
df['ama5'] = g2['amount'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['amount'] / df['ama5'].replace(0, np.nan)
df['notlim'] = df['ret'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)
F = (df['close'] > df['ma20']) & (df['cp'] <= df['ma20p'])
A3 = F & (df['ret'] >= 5) & (df['vr'] >= 1.5) & df['notlim']
rep('A3 全体', A3)
rep('A3 & K<=80（保留）', A3 & (df['k'] <= 80))
rep('A3 & K>80（拟剔除）', A3 & (df['k'] > 80))
rep('A3 & K<=60（更严）', A3 & (df['k'] <= 60))
rep('A3 & K>60（拟剔除·更严）', A3 & (df['k'] > 60))

print("\n【二、全市场：K>80 是否系统性负期望（按当日市场强弱分层，排除「高位=弱市」混杂）】")
x = df[win].copy()
x['mkp'] = x['trade_date'].map(MKTPCT)
for lbl, sub in [('强市日(全市场均涨>0.5%)', x[x['mkp'] > 0.5]),
                 ('平市日(-0.5~0.5%)', x[(x['mkp'] >= -0.5) & (x['mkp'] <= 0.5)]),
                 ('弱市日(<-0.5%)', x[x['mkp'] < -0.5])]:
    print(f"  — {lbl} (n={len(sub)}) —")
    for kk, sm in [('K<=80', sub[sub['k'] <= 80]), ('K>80', sub[sub['k'] > 80])]:
        print(f"      {kk:<7} n={len(sm):>7}  OC{sm['n_oc'].mean():+.3f}%  胜率{(sm['n_oc']>0).mean()*100:4.1f}%")

print("\n【三、K>80 的「绝对负期望」是否只是市场同期偏弱？（同日配对检验）】")
hi = df[win & (df['k'] > 80)]
lo = df[win & (df['k'] <= 80)]
# 只在同一天同时存在两组的交易日比较
dh = hi.groupby('trade_date')['n_oc'].mean().rename('hi')
dl = lo.groupby('trade_date')['n_oc'].mean().rename('lo')
j = pd.concat([dh, dl], axis=1).dropna()
j['d'] = j['hi'] - j['lo']
print(f"  同日成对交易日 {len(j)}  K>80 组均值 {j['hi'].mean():+.3f}%  K<=80 组均值 {j['lo'].mean():+.3f}%")
print(f"  组间差 {j['d'].mean():+.3f}pp  t={j['d'].mean() / (j['d'].std() / np.sqrt(len(j))):+.2f}（负=高位更差）")
jj = j.assign(y=j.index.str[:4])
for y, s in jj.groupby('y'):
    t2 = s['d'].mean() / (s['d'].std() / np.sqrt(len(s))) if s['d'].std() > 0 else 0
    print(f"    {y}  日数 {len(s):<4} 组间差 {s['d'].mean():+.3f}pp  t={t2:+.2f}")
