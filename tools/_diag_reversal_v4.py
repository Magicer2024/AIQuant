"""
反转首日线 —— 第四轮：W5 内部提纯 + 近期实际信号核对
W5 = 首次站上MA20 & 大阳≥5% & 放量≥1.5 & 非涨停   (n=15214, 日均28, 超额+0.185pp, t=+2.92)
问：内部哪个维度单调驱动超额？能否 top-N 提纯？

⚠ 已作废：本脚本早于「pct_change 口径事故」修正，样本按旧的 pct_change 涨跌幅口径统计。
  权威结果见 _diag_reversal_v5.py（close 权威口径）：n=15841、日均 29.1、超额 +0.174pp、t=+2.80。
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 240)

con = sqlite3.connect('core/quant.db')
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,pct_change,turnover FROM daily_price "
                 "WHERE trade_date >= '2023-06-01' ORDER BY code, trade_date", con)
names = pd.read_sql("SELECT code, name FROM stock_info", con)
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
df['ext'] = (df['close'] / df['ma20'] - 1) * 100
df['amplitude'] = (df['high'] - df['low']) / df['close_prev'] * 100
df['close_pos'] = (df['close'] - df['low']) / (df['high'] - df['low']).replace(0, np.nan)
df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
df['oc1'] = (g['close'].shift(-1) / df['n_open'] - 1) * 100
df['oc3'] = (g['close'].shift(-3) / df['n_open'] - 1) * 100
df['unbuyable'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])
df['notlimit'] = df['pct'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)

W5 = ((df['close'] > df['ma20']) & (df['close_prev'] <= df['ma20_prev']) & (df['pct'] >= 5)
      & (df['vr'] >= 1.5) & df['notlimit'])
win = (df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23') & df['n_open'].notna() & (~df['unbuyable'])
mkt = df[win].groupby('trade_date')['oc1'].mean().rename('mkt')


def split_report(title, col, bins, labels):
    m = df[win & W5].copy()
    m['bucket'] = pd.cut(m[col], bins=bins, labels=labels)
    print(f"\n【{title}】")
    print(f"  {'档':<14}{'n':>7}{'日均':>7}{'OC1':>9}{'胜率':>7}{'超额pp':>9}{'t':>7}")
    for b, s in m.groupby('bucket', observed=True):
        e = s.groupby('trade_date')['oc1'].mean().sub(mkt, axis=0).dropna()
        t = e.mean() / (e.std() / np.sqrt(len(e))) if e.std() > 0 else 0
        print(f"  {str(b):<14}{len(s):>7}{len(s)/544:>7.2f}{s['oc1'].mean():>8.2f}%"
              f"{(s['oc1']>0).mean()*100:>6.1f}%{e.mean():>9.3f}{t:>+7.2f}")


split_report('量比 vr 分档', 'vr', [1.5, 2, 2.5, 3, 4, 100], ['1.5-2', '2-2.5', '2.5-3', '3-4', '>4'])
split_report('信号日涨幅 pct 分档', 'pct', [5, 6, 7, 8, 9.8, 19.8], ['5-6', '6-7', '7-8', '8-9.8', '9.8-20'])
split_report('入场偏离 ext 分档', 'ext', [-100, 0, 2, 4, 6, 100], ['<0', '0-2', '2-4', '4-6', '>6'])
split_report('前20日涨幅 ret20_prev 分档', 'ret20_prev', [-100, -20, -10, 0, 10, 100],
             ['<-20', '-20~-10', '-10~0', '0~10', '>10'])
split_report('收盘位置 close_pos 分档', 'close_pos', [0, 0.5, 0.8, 1.01], ['<0.5', '0.5-0.8', '>0.8'])
split_report('振幅 amplitude 分档', 'amplitude', [0, 5, 8, 12, 100], ['<5', '5-8', '8-12', '>12'])

# ── top-N by vr ────────────────────────────────────────────────────────
print("\n【每日按量比取 top-N（提纯检验）】")
m = df[win & W5].copy()
for N in (3, 5, 10):
    top = m.sort_values('vr', ascending=False).groupby('trade_date').head(N)
    e = top.groupby('trade_date')['oc1'].mean().sub(mkt, axis=0).dropna()
    t = e.mean() / (e.std() / np.sqrt(len(e)))
    print(f"  top{N:<3} n={len(top):<6} 日均 {len(top)/544:.1f}  OC1 {top['oc1'].mean():+.2f}% "
          f"胜率 {(top['oc1']>0).mean()*100:.1f}%  超额 {e.mean():+.3f}pp t={t:+.2f}")

# ── 最近 10 日实际信号 ─────────────────────────────────────────────────
print("\n【最近 12 个交易日的 W5 信号（含 ext / 前期跌幅）】")
recent_dates = sorted(df[win]['trade_date'].unique())[-12:]
nm = dict(zip(names['code'], names['name']))
for d in recent_dates:
    sub = df[win & W5 & (df['trade_date'] == d)]
    if len(sub) == 0:
        continue
    sub = sub.sort_values('vr', ascending=False)
    top = sub.head(6)
    txt = "  ".join(f"{r.code}{nm.get(r.code,'')[:4]}(+{r.pct:.1f}% vr{r.vr:.1f} ext{r.ext:+.1f}%)"
                    for r in top.itertuples())
    print(f"  {d}  n={len(sub):<3} {txt}")

# 600641 命中核对（逐条件）
print("\n【600641 逐条件核对 2026-09-11~09-23】")
c = df[(df['code'] == '600641') & (df['trade_date'] >= '2026-09-11')].copy()
c['首次站上'] = (c['close'] > c['ma20']) & (c['close_prev'] <= c['ma20_prev'])
c['大阳≥5%'] = c['pct'] >= 5
c['放量≥1.5'] = c['vr'] >= 1.5
c['非涨停'] = c['notlimit']
c['W5命中'] = c['首次站上'] & c['大阳≥5%'] & c['放量≥1.5'] & c['非涨停']
print(c[['trade_date', 'close', 'ma20', 'pct', 'vr', 'ext', '首次站上', '大阳≥5%', '放量≥1.5', '非涨停', 'W5命中']]
      .to_string(index=False))
