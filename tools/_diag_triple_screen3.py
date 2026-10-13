"""
三重滤网 —— 第三轮：① long 上唯一正向发现的稳定性  ② 与现网「缩量回踩」正面对照
                    ③ 位置（距 MA40 偏离）单调性 —— 回答「位置低是否真的更好」
=============================================================================
前两轮已定：三重滤网作为独立线无 alpha；挂现有 short 推荐上 Δ≈0；long 全链 Δ 为负。
本轮只查三件可能翻案的事，查完即给结论。
"""
import os
import sqlite3
import numpy as np
import pandas as pd

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 240)

con = sqlite3.connect('core/quant.db')
px = pd.read_sql(
    "SELECT code,trade_date,open,high,low,close,volume FROM daily_price "
    "WHERE trade_date >= '2018-06-01' AND (code LIKE '60%' OR code LIKE '00%') "
    "ORDER BY code, trade_date", con)
sig = pd.read_sql("SELECT code,trade_date,horizon,strategy FROM stock_signal", con)
con.close()

px = px[(px['close'] > 0) & (px['open'] > 0) & (px['high'] > 0) & (px['low'] > 0)].reset_index(drop=True)


def G(c):
    return px.groupby('code', sort=False)[c]


g = px.groupby('code', sort=False)
for p in (10, 20, 40, 160):
    px[f'ma{p}'] = g['close'].transform(lambda s: s.rolling(p).mean())
px['ma20_s5'] = G('ma20').shift(5)
px['vol5'] = g['volume'].transform(lambda s: s.rolling(5).mean())
px['shrink'] = px['volume'] / px['vol5'].replace(0, np.nan)
px['win_max'] = g['close'].transform(lambda s: s.rolling(20).max().shift(1))
px['win_first'] = G('close').shift(20)
px['rally'] = px['win_max'] / px['win_first'] - 1
for sp in (10, 20):
    px[f'e{sp}'] = g['close'].transform(lambda s: s.ewm(span=sp, adjust=False).mean())
px['dif'] = px['e10'] - px['e20']
px['dea'] = G('dif').transform(lambda s: s.ewm(span=5, adjust=False).mean())
px['dif_p'] = G('dif').shift(1)
px['dea_p'] = G('dea').shift(1)
px['cp'] = G('close').shift(1)
px['n_open'] = G('open').shift(-1)
px['n_high'] = G('high').shift(-1)
px['n_low'] = G('low').shift(-1)
px['n_close'] = G('close').shift(-1)
px['oc1'] = (px['n_close'] / px['n_open'] - 1) * 100
px['unbuy'] = (px['n_open'] == px['n_high']) & (px['n_open'] == px['n_low'])
px['ma10max'] = g['close'].transform(lambda s: s.rolling(10).max())

ok = px['n_open'].notna() & (~px['unbuy']) & px['ma160'].notna() & px['dif'].notna() & px['oc1'].notna()

T1 = (px['ma40'] > px['ma160']) & (px['close'] > px['ma160'])
CROSS = (px['dif'] > px['dea']) & (px['dif_p'] <= px['dea_p'])
CROSS_MA10 = CROSS & (px['close'] > px['ma10'])
DD5 = (px['close'] / px['ma10max'] - 1) <= -0.05
TRI_FULL = T1 & ((px['close'] / px['ma40'] - 1).abs() <= 0.03) & CROSS_MA10
# 现网「缩量回踩 V3」（strategy/pullback_dip.py 逐条复刻）
V3 = ((px['low'] <= px['ma10'] * 1.01) & (px['close'] >= px['ma10'] * 0.99)
      & (px['shrink'] <= 0.6) & (px['ma20'] > px['ma20_s5']) & (px['close'] > px['ma20'])
      & (px['rally'] >= 0.10))


def ex_t(sub, mkt):
    d = sub.groupby('trade_date')['oc1'].mean().sub(mkt, axis=0).dropna()
    t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 2 and d.std(ddof=1) > 0 else np.nan
    return d.mean(), t


# ══ ① long 上「趋势+近10日回撤≥5%」的稳定性 ══════════════════════
print("=" * 118)
print("① long(中长线) 推荐 × 入场时机条件 —— 桶内 Δ 与分年（这是 C 段唯一正向发现，必须验稳）")
print("=" * 118)
s2 = sig[sig['code'].str.startswith(('60', '00'))].copy()
tr = px[['code', 'trade_date', 'oc1', 'close', 'ma40', 'ma160', 'ma10max', 'dif', 'dea', 'dif_p', 'dea_p', 'ma10']].copy()
tr['c_tri'] = TRI_FULL
tr['c_dd'] = T1 & DD5
tr['c_trend'] = T1
m = s2.merge(tr, on=['code', 'trade_date'], how='inner')
m = m[m['oc1'].notna()]
for hz in ('long', 'short'):
    s = m[m['horizon'] == hz]
    print(f"\n  ── horizon={hz}  n={len(s)}  整体 {s['oc1'].mean():+.3f}% / 胜率 {(s['oc1']>0).mean()*100:.1f}%")
    for col, nm in [('c_dd', '趋势 + 近10日回撤≥5%'), ('c_tri', '三重滤网全链')]:
        hit = s[s[col]]
        if len(hit) < 30:
            print(f"     {nm:<24} n={len(hit)} 不足"); continue
        miss = s[~s[col]]
        d_ret = hit['oc1'].mean() - miss['oc1'].mean()
        d_win = (hit['oc1'] > 0).mean() * 100 - (miss['oc1'] > 0).mean() * 100
        print(f"     {nm:<24} Δ收益 {d_ret:+.3f}pp  Δ胜率 {d_win:+.1f}pp")
        for y in sorted(hit['trade_date'].str[:4].unique()):
            h, ms = hit[hit['trade_date'].str[:4] == y], miss[miss['trade_date'].str[:4] == y]
            if len(h) < 15 or len(ms) < 15:
                print(f"        {y}: n={len(h):<5} 样本不足"); continue
            print(f"        {y}: 命中 {h['oc1'].mean():+.3f}%(n={len(h)}) 未命中 {ms['oc1'].mean():+.3f}%(n={len(ms)})"
                  f"  Δ{h['oc1'].mean()-ms['oc1'].mean():+.3f}pp  胜率Δ{(h['oc1']>0).mean()*100-(ms['oc1']>0).mean()*100:+.1f}pp")

# ══ ② 三重滤网 vs 现网缩量回踩 V3 ══════════════════════════════
print("\n" + "=" * 118)
print("② 正面对照（同窗 2024-07-01~2026-09-23 主板，OC1 口径）—— 谁的入场时机更值钱")
print("=" * 118)
w = ok & (px['trade_date'] >= '2024-07-01') & (px['trade_date'] <= '2026-09-23')
mkt = px.loc[w].groupby('trade_date')['oc1'].mean()
print(f"  基线：全市场 OC1 {px.loc[w,'oc1'].mean():+.3f}%  胜率 {(px.loc[w,'oc1']>0).mean()*100:.1f}%")
print(f"  {'信号':<30}{'n':>8}{'日均':>8}{'均值':>9}{'胜率':>8}{'超额pp':>9}{'t':>7}")
for nm, msk in [('三重滤网全链', TRI_FULL), ('三重滤网 破MA40收回+金叉', T1 & (px['low'] < px['ma40']) & (px['close'] > px['ma40']) & CROSS_MA10),
                ('现网 缩量回踩V3', V3), ('V3 去掉量缩条件', ((px['low'] <= px['ma10'] * 1.01) & (px['close'] >= px['ma10'] * 0.99)
                                                          & (px['ma20'] > px['ma20_s5']) & (px['close'] > px['ma20']) & (px['rally'] >= 0.10)))]:
    sub = px.loc[w & msk]
    e, t = ex_t(sub, mkt)
    print(f"  {nm:<30}{len(sub):>8}{len(sub)/544:>8.2f}{sub['oc1'].mean():>8.3f}%"
          f"{(sub['oc1']>0).mean()*100:>7.1f}%{e:>9.3f}{t:>7.2f}")

# ══ ③ 位置单调性：距 MA40 偏离度分档 ═══════════════════════════
print("\n" + "=" * 118)
print("③ 「位置越低越好吗」—— 趋势前提下，按距 MA40 偏离度分档（同窗）")
print("=" * 118)
sub = px.loc[w & T1].copy()
sub['dev'] = (sub['close'] / sub['ma40'] - 1) * 100
bins = [-100, -5, -2, 0, 2, 5, 10, 100]
labels = ['<-5%', '-5~-2%', '-2~0%', '0~2%', '2~5%', '5~10%', '>10%']
sub['bucket'] = pd.cut(sub['dev'], bins, labels=labels)
print(f"  {'距MA40':<12}{'n':>9}{'日均':>8}{'均值OC1':>10}{'胜率':>8}{'超额pp':>9}{'t':>7}")
for b, s in sub.groupby('bucket', observed=True):
    e, t = ex_t(s, mkt)
    print(f"  {str(b):<12}{len(s):>9}{len(s)/544:>8.1f}{s['oc1'].mean():>9.3f}%"
          f"{(s['oc1']>0).mean()*100:>7.1f}%{e:>9.3f}{t:>7.2f}")
print("\n完成。")
