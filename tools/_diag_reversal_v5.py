"""
反转首日线 —— 第五轮：最终参数锁定
1) 热门日门控：09-21 全市场普涨日 W5 出了 132 只信号，需确认热门日是否为负期望子集
2) 出场配置：裸持有 T+1 vs 止损/止盈/ATR 多档，选定最优
口径：W5 = 首次站上MA20 & 大阳≥5% & 放量≥1.5 & 非涨停
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 220)

con = sqlite3.connect('core/quant.db')
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,pct_change,turnover FROM daily_price "
                 "WHERE trade_date >= '2023-06-01' ORDER BY code, trade_date", con)
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[df['close'] > 0]

g = df.groupby('code', sort=False)
df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
df['ma20_prev'] = g['ma20'].shift(1)
df['close_prev'] = g['close'].shift(1)
df['pct'] = (df['close'] / df['close_prev'] - 1) * 100
df['vma5_prev'] = g['volume'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['volume'] / df['vma5_prev'].replace(0, np.nan)
df['ext'] = (df['close'] / df['ma20'] - 1) * 100
df['notlimit'] = df['pct'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)

# ATR14
pc = g['close'].shift(1)
tr = pd.concat([df['high'] - df['low'], (df['high'] - pc).abs(), (df['low'] - pc).abs()], axis=1).max(axis=1)
df['atr14'] = tr.groupby(df['code']).transform(lambda s: s.rolling(14).mean())

W5 = ((df['close'] > df['ma20']) & (df['close_prev'] <= df['ma20_prev']) & (df['pct'] >= 5)
      & (df['vr'] >= 1.5) & df['notlimit'])
df['oc1'] = (g['close'].shift(-1) / g['open'].shift(-1) - 1) * 100
win = (df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23') & df['close'].notna()
sig = df[win & W5].copy()
print(f"W5 样本 n={len(sig)}  日均 {len(sig)/544:.2f}")

# ── 1. 热门日门控 ───────────────────────────────────────────────────────
# 全市场当日平均涨幅（T 日，即信号日）
mkt_pct = df[win].groupby('trade_date')['pct'].mean().rename('mkt_pct')
_o1, _h1, _l1 = g['open'].shift(-1), g['high'].shift(-1), g['low'].shift(-1)
_tradable = win & ~((_o1 == _h1) & (_o1 == _l1))
mkt_oc1 = df[_tradable].groupby('trade_date')['oc1'].mean().rename('mkt_oc1')

print("\n【热门日门控：按信号日全市场平均涨幅分档】")
s = sig.merge(mkt_pct, left_on='trade_date', right_index=True, how='left')
s = s.merge(mkt_oc1, left_on='trade_date', right_index=True, how='left')
s['bucket'] = pd.cut(s['mkt_pct'], [-100, 0, 0.5, 1.0, 100], labels=['<0 (弱)', '0~0.5', '0.5~1.0', '>1.0 (热)'])
print(f"  {'档':<14}{'n':>7}{'日均':>7}{'OC1':>9}{'胜率':>7}{'超额pp':>9}")
for b, sub in s.groupby('bucket', observed=True):
    e = (sub.groupby('trade_date')['oc1'].mean() - sub.groupby('trade_date')['mkt_oc1'].mean()).dropna()
    print(f"  {str(b):<14}{len(sub):>7}{len(sub)/544:>7.2f}{sub['oc1'].mean():>8.2f}%"
          f"{(sub['oc1']>0).mean()*100:>6.1f}%{e.mean():>9.3f}")
for th in (0.5, 1.0, 1.5):
    sub = s[s['mkt_pct'] < th]
    e = (sub.groupby('trade_date')['oc1'].mean() - sub.groupby('trade_date')['mkt_oc1'].mean()).dropna()
    print(f"  门控 mkt_pct<{th}: n={len(sub)} 日均{len(sub)/544:.2f} OC1 {sub['oc1'].mean():+.2f}% "
          f"胜率{(sub['oc1']>0).mean()*100:.1f}% 超额{e.mean():+.3f}pp")

# ── 2. 出场路径模拟 ─────────────────────────────────────────────────────
print("\n【出场配置路径模拟（T+1 开盘建仓，最多持有 max_hold 日）】")
base = df[win & W5].index
n_open = df.groupby('code')['open'].shift(-1)
sim_cols = {}
for k in range(1, 11):
    sim_cols[f'o{k}'] = df.groupby('code')['open'].shift(-k)
    sim_cols[f'h{k}'] = df.groupby('code')['high'].shift(-k)
    sim_cols[f'l{k}'] = df.groupby('code')['low'].shift(-k)
    sim_cols[f'c{k}'] = df.groupby('code')['close'].shift(-k)
S = pd.DataFrame(sim_cols).loc[base]
ENTRY = n_open.loc[base]
CLOSE0 = df.loc[base, 'close']
ATR = df.loc[base, 'atr14']
valid = ENTRY.notna() & (ENTRY > 0) & (S['c1'].notna())
# 剔除 T+1 一字板
unbuy = (S['o1'] == S['h1']) & (S['o1'] == S['l1'])
valid &= ~unbuy
ENTRY, S, CLOSE0, ATR = ENTRY[valid], S[valid], CLOSE0[valid], ATR[valid]
print(f"  有效样本 n={len(ENTRY)}")


def simulate(stop_pct, take_pct, max_hold, atr_k=None, atr_lo=None, atr_hi=None, judge_entry=True):
    """返回逐笔收益 %。止损/止盈以信号日收盘价为基准（与线上隔日动量同口径）。"""
    e = ENTRY.to_numpy()
    ref = CLOSE0.to_numpy()
    if atr_k is not None:
        a = np.clip(atr_k * ATR.to_numpy() / ref, atr_lo, atr_hi)
        sl = ref * (1 - a)
    else:
        sl = ref * (1 + stop_pct)
    tp = ref * (1 + take_pct)
    out = np.full(len(e), np.nan)
    for i in range(len(e)):
        start = 1 if judge_entry else 2
        hit = False
        for k in range(start, max_hold + 1):
            hi, lo, cl = S[f'h{k}'].iloc[i], S[f'l{k}'].iloc[i], S[f'c{k}'].iloc[i]
            o = S[f'o{k}'].iloc[i]
            if not np.isfinite(cl):
                break
            if np.isfinite(lo) and lo <= sl[i]:          # 先判止损（保守）
                out[i] = (sl[i] / e[i] - 1) * 100
                hit = True
                break
            if np.isfinite(hi) and hi >= tp[i]:
                out[i] = (tp[i] / e[i] - 1) * 100
                hit = True
                break
            if k == max_hold:
                out[i] = (cl / e[i] - 1) * 100
                hit = True
        if not hit:
            out[i] = np.nan
    return pd.Series(out, index=ENTRY.index)


CFG = [
    ('裸持有 T+1 收盘 (OC1)', dict(stop_pct=-99, take_pct=99, max_hold=1)),
    ('止损-4% / 止盈+6.5% / 5日', dict(stop_pct=-0.04, take_pct=0.065, max_hold=5)),
    ('止损-5% / 止盈+8% / 5日', dict(stop_pct=-0.05, take_pct=0.08, max_hold=5)),
    ('止损-6% / 止盈+10% / 5日', dict(stop_pct=-0.06, take_pct=0.10, max_hold=5)),
    ('ATR2.5(5~15%) / 止盈+8% / 5日', dict(stop_pct=0, take_pct=0.08, max_hold=5,
                                          atr_k=2.5, atr_lo=0.05, atr_hi=0.15)),
    ('ATR2.5(5~15%) / 止盈+10% / 5日', dict(stop_pct=0, take_pct=0.10, max_hold=5,
                                           atr_k=2.5, atr_lo=0.05, atr_hi=0.15)),
    ('ATR2.5(5~15%) / 止盈+12% / 5日', dict(stop_pct=0, take_pct=0.12, max_hold=5,
                                           atr_k=2.5, atr_lo=0.05, atr_hi=0.15)),
    ('ATR2.5(5~15%) / 止盈+10% / 10日', dict(stop_pct=0, take_pct=0.10, max_hold=10,
                                            atr_k=2.5, atr_lo=0.05, atr_hi=0.15)),
    ('止损-6% / 止盈+12% / 5日', dict(stop_pct=-0.06, take_pct=0.12, max_hold=5)),
    ('止损-6% / 止盈+10% / 10日', dict(stop_pct=-0.06, take_pct=0.10, max_hold=10)),
]
print(f"  {'配置':<30}{'n':>7}{'均值':>9}{'胜率':>8}{'超额pp':>9}{'盈亏比':>8}")
dates = df.loc[ENTRY.index, 'trade_date']


def mkt_bare(k):
    """市场同持有期基线（可交易子集）：全市场 T+1 开盘买 → T+k 收盘卖，无止损。
    ⚠ 必须与信号组同口径剔除 T+1 一字板（那些日子收益巨大但根本买不到，
       不剔除会把基线抬高 0.5~0.9pp，导致「出场纪律有害」的假结论）。"""
    o1 = df.groupby('code')['open'].shift(-1)
    h1 = df.groupby('code')['high'].shift(-1)
    l1 = df.groupby('code')['low'].shift(-1)
    r = (df.groupby('code')['close'].shift(-k) / o1 - 1) * 100
    tradable = win & ~((o1 == h1) & (o1 == l1))
    return r[tradable].groupby(df.loc[tradable, 'trade_date']).mean()


for nm, kw in CFG:
    r = simulate(**kw)
    ok = r.notna()
    if ok.sum() < 30:
        print(f"  {nm:<30} 样本不足"); continue
    rr = r[ok]
    dd = dates[ok]
    mb = mkt_bare(kw['max_hold'])
    ss = pd.Series(rr.values, index=dd.values)
    ex = ss.groupby(level=0).mean().sub(mb, axis=0).dropna()
    win_r = (rr > 0).mean() * 100
    pl = rr[rr > 0].mean() / abs(rr[rr < 0].mean()) if (rr < 0).any() else np.nan
    print(f"  {nm:<30}{ok.sum():>7}{rr.mean():>8.2f}%{win_r:>7.1f}%{ex.mean():>9.3f}{pl:>8.2f}")

# 分年看最优候选
print("\n【候选出场配置 · 分年】（超额已配同持有期市场基线）")
for nm, kw in [('裸持有OC1', CFG[0][1]), ('止损-6%/止盈+10%/5日', CFG[3][1]),
               ('ATR2.5(5~15%)/止盈+10%/5日', CFG[5][1])]:
    r = simulate(**kw)
    ok = r.notna()
    mb = mkt_bare(kw['max_hold'])
    yr = dates[ok].str[:4]
    print(f"  {nm}:")
    for y in sorted(yr.unique()):
        sub = r[ok][yr == y]
        dd = dates[ok][yr == y]
        ex = pd.Series(sub.values, index=dd.values).groupby(level=0).mean().sub(mb, axis=0).dropna()
        print(f"     {y}  n={len(sub):<5} 均值{sub.mean():+.2f}%  胜率{(sub>0).mean()*100:.1f}%  超额{ex.mean():+.3f}pp")
