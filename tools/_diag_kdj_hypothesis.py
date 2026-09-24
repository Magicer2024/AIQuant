"""
v8 —— 正确口径（close 权威）下重做「反转首日 + 用户 KDJ/MACD 0轴假设」检验
================================================================
口径定论（tools/_diag_which_is_truth.py）：
  · daily_price.pct_change 有 63.3% 的行恒为 0（turnover=0 行 89% 为 0）⇒ 不可用
  · daily_price.close 跨行连续（换源处无假跳空、98.99% 的自算涨幅 <=10.5%）⇒ 权威
  · 故涨跌幅一律用 close/close.shift(1)，并对 |涨幅|>25% 的行做剔除（0.74% 的
    换源/复牌残差，避免污染 MA/MACD/KDJ）
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 240)

con = sqlite3.connect('core/quant.db')
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,amount,turnover "
                 "FROM daily_price WHERE trade_date>='2023-06-01' ORDER BY code,trade_date", con)
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[(df['close'] > 0) & (df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0)].reset_index(drop=True)

g = df.groupby('code', sort=False)
df['close_prev'] = g['close'].shift(1)
df['ret'] = (df['close'] / df['close_prev'] - 1) * 100          # 权威日收益
# 剔除换源/复牌残差行（|收益|>25%），并置该行及其后一行不可用
df['bad'] = df['ret'].abs() > 25
df['bad_next'] = g['bad'].shift(-1).fillna(False)

df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
df['ma20_prev'] = g['ma20'].shift(1)

df['dif'] = (g['close'].transform(lambda s: s.ewm(span=12, adjust=False).mean())
             - g['close'].transform(lambda s: s.ewm(span=26, adjust=False).mean()))
df['dea'] = df.groupby('code', sort=False)['dif'].transform(lambda s: s.ewm(span=9, adjust=False).mean())
df['dif_prev'] = g['dif'].shift(1)

low9 = g['low'].transform(lambda s: s.rolling(9).min())
high9 = g['high'].transform(lambda s: s.rolling(9).max())
rsv = (df['close'] - low9) / (high9 - low9 + 1e-12) * 100
df['k'] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
df['d'] = df.groupby('code', sort=False)['k'].transform(lambda s: s.ewm(alpha=1 / 3, adjust=False).mean())
df['k_prev'] = g['k'].shift(1)
df['d_prev'] = g['d'].shift(1)

df['ama5'] = g['amount'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['amount'] / df['ama5'].replace(0, np.nan)
df['ext'] = (df['close'] / df['ma20'] - 1) * 100

df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
df['n_oc'] = (g['close'].shift(-1) / df['n_open'] - 1) * 100
for k in (3, 5, 10):
    df[f'oc{k}'] = (g['close'].shift(-k) / df['n_open'] - 1) * 100
df['unbuy'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])
df['notlim'] = df['ret'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)

win = ((df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23')
       & df['n_open'].notna() & (~df['unbuy']) & df['ma20'].notna()
       & (~df['bad']) & (~df['bad_next']))
mkt = df[win].groupby('trade_date')['n_oc'].mean().rename('mkt')
MB = {k: (df.groupby('code')['close'].shift(-k) / df.groupby('code')['open'].shift(-1) - 1)
      .mul(100)[win].groupby(df.loc[win, 'trade_date']).mean() for k in (1, 3, 5, 10)}
ND = len(mkt)
print(f"可用样本 {int(win.sum()):,}  交易日 {ND}  市场基线 OC {df[win]['n_oc'].mean():+.3f}%\n")
print("=" * 118)


def rep(name, mask, h='n_oc', mb=None):
    m = df[win & mask]
    if len(m) < 40:
        print(f"▌{name:<42} n={len(m)} 过少"); return
    ref = MB.get({1: 1, 3: 3, 5: 5, 10: 10}.get(len(h), 1)) if h != 'n_oc' else MB[1]
    ref = mb if mb is not None else MB[1]
    e = m.groupby('trade_date')[h].mean().sub(ref, axis=0).dropna()
    t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
    m2 = m.assign(y=m['trade_date'].str[:4])
    yr = []
    for y, s in m2.groupby('y'):
        ee = s.groupby('trade_date')[h].mean().sub(ref, axis=0).dropna()
        yr.append((y, s[h].mean(), (s[h] > 0).mean() * 100, ee.mean()))
    print(f"▌{name:<42} n={len(m):<6} 日均{len(m)/ND:5.2f} OC{m[h].mean():+.3f}% "
          f"胜率{(m[h]>0).mean()*100:4.1f}% 超额{e.mean():+.3f}pp t={t:+.2f} "
          f"{'✅' if all(x[3] > 0 for x in yr) else '❌'}")
    print("      " + " | ".join(f"{y}:{o:+.2f}/{(w):.0f}%/ex{x:+.2f}" for y, o, w, x in yr))


F = (df['close'] > df['ma20']) & (df['close_prev'] <= df['ma20_prev'])
A3 = F & (df['ret'] >= 5) & (df['vr'] >= 1.5) & df['notlim']
DIFUP0 = (df['dif'] > 0) & (df['dif_prev'] <= 0)

print("【一、反转首日主条件（正确口径复核）】")
rep('A3 首次站上MA20+大阳5%+放量+非涨停', A3)
rep('A3 + KDJ K∈20~40', A3 & df['k'].between(20, 40))
rep('A3 + KDJ K<40', A3 & (df['k'] < 40))
rep('A3 + DIF>0', A3 & (df['dif'] > 0))
rep('A3 + KDJ低位金叉(K<50且K上穿D)', A3 & (df['k'] < 50) & (df['k'] > df['d']) & (df['k_prev'] <= df['d_prev']))

print("\n【二、用户假设：MACD 反转到 0 值以上 + KDJ K 在 20~40】")
rep('B0 MACD DIF 上穿 0 轴', DIFUP0)
rep('B1 DIF>0 且 KDJ K∈20~40', (df['dif'] > 0) & df['k'].between(20, 40))
rep('B2 DIF 上穿0 且 K∈20~40', DIFUP0 & df['k'].between(20, 40))
rep('B3 B2 + 首次站上MA20', DIFUP0 & df['k'].between(20, 40) & F)
rep('B4 B1 + 首次站上MA20 + 大阳5%', (df['dif'] > 0) & df['k'].between(20, 40) & A3)

print("\n【三、KDJ 分档（对照）】")
for pre_nm, pre in [('DIF>0', df['dif'] > 0), ('全体', pd.Series(True, index=df.index))]:
    m = df[win & pre].copy()
    m['kb'] = pd.cut(m['k'], [-1, 20, 40, 60, 80, 101], labels=['<20', '20-40', '40-60', '60-80', '>80'])
    print(f"  — {pre_nm} —")
    for b, s in m.groupby('kb', observed=True):
        e = s.groupby('trade_date')['n_oc'].mean().sub(MB[1], axis=0).dropna()
        t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
        print(f"    K {str(b):<6} n={len(s):>7} 日均{len(s)/ND:6.1f} OC{s['n_oc'].mean():+.3f}% "
              f"胜率{(s['n_oc']>0).mean()*100:4.1f}% 超额{e.mean():+.3f}pp t={t:+.2f}")
