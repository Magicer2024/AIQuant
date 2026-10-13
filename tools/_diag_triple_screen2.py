"""
三重滤网 —— 第二轮：长样本复检 + 低吸形态变体 + 「现有推荐内部增量」检验
========================================================================
第一轮结论（_diag_triple_screen.py / 2024-07~2026-09 主板）：
  · 用户原味三重滤网 全链 n=13907、OC1 +0.103%、胜率 48.1%、超额 -0.040pp(t=-0.67)
    分年 2024 +0.136 / 2025 +0.014 / 2026 -0.233(t=-1.99) ⇒ 铁律 3 不过
  · LOO：去掉任一重，超额都在 -0.05 ~ 0.00 之间，**没有任何子集产生稳定正超额**
  · 2:1（-5%/+10%）实盘达成盈亏比仅 1.48、胜率 43.8% ⇒ 扣市场基线后 -0.649pp(t=-3.84)

本轮要回答三个问题：
  A. 长样本（2020-2026）是否一致？会不会只是这段行情的偶然？
  B. 换成真正的「低吸」形态（跌破 MA40 收回 / 首次站上 MA40）会不会更好？
  C. **最关键**：把三重滤网挂到现有 stock_signal 推荐上做「入场时机」过滤，
     桶内增量 Δ 是多少？（铁律 6/14：子群主判据是桶内增量，不是绝对收益）

口径同第一轮：主板 60/00、OC1 = T+1 开盘买→T+1 收盘卖、剔除 T+1 一字板、
             涨跌幅 = close/close.shift(1)、超额 = 组日均 − 当日全市场主板日均。
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
sig = pd.read_sql("SELECT code,scan_date,trade_date,horizon,strategy,pct_above_ma20 "
                  "FROM stock_signal", con)
con.close()

px = px[(px['close'] > 0) & (px['open'] > 0) & (px['high'] > 0) & (px['low'] > 0)]
px = px.reset_index(drop=True)
print(f"主板行情 {len(px):,} 行 / {px['code'].nunique()} 只 / "
      f"{px['trade_date'].min()}~{px['trade_date'].max()}")


def G(col):
    return px.groupby('code', sort=False)[col]


g = px.groupby('code', sort=False)
for p in (10, 40, 160):
    px[f'ma{p}'] = g['close'].transform(lambda s: s.rolling(p).mean())
    px[f'ma{p}_p'] = G(f'ma{p}').shift(1)

for span in (10, 20):
    px[f'e{span}'] = g['close'].transform(lambda s: s.ewm(span=span, adjust=False).mean())
px['dif'] = px['e10'] - px['e20']
px['dea'] = G('dif').transform(lambda s: s.ewm(span=5, adjust=False).mean())
px['dif_p'] = G('dif').shift(1)
px['dea_p'] = G('dea').shift(1)

px['cp'] = G('close').shift(1)
px['pct'] = (px['close'] / px['cp'] - 1) * 100
px['ma10max'] = g['close'].transform(lambda s: s.rolling(10).max())
px['n_open'] = G('open').shift(-1)
px['n_high'] = G('high').shift(-1)
px['n_low'] = G('low').shift(-1)
px['n_close'] = G('close').shift(-1)
px['oc1'] = (px['n_close'] / px['n_open'] - 1) * 100
px['unbuy'] = (px['n_open'] == px['n_high']) & (px['n_open'] == px['n_low'])

base_ok = (px['n_open'].notna() & (~px['unbuy']) & px['ma160'].notna()
           & px['dif'].notna() & px['oc1'].notna() & px['close'].notna())

T1 = (px['ma40'] > px['ma160']) & (px['close'] > px['ma160'])
P_PULL = (px['close'] / px['ma40'] - 1).abs() <= 0.03
CROSS = (px['dif'] > px['dea']) & (px['dif_p'] <= px['dea_p'])
CROSS_MA10 = CROSS & (px['close'] > px['ma10'])

# ── 低吸形态变体 ───────────────────────────────────────────────
V_dip_recover = (px['low'] < px['ma40']) & (px['close'] > px['ma40'])          # 盘中破 MA40 收回
V_first_ma40 = (px['close'] > px['ma40']) & (px['cp'] <= px['ma40_p'])         # 首次站上 MA40
V_below_ma40 = (px['close'] / px['ma40'] - 1).between(-0.05, -0.005)           # 尚在 MA40 下方 0.5~5%
V_pull_ma10 = (px['low'] <= px['ma10']) & (px['close'] >= px['ma10'] * 0.99)   # 回踩 MA10
V_deep_dd = (px['close'] / px['ma10max'] - 1) <= -0.05                          # 近 10 日回撤≥5%


def stats(mask, win_mask, mkt):
    sub = px.loc[win_mask & mask]
    if len(sub) < 30:
        return None
    r = sub['oc1']
    daily = sub.groupby('trade_date')['oc1'].mean()
    ex = daily.sub(mkt, axis=0).dropna()
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex))) if len(ex) > 2 and ex.std(ddof=1) > 0 else np.nan
    return dict(n=len(r), mean=r.mean(), win=(r > 0).mean() * 100, ex=ex.mean(), t=t,
                per_yr={y: (lambda s: (len(s), s['oc1'].mean(), (s['oc1'] > 0).mean() * 100,
                                       s.groupby('trade_date')['oc1'].mean().sub(mkt, axis=0).dropna().mean()))(sub[yr == y])
                        for y, yr in [(y, sub['trade_date'].str[:4]) for y in sorted(sub['trade_date'].str[:4].unique())]})


def line(name, st, show_yr=True):
    if st is None:
        print(f"  {name:<40} 样本不足")
        return
    print(f"  {name:<40}{st['n']:>8}{st['mean']:>9.3f}%{st['win']:>7.1f}%"
          f"{st['ex']:>9.3f}{st['t']:>7.2f}")
    if show_yr:
        p = " | ".join(f"{y}:{v[3]:+.3f}(n={v[0]})" for y, v in st['per_yr'].items())
        print(f"       {p}")


# ══ A. 长样本 ═══════════════════════════════════════════════════
print("\n" + "=" * 118)
print("A) 长样本 2020-01-01 ~ 2026-09-23（主板）—— 表头 n / 均值OC1 / 胜率 / 超额pp / t")
print("=" * 118)
wA = base_ok & (px['trade_date'] >= '2020-01-01') & (px['trade_date'] <= '2026-09-23')
mktA = px.loc[wA].groupby('trade_date')['oc1'].mean()
print(f"  基线：全市场 OC1 {px.loc[wA,'oc1'].mean():+.3f}%  胜率 {(px.loc[wA,'oc1']>0).mean()*100:.1f}%"
      f"  交易日 {px.loc[wA,'trade_date'].nunique()}")
line('第一重 趋势', stats(T1, wA, mktA), False)
line('第三重 金叉&MA10', stats(CROSS_MA10, wA, mktA), False)
line('全链 趋势+贴近MA40+金叉&MA10', stats(T1 & P_PULL & CROSS_MA10, wA, mktA))
line('低吸 趋势+破MA40收回+金叉&MA10', stats(T1 & V_dip_recover & CROSS_MA10, wA, mktA))
line('低吸 趋势+首次站上MA40+金叉&MA10', stats(T1 & V_first_ma40 & CROSS_MA10, wA, mktA))

# ══ B. 低吸形态（同第一轮窗口，便于对照）══════════════════════════
print("\n" + "=" * 118)
print("B) 低吸形态变体（2024-07-01 ~ 2026-09-23，与第一轮同窗可比）")
print("=" * 118)
wB = base_ok & (px['trade_date'] >= '2024-07-01') & (px['trade_date'] <= '2026-09-23')
mktB = px.loc[wB].groupby('trade_date')['oc1'].mean()
for nm, m in [('趋势 + MA40下方0.5~5%（提前埋伏）', T1 & V_below_ma40),
              ('趋势 + MA40下方0.5~5% + 金叉', T1 & V_below_ma40 & CROSS),
              ('趋势 + 破MA40收回 + 金叉&MA10', T1 & V_dip_recover & CROSS_MA10),
              ('趋势 + 回踩MA10 + 金叉&MA10', T1 & V_pull_ma10 & CROSS_MA10),
              ('趋势 + 首次站上MA40 + 金叉&MA10', T1 & V_first_ma40 & CROSS_MA10),
              ('趋势 + 首次站上MA40（无金叉）', T1 & V_first_ma40),
              ('第二重 近10日回撤≥5%（第一轮弱显著项）', V_deep_dd),
              ('趋势 + 近10日回撤≥5%', T1 & V_deep_dd),
              ('趋势 + 近10日回撤≥5% + 金叉', T1 & V_deep_dd & CROSS)]:
    line(nm, stats(m, wB, mktB))

# ══ C. 现有推荐内部的增量（最关键）══════════════════════════════
print("\n" + "=" * 118)
print("C) 挂在现有 stock_signal 推荐上：桶内增量 Δ（铁律 6/14）")
print("=" * 118)
sig = sig[sig['code'].str.startswith(('60', '00'))].copy()
print(f"  stock_signal 主板 {len(sig):,} 行；horizon 分布 {sig['horizon'].value_counts().to_dict()}")

tr = px[['code', 'trade_date', 'oc1', 'close', 'ma40', 'ma160', 'dif', 'dea', 'dif_p', 'dea_p',
         'ma10', 'cp', 'ma40_p', 'low', 'ma10max', 'n_open']].copy()
tr['tri_full'] = T1 & P_PULL & CROSS_MA10
tr['tri_trend'] = T1
tr['tri_cross'] = CROSS_MA10
tr['tri_dip'] = T1 & V_dip_recover & CROSS_MA10
tr['tri_dd'] = T1 & V_deep_dd

for sd, td in (('scan_date', 'trade_date'),):
    tr_key = tr.rename(columns={td: '_td'})
    m = sig.merge(tr_key, left_on=['code', td], right_on=['code', '_td'], how='inner')
    m = m[m['oc1'].notna() & m['n_open'].notna()]
    print(f"  可评估匹配 {len(m):,} 行（{sd} 口径）")
    for hz in ('short', 'long', 'mid'):
        s = m[m['horizon'] == hz]
        if len(s) < 50:
            continue
        tot_mean, tot_win = s['oc1'].mean(), (s['oc1'] > 0).mean() * 100
        print(f"\n   ── horizon={hz}  n={len(s)}  整体 OC1 {tot_mean:+.3f}%  胜率 {tot_win:.1f}%")
        for col, nm in [('tri_full', '三重滤网全链命中'), ('tri_trend', '趋势命中'),
                        ('tri_cross', '金叉命中'), ('tri_dip', '破MA40收回+金叉'),
                        ('tri_dd', '趋势+近10日回撤≥5%')]:
            hit = s[s[col] == True]
            miss = s[s[col] != True]
            if len(hit) < 20:
                print(f"     {nm:<22} 命中 {len(hit):<5} 样本不足")
                continue
            print(f"     {nm:<22} 命中 {len(hit):<6} {hit['oc1'].mean():>+7.3f}% / 胜率{(hit['oc1']>0).mean()*100:>5.1f}%"
                  f"  | 未命中 {len(miss):<6} {miss['oc1'].mean():>+7.3f}% / 胜率{(miss['oc1']>0).mean()*100:>5.1f}%"
                  f"  | Δ收益 {hit['oc1'].mean()-miss['oc1'].mean():>+7.3f}pp"
                  f"  Δ胜率 {(hit['oc1']>0).mean()*100-(miss['oc1']>0).mean()*100:>+6.1f}pp")

# ══ D. 位置特征（回答「能否避免买在高点」）════════════════════════
print("\n" + "=" * 118)
print("D) 位置特征：信号日位置 vs 现网既有两线的对照（2024-07 起主板）")
print("=" * 118)
sub = px.loc[wB]
for nm, m in [('三重滤网全链', T1 & P_PULL & CROSS_MA10),
              ('短线过滤链要求 pct_above_ma20≥5.5%', (sub['close'] / sub['ma10'] - 1) > 0)]:
    pass
# 直接比：三重滤网信号 vs stock_signal 短线推荐 的「距 MA40 / MA160 偏离」
tri_idx = px.index[wB & T1 & P_PULL & CROSS_MA10]
tr_pos = ((px.loc[tri_idx, 'close'] / px.loc[tri_idx, 'ma40'] - 1) * 100)
tr_pos160 = ((px.loc[tri_idx, 'close'] / px.loc[tri_idx, 'ma160'] - 1) * 100)
ss = sig[sig['horizon'] == 'short'].copy()
ss['pa20'] = pd.to_numeric(ss['pct_above_ma20'], errors='coerce') * 100
print(f"  三重滤网信号  n={len(tri_idx)}  距MA40中位 {tr_pos.median():+.2f}%  距MA160中位 {tr_pos160.median():+.2f}%")
print(f"  现网短线推荐   n={len(ss)}     pct_above_ma20 中位 {ss['pa20'].median():+.2f}%  "
      f"（>0 占比 {(ss['pa20']>0).mean():.1%}）")
print("\n完成。")
