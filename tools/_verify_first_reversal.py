"""
反转首日线 —— 上线验收 v3（close 权威口径）
A) 模块 vs 向量化 mask 逐笔对账（铁律 18）
B) pct_change 数据健康自检（audit_daily_price）
C) 600641 命中核对（应有且仅有 2026-09-16）
D) Flask test_client 路由验证 + 今日推荐/强势观察回归
D2) /reversal_picks 载荷字段 + 统计口径对账（n/excess/t/win_rate）
F) 前端接线（dashboard.html 面板 id + load/render 函数 + 接口路径）
E) 全文件语法检查 + 变更点确认（已无 build_consistent_ohlc 残留）
"""
import os
import sys
import sqlite3
import random
import pandas as pd
import numpy as np

os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.first_reversal import scan_first_reversal
from strategy.indicators import calc_true_ret, audit_daily_price
from config.strategy_params import FIRST_REVERSAL

con = sqlite3.connect('core/quant.db')
df = pd.read_sql("SELECT code,trade_date,open,high,low,close,volume,amount,turnover,pct_change "
                 "FROM daily_price WHERE trade_date>='2023-06-01' ORDER BY code,trade_date", con)
con.close()
df = df[~df['code'].str.startswith(('688', '689', '8', '9'))].copy()
df = df[(df['close'] > 0) & (df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0)].reset_index(drop=True)

print("=" * 100)
print("B) pct_change 数据健康自检")
h = audit_daily_price(df)
print(f"  n={h['n']:,}  pct_change==0 占比 {h['pct_zero_ratio']:.2%}  warn={h['warn']}")
print("  ✅ 自检正确识别字段缺陷（该列不可用，改用 calc_true_ret）"
      if h['warn'] else "  ❌ 未识别")

g = df.groupby('code', sort=False)
df['ret'] = calc_true_ret(df)
df['cp'] = g['close'].shift(1)
df['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
df['ma20p'] = g['ma20'].shift(1)
df['ama5'] = g['amount'].transform(lambda s: s.shift(1).rolling(5).mean())
df['vr'] = df['amount'] / df['ama5'].replace(0, np.nan)
df['n_open'] = g['open'].shift(-1)
df['n_high'] = g['high'].shift(-1)
df['n_low'] = g['low'].shift(-1)
df['unbuy'] = (df['n_open'] == df['n_high']) & (df['n_open'] == df['n_low'])
df['notlim'] = df['ret'] < np.where(df['code'].str.startswith(('300', '301')), 19.8, 9.8)
win = ((df['trade_date'] >= '2024-07-01') & (df['trade_date'] <= '2026-09-23')
       & df['n_open'].notna() & (~df['unbuy']) & df['ma20'].notna() & df['ret'].notna())
mask = win & (df['close'] > df['ma20']) & (df['cp'] <= df['ma20p']) \
    & (df['ret'] >= 5.0) & (df['vr'] >= 1.5) & df['notlim']
print(f"\nA) 向量化 mask 命中 n={int(mask.sum())}（v5 报告 n=15841）")

random.seed(11)
pos = df.index[mask].tolist()
neg = df.index[win & (~mask)].tolist()
sample = random.sample(pos, min(2500, len(pos))) + random.sample(neg, min(2500, len(neg)))
by_code = {c: s for c, s in df.groupby('code', sort=False)}
mis = []
for i in sample:
    row = df.loc[i]
    sub = by_code[row['code']]
    upto = sub[sub['trade_date'] <= row['trade_date']]
    if len(upto) < 21:
        continue
    d = upto[['open', 'high', 'low', 'close', 'volume', 'amount']].copy()
    d.index = pd.to_datetime(upto['trade_date'])
    sig = scan_first_reversal(d, params=FIRST_REVERSAL, code=row['code'])
    if (sig is not None) != bool(row['_m'] if '_m' in row else mask.loc[i]):
        mis.append((row['code'], row['trade_date'], bool(mask.loc[i]), sig is not None))
print(f"  对账：不一致 {len(mis)} / {len(sample)}")
for m in mis[:10]:
    print("   ", m)
if not mis:
    print("  ✅ 模块判定与回测 mask 完全一致")

print("\n" + "=" * 100)
print("C) 600641 命中核对")
hits = []
s = by_code['600641']
for dt in s['trade_date']:
    if dt < '2026-09-01':
        continue
    upto = s[s['trade_date'] <= dt]
    d = upto[['open', 'high', 'low', 'close', 'volume', 'amount']].copy()
    d.index = pd.to_datetime(upto['trade_date'])
    sig = scan_first_reversal(d, params=FIRST_REVERSAL, code='600641')
    if sig:
        hits.append((dt, sig['ext_pct'], sig['vol_ratio'], sig['kdj_k'], sig['pct_change'],
                     sig['triggers']))
print(f"  命中 {len(hits)} 次：")
for x in hits:
    print(f"    {x[0]}  ext={x[1]:+.2f}%  量比={x[2]}  KDJ_K={x[3]}  涨幅={x[4]:+.2f}%")
    for t in x[5]:
        print(f"       · {t}")

print("\n" + "=" * 100)
print("D) Flask test_client")
from flask import Flask
from routes.investor import investor_bp

app = Flask(__name__)
app.register_blueprint(investor_bp, url_prefix="/api/investor")
client = app.test_client()
for url in ("/api/investor/reversal_picks",
            "/api/investor/reversal_picks?limit=5",
            "/api/investor/today",
            "/api/investor/surge_picks",
            "/api/investor/recommend_outcome"):
    try:
        r = client.get(url)
        b = r.get_json() or {}
        dg = b.get("data") or {}
        ex = f" count={dg.get('count')} enabled={dg.get('enabled')}" if "reversal" in url else ""
        print(f"  GET {url:<48} → HTTP {r.status_code}{ex}")
    except Exception as e:
        print(f"  GET {url:<48} → ERROR {type(e).__name__}: {str(e)[:150]}")

print("\n" + "=" * 100)
print("D2) /reversal_picks 载荷字段 + 统计口径对账")
r = client.get("/api/investor/reversal_picks")
dg = (r.get_json() or {}).get("data") or {}
st = dg.get("stats") or {}
exp = {"n": 15841, "excess_pp": 0.174, "t": 2.80, "win_rate": 49.5}
ok = all(st.get(k) == v for k, v in exp.items())
for k, v in exp.items():
    got = st.get(k)
    print(f"  stats.{k:<10} = {got}  (期望 {v})  {'OK' if got == v else 'XX'}")
print("  keys:", sorted(dg.keys()))
print("  ✅ 统计口径与文档/模块一致" if ok else "  ❌ 统计口径与文档/模块不一致")

print("\n" + "=" * 100)
print("D3) 数据源合并（实时 ∪ 历史回填）+ 单位口径修复  [2026-09-24]")
r = client.get("/api/investor/reversal_picks?limit=40")
dg = (r.get_json() or {}).get("data") or {}
its = dg.get("items") or []
print(f"  HTTP {r.status_code}  date={dg.get('date')}  count={dg.get('count')}  "
      f"source={dg.get('source')}")
# 1) 历史回填行必须可见（此前只查 stock_signal ⇒ 当日链路未产出时面板空白）
assert its, "❌ items 为空：历史回填信号未接入面板"
# 2) source 字段必须存在且自洽
assert dg.get("source") in ("live", "hist", "mixed"), \
    f"❌ 未知 source={dg.get('source')!r}"
# 3) ext_pct 单位：必须是百分数（历史表 ext_pct=5.39；实时表 pct_above_ma20=0.051 → *100）
_exts = [it["ext_pct"] for it in its if it.get("ext_pct") is not None]
assert _exts, "❌ 所有 item 的 ext_pct 均为空"
_max_ext = max(_exts)
assert _max_ext is None or _max_ext > 1.0, \
    f"❌ ext_pct 仍为分数口径（max={_max_ext}，应 > 1 表示百分数）"
print(f"  ext_pct: n={len(_exts)} 范围 [{min(_exts):.2f}, {max(_exts):.2f}] "
      f"→ {'百分数口径 OK' if _max_ext > 1 else '疑似分数口径 XX'}")
# 4) 历史行必须补出 triggers（含 KDJ），否则卡片信息密度低于实时行
_hist = [it for it in its if it.get("source") == "hist"]
_missing_trg = [it["code"] for it in _hist if not it.get("triggers")]
print(f"  历史行 n={len(_hist)}，无 triggers 的 {len(_missing_trg)} 只 "
      f"→ {'OK（已按同口径重建）' if not _missing_trg else 'XX ' + str(_missing_trg[:5])}")
assert not _missing_trg, "❌ 历史行 triggers 未重建"
n_kdj = sum(1 for it in _hist if any("KDJ" in t for t in it.get("triggers", [])))
print(f"  历史行含 KDJ 触发器 n={n_kdj}  → {'OK' if n_kdj == len(_hist) else 'XX'}")
# 5) pct_day 用 close/prev_close（pct_change 有大量未写入的 0，铁律 19）
_pd = [it["pct_day"] for it in its if it.get("pct_day") is not None]
_nz = sum(1 for v in _pd if v != 0)
print(f"  pct_day n={len(_pd)} 非零 {_nz}  "
      f"→ {'OK（未退化为全 0）' if _nz >= len(_pd) * 0.9 else 'XX 疑似全 0'}")
# 6) 样本展示
print("  ── 前 5 条（source / ext_pct / KDJ 触发器）──")
for it in its[:5]:
    kdj = [t for t in it.get("triggers", []) if "KDJ" in t]
    print(f"    {it['code']} {it.get('name')}  src={it.get('source')}  "
          f"ext={it.get('ext_pct')}  pct_day={it.get('pct_day')}  {kdj[0] if kdj else '-'}")
print("  ✅ D3 全部通过")

print("\n" + "=" * 100)
print("D4) 板块口径（跟踪组主板化）+ 推荐线隔离  [2026-09-24]")
from core.outcome_tracker import get_merged_summary
tr = dg.get("tracked") or {}
print(f"  tracked.total={tr.get('total')}  win={tr.get('win_rate')}%  "
      f"avg={tr.get('avg_return')}%  pf={tr.get('profit_factor')}  "
      f"T+1={((tr.get('t1') or {}).get('win_rate'))}%")
assert tr.get("total"), "❌ tracked 为空（反转首日组未写入 recommend_outcome）"
# 跟踪组必须与卡片同口径：MAIN_BOARD_ONLY 下不得含创业板/科创板
_bad = [it["code"] for it in its if it["code"][:2] in ("30", "68")]
print(f"  卡片非主板代码: {len(_bad)} {_bad[:5]} → "
      f"{'✅ 与 MAIN_BOARD_ONLY 一致' if not _bad else '❌ 板块过滤未生效'}")
assert not _bad, "❌ 卡片含创业板/科创板代码"
# 推荐线（默认 strategy=None ⇒ 排除反转首日）不应被 ~485 条观察池污染
rec = get_merged_summary(60, "short")
print(f"  推荐线(60d): total={rec.get('total')} win={rec.get('win_rate')}% "
      f"avg={rec.get('avg_return')}% pf={rec.get('profit_factor')} → "
      f"{'✅ 未被观察池污染（仍是个位数~几十条推荐线）' if rec.get('total', 1e9) < 200 else '❌ 疑似被污染'}")
assert rec.get("total", 1e9) < 200, "❌ 推荐线被反转首日组污染"
print("  ✅ D4 通过")

print("\n" + "=" * 100)
print("D5) /reversal_history 历史表现接口（近 N 天明细 + 成绩单）  [2026-09-24]")
r = client.get("/api/investor/reversal_history?days=60&limit=600")
assert r.status_code == 200, f"❌ HTTP {r.status_code}"
dh = (r.get_json() or {}).get("data") or {}
hi = dh.get("items") or []
hs = dh.get("summary") or {}
print(f"  HTTP 200  days={dh.get('days')}  total={dh.get('total')}  "
      f"range={dh.get('range')}  truncated={dh.get('truncated')}")
assert hi, "❌ items 为空（复盘组未接入历史表现列表）"
# 1) 列表与成绩单必须同源同数（实现上共用同一份明细 ⇒ 不得分叉）
assert len(hi) == hs.get("total"), \
    f"❌ 列表 {len(hi)} != 成绩单 {hs.get('total')}（口径分叉）"
print(f"  列表/成绩单同源：items={len(hi)} summary.total={hs.get('total')} ✅")
# 2) 窗口正确：最早信号日不得早于 days 天前
from datetime import date as _dt, timedelta as _td
_cut = (_dt.today() - _td(days=dh.get("days"))).isoformat()
_ds = [it["scan_date"] for it in hi if it.get("scan_date")]
assert min(_ds) >= _cut, f"❌ 窗口越界：最早 {min(_ds)} < cutoff {_cut}"
# 3) 排序：信号日降序
assert _ds == sorted(_ds, reverse=True), "❌ 未按信号日降序"
print(f"  窗口 {min(_ds)} ~ {max(_ds)}（cutoff {_cut}）· 信号日降序 ✅")
# 4) 板块：与卡片/跟踪组同口径，只能主板
_bad2 = sorted({it["code"] for it in hi if it["code"][:2] in ("30", "68")})
assert not _bad2, f"❌ 含创业板/科创板 {_bad2[:5]}"
print(f"  非主板残留 {len(_bad2)} → ✅ 与 MAIN_BOARD_ONLY 一致")
# 5) 出场原因 → 标签/色调全覆盖，且色调语义 = 本项目红涨绿跌
_TONE = {"positive", "negative", "neutral", "holding"}
_bad_tone = [it["code"] for it in hi if it.get("exit_tone") not in _TONE]
assert not _bad_tone, f"❌ 未知 exit_tone: {_bad_tone[:3]}"
_wrong = [it["code"] for it in hi
          if (it.get("exit_reason") in ("take_profit", "trailing_stop")
              and it["exit_tone"] != "positive")
          or (it.get("exit_reason") == "stop_loss"
              and it["exit_tone"] != "negative")
          or (it.get("exit_reason") is None
              and it["exit_tone"] != "holding")]
assert not _wrong, f"❌ 色调语义错（止盈应 positive/止损应 negative）: {_wrong[:5]}"
print("  出场标签/色调：全覆盖 + 语义正确（止盈=positive 红、止损=negative 绿）✅")
# 6) 出场分布合计必须等于 total（前端以此算「已结算」）
_bd = dh.get("exit_breakdown") or []
assert sum(b["n"] for b in _bd) == dh.get("total"), \
    f"❌ 分布合计 {sum(b['n'] for b in _bd)} != total {dh.get('total')}"
print(f"  出场分布 {[(b['label'], b['n']) for b in _bd]} "
      f"合计={sum(b['n'] for b in _bd)} ✅")
# 7) ext/vol/kdj 填充率（复盘表不存这三列 ⇒ hist ∪ live 补；实时行从 trigger 文本兜底）
for _k in ("ext_pct", "vol_ratio", "kdj_k"):
    _n = sum(1 for it in hi if it.get(_k) is not None)
    print(f"  {_k} 填充率 {_n}/{len(hi)}")
    assert _n >= len(hi) * 0.9, f"❌ {_k} 填充率过低（{_n}/{len(hi)}）"
# 8) 已结算 + 未结算 = total
_settled = sum(1 for it in hi if it.get("settled"))
_unsettled = sum(b["n"] for b in _bd if not b["reason"])
assert _settled + _unsettled == dh.get("total"), "❌ settled/unsettled 与 total 不符"
print(f"  已结算 {_settled} + 未结算 {_unsettled} = {dh.get('total')} ✅")
# 9) days 钳制（下限 5 / 上限 180）
for _q, _exp in (("?days=1", 5), ("?days=9999", 180)):
    _dd = ((client.get("/api/investor/reversal_history" + _q).get_json()
            or {}).get("data") or {})
    _got = _dd.get("days")
    print(f"  days 钳制 {_q:<12} → days={_got} "
          f"{'✅' if _got == _exp else '❌ 期望 ' + str(_exp)}")
    assert _got == _exp, f"❌ {_q} 钳制错误：{_got} != {_exp}"
print("  ✅ D5 全部通过")

print("\n" + "=" * 100)
print("F) 前端接线（dashboard.html）")
html = open("dashboard.html", encoding="utf-8").read()
need = ['id="reversalPicks"', 'id="reversalPicksHint"', "loadReversalPicks()",
        "function loadReversalPicks", "function renderReversalCard",
        "function renderReversalStats", "investor/reversal_picks",
        # 历史表现（2026-09-24）
        "function renderReversalHistoryShell", "function renderReversalHistory",
        "function toggleReversalHistory", "onReversalHistDaysChange",
        "onReversalHistSettledChange", "id=\"revHistBox\"", "id=\"revHistSettled\"",
        "investor/reversal_history", "REVERSAL_HISTORY_DAYS"]
for tok in need:
    print(f"  {'✅' if tok in html else '❌'} {tok}")
print("  ✅ 前端接线齐全" if all(t in html for t in need) else "  ❌ 前端接线不完整")
assert all(t in html for t in need), "❌ 前端接线不完整"

print("\n" + "=" * 100)
print("E) 语法检查 + 残留确认")
import py_compile
for f in ("core/sync.py", "routes/investor.py", "core/outcome_tracker.py",
          "strategy/first_reversal.py", "strategy/indicators.py", "config/strategy_params.py"):
    try:
        py_compile.compile(f, doraise=True)
        print(f"  ✅ {f}")
    except Exception as e:
        print(f"  ❌ {f}: {str(e)[:200]}")
src = open("strategy/indicators.py", encoding="utf-8").read()
print("  build_consistent_ohlc 残留:", "build_consistent_ohlc" in src)
print("  calc_true_ret 存在:", "def calc_true_ret" in src)

# 内联 JS 语法（dashboard.html 是单文件前端、无构建 ⇒ 改完必须过这一步）
import subprocess as _sp
_node = None
for _cand in (r"C:\Users\Magicer\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
              "node"):
    try:
        _sp.run([_cand, "--version"], capture_output=True, check=True)
        _node = _cand
        break
    except Exception:
        continue
if _node:
    _r2 = _sp.run([_node, "tools/_check_dashboard_js.mjs"], capture_output=True, text=True)
    _tail = (_r2.stdout or "").strip().splitlines()[-1:] or [""]
    print(f"  dashboard.html 内联 JS: {'✅' if _r2.returncode == 0 else '❌'} {_tail[0]}")
    # 「历史表现」前端状态机（展开/重绘/过滤/换窗）：语法检查抓不到状态与 DOM 脱节
    _r3 = _sp.run([_node, "tools/_smoke_reversal_history.mjs"], capture_output=True, text=True)
    _t3 = (_r3.stdout or "").strip().splitlines()[-1:] or [""]
    print(f"  历史表现前端状态机: {'✅' if _r3.returncode == 0 else '❌'} {_t3[0]}")
    assert _r3.returncode == 0, "❌ 历史表现前端状态机冒烟测试失败"
else:
    print("  dashboard.html 内联 JS: ⏭ 跳过（未找到 node）")
