"""读取 reports/_triple_cmp_data.json，生成可视化 HTML 对比报告。"""
import json
import sqlite3

D = json.load(open('reports/_triple_cmp_data.json', encoding='utf-8'))
OUT = 'reports/triple_screen_vs_existing.html'

DAYS = D['base']['alldays']

POS_C, NEG_C = '#d64541', '#0f9d58'      # A 股惯例：红涨（正）绿跌（负）
INK, MUTED, LINE = '#1f2733', '#7a8593', '#e4e9f0'
PALETTE = ['#d64541', '#2f6feb', '#f2a104', '#7b5cd6', '#0f9d58', '#0aa0b5', '#c2456e', '#8a94a6']


def fmt(v, unit='', plus=False, dec=3):
    if v is None:
        return '—'
    s = f"{v:+.{dec}f}" if plus else f"{v:.{dec}f}"
    return s + unit


def tone(v):
    if v is None:
        return MUTED
    return POS_C if v > 0 else (NEG_C if v < 0 else MUTED)


def tone_bg(v):
    if v is None:
        return '#f2f5f9'
    return 'rgba(214,69,65,.13)' if v > 0 else 'rgba(15,157,88,.13)'


# ── 主对比表 ─────────────────────────────────────────────────────
lines = sorted(D['lines'], key=lambda x: -x['excess'])
mx = max(abs(l['excess']) for l in lines) or 1
rows = ''
for l in lines:
    is_tri = l['group'] == '待评估'
    w = abs(l['excess']) / mx * 100
    bar = (f'<div class="bar"><span style="width:{w:.1f}%;background:{tone(l["excess"])}"></span></div>')
    verdict = ('✅ 有超额' if l['excess'] > 0.10 and l['t'] and l['t'] > 1.5 else
               '⚠️ 超额≈0' if abs(l['excess']) <= 0.10 else '❌ 负超额')
    tag = '<span class="tag tri">待评估</span>' if is_tri else '<span class="tag on">线上</span>'
    rows += f"""<tr class="{'hl' if is_tri else ''}">
<td class="nm">{l['name']}{tag}</td><td>{l['n']:,}</td><td>{l['per_day']:.2f}</td>
<td>{fmt(l['mean'],'%')}</td><td>{fmt(l['win'],'%',dec=1)}</td>
<td class="ex" style="color:{tone(l['excess'])}">{fmt(l['excess'],plus=True)}</td>
<td class="ex">{bar}</td>
<td>{fmt(l['t'],dec=2)}</td><td class="vd">{verdict}</td></tr>"""

# ── 分年表 ───────────────────────────────────────────────────────
years = sorted({y for l in D['lines'] for y in l['years']})
yr_head = ''.join(f'<th>{y}</th>' for y in years)
yr_rows = ''
for l in lines:
    cells = ''
    for y in years:
        v = l['years'].get(y)
        if not v:
            cells += '<td class="muted">—</td>'
            continue
        cells += (f'<td style="background:{tone_bg(v["excess"])};color:{tone(v["excess"])}">'
                  f'{fmt(v["excess"],plus=True)}<i>n={v["n"]:,}</i></td>')
    yr_rows += f'<tr><td class="nm">{l["name"]}</td>{cells}</tr>'

# ── 净值曲线 ─────────────────────────────────────────────────────
W, H, PL, PR, PT, PB = 1000, 340, 56, 150, 18, 34
n = len(DAYS)
allv = [v for l in D['lines'] for v in l['nav']] + D['base']['nav']
y0, y1 = min(allv), max(allv)
y0, y1 = min(y0, 1.0) - 0.01, max(y1, 1.0) + 0.01


def xy(i, v):
    x = PL + (W - PL - PR) * (i / max(n - 1, 1))
    y = PT + (H - PT - PB) * (1 - (v - y0) / (y1 - y0))
    return x, y


grid = ''
for k in range(6):
    v = y0 + (y1 - y0) * k / 5
    _, yy = xy(0, v)
    grid += (f'<line x1="{PL}" y1="{yy:.1f}" x2="{W-PR}" y2="{yy:.1f}" stroke="{LINE}" stroke-width="1"/>'
             f'<text x="{PL-8}" y="{yy+4:.1f}" class="ax" text-anchor="end">{v:.2f}</text>')
ticks = ''
for i, d in enumerate(DAYS):
    if d[8:10] in ('01',) and d[5:7] in ('01', '07'):
        x, _ = xy(i, y0)
        ticks += f'<text x="{x:.1f}" y="{H-12}" class="ax" text-anchor="middle">{d[:7]}</text>'

paths, legend = '', ''
draw = [dict(name='全市场基线', nav=D['base']['nav'], color='#8a94a6', dash=True)]
for idx, l in enumerate(lines):
    draw.append(dict(name=l['name'], nav=l['nav'], color=PALETTE[idx % len(PALETTE)], dash=False))
for s in draw:
    pts = ' '.join(f'{xy(i,v)[0]:.1f},{xy(i,v)[1]:.1f}' for i, v in enumerate(s['nav']))
    dash_attr = ' stroke-dasharray="5 4"' if s['dash'] else ''
    paths += (f'<polyline points="{pts}" fill="none" stroke="{s["color"]}" stroke-width="2.2"'
              f'{dash_attr} stroke-linejoin="round"/>')
ly = 26 + 20 * draw.index(s)
legend += (f'<rect x="{W-PR+14}" y="{ly-8}" width="10" height="10" rx="2" fill="{s["color"]}"/>'
           f'<text x="{W-PR+30}" y="{ly+1}" class="lg">{s["name"][:14]}</text>')

# ── LOO ──────────────────────────────────────────────────────────
loo = [x for x in D['loo'] if x['name'] != '第二重 × 第三重（无趋势）']
lmx = max(abs(x['excess']) for x in loo) or 1
loo_rows = ''
for x in loo:
    w = abs(x['excess']) / lmx * 100
    loo_rows += (f'<tr><td class="nm">{x["name"]}</td><td>{x["n"]:,}</td><td>{fmt(x["mean"],"%")}</td>'
                 f'<td>{fmt(x["win"],"%",dec=1)}</td>'
                 f'<td style="color:{tone(x["excess"])}">{fmt(x["excess"],plus=True)}</td>'
                 f'<td><div class="bar"><span style="width:{w:.1f}%;background:{tone(x["excess"])}"></span></div></td>'
                 f'<td>{fmt(x["t"],dec=2)}</td></tr>')

# ── 位置分档 ─────────────────────────────────────────────────────
pmx = max(abs(p['excess']) for p in D['pos']) or 1
pos_rows = ''
for p in D['pos']:
    w = abs(p['excess']) / pmx * 100
    flag = ' ← 三重滤网信号区' if p['bucket'] in ('-2~0%', '0~2%') else (' ← 最优' if p['excess'] > 0.09 else '')
    pos_rows += (f'<tr class="{"hl" if flag else ""}"><td class="nm">{p["bucket"]}{flag}</td><td>{p["n"]:,}</td>'
                 f'<td>{fmt(p["mean"],"%")}</td><td>{fmt(p["win"],"%",dec=1)}</td>'
                 f'<td style="color:{tone(p["excess"])}">{fmt(p["excess"],plus=True)}</td>'
                 f'<td><div class="bar"><span style="width:{w:.1f}%;background:{tone(p["excess"])}"></span></div></td>'
                 f'<td>{fmt(p["t"],dec=2)}</td></tr>')

# ── 出场 ─────────────────────────────────────────────────────────
ex_rows = ''
for e in D['exits']:
    ex_rows += (f'<tr><td class="nm">{e["name"]}</td>'
                f'<td>{fmt(e["m1"],"%")}</td><td>{fmt(e["w1"],"%",dec=1)}</td>'
                f'<td>{fmt(e["m2"],"%")}</td><td>{fmt(e["w2"],"%",dec=1)}</td>'
                f'<td>{fmt(e["pl2"],dec=2)}</td><td>{fmt(e["mkt10"],"%",plus=True)}</td>'
                f'<td style="color:{tone(e["ex2"])}">{fmt(e["ex2"],plus=True)}</td>'
                f'<td>{fmt(e["t2"],dec=2)}</td></tr>')

tri = next(l for l in D['lines'] if l['name'] == '三重滤网（全链）')
best = max(D['lines'], key=lambda x: x['excess'])

# ── 假如整合：桶内增量 ────────────────────────────────────────────
int_rows = ''
for x in D['ints']:
    int_rows += (f'<tr><td class="nm">{x["hz"]}</td><td>{x["cond"]}</td>'
                 f'<td>{fmt(x["base_mean"],"%")} / {fmt(x["base_win"],"%",dec=1)}</td>'
                 f'<td>{x["hit_n"]:,}</td>'
                 f'<td>{fmt(x["hit_mean"],"%")} / {fmt(x["hit_win"],"%",dec=1)}</td>'
                 f'<td>{fmt(x["miss_mean"],"%")} / {fmt(x["miss_win"],"%",dec=1)}</td>'
                 f'<td style="color:{tone(x["d_ret"])};font-weight:600">{fmt(x["d_ret"],plus=True)}</td>'
                 f'<td style="color:{tone(x["d_win"])};font-weight:600">{fmt(x["d_win"],plus=True,dec=1)}</td></tr>')

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>三重滤网 vs 现有策略 · 实证对比</title>
<style>
:root{{--pos:{POS_C};--neg:{NEG_C};--ink:{INK};--muted:{MUTED};--line:{LINE};
--bg:#f4f7fb;--card:#fff;--accent:#2f6feb}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1120px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:20px 22px;margin-bottom:20px;box-shadow:0 1px 3px rgba(31,39,51,.04)}}
h2{{font-size:16px;margin:0 0 4px;display:flex;align-items:center;gap:8px}}
h2 .no{{display:inline-flex;width:22px;height:22px;border-radius:6px;background:var(--accent);
color:#fff;font-size:12px;align-items:center;justify-content:center;font-weight:600}}
.note{{color:var(--muted);font-size:12px;margin:2px 0 14px}}
.verdict{{border-left:4px solid var(--neg)}}
.verdict h2{{color:#0b6b3e}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:14px}}
.kpi{{background:#f8fafd;border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.kpi b{{display:block;font-size:21px;letter-spacing:-.02em;margin-bottom:2px}}
.kpi span{{color:var(--muted);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:right;font-weight:600;color:var(--muted);font-size:12px;
padding:8px 8px;border-bottom:2px solid var(--line);white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}
td{{padding:8px 8px;border-bottom:1px solid #f1f4f9;text-align:right;white-space:nowrap}}
td.nm{{font-weight:500}}
td.vd{{color:var(--muted);font-size:12px}}
td.muted{{color:#c3cad4}}
td i{{display:block;font-size:10px;font-style:normal;opacity:.6}}
tr.hl{{background:#fffdf5}}
tr:hover td{{background:#f8fbff}}
.bar{{width:90px;height:7px;background:#eef2f7;border-radius:4px;overflow:hidden;margin-left:auto}}
.bar span{{display:block;height:100%;border-radius:4px}}
.tag{{font-size:10px;padding:1px 6px;border-radius:4px;margin-left:7px;vertical-align:1px}}
.tag.tri{{background:rgba(47,111,235,.12);color:var(--accent)}}
.tag.on{{background:rgba(122,133,147,.14);color:var(--muted)}}
svg{{display:block;width:100%;height:auto}}
.ax{{fill:var(--muted);font-size:10px}}
.lg{{fill:var(--ink);font-size:11px}}
ul{{margin:8px 0 0;padding-left:20px}}li{{margin:5px 0}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:820px){{.kpis,.two{{grid-template-columns:1fr}}}}
.foot{{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}}
</style></head><body><div class="wrap">

<h1>三重滤网 vs 现有策略 · 实证对比</h1>
<div class="sub">窗口 2024-07-01 ~ 2026-09-23（{D['base']['days']} 交易日）·
A 股主板 60/00 · OC1 = T+1 开盘买 → T+1 收盘卖（剔除一字板）·
超额 = 组内日均 − 当日全市场主板可交易子集均值 · 红=正 绿=负</div>

<div class="card verdict">
<h2>结论：不建议整合</h2>
<div class="note">三重滤网未跑赢现网任何一条在跑的策略，其核心机制「等回踩」是负贡献。</div>
<div class="kpis">
<div class="kpi"><b style="color:{tone(tri['excess'])}">{fmt(tri['excess'],plus=True)}pp</b>
<span>三重滤网超额（t={tri['t']}）</span></div>
<div class="kpi"><b style="color:var(--muted)">0.136%</b><span>全市场基线日收益</span></div>
<div class="kpi"><b style="color:var(--pos)">{fmt(best['excess'],plus=True)}pp</b>
<span>最高：{best['name']}</span></div>
<div class="kpi"><b style="color:var(--neg)">1.48</b><span>2:1 出场实际达成盈亏比</span></div>
</div></div>

<div class="card">
<h2><span class="no">1</span>横向对比</h2>
<div class="note">同一窗口、同一口径。带「待评估」标签的是三重滤网；其余为线上真实信号。</div>
<table><thead><tr><th>策略</th><th>信号数</th><th>日均</th><th>均值OC1</th><th>胜率</th>
<th>超额pp</th><th></th><th>t</th><th>判定</th></tr></thead><tbody>{rows}</tbody></table>
</div>

<div class="card">
<h2><span class="no">2</span>分年超额（pp）</h2>
<div class="note">铁律 3：任何策略必须在「分年 + 剔除单年」下都成立。单元格内 n = 该年信号数。</div>
<table><thead><tr><th>策略</th>{yr_head}</tr></thead><tbody>{yr_rows}</tbody></table>
</div>

<div class="card">
<h2><span class="no">3</span>净值曲线（信号组日度等权累乘）</h2>
<div class="note">不是真实组合回测：每日等权买入当日全部信号、持有 1 个交易日。看的是「信号组日收益序列」的长期方向。</div>
<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet">
{grid}{ticks}{paths}{legend}
</svg></div>

<div class="card">
<h2><span class="no">4</span>Leave-one-out：三重条件有叠加效应吗</h2>
<div class="note">去掉任一重，超额只是从「负」变成「≈0」——说明三个条件没有互补性，只是把同一个动量因子切三份。</div>
<table><thead><tr><th>组合</th><th>信号数</th><th>均值OC1</th><th>胜率</th>
<th>超额pp</th><th></th><th>t</th></tr></thead><tbody>{loo_rows}</tbody></table>
</div>

<div class="card">
<h2><span class="no">5</span>位置分档：为什么「等回踩」是负贡献</h2>
<div class="note">趋势前提下（MA40&gt;MA160 且 close&gt;MA160），按「距 MA40 偏离度」分档。单调性成立：越贴近 MA40 越差。</div>
<table><thead><tr><th>距 MA40</th><th>样本</th><th>均值OC1</th><th>胜率</th>
<th>超额pp</th><th></th><th>t</th></tr></thead><tbody>{pos_rows}</tbody></table>
</div>

<div class="card">
<h2><span class="no">6</span>出场路径：2:1 盈亏比的实践达成</h2>
<div class="note">基线 = 信号日收盘价；止损 -5% / 止盈 +10%，最多持有 10 日；开盘破止损按开盘价出（跳空现实）。</div>
<table><thead><tr><th>策略</th><th>裸持有均值</th><th>胜率</th><th>2:1 均值</th><th>胜率</th>
<th>实际盈亏比</th><th>同期市场10日</th><th>超额pp</th><th>t</th></tr></thead><tbody>{ex_rows}</tbody></table>
<div class="note" style="margin-top:12px">
「胜率 &gt; 33% 即正期望」的前提是盈亏比真的维持 2:1。实测两份都只有 1.48 / 1.63——
止损跳空 + 止盈硬顶把赔率压掉约 25%。更关键：同期「随便买一只持有 10 天」有 +1.12%，两条线都是<strong>跑输</strong>的。</div>
</div>

<div class="card">
<h2><span class="no">7</span>假如整合：挂在现有推荐上的桶内增量 Δ</h2>
<div class="note">直接回答「整合进来会怎样」。按「命中 / 未命中」分组比较同一批推荐——判据是 Δ（桶内增量），不是绝对收益。</div>
<table><thead><tr><th>现有线</th><th>加入的条件</th><th>该线整体</th><th>命中数</th>
<th>命中组</th><th>未命中组</th><th>Δ收益pp</th><th>Δ胜率pp</th></tr></thead>
<tbody>{int_rows}</tbody></table>
<div class="note" style="margin-top:12px">
短线上加三重滤网：Δ收益 <strong>-0.081pp</strong>、Δ胜率 -1.0pp；长线上 Δ收益 -0.392pp、Δ胜率 -6.1pp。
<strong>两条线都是负增量</strong>——加进去只会把好的和坏的一起打散，不会提升。</div>
</div>

<div class="card">
<h2><span class="no">8</span>决策要点</h2>
<ul>
<li><strong>数据不支持整合。</strong>三重滤网超额 {fmt(tri['excess'],plus=True)}pp（t={tri['t']}），
未跑赢现网任何一条线；长样本 7 年中 5 年为负；挂到现有推荐上 Δ 收益同样为负
（short −0.081pp / long −0.392pp）。</li>
<li><strong>「避免买在高点」这半个主张成立。</strong>信号日距 MA40 中位仅 +0.37%（现网短线 +2.06%）——
位置确实更低。但在本样本区间，低位置本身不产生超额，反而错过动量。</li>
<li><strong>现网多数线超额也不显著。</strong>短线融合 {fmt(next(l['excess'] for l in lines if l['name'].startswith('短线融合')),plus=True)}pp、
缩量回踩 {fmt(next(l['excess'] for l in lines if l['name'].startswith('缩量回踩')),plus=True)}pp、
长线趋势 {fmt(next(l['excess'] for l in lines if l['name'].startswith('长线趋势')),plus=True)}pp。
唯一为正的是「反转首日」{fmt(best['excess'],plus=True)}pp，但 t={best['t']}（样本 {best['n']} 条）不显著。</li>
<li><strong>这套体系里唯一可用的东西你已经在跑了</strong>：第二重「回踩」在
<code>strategy/pullback_dip.py</code> 里有更完整的实现（MA10/MA20 + 量缩 + 前期涨幅）。</li>
<li><strong>若要保留</strong>：仅建议做「入场位置标签」（只展示不筛选，零风险），
或把「距 MA40 偏离度」单独立项验证为 long 排序键——注意它的方向与「低吸」直觉相反。</li>
</ul>
</div>

<div class="foot">生成于 2026-09-27 · 复现：tools/_diag_triple_screen.py / _2 / _3 · _cmp_triple_vs_existing.py<br>
完整报告：docs/triple-screen-verification.md</div>
</div></body></html>"""

with open(OUT, 'w', encoding='utf-8') as f:
    f.write(HTML)
print(f"✅ 已生成 {OUT}（{len(HTML):,} 字符）")
print(f"   交易日 {len(DAYS)} · 对比 {len(lines)} 条线")
