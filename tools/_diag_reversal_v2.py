"""
反转首日线 —— 严格配对检验 v2
1) 市场中性化：每个信号日的超额 = 当日信号组均值 OC - 当日全市场均值 OC
   （消除 beta，只留选股 alpha）
2) 多日持有：反转线的价值可能在后续 3~5 日，而非 T+1
3) 对照组：现有「短线融合」线上信号的同期超额
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

DB = 'core/quant.db'
WARMUP_START = '2023-06-01'
SIG_START = '2024-07-01'
SIG_END = '2026-09-23'
pd.set_option('display.width', 220)

con = sqlite3.connect(DB)
df = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, pct_change "
    "FROM daily_price WHERE trade_date >= ? ORDER BY code, trade_date", con, params=(WARMUP_START,))
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[df['close'] > 0]

g = df.groupby('code', sort=False)
df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
df['ma20_prev'] = g['ma20'].shift(1)
df['close_prev'] = g['close'].shift(1)
df['ema12'] = g['close'].transform(lambda s: s.ewm(span=12, adjust=False).mean())
df['ema26'] = g['close'].transform(lambda s: s.ewm(span=26, adjust=False).mean())
df['dif'] = df['ema12'] - df['ema26']
df['dea'] = df.groupby('code', sort=False)['dif'].transform(lambda s: s.ewm(span=9, adjust=False).mean())
df['dif_prev'] = g['dif'].shift(1)
df['dea_prev'] = g['dea'].shift(1)
df['vma5_prev'] = g['volume'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['volume'] / df['vma5_prev'].replace(0, np.nan)
df['ret20_prev'] = (df['close_prev'] / g['close'].shift(21) - 1) * 100
df['pct'] = (df['close'] / df['close_prev'] - 1) * 100
df['limit_thr'] = np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)

# T+1 建仓价 & T+k 收盘
df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
for k in (1, 2, 3, 5, 10):
    df[f'c{k}'] = g['close'].shift(-k)
    df[f'oc{k}'] = (df[f'c{k}'] / df['n_open'] - 1) * 100      # T+1 开盘买 → T+k 收盘卖
df['unbuyable'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])

c_cross_up = (df['close'] > df['ma20']) & (df['close_prev'] <= df['ma20_prev'])
c_vol = df['vr'] >= 1.5
c_macd = (df['dif'] > df['dea']) & (df['dif_prev'] <= df['dea_prev'])
c_turn = df['ma20'] > df['ma20_prev']
c_notlimit = df['pct'] < df['limit_thr']
c_down = df['ret20_prev'] <= -10

win = (df['trade_date'] >= SIG_START) & (df['trade_date'] <= SIG_END) & df['n_open'].notna() & (~df['unbuyable'])

VARIANTS = {
    'V0 首次站上MA20':        c_cross_up,
    'V1 V0+放量':             c_cross_up & c_vol,
    'V3 V1+MA20拐头':         c_cross_up & c_vol & c_turn,
    'V4 V1+金叉+拐头':        c_cross_up & c_vol & c_macd & c_turn,
    'V6 V4+非涨停':           c_cross_up & c_vol & c_macd & c_turn & c_notlimit,
    'V6b V3+非涨停(去金叉)':  c_cross_up & c_vol & c_turn & c_notlimit,
    'V5 V4+前期跌10%':        c_cross_up & c_vol & c_macd & c_turn & c_down,
    'V9 V6+前期跌10%':        c_cross_up & c_vol & c_macd & c_turn & c_notlimit & c_down,
}

# ── 市场基线（按日）─────────────────────────────────────────────────────
base = df[win].copy()
base['year'] = base['trade_date'].str[:4]
mkt = base.groupby('trade_date')['oc1'].mean().rename('mkt_oc1')

print("=" * 110)
print(f"【市场基线】n={len(base):,}  OC1 {base['oc1'].mean():+.2f}%  胜率 {(base['oc1'] > 0).mean() * 100:.1f}%  交易日 {len(mkt)}")
for y, s in base.groupby('year'):
    print(f"   {y}  n={len(s):<8} OC1 {s['oc1'].mean():+.2f}%  胜率 {(s['oc1'] > 0).mean() * 100:.1f}%")


def paired(name, mask, horizon='oc1'):
    m = df[win & mask].copy()
    if len(m) < 30:
        print(f"\n▌{name}: n={len(m)} 过少")
        return
    m['year'] = m['trade_date'].str[:4]
    dm = m.groupby('trade_date')[horizon].mean().rename('sig')
    j = pd.concat([dm, mkt], axis=1).dropna()
    j['ex'] = j['sig'] - j['mkt_oc1']
    ex, sd, n = j['ex'].mean(), j['ex'].std(), len(j)
    t = ex / (sd / np.sqrt(n)) if sd > 0 else 0
    raw = m[horizon].mean()
    wr = (m[horizon] > 0).mean() * 100
    # 分年超额
    yr_lines = []
    for y in sorted(m['year'].unique()):
        sub = m[m['year'] == y]
        dd = sub.groupby('trade_date')[horizon].mean().rename('sig')
        jj = pd.concat([dd, mkt], axis=1).dropna()
        yr_lines.append((y, len(sub), sub[horizon].mean(), (sub[horizon] > 0).mean() * 100,
                         (jj['sig'] - jj['mkt_oc1']).mean()))
    print(f"\n▌{name}   horizon={horizon}   n={len(m)}  日均 {len(m)/len(mkt):.2f} 只")
    print(f"    原始 OC {raw:+.2f}% / 胜率 {wr:.1f}%   |  市场中性超额 {ex:+.3f}pp  t={t:+.2f}  (有效日 {n})")
    for y, nn, o, w, xx in yr_lines:
        print(f"      {y}  n={nn:<5} OC {o:+.2f}%  胜率 {w:.1f}%  超额 {xx:+.3f}pp")
    ys = [r[4] for r in yr_lines]
    print(f"      → 分年超额全正: {'✅' if all(x > 0 for x in ys) else '❌'}")


print("\n" + "=" * 110)
print("【变体 × 市场中性超额 (T+1 OC)】")
for k, v in VARIANTS.items():
    paired(k, v, 'oc1')

print("\n" + "=" * 110)
print("【多日持有：V6 / V4 在 T+k 的表现】")
for k in ('V6 V4+非涨停', 'V4 V1+金叉+拐头', 'V3 V1+MA20拐头'):
    for h in ('oc1', 'oc2', 'oc3', 'oc5', 'oc10'):
        paired(k, VARIANTS[k], h)

# ── 对照组：现有短线融合 ────────────────────────────────────────────────
print("\n" + "=" * 110)
print("【对照组：现有线上「短线融合」信号】")
con = sqlite3.connect(DB)
sig = pd.read_sql(
    "SELECT trade_date, code FROM stock_signal WHERE horizon='short' AND strategy='短线融合' "
    "AND trade_date >= ? AND trade_date <= ?", con, params=(SIG_START, SIG_END))
lhb = pd.read_sql(
    "SELECT trade_date, code FROM stock_signal WHERE horizon='short' AND strategy='隔日动量' "
    "AND trade_date >= ? AND trade_date <= ?", con, params=(SIG_START, SIG_END))
con.close()
key = df[['code', 'trade_date', 'oc1']].copy()
for nm, s in (('短线融合', sig), ('隔日动量', lhb)):
    s = s.drop_duplicates(['trade_date', 'code'])
    merged = s.merge(key, on=['code', 'trade_date'], how='inner')
    merged['year'] = merged['trade_date'].str[:4]
    dm = merged.groupby('trade_date')['oc1'].mean().rename('sig')
    j = pd.concat([dm, mkt], axis=1).dropna()
    ex = (j['sig'] - j['mkt_oc1']).mean()
    print(f"\n▌{nm}  n={len(merged)}  日均 {len(merged)/len(mkt):.2f}  "
          f"原始 OC {merged['oc1'].mean():+.2f}% / 胜率 {(merged['oc1'] > 0).mean() * 100:.1f}%  "
          f"超额 {ex:+.3f}pp")
    for y, gsub in merged.groupby('year'):
        dd = gsub.groupby('trade_date')['oc1'].mean().rename('sig')
        jj = pd.concat([dd, mkt], axis=1).dropna()
        print(f"      {y}  n={len(gsub):<5} OC {gsub['oc1'].mean():+.2f}%  "
              f"胜率 {(gsub['oc1'] > 0).mean() * 100:.1f}%  超额 {(jj['sig'] - jj['mkt_oc1']).mean():+.3f}pp")
