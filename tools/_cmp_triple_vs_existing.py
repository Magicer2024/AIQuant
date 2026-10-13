"""
三重滤网 vs 现有策略 —— 横向对比 + HTML 报告生成
==================================================
统一口径（全部对齐项目铁律）：
  · 窗口 2024-07-01 ~ 2026-09-23（544 交易日）
  · 主板白名单 60/00（铁律 21）
  · OC1 = T+1 开盘买 → T+1 收盘卖，剔除 T+1 一字板
  · 涨跌幅 = close/close.shift(1)（铁律 19）
  · 超额 = 组内日均 OC1 − 当日全市场主板可交易子集 OC1 均值（铁律 7）

输出：reports/triple_screen_vs_existing.html
"""
import os
import json
import sqlite3
import numpy as np
import pandas as pd

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

START, END, NDAYS = '2024-07-01', '2026-09-23', 544
OUT = 'reports/triple_screen_vs_existing.html'

con = sqlite3.connect('core/quant.db')
px = pd.read_sql(
    "SELECT code,trade_date,open,high,low,close,volume FROM daily_price "
    "WHERE trade_date >= '2023-06-01' AND (code LIKE '60%' OR code LIKE '00%') "
    "ORDER BY code, trade_date", con)
sig = pd.read_sql("SELECT code,trade_date,horizon,strategy,fusion_score FROM stock_signal", con)
frh = pd.read_sql("SELECT code,scan_date FROM first_reversal_hist", con)
con.close()

# 主板白名单（铁律 21）
sig = sig[sig['code'].str.startswith(('60', '00'))].copy()
frh = frh[frh['code'].str.startswith(('60', '00'))].copy()

px = px[(px['close'] > 0) & (px['open'] > 0) & (px['high'] > 0) & (px['low'] > 0)].reset_index(drop=True)


def G(c):
    return px.groupby('code', sort=False)[c]


g = px.groupby('code', sort=False)
for p in (10, 20, 40, 160):
    px[f'ma{p}'] = g['close'].transform(lambda s: s.rolling(p).mean())
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
px['ma40dev'] = (px['close'] / px['ma40'] - 1) * 100

T1 = (px['ma40'] > px['ma160']) & (px['close'] > px['ma160'])
CROSS = (px['dif'] > px['dea']) & (px['dif_p'] <= px['dea_p'])
CROSS_MA10 = CROSS & (px['close'] > px['ma10'])
DD5 = (px['close'] / px['ma10max'] - 1) <= -0.05
TRI_FULL = T1 & (px['ma40dev'].abs() <= 3) & CROSS_MA10
TRI_DIP = T1 & (px['low'] < px['ma40']) & (px['close'] > px['ma40']) & CROSS_MA10

IDX = px[['code', 'trade_date', 'oc1', 'n_open', 'n_high', 'n_low', 'unbuy', 'ma40dev',
          'close', 'ma40', 'ma160', 'ma10', 'dif', 'dea']].rename(columns={'trade_date': 'd'})
IDX['pi'] = px.index  # 保留 px 原索引，供路径模拟按行取值
IDX['tri_full'] = TRI_FULL.values
IDX['tri_dip'] = TRI_DIP.values

# 当日全市场主板可交易基线
_w = (px['trade_date'] >= START) & (px['trade_date'] <= END) & px['n_open'].notna() & (~px['unbuy']) & px['oc1'].notna()
MKT = px.loc[_w].groupby('trade_date')['oc1'].mean()
BASE_MEAN = px.loc[_w, 'oc1'].mean()
BASE_WIN = (px.loc[_w, 'oc1'] > 0).mean() * 100
ALL_DAYS = sorted(px.loc[_w, 'trade_date'].unique())


def eval_keys(name, keys, group='其他'):
    """keys: DataFrame(code, d) → 评估指标"""
    m = keys.merge(IDX, on=['code', 'd'], how='inner')
    m = m[(m['d'] >= START) & (m['d'] <= END) & m['n_open'].notna()
          & (~m['unbuy']) & m['oc1'].notna()].drop_duplicates(subset=['code', 'd'])
    if len(m) < 30:
        return None
    r = m['oc1']
    daily = m.groupby('d')['oc1'].mean()
    ex = daily.sub(MKT, axis=0).dropna()
    t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex))) if len(ex) > 2 and ex.std(ddof=1) > 0 else np.nan
    per_yr = {}
    for y in sorted(m['d'].str[:4].unique()):
        sy = m[m['d'].str[:4] == y]
        ey = sy.groupby('d')['oc1'].mean().sub(MKT, axis=0).dropna()
        ty = ey.mean() / (ey.std(ddof=1) / np.sqrt(len(ey))) if len(ey) > 2 and ey.std(ddof=1) > 0 else np.nan
        per_yr[y] = dict(n=len(sy), excess=round(float(ey.mean()), 4), t=round(float(ty), 2),
                         mean=round(float(sy['oc1'].mean()), 4))
    pl = float(r[r > 0].mean() / abs(r[r < 0].mean())) if (r < 0).any() else None
    return dict(
        name=name, group=group, n=int(len(m)),
        per_day=round(len(m) / NDAYS, 2),
        mean=round(float(r.mean()), 4),
        win=round(float((r > 0).mean() * 100), 1),
        excess=round(float(ex.mean()), 4), t=round(float(t), 2),
        pl=round(pl, 2) if pl else None,
        years=per_yr,
        nav=_nav(daily),
    )


def _nav(daily):
    """信号组日度等权收益累乘净值（基准 1.0）；缺失日保持前值"""
    s = daily.reindex(ALL_DAYS).dropna()
    nav, cur = [], 1.0
    for d in ALL_DAYS:
        if d in s.index:
            cur *= (1 + s[d] / 100)
        nav.append(round(cur, 5))
    return nav


LINES = []
# ① 三重滤网（向量化）
trik = px.loc[TRI_FULL & (px['trade_date'] >= START) & (px['trade_date'] <= END), ['code', 'trade_date']].rename(columns={'trade_date': 'd'})
LINES.append(eval_keys('三重滤网（全链）', trik, '待评估'))
trid = px.loc[TRI_DIP & (px['trade_date'] >= START) & (px['trade_date'] <= END), ['code', 'trade_date']].rename(columns={'trade_date': 'd'})
LINES.append(eval_keys('三重滤网（破MA40收回版）', trid, '待评估'))

# ② 现有线上各线（stock_signal 真实信号）
for nm, strat, hz in [('短线融合（现网主引擎）', '短线融合', 'short'),
                      ('缩量回踩（现网独立线）', '缩量回踩', None),
                      ('长线趋势（long）', '长线趋势', None),
                      ('中线综合（mid）', '中线综合', None),
                      ('隔日动量（龙虎榜）', '隔日动量', None)]:
    s = sig[(sig['strategy'] == strat) & (~sig['code'].str.startswith(('30', '68', '8', '9')))]
    if hz:
        s = s[s['horizon'] == hz]
    k = s[['code', 'trade_date']].rename(columns={'trade_date': 'd'})
    LINES.append(eval_keys(nm, k, '现有线上'))

# ③ 反转首日（回填表 ∪ 实时）
fr1 = frh.rename(columns={'scan_date': 'd'})[['code', 'd']]
fr2 = sig[sig['strategy'] == '反转首日'][['code', 'trade_date']].rename(columns={'trade_date': 'd'})
LINES.append(eval_keys('反转首日（观察池线）', pd.concat([fr1, fr2]), '现有线上'))

LINES = [x for x in LINES if x]

# ── LOO ─────────────────────────────────────────────────────────
def mask_keys(mask):
    return px.loc[mask & (px['trade_date'] >= START) & (px['trade_date'] <= END),
                  ['code', 'trade_date']].rename(columns={'trade_date': 'd'})


P_PULL = px['ma40dev'].abs() <= 3
LOO = []
for nm, m in [('全链：趋势 + 回踩MA40 ±3% + 金叉&MA10', TRI_FULL),
              ('去掉第一重（趋势）', P_PULL & CROSS_MA10),
              ('去掉第二重（回踩）', T1 & CROSS_MA10),
              ('去掉第三重（金叉&MA10）', T1 & P_PULL),
              ('仅第一重（趋势）', T1),
              ('仅第二重（回踩MA40 ±3%）', P_PULL),
              ('仅第三重（金叉&MA10）', CROSS_MA10),
              ('第一重 × 第三重（无回踩）', T1 & CROSS_MA10),
              ('第二重 × 第三重（无趋势）', P_PULL & CROSS_MA10)]:
    st = eval_keys(nm, mask_keys(m))
    if st:
        LOO.append(dict(name=nm, n=st['n'], excess=st['excess'], t=st['t'],
                        mean=st['mean'], win=st['win']))

# ── 位置分档 ────────────────────────────────────────────────────
sub = px.loc[_w & T1].copy()
bins = [-100, -5, -2, 0, 2, 5, 10, 100]
labels = ['< -5%', '-5~-2%', '-2~0%', '0~2%', '2~5%', '5~10%', '> 10%']
sub['bucket'] = pd.cut(sub['ma40dev'], bins, labels=labels)
POS = []
for b, s in sub.groupby('bucket', observed=True):
    d = s.groupby('trade_date')['oc1'].mean().sub(MKT, axis=0).dropna()
    t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 2 and d.std(ddof=1) > 0 else np.nan
    POS.append(dict(bucket=str(b), n=int(len(s)), mean=round(float(s['oc1'].mean()), 4),
                    win=round(float((s['oc1'] > 0).mean() * 100), 1),
                    excess=round(float(d.mean()), 4), t=round(float(t), 2)))

# ── 出场路径（三重滤网 / 短线融合 同口径）────────────────────────
simc = {}
for k in range(1, 11):
    simc[f'o{k}'] = G('open').shift(-k)
    simc[f'h{k}'] = G('high').shift(-k)
    simc[f'l{k}'] = G('low').shift(-k)
    simc[f'c{k}'] = G('close').shift(-k)
SIM = pd.DataFrame(simc)


def sim_exit(idx, stop_pct, take_pct, max_hold):
    out = pd.Series(np.nan, index=idx)
    for i in idx:
        ei = px.at[i, 'n_open']
        ri = px.at[i, 'close']
        if not (np.isfinite(ei) and ei > 0 and np.isfinite(ri)):
            continue
        sl, tp = ri * (1 + stop_pct), ri * (1 + take_pct)
        for k in range(1, max_hold + 1):
            hi, lo, cl, op = SIM.at[i, f'h{k}'], SIM.at[i, f'l{k}'], SIM.at[i, f'c{k}'], SIM.at[i, f'o{k}']
            if not np.isfinite(cl):
                break
            if op <= sl:
                out.at[i] = (op / ei - 1) * 100
                break
            if np.isfinite(lo) and lo <= sl:
                out.at[i] = (sl / ei - 1) * 100
                break
            if np.isfinite(hi) and hi >= tp:
                out.at[i] = (tp / ei - 1) * 100
                break
            if k == max_hold:
                out.at[i] = (cl / ei - 1) * 100
    return out.dropna()


def exit_eval(name, idx):
    r1 = sim_exit(idx, -99, 99, 1)
    r2 = sim_exit(idx, -0.05, 0.10, 10)
    o1 = G('open').shift(-1)
    c10 = G('close').shift(-10)
    mk10 = ((c10 / o1 - 1) * 100)[_w]
    mk10 = mk10.groupby(px.loc[_w, 'trade_date']).mean()
    d2 = r2.groupby(px.loc[r2.index, 'trade_date']).mean().sub(mk10, axis=0).dropna()
    t2 = d2.mean() / (d2.std(ddof=1) / np.sqrt(len(d2))) if len(d2) > 2 and d2.std(ddof=1) > 0 else np.nan
    return dict(name=name, n1=len(r1), m1=round(float(r1.mean()), 3), w1=round(float((r1 > 0).mean() * 100), 1),
                n2=len(r2), m2=round(float(r2.mean()), 3), w2=round(float((r2 > 0).mean() * 100), 1),
                pl2=round(float(r2[r2 > 0].mean() / abs(r2[r2 < 0].mean())), 2) if (r2 < 0).any() else None,
                mkt10=round(float(mk10.mean()), 3), ex2=round(float(d2.mean()), 3), t2=round(float(t2), 2))


EXITS = []
tidx = px.index[TRI_FULL & _w]
EXITS.append(exit_eval('三重滤网（全链）', tidx))
sk = sig[(sig['strategy'] == '短线融合')][['code', 'trade_date']].rename(columns={'trade_date': 'd'})
skm = sk.merge(IDX, on=['code', 'd'], how='inner')
skm = skm[(skm['d'] >= START) & (skm['d'] <= END) & skm['n_open'].notna() & (~skm['unbuy']) & skm['oc1'].notna()]
EXITS.append(exit_eval('短线融合（现网主引擎）', skm['pi']))

# ── 假如整合：挂在现有推荐上的桶内增量 Δ（铁律 6/14）─────────────
INTS = []
for hz, label in [('short', '短线 short'), ('long', '中长线 long')]:
    s = sig[sig['horizon'] == hz][['code', 'trade_date']].rename(columns={'trade_date': 'd'})
    m = s.merge(IDX, on=['code', 'd'], how='inner')
    m = m[(m['d'] >= START) & (m['d'] <= END) & m['oc1'].notna()
          & m['n_open'].notna() & (~m['unbuy'])].drop_duplicates(subset=['code', 'd'])
    if len(m) < 50:
        continue
    for col, nm in [('tri_full', '三重滤网全链'), ('tri_dip', '破MA40收回+金叉')]:
        h, ms = m[m[col]], m[~m[col]]
        if len(h) < 25:
            continue
        INTS.append(dict(hz=label, cond=nm, base_n=int(len(m)),
                         base_mean=round(float(m['oc1'].mean()), 3),
                         base_win=round(float((m['oc1'] > 0).mean() * 100), 1),
                         hit_n=int(len(h)),
                         hit_mean=round(float(h['oc1'].mean()), 3),
                         hit_win=round(float((h['oc1'] > 0).mean() * 100), 1),
                         miss_mean=round(float(ms['oc1'].mean()), 3),
                         miss_win=round(float((ms['oc1'] > 0).mean() * 100), 1),
                         d_ret=round(float(h['oc1'].mean() - ms['oc1'].mean()), 3),
                         d_win=round(float((h['oc1'] > 0).mean() * 100 - (ms['oc1'] > 0).mean() * 100), 1)))

DATA = dict(lines=LINES, loo=LOO, pos=POS, exits=EXITS, ints=INTS,
            base=dict(mean=round(float(BASE_MEAN), 4), win=round(float(BASE_WIN), 1),
                      days=len(ALL_DAYS), nav=_nav(MKT), alldays=list(ALL_DAYS)))
with open('reports/_triple_cmp_data.json', 'w', encoding='utf-8') as f:
    json.dump(DATA, f, ensure_ascii=False, indent=1)

print(f"对比完成：{len(LINES)} 条线 / LOO {len(LOO)} 行 / 位置 {len(POS)} 档 / 出场 {len(EXITS)} 行")
print(f"\n{'策略':<26}{'n':>8}{'日均':>7}{'均值OC1':>9}{'胜率':>7}{'超额pp':>9}{'t':>7}")
print(f"{'【全市场基线】':<26}{'-':>8}{'-':>7}{BASE_MEAN:>8.3f}%{BASE_WIN:>6.1f}%{0.0:>9.3f}{'-':>7}")
for l in sorted(LINES, key=lambda x: -x['excess']):
    print(f"{l['name']:<26}{l['n']:>8}{l['per_day']:>7.2f}{l['mean']:>8.3f}%{l['win']:>6.1f}%{l['excess']:>9.3f}{l['t']:>7.2f}")
print("\nLOO:")
for l in LOO:
    print(f"  {l['name']:<34}{l['n']:>8}{l['mean']:>8.3f}%{l['win']:>6.1f}%{l['excess']:>9.3f}{l['t']:>7.2f}")
print("\n位置分档:")
for p in POS:
    print(f"  {p['bucket']:<10}{p['n']:>8}{p['mean']:>8.3f}%{p['win']:>6.1f}%{p['excess']:>9.3f}{p['t']:>7.2f}")
print("\n出场:")
for e in EXITS:
    print(f"  {e['name']:<22} 裸持有 {e['m1']:+.3f}%/{e['w1']:.1f}%  |  2:1 {e['m2']:+.3f}%/{e['w2']:.1f}%"
          f" 盈亏比{e['pl2']} 市场10日{e['mkt10']:+.2f}% 超额{e['ex2']:+.3f}(t={e['t2']})")
print("\n假如整合（挂现有推荐的桶内增量）：")
for x in INTS:
    print(f"  {x['hz']:<14}{x['cond']:<14} 基准 {x['base_mean']:+.3f}%/{x['base_win']:.1f}%"
          f" | 命中 {x['hit_n']:<6} {x['hit_mean']:+.3f}%/{x['hit_win']:.1f}%"
          f" | 未命中 {x['miss_mean']:+.3f}%/{x['miss_win']:.1f}%"
          f" | Δ收益 {x['d_ret']:+.3f}pp Δ胜率 {x['d_win']:+.1f}pp")
