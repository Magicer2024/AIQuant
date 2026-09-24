"""
反转首日信号线 —— 最小实验（报告 docs/short-lhb-chase-diagnosis.md §6C）

目的：验证「首次站上 MA20 + 放量 + MACD 金叉」能否成为一条独立正期望短线线。
口径：T 日收盘出信号 → T+1 开盘买入 → T+1 收盘卖出（OC，现实口径，与隔日动量对齐）。
      剔除 T+1 一字板（买不到）。
输出：变体网格 × 分年 OC 均值/胜率 + 日均信号数 + 市场基线对照。
"""
import os
import sqlite3
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

DB = 'core/quant.db'
WARMUP_START = '2023-06-01'   # 预热（MA20/MACD EMA 需要）
SIG_START = '2024-07-01'      # 信号评估起点（与隔日动量线同窗）
SIG_END = '2026-09-23'

pd.set_option('display.width', 200)
pd.set_option('display.max_columns', 50)

# ── 1. 载入 ──────────────────────────────────────────────────────────────
con = sqlite3.connect(DB)
df = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, pct_change, turnover "
    "FROM daily_price WHERE trade_date >= ? ORDER BY code, trade_date", con, params=(WARMUP_START,))
con.close()

# 剔除科创板/北交所（本地 daily_price 覆盖不足 + 20cm 口径差异）
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[df['close'] > 0]
print(f"载入 {len(df):,} 行 / {df['code'].nunique()} 只 / {df['trade_date'].min()} ~ {df['trade_date'].max()}")

g = df.groupby('code', sort=False)

# ── 2. 指标 ─────────────────────────────────────────────────────────────
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

# 前期动量（不含信号日）：前 20 日涨幅
df['ret20_prev'] = (df['close_prev'] / g['close'].shift(21) - 1) * 100

# ── 3. T+1 出场字段 ─────────────────────────────────────────────────────
df['n_open'] = g['open'].shift(-1)
df['n_close'] = g['close'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
df['n_pct'] = g['pct_change'].shift(-1)
df['oc'] = (df['n_close'] / df['n_open'] - 1) * 100          # T+1 open->close
df['n2_open'] = g['open'].shift(-2)
df['cc2'] = (df['n_close'] / df['n_open'] - 1) * 100

# 信号日涨跌幅（自算，前复权比价，避免混源）
df['pct'] = (df['close'] / df['close_prev'] - 1) * 100

# ── 4. 可交易性过滤 ─────────────────────────────────────────────────────
df['is_20cm'] = df['code'].str.startswith(('300', '301'))
df['limit_thr'] = np.where(df['is_20cm'], 19.8, 9.8)

# T+1 一字板（全天同价）→ 买不到
df['unbuyable'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])
# T+1 缺数据
df['no_next'] = df['n_open'].isna() | df['n_close'].isna()

# ── 5. 条件构件 ─────────────────────────────────────────────────────────
c_cross_up = (df['close'] > df['ma20']) & (df['close_prev'] <= df['ma20_prev'])   # 首次站上 MA20
c_vol = df['vr'] >= 1.5                                                          # 放量
c_macd_cross = (df['dif'] > df['dea']) & (df['dif_prev'] <= df['dea_prev'])      # MACD 金叉当日
c_ma20_turn = df['ma20'] > df['ma20_prev']                                       # MA20 拐头向上
c_not_limit = df['pct'] < df['limit_thr']                                        # 信号日非涨停
c_downtrend = df['ret20_prev'] <= -10                                            # 前期跌过（反转前提）
c_above = df['close'] > df['ma20']                                               # 仅站上（不限首次）

# 评估窗口
win = (df['trade_date'] >= SIG_START) & (df['trade_date'] <= SIG_END) & (~df['no_next']) & (~df['unbuyable'])

VARIANTS = {
    'V0 仅首次站上MA20':            c_cross_up,
    'V1 V0+放量≥1.5':               c_cross_up & c_vol,
    'V2 V1+MACD金叉':               c_cross_up & c_vol & c_macd_cross,
    'V3 V1+MA20拐头向上':           c_cross_up & c_vol & c_ma20_turn,
    'V4 V1+金叉+拐头(§6C主条件)':   c_cross_up & c_vol & c_macd_cross & c_ma20_turn,
    'V5 V4+前期跌≥10%':             c_cross_up & c_vol & c_macd_cross & c_ma20_turn & c_downtrend,
    'V6 V4+信号日非涨停':           c_cross_up & c_vol & c_macd_cross & c_ma20_turn & c_not_limit,
    'V7 V1+金叉':                   c_cross_up & c_vol & c_macd_cross,
    'V8 站上MA20(不限首次)+金叉+放量': c_above & c_vol & c_macd_cross & c_ma20_turn,
}


def evaluate(name, mask):
    m = df[win & mask].copy()
    if len(m) == 0:
        print(f"{name:<34} 无样本")
        return None
    m['year'] = m['trade_date'].str[:4]
    days = df.loc[win, 'trade_date'].nunique()
    rows = []
    for y, sub in m.groupby('year'):
        rows.append((y, len(sub), sub['oc'].mean(), (sub['oc'] > 0).mean() * 100))
    all_oc, all_wr = m['oc'].mean(), (m['oc'] > 0).mean() * 100
    per_day = len(m) / days
    print(f"\n▌{name}   n={len(m)}  日均 {per_day:.2f} 只  全期 OC {all_oc:+.2f}% / 胜率 {all_wr:.1f}%")
    for y, n, oc, wr in rows:
        print(f"    {y}  n={n:<5} OC {oc:+.2f}%  胜率 {wr:.1f}%")
    yrs = [r[2] for r in rows]
    flag = '✅ 分年全正' if all(x > 0 for x in yrs) else ('❌ 有负年' if any(x < 0 for x in yrs) else '')
    print(f"    → {flag}")
    return dict(name=name, n=len(m), per_day=per_day, oc=all_oc, wr=all_wr, years=yrs)


# ── 6. 市场基线对照 ─────────────────────────────────────────────────────
base = df[win]
print("\n" + "=" * 96)
print(f"【市场基线】全市场所有可交易样本  n={len(base):,}  OC {base['oc'].mean():+.2f}% / 胜率 {(base['oc'] > 0).mean() * 100:.1f}%")
by = base.assign(year=base['trade_date'].str[:4]).groupby('year')['oc'].agg(['size', 'mean'])
for y, r in by.iterrows():
    print(f"    {y}  n={int(r['size']):<8} OC {r['mean']:+.2f}%")

print("\n" + "=" * 96)
print("【变体网格】")
res = [evaluate(k, v) for k, v in VARIANTS.items()]

# ── 7. 关键案例核对：600641 09-16 是否命中 ───────────────────────────────
print("\n" + "=" * 96)
print("【案例核对 600641（反转首日应命中 2026-09-16）】")
case = df[(df['code'] == '600641') & (df['trade_date'] >= '2026-09-10') &
          (df['trade_date'] <= '2026-09-24')].copy()
cols = ['trade_date', 'close', 'ma20', 'pct', 'vr', 'dif', 'dea', 'ret20_prev', 'oc']
case['cross'] = case['close'] > case['ma20']
print(case[cols + ['cross']].to_string(index=False))
