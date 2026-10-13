"""
三重滤网（Elder Triple Screen 变体）—— A 股实证检验
=====================================================
用户命题（2026-09-27）：40/160 均线定多空 → 等回踩 → MACD(10,20,5) 金叉 + 均线突破
                       精确到单根 K 线；2:1 盈亏比下胜率 >33% 即正期望。

本脚本不采信命题，做三件事：
  1) 把三重拆成可独立开关的条件，做 leave-one-out 增量（铁律 14）
  2) 全部以「当日全市场主板可交易子集 OC1 均值」为超额基线（铁律 7/21）
  3) 分年 + 剔除单年，且用「带出场纪律」的口径做最终校验（铁律 3/12）

口径：
  · 样本 2024-07-01 ~ 2026-09-23，A 股主板（60/00，铁律 21）
  · OC1 = T+1 开盘买 → T+1 收盘卖（realistic，剔除 T+1 一字板）
  · 涨跌幅一律 close/close.shift(1)（calc_true_ret 口径，铁律 19）
  · MACD 参数按用户给定 10/20/5，另跑 12/26/9 对照
"""
import os
import sqlite3
import numpy as np
import pandas as pd

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
pd.set_option('display.width', 240)

START, END = '2024-07-01', '2026-09-23'
N_DAYS = 544  # 与 first_reversal 报告同口径（实测交易日数）

con = sqlite3.connect('core/quant.db')
df = pd.read_sql(
    "SELECT code,trade_date,open,high,low,close,volume FROM daily_price "
    "WHERE trade_date >= '2023-01-01' ORDER BY code, trade_date", con)
con.close()

# ── 主板白名单（铁律 21：可交易性硬约束，不是偏好）───────────────
n_all = len(df)
df = df[df['code'].str.startswith(('60', '00'))].copy()
df = df[(df['close'] > 0) & (df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0)]
df = df.reset_index(drop=True)
print(f"主板行 {len(df):,} / 全量 {n_all:,}（{len(df)/max(n_all,1):.1%}）；"
      f"主板股票 {df['code'].nunique()} 只")


def G(col):
    return df.groupby('code', sort=False)[col]


g = df.groupby('code', sort=False)

# ── 指标 ────────────────────────────────────────────────────────
for p in (10, 40, 160):
    df[f'ma{p}'] = g['close'].transform(lambda s: s.rolling(p).mean())

# MACD(10,20,5) —— 用户给定参数
ema_f = g['close'].transform(lambda s: s.ewm(span=10, adjust=False).mean())
ema_s = g['close'].transform(lambda s: s.ewm(span=20, adjust=False).mean())
df['dif'] = ema_f - ema_s
df['dea'] = G('dif').transform(lambda s: s.ewm(span=5, adjust=False).mean())
# MACD(12,26,9) —— 标准参数对照
ema_f2 = g['close'].transform(lambda s: s.ewm(span=12, adjust=False).mean())
ema_s2 = g['close'].transform(lambda s: s.ewm(span=26, adjust=False).mean())
df['dif2'] = ema_f2 - ema_s2
df['dea2'] = G('dif2').transform(lambda s: s.ewm(span=9, adjust=False).mean())

df['cp'] = g['close'].shift(1)
df['pct'] = (df['close'] / df['cp'] - 1) * 100
df['dif_p'] = G('dif').shift(1)
df['dea_p'] = G('dea').shift(1)
df['dif2_p'] = G('dif2').shift(1)
df['dea2_p'] = G('dea2').shift(1)
df['hist'] = df['dif'] - df['dea']

# ATR14
pc = g['close'].shift(1)
tr = pd.concat([df['high'] - df['low'], (df['high'] - pc).abs(),
                (df['low'] - pc).abs()], axis=1).max(axis=1)
df['atr14'] = tr.groupby(df['code']).transform(lambda s: s.rolling(14).mean())

# T+1 结果
df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
df['n_close'] = g['close'].shift(-1)
df['oc1'] = (df['n_close'] / df['n_open'] - 1) * 100
df['unbuy'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])

win = ((df['trade_date'] >= START) & (df['trade_date'] <= END)
       & df['n_open'].notna() & (~df['unbuy']) & df['ma160'].notna()
       & df['pct'].notna() & df['oc1'].notna() & df['dif'].notna())
print(f"窗口内可评估行 {int(win.sum()):,}；交易日 {df.loc[win, 'trade_date'].nunique()}")

# ── 全市场同口径基线（每交易日 OC1 均值）────────────────────────
mkt = df.loc[win].groupby('trade_date')['oc1'].mean()
print(f"全市场主板基线：OC1 均值 {df.loc[win,'oc1'].mean():+.3f}%  "
      f"胜率 {(df.loc[win,'oc1']>0).mean():.1%}  日度基线均值 {mkt.mean():+.3f}%")


def report(name, mask, col='oc1'):
    """输出 n / 日均 / 均值 / 胜率 / 超额 / t / 分年（超额按交易日配对市场基线）"""
    sub = df.loc[win & mask]
    if len(sub) < 30:
        print(f"  {name:<34} n={len(sub):<6} 样本不足")
        return
    r = sub[col].dropna()
    daily = sub.groupby('trade_date')[col].mean()
    ex = daily.sub(mkt, axis=0).dropna()
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex))) if len(ex) > 2 and ex.std(ddof=1) > 0 else np.nan
    pl = (r[r > 0].mean() / abs(r[r < 0].mean())) if (r < 0).any() else np.nan
    print(f"  {name:<34}{len(r):>7}{len(r)/N_DAYS:>7.2f}{r.mean():>9.3f}%"
          f"{(r>0).mean()*100:>7.1f}%{ex.mean():>9.3f}{t:>7.2f}{pl:>7.2f}")
    yr = sub['trade_date'].str[:4]
    parts = []
    for y in sorted(yr.unique()):
        sy = sub[yr == y]
        ey = sy.groupby('trade_date')[col].mean().sub(mkt, axis=0).dropna()
        parts.append(f"{y}: n={len(sy)} 超额{ey.mean():+.3f}(t={ey.mean()/(ey.std(ddof=1)/np.sqrt(len(ey))) if len(ey)>2 and ey.std(ddof=1)>0 else 0:+.2f})")
    print("       " + " | ".join(parts))


print("\n" + "=" * 118)
print("表头：n / 日均 / 均值OC1 / 胜率 / 超额pp / t / 盈亏比")
print("=" * 118)

# ── 三重条件定义 ────────────────────────────────────────────────
T1 = (df['ma40'] > df['ma160']) & (df['close'] > df['ma160'])          # 第一重：多空趋势
T1_strict = (df['close'] > df['ma40']) & (df['ma40'] > df['ma160'])    # 强多头排列
P1 = (df['close'] / df['ma40'] - 1).abs() <= 0.03                       # 第二重：贴近 MA40（回踩）
P2 = (df['close'] / df['ma40'] - 1).between(-0.02, 0.05)                # 第二重：MA40 上方 0~5%
P3 = df['hist'] < 0                                                     # 第二重：MACD 柱翻绿（动能回撤）
P4 = (df['close'] / G('close').transform(lambda s: s.rolling(10).max()) - 1) <= -0.05  # 近10日回撤≥5%
CROSS = (df['dif'] > df['dea']) & (df['dif_p'] <= df['dea_p'])          # 第三重：MACD(10,20,5) 金叉
CROSS_STD = (df['dif2'] > df['dea2']) & (df['dif2_p'] <= df['dea2_p'])  # 标准 12/26/9 金叉
CROSS_MA10 = CROSS & (df['close'] > df['ma10'])                         # 金叉 + 站上 MA10

print("\n【0. 单条件命中（各自独立）】")
for nm, m in [('第一重 趋势 MA40>MA160&close>MA160', T1),
              ('第一重 强多头 close>MA40>MA160', T1_strict),
              ('第二重 贴近MA40 ±3%', P1),
              ('第二重 MA40上方0~5%', P2),
              ('第二重 MACD柱<0', P3),
              ('第二重 近10日回撤≥5%', P4),
              ('第三重 MACD(10,20,5)金叉', CROSS),
              ('第三重 金叉&close>MA10', CROSS_MA10),
              ('第三重 MACD(12,26,9)金叉', CROSS_STD)]:
    report(nm, m)

print("\n【1. 用户原味三重滤网（三种第二重 + 第三重口径）】")
for pnm, P in [('贴近MA40', P1), ('MA40上方0~5%', P2), ('MACD柱<0', P3), ('近10日回撤≥5%', P4)]:
    report(f'全三重 [趋势+{pnm}+金叉]', T1 & P & CROSS)
    report(f'全三重 [趋势+{pnm}+金叉&MA10]', T1 & P & CROSS_MA10)

print("\n【2. leave-one-out（以「趋势+贴近MA40+金叉&MA10」为全链基准）】")
FULL = T1 & P1 & CROSS_MA10
for nm, m in [('全链', FULL),
              ('去第一重(趋势)', P1 & CROSS_MA10),
              ('去第二重(回踩)', T1 & CROSS_MA10),
              ('去第三重(金叉MA10)', T1 & P1),
              ('仅第三重(金叉&MA10)', CROSS_MA10),
              ('仅第一重(趋势)', T1),
              ('金叉 × 趋势（无回踩）', T1 & CROSS),
              ('金叉 × 回踩（无趋势）', P1 & CROSS)]:
    report(nm, m)

print("\n【3. MACD 参数对照（全链 = 趋势+贴近MA40+金叉&MA10）】")
report('金叉用 10/20/5', T1 & P1 & CROSS & (df['close'] > df['ma10']))
report('金叉用 12/26/9', T1 & P1 & CROSS_STD & (df['close'] > df['ma10']))

print("\n【4. 放松版（不要求贴近，仅要求「在 MA40 上方」的回踩区）】")
report('趋势 + close≥MA40 + 金叉&MA10', T1 & (df['close'] >= df['ma40']) & CROSS_MA10)
report('强多头 + close≥MA40 + 金叉&MA10', T1_strict & CROSS_MA10)

# ── 出场纪律（2:1 盈亏比）────────────────────────────────────────
print("\n" + "=" * 118)
print("【5. 出场路径：用户主张 2:1 盈亏比（止损 -5% / 止盈 +10%），基线=信号日收盘价】")
print("=" * 118)

sim_cols = {}
for k in range(1, 11):
    sim_cols[f'o{k}'] = G('open').shift(-k)
    sim_cols[f'h{k}'] = G('high').shift(-k)
    sim_cols[f'l{k}'] = G('low').shift(-k)
    sim_cols[f'c{k}'] = G('close').shift(-k)
S_ALL = pd.DataFrame(sim_cols)


def simulate(base_idx, stop_pct, take_pct, max_hold):
    e = df.loc[base_idx, 'n_open']
    ref = df.loc[base_idx, 'close']
    S = S_ALL.loc[base_idx]
    out = pd.Series(np.nan, index=base_idx)
    for i in base_idx:
        ei, ri = e.loc[i], ref.loc[i]
        if not (np.isfinite(ei) and ei > 0 and np.isfinite(ri)):
            continue
        sl, tp = ri * (1 + stop_pct), ri * (1 + take_pct)
        for k in range(1, max_hold + 1):
            hi, lo, cl, op = S.at[i, f'h{k}'], S.at[i, f'l{k}'], S.at[i, f'c{k}'], S.at[i, f'o{k}']
            if not np.isfinite(cl):
                break
            # 开盘即破止损 → 按开盘价出（跳空现实）
            if op <= sl:
                out.loc[i] = (op / ei - 1) * 100
                break
            if np.isfinite(lo) and lo <= sl:
                out.loc[i] = (sl / ei - 1) * 100
                break
            if np.isfinite(hi) and hi >= tp:
                out.loc[i] = (tp / ei - 1) * 100
                break
            if k == max_hold:
                out.loc[i] = (cl / ei - 1) * 100
    return out


def exit_report(name, mask, stop_pct, take_pct, max_hold):
    idx = df.index[win & mask]
    r = simulate(idx, stop_pct, take_pct, max_hold).dropna()
    if len(r) < 30:
        print(f"  {name:<40} n={len(r)} 不足")
        return
    dd = df.loc[r.index, 'trade_date']
    # 同持有期市场基线（T+1 开盘买 → T+k 收盘卖，剔除一字板）
    o1, ck = G('open').shift(-1), G('close').shift(-max_hold)
    mk = ((ck / o1 - 1) * 100)[win]
    mk = mk.groupby(df.loc[win, 'trade_date']).mean()
    ex = pd.Series(r.values, index=dd.values).groupby(level=0).mean().sub(mk, axis=0).dropna()
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex))) if len(ex) > 2 and ex.std(ddof=1) > 0 else np.nan
    pl = r[r > 0].mean() / abs(r[r < 0].mean()) if (r < 0).any() else np.nan
    print(f"  {name:<40}{len(r):>7}{r.mean():>9.2f}%{(r>0).mean()*100:>7.1f}%"
          f"{ex.mean():>9.3f}{t:>7.2f}{pl:>7.2f}"
          f"{'  市场'+format(mk.mean(),'+.2f')+'%':>14}")


for nm, m in [('全链 趋势+贴近MA40+金叉&MA10', FULL),
              ('趋势+MA40上方+金叉&MA10', T1 & (df['close'] >= df['ma40']) & CROSS_MA10),
              ('仅第三重 金叉&MA10', CROSS_MA10)]:
    for cfg_nm, kw in [('裸持有T+1', dict(stop_pct=-99, take_pct=99, max_hold=1)),
                       ('2:1 止损-5%/止盈+10%/10日', dict(stop_pct=-0.05, take_pct=0.10, max_hold=10)),
                       ('2:1 止损-5%/止盈+10%/5日', dict(stop_pct=-0.05, take_pct=0.10, max_hold=5)),
                       ('ATR2.5(5~15%)/止盈+10%/10日', dict(stop_pct=-0.05, take_pct=0.10, max_hold=10))]:
        exit_report(f'{nm[:20]} · {cfg_nm}', m, **kw)

print("\n完成。")
