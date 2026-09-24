"""v7：干净口径下 A3 × KDJ/MACD 叠加 + 出场配置，决定模块最终参数。"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 240)

con = sqlite3.connect('core/quant.db')
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,amount,turnover,pct_change "
                 "FROM daily_price WHERE trade_date >= '2023-06-01' ORDER BY code,trade_date", con)
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[(df['close'] > 0) & (df['open'] > 0) & df['pct_change'].notna()].reset_index(drop=True)
g = df.groupby('code', sort=False)
df['one_r'] = 1 + (df['pct_change'] / 100).clip(-0.25, 0.25)
df['adj'] = df.groupby('code', sort=False)['one_r'].transform('cumprod')
df['adj_high'] = df['adj'] * (df['close'] / df['high'])
df['adj_low'] = df['adj'] * (df['close'] / df['low'])
df['ret'] = df['pct_change']

g = df.groupby('code', sort=False)
df['ma20'] = g['adj'].transform(lambda s: s.rolling(20).mean())
df['ma20_prev'] = g['ma20'].shift(1)
df['adj_prev'] = g['adj'].shift(1)
df['h60'] = g['adj_high'].transform(lambda s: s.rolling(60).max())
df['dif'] = (g['adj'].transform(lambda s: s.ewm(span=12, adjust=False).mean())
             - g['adj'].transform(lambda s: s.ewm(span=26, adjust=False).mean()))
df['dea'] = df.groupby('code', sort=False)['dif'].transform(lambda s: s.ewm(span=9, adjust=False).mean())
low9 = g['adj_low'].transform(lambda s: s.rolling(9).min())
high9 = g['adj_high'].transform(lambda s: s.rolling(9).max())
rsv = (df['adj'] - low9) / (high9 - low9 + 1e-12) * 100
df['k'] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
df['d'] = df.groupby('code', sort=False)['k'].transform(lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
df['k_prev'] = g['k'].shift(1)
df['d_prev'] = g['d'].shift(1)
df['ama5'] = g['amount'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['amount'] / df['ama5'].replace(0, np.nan)
df['ext'] = (df['adj'] / df['ma20'] - 1) * 100

df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
df['n_oc'] = (g['close'].shift(-1) / df['n_open'] - 1) * 100
for k in (3, 5, 10):
    df[f'oc{k}'] = (g['close'].shift(-k) / df['n_open'] - 1) * 100
df['unbuy'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])

win = ((df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23')
       & df['n_open'].notna() & (~df['unbuy']) & df['ma20'].notna() & df['h60'].notna())
mkt = df[win].groupby('trade_date')['n_oc'].mean().rename('mkt')
ND = len(mkt)

F = (df['adj'] > df['ma20']) & (df['adj_prev'] <= df['ma20_prev'])
NTL = df['ret'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)
A3 = F & (df['ret'] >= 5) & (df['vr'] >= 1.5) & NTL


def rep(name, mask, h='n_oc'):
    m = df[win & mask]
    if len(m) < 40:
        print(f"▌{name:<44} n={len(m)} 过少"); return None
    e = m.groupby('trade_date')[h].mean().sub(mkt, axis=0).dropna()
    t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
    m2 = m.assign(y=m['trade_date'].str[:4])
    yr = []
    for y, s in m2.groupby('y'):
        ee = s.groupby('trade_date')[h].mean().sub(mkt, axis=0).dropna()
        yr.append((y, s[h].mean(), (s[h] > 0).mean() * 100, ee.mean()))
    print(f"▌{name:<44} n={len(m):<6} 日均{len(m)/ND:5.2f} OC{m[h].mean():+.3f}% "
          f"胜率{(m[h]>0).mean()*100:4.1f}% 超额{e.mean():+.3f}pp t={t:+.2f} "
          f"{'✅' if all(x[3] > 0 for x in yr) else '❌'}")
    print("      " + " | ".join(f"{y}:{o:+.2f}/{(w):.0f}%/ex{x:+.2f}" for y, o, w, x in yr))
    return m


print("=" * 116)
print("【A3 叠加 KDJ / MACD / 前期跌幅】")
rep('A3 基线', A3)
rep('A3 + K∈20~40', A3 & df['k'].between(20, 40))
rep('A3 + K<40', A3 & (df['k'] < 40))
rep('A3 + K<50', A3 & (df['k'] < 50))
rep('A3 + DIF>0', A3 & (df['dif'] > 0))
rep('A3 + DIF上穿0', A3 & (df['dif'] > 0) & (df['dif'] <= 0))
rep('A3 + K∈20~40 + DIF>0', A3 & df['k'].between(20, 40) & (df['dif'] > 0))
rep('A3 + K∈20~40 + KDJ金叉', A3 & df['k'].between(20, 40) & (df['k'] > df['d']) & (df['k_prev'] <= df['d_prev']))
rep('A3 + 前期跌≥10%(前20日)', A3 & ((df['adj_prev'] / g['adj'].shift(21) - 1) * 100 <= -10))

print("\n【出场配置（A3 + K<50 候选池）】")
E = df[win & A3 & (df['k'] < 50)].index
ENTRY = df.loc[E, 'n_open']
REF = df.loc[E, 'close']
tr = pd.concat([df['high'] - df['low'],
                (df['high'] - df.groupby('code')['close'].shift(1)).abs(),
                (df['low'] - df.groupby('code')['close'].shift(1)).abs()], axis=1).max(axis=1)
df['atr14'] = tr.groupby(df['code']).transform(lambda s: s.rolling(14).mean())
for k in range(1, 11):
    df[f'c{k}'] = df.groupby('code')['close'].shift(-k)
    df[f'h{k}'] = df.groupby('code')['high'].shift(-k)
    df[f'l{k}'] = df.groupby('code')['low'].shift(-k)

import numpy as _np


def simulate(stop_pct, take_pct, hold, atr_k=None, lo=None, hi=None):
    e = ENTRY.to_numpy(); ref = REF.to_numpy()
    if atr_k:
        w = _np.clip(atr_k * df.loc[E, 'atr14'].to_numpy() / ref, lo, hi)
        sl = ref * (1 - w)
    else:
        sl = ref * (1 + stop_pct)
    tp = ref * (1 + take_pct)
    out = _np.full(len(e), _np.nan)
    for i in range(len(e)):
        for k in range(1, hold + 1):
            h_, l_, c_ = df.loc[E, f'h{k}'].iloc[i], df.loc[E, f'l{k}'].iloc[i], df.loc[E, f'c{k}'].iloc[i]
            if not _np.isfinite(c_):
                break
            if _np.isfinite(l_) and l_ <= sl[i]:
                out[i] = (sl[i] / e[i] - 1) * 100; break
            if _np.isfinite(h_) and h_ >= tp[i]:
                out[i] = (tp[i] / e[i] - 1) * 100; break
            if k == hold:
                out[i] = (c_ / e[i] - 1) * 100
    return pd.Series(out, index=ENTRY.index)


dates = df.loc[E, 'trade_date']
def mkt_bare(k):
    r = (df.groupby('code')['close'].shift(-k) / df.groupby('code')['open'].shift(-1) - 1) * 100
    tr2 = win & ~((df.groupby('code')['open'].shift(-1) == df.groupby('code')['high'].shift(-1))
                  & (df.groupby('code')['open'].shift(-1) == df.groupby('code')['low'].shift(-1)))
    return r[tr2].groupby(df.loc[tr2, 'trade_date']).mean()


print(f"  {'配置':<32}{'n':>6}{'均值':>9}{'胜率':>8}{'超额pp':>9}{'盈亏比':>8}")
for nm, kw in [('裸持有 T+1 收盘', dict(stop_pct=-9, take_pct=9, hold=1)),
               ('止损-5%/止盈+8%/5日', dict(stop_pct=-0.05, take_pct=0.08, hold=5)),
               ('止损-6%/止盈+10%/5日', dict(stop_pct=-0.06, take_pct=0.10, hold=5)),
               ('ATR2.5(5~15%)/止盈+10%/5日', dict(stop_pct=0, take_pct=0.10, hold=5, atr_k=2.5, lo=0.05, hi=0.15)),
               ('ATR2.5(5~15%)/止盈+12%/10日', dict(stop_pct=0, take_pct=0.12, hold=10, atr_k=2.5, lo=0.05, hi=0.15))]:
    r = simulate(**kw); ok = r.notna()
    rr = r[ok]; dd = dates[ok]
    mb = mkt_bare(kw['hold'])
    ex = pd.Series(rr.values, index=dd.values).groupby(level=0).mean().sub(mb, axis=0).dropna()
    pl = rr[rr > 0].mean() / abs(rr[rr < 0].mean())
    print(f"  {nm:<32}{ok.sum():>6}{rr.mean():>8.2f}%{(rr>0).mean()*100:>7.1f}%{ex.mean():>9.3f}{pl:>8.2f}")
