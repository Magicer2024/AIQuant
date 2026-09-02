# -*- coding: utf-8 -*-
"""backtest_short_stress.py —— 对 backtest_short_live_1y.py 结论的反向压力测试（只读）

目标：独立验证寇豆码 5 项挑战中的可实证项：
  C1 portfolio() 顺序不稳定（@5% 关 T1 后 PF 升但组合降的悖论）
  C2 死区是时间窗/regime 过拟合（H1 5-8% 反而最佳；5-8% 仅在 cold 市差）
  C3 扩展度非单调（>=12% 是正的 U 形）
  C4 与 top-3 名额 + 坏月混杂
  C5（in-sample 过拟合）属方法论，直接承认，不在本脚本复跑。

口径：与 backtest_short_live_1y.py 完全一致的 evaluate_pick / select_day / portfolio，
仅新增分层与稳定性分析。纯 stdlib，不改库。
"""
import sqlite3
import sys
import random
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

def regime_from_score(score):
    if score >= 60: return "hot"
    if score >= 55: return "warm"
    if score >= 45: return "neutral"
    if score >= 40: return "cool"
    return "cold"

def compute_regime_series(conn, start_date, end_date=None):
    end_filter = " AND trade_date <= ?" if end_date else ""
    params = (start_date, end_date) if end_date else (start_date,)
    rows = conn.execute(f"""
        SELECT trade_date, SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up,
               COUNT(*) AS total FROM daily_price
        WHERE trade_date >= date(?, '-12 days'){end_filter} GROUP BY trade_date ORDER BY trade_date
    """, params).fetchall()
    up_ratio = {r["trade_date"]: ((r["up"] or 0)/r["total"] if r["total"] else None) for r in rows}
    wrows = conn.execute(f"""
        SELECT d.trade_date, AVG(CASE WHEN d.close > d.ma20 THEN 1.0 ELSE 0.0 END) AS width
        FROM (SELECT code, trade_date, close,
              AVG(close) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20
              FROM daily_price WHERE trade_date >= date(?, '-30 days'){end_filter}) d
        GROUP BY d.trade_date ORDER BY d.trade_date
    """, params).fetchall()
    width = {r["trade_date"]: float(r["width"] or 0) for r in wrows}
    slopes = []
    for icode in ("000300", "000905"):
        try:
            irows = conn.execute(f"""
                SELECT trade_date, close FROM index_daily WHERE code = ?
                AND trade_date >= date(?, '-20 days'){end_filter} ORDER BY trade_date
            """, (icode, *params)).fetchall()
        except Exception:
            irows = []
        closes = {r["trade_date"]: float(r["close"]) for r in irows if r["close"]}
        ds = sorted(closes)
        if len(ds) >= 6:
            s = {ds[i]: closes[ds[i]]/closes[ds[i-5]] - 1 for i in range(5, len(ds)) if closes[ds[i-5]]}
            slopes.append(s)
    idx_slope = {}
    if slopes:
        alld = set()
        for s in slopes: alld |= set(s)
        for d in alld:
            vals = [s[d] for s in slopes if d in s]
            if vals: idx_slope[d] = sum(vals)/len(vals)
    def roll5(d):
        dates = sorted(d); out = {}
        for i, dt in enumerate(dates):
            win = [d[x] for x in dates[max(0, i-4):i+1] if d[x] is not None]
            out[dt] = sum(win)/len(win) if win else 0.5
        return out
    ur_s = roll5(up_ratio)
    wd_s = roll5({k: (v if v is not None else 0.5) for k, v in width.items()})
    out = {}
    for d in sorted(set(ur_s) | set(wd_s) | set(idx_slope)):
        if d < start_date or (end_date and d > end_date): continue
        ur = ur_s.get(d); wd = wd_s.get(d)
        if ur is None or wd is None: continue
        sl = idx_slope.get(d)
        score = 50.0 + (ur-0.5)*100*0.4 + (wd-0.5)*100*0.4 + (sl*300 if sl is not None else 0.0)
        out[d] = regime_from_score(score)
    return out

DB = "core/quant.db"
START = "2025-08-25"
END = "2026-08-25"
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

GATE = float(get_param("short_conf_gate"))
LAUNCH_RATIO = get_param("short_take_profit")
TRAIL_PCT = get_param("short_trailing_pct")
STOP_RATIO = get_param("short_stop_loss")
MAX_HOLD = 10
MKT_GATE = float(get_param("short_down_market_gate"))
DEV_MAX = float(get_param("short_dev_ma5_max"))
SHORT_TOP_N = max(1, int(get_param("short_top_n")))
EXT_SORT_DESC = int(get_param("short_ext_sort_desc"))

regime = compute_regime_series(conn, START, END)
active_days = sorted(d for d, r in regime.items() if r in ("cold", "cool"))
REGIME_OF = regime
mkt_avg = {}
if active_days and MKT_GATE < 99:
    ph = ",".join("?" * len(active_days))
    for r in conn.execute(f"SELECT trade_date, AVG(pct_change) m FROM daily_price "
        f"WHERE trade_date IN ({ph}) AND pct_change IS NOT NULL GROUP BY trade_date", active_days):
        mkt_avg[r["trade_date"]] = r["m"] or 0.0

board_sql = ""
if MAIN_BOARD_ONLY:
    board_sql = " AND " + " AND ".join(f"s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
rows = conn.execute(f"""
    SELECT s.code, s.scan_date, s.strategy, s.buy_price, s.stop_loss, s.take_profit,
           s.fusion_score, s.pct_above_ma20 AS ext, s.name
    FROM stock_signal s
    WHERE s.scan_date >= ? AND s.scan_date <= ?
      AND COALESCE(s.horizon,'short') = 'short'
      AND s.buy_price IS NOT NULL AND s.buy_price > 0
      AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
      AND COALESCE(s.strategy,'') != '强势突破' {board_sql}
""", (START, END)).fetchall()

distinct_codes = sorted({r["code"] for r in rows})
lo = (date.fromisoformat(START) - timedelta(days=20)).isoformat()
price_cache = {}
for code in distinct_codes:
    pr = conn.execute("SELECT trade_date, open, close, high, low FROM daily_price "
        "WHERE code=? AND trade_date>=? ORDER BY trade_date", (code, lo)).fetchall()
    price_cache[code] = {p["trade_date"]: (p["open"], p["close"], p["high"], p["low"]) for p in pr}

def series_after(code, scan_date, n=15):
    pc = price_cache.get(code)
    if not pc: return []
    dates = sorted(d for d in pc if d > scan_date)
    return [(d, pc[d][0], pc[d][1], pc[d][2], pc[d][3]) for d in dates[:n]]

dev5_cache = {}
if active_days and DEV_MAX < 99:
    need_codes = sorted({r["code"] for r in rows if r["scan_date"] in active_days
                         and r["strategy"] not in ("隔日动量", "缩量回踩")})
    for code in need_codes:
        pc = price_cache.get(code)
        if not pc: continue
        sd = sorted(pc); closes = [pc[d][1] for d in sd]; m5 = {}
        for i, d in enumerate(sd):
            if i >= 4: m5[d] = sum(closes[i-4:i+1])/5.0
        dev5_cache[code] = m5

def evaluate_pick(r):
    code, scan = r["code"], r["scan_date"]
    entry_close = r["buy_price"]
    prices = series_after(code, scan, 15)
    if not prices: return None
    first = prices[0]
    exec_entry = float(first[1]) if first[1] else (float(first[2]) or entry_close)
    if not exec_entry or exec_entry <= 0: return None
    stop = r["stop_loss"]
    if (stop is None or stop <= 0): stop = exec_entry * (1 + STOP_RATIO)
    launch = None
    if LAUNCH_RATIO and 0 < LAUNCH_RATIO < 1.0: launch = exec_entry * (1 + LAUNCH_RATIO)
    elif r["take_profit"] and r["take_profit"] > 0: launch = r["take_profit"]
    oc = (prices[0][2] - prices[0][1]) / prices[0][1] * 100 if prices[0][1] else None
    cc = (prices[0][2] - entry_close) / entry_close * 100 if entry_close else None
    highest = None; tline = None; launched = False
    trail_ok = TRAIL_PCT is not None and 0.01 <= TRAIL_PCT < 1.0
    exit_ret = None; exit_reason = None; exit_date = None
    for j, p in enumerate(prices[:MAX_HOLD], start=1):
        close = p[2]
        if not close or close <= 0: break
        high = p[3] or close
        if highest is None or high > highest: highest = high
        if not launched and launch and highest >= launch: launched = True
        if launched and trail_ok:
            line = highest * (1 - TRAIL_PCT)
            if tline is None or line > tline: tline = line
        if j == 1: continue
        if stop and close <= stop:
            exit_reason = "stop_loss"; exit_date = p[0]; exit_ret = (close - exec_entry)/exec_entry*100; break
        if launched and tline is not None and close <= tline:
            exit_reason = "trailing_stop"; exit_date = p[0]; exit_ret = (close - exec_entry)/exec_entry*100; break
        if j == MAX_HOLD:
            exit_reason = "max_hold_days"; exit_date = p[0]; exit_ret = (close - exec_entry)/exec_entry*100
    if exit_ret is None:
        if prices:
            c = prices[-1][2]; exit_ret = (c - exec_entry)/exec_entry*100
            exit_reason = "data_short"; exit_date = prices[-1][0]
    return {"code": code, "scan_date": scan, "strategy": r["strategy"], "ext": r["ext"] or 0.0,
            "fusion": r["fusion_score"] or 0.0, "exec_entry": exec_entry, "entry_close": entry_close,
            "oc": oc, "cc": cc, "exit_return": exit_ret, "exit_reason": exit_reason,
            "exit_date": exit_date, "regime": REGIME_OF.get(scan, "unknown")}

def t1_blocked(r):
    d = r["scan_date"]
    if d not in active_days: return False
    if r["strategy"] in ("隔日动量", "缩量回踩"): return False
    if MKT_GATE < 99 and mkt_avg.get(d, 0) >= MKT_GATE: return True
    if DEV_MAX < 99:
        m5 = dev5_cache.get(r["code"], {}).get(d)
        if m5 is None: return True
        if (r["buy_price"]/m5 - 1)*100 > DEV_MAX: return True
    return False

def gap_blocked(evp):
    if not evp: return False
    gap = (evp["exec_entry"] - evp["entry_close"])/evp["entry_close"]*100
    return gap > LAUNCH_RATIO*100 + 0.5

evaluated = {}
for r in rows:
    evp = evaluate_pick(r)
    if evp is None: continue
    evaluated[(r["code"], r["scan_date"])] = (r, evp)
per_day = defaultdict(list)
for (code, scan), (r, evp) in evaluated.items():
    per_day[scan].append((r, evp))

def order_key(r, evp):
    if r["strategy"] == "隔日动量": pri = 0
    elif r["strategy"] == "缩量回踩": pri = 1
    else: pri = 2
    if EXT_SORT_DESC: return (pri, -evp["ext"], -evp["fusion"])
    return (pri, evp["ext"], -evp["fusion"])

all_td = sorted(d for d in REGIME_OF)
EVAL_CUTOFF = all_td[-13] if len(all_td) >= 13 else all_td[0]

def select_day(scan, use_t1=True, use_gap=True, use_gate=True, top=SHORT_TOP_N, ext_cap=None):
    cands = per_day.get(scan, [])
    picked = []
    for r, evp in cands:
        if use_gate and (r["fusion_score"] or 0) < GATE: continue
        if use_t1 and t1_blocked(r): continue
        if use_gap and gap_blocked(evp): continue
        if ext_cap is not None and evp["ext"] > ext_cap: continue
        picked.append((r, evp))
    picked.sort(key=lambda x: order_key(x[0], x[1]))
    return picked[:top]

def stats(evp_list, label, silent=False):
    n = len(evp_list)
    if n == 0:
        if not silent: print(f"  [{label}] n=0")
        return None
    ex = [e["exit_return"] for e in evp_list if e["exit_return"] is not None]
    def wr(xs): return sum(1 for x in xs if x > 0)/len(xs)*100 if xs else 0
    def mean(xs): return sum(xs)/len(xs) if xs else 0
    pos = sum(x for x in ex if x > 0); neg = -sum(x for x in ex if x < 0)
    pf = (pos/neg) if neg > 0 else float("inf")
    if not silent:
        print(f"  [{label}] n={n}  持仓均值 {mean(ex):+6.3f}%  胜率 {wr(ex):5.1f}%  PF={pf:.3f}")
    return dict(n=n, mean=mean(ex), wr=wr(ex), pf=pf)

def portfolio(evp_list, slots=SHORT_TOP_N):
    all_dates = sorted({d for d in REGIME_OF})
    by_day = defaultdict(list)
    for e in evp_list: by_day[e["scan_date"]].append(e)
    occ = [None]*slots; equity = 1.0; curve = []
    fills = 0; offered = len(evp_list)
    for d in all_dates:
        for i in range(slots):
            if occ[i] and occ[i]["exit_date"] < d:
                equity += (1.0/slots)*occ[i]["ret"]/100.0; occ[i] = None
        for e in by_day.get(d, []):
            for i in range(slots):
                if occ[i] is None:
                    occ[i] = {"exit_date": e["exit_date"], "ret": e["exit_return"]}; fills += 1; break
        curve.append((d, equity))
    ret = equity - 1.0
    peak = 1.0; mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = min(mdd, eq/peak - 1.0)
    return ret*100, mdd*100, fills, offered

# ───────────────── 候选池级扩展度 U 形（C3） ─────────────────
print("=" * 70)
print("【C3】候选池级扩展度分桶（全部 stock_signal 候选，含被否决行）")
print("=" * 70)
def ext_bucket(x):
    if x < 0.02: return "<2%"
    if x < 0.05: return "2-5%"
    if x < 0.08: return "5-8%"
    if x < 0.12: return "8-12%"
    return ">=12%"
all_evp = [evp for (_, evp) in evaluated.values()]
by_ext = defaultdict(list)
for e in all_evp: by_ext[ext_bucket(e["ext"])].append(e)
for b in ("<2%", "2-5%", "5-8%", "8-12%", ">=12%"):
    if by_ext.get(b): stats(by_ext[b], f"ext={b}")

# ───────────────── H1/H2 与 regime 拆分（C2） ─────────────────
print("\n" + "=" * 70)
print("【C2】死区是否时间窗/regime 过拟合")
print("=" * 70)
H1 = "2026-02-25"
for b in ("5-8%", "8-12%"):
    sub = by_ext.get(b, [])
    if not sub: continue
    h1 = [e for e in sub if e["scan_date"] <= H1]
    h2 = [e for e in sub if e["scan_date"] > H1]
    print(f"-- ext={b} --")
    stats(h1, f"  H1(≤{H1})")
    stats(h2, f"  H2(>{H1})")
print("-- ext=5-8% 按 regime --")
sub58 = by_ext.get("5-8%", [])
for reg in ("cold", "cool", "neutral", "warm", "hot"):
    rsub = [e for e in sub58 if e["regime"] == reg]
    if rsub: stats(rsub, f"  regime={reg}")

# ───────────────── 月份集中度（C4） ─────────────────
print("\n" + "=" * 70)
print("【C4】5-12% 死区是否集中在坏月 + 与 top-3 名额混杂")
print("=" * 70)
selected = []
for scan in sorted(per_day):
    if scan > EVAL_CUTOFF: continue
    for r, evp in select_day(scan): selected.append(evp)
by_m = defaultdict(list)
for e in selected: by_m[e["scan_date"][:7]].append(e)
print("  月份 | 选中数 | 其中 5-12% 数 | 5-12% 占比 | 该月 PF | 该月均值")
for m in sorted(by_m):
    sub = by_m[m]
    dead = [e for e in sub if 0.05 <= e["ext"] < 0.12]
    s = stats(sub, f"month={m}", silent=True)
    dpf = stats(dead, "", silent=True)
    dpct = (len(dead)/len(sub)*100) if sub else 0
    dpf_s = f"{dpf['pf']:.2f}" if dpf else "-"
    print(f"  {m} | {len(sub):3d} | {len(dead):3d} | {dpct:5.1f}% | PF {s['pf']:.2f} | 均值 {s['mean']:+.2f}%")

# ───────────────── C1 portfolio() 顺序不稳定 ─────────────────
print("\n" + "=" * 70)
print("【C1】portfolio() 是否顺序不稳定（铁证复现 + 随机扰动）")
print("=" * 70)
# (a) 复现用户证据：@5% 开/关 T1
sel_at5 = []
for scan in sorted(per_day):
    if scan > EVAL_CUTOFF: continue
    for r, evp in select_day(scan, use_t1=True, ext_cap=0.05): sel_at5.append(evp)
sel_at5_not1 = []
for scan in sorted(per_day):
    if scan > EVAL_CUTOFF: continue
    for r, evp in select_day(scan, use_t1=False, ext_cap=0.05): sel_at5_not1.append(evp)
s_a = stats(sel_at5, "@5% (T1开)")
ra, ma, fa, oa = portfolio(sel_at5)
s_b = stats(sel_at5_not1, "@5% (T1关)")
rb, mb, fb, ob = portfolio(sel_at5_not1)
print(f"  → PF: @5%(T1开)={s_a['pf']:.3f}  @5%(T1关)={s_b['pf']:.3f}  （PF 升）")
print(f"  → 组合: @5%(T1开)={ra:+.2f}%  @5%(T1关)={rb:+.2f}%  （组合却降 {rb-ra:+.2f}pt）")
print(f"  → 填充/候选: T1开 {fa}/{oa}   T1关 {fb}/{ob}  （关T1后更多候选挤占3槽）")

# (b) 随机打乱填充优先级，看组合摆动
def portfolio_with_order(evp_list, order, slots=SHORT_TOP_N):
    evp_sorted = sorted(evp_list, key=order)
    return portfolio(evp_sorted, slots)
random.seed(42)
rets = []
for t in range(40):
    rnd = random.Random(t)
    o = portfolio_with_order(sel_at5, lambda e: rnd.random())
    rets.append(o[0])
print(f"  → @5% 集随机填充优先级 40 次组合收益: min {min(rets):+.2f}%  "
      f"max {max(rets):+.2f}%  mean {sum(rets)/len(rets):+.2f}%  "
      f"极差 {max(rets)-min(rets):.2f}pt")
# 上下界：最佳优先 / 最差优先
best = portfolio_with_order(sel_at5, lambda e: -e["exit_return"])[0]
worst = portfolio_with_order(sel_at5, lambda e: e["exit_return"])[0]
print(f"  → 同集同3槽，最佳优先上限 {best:+.2f}% / 最差优先下限 {worst:+.2f}%  "
      f"（仅因调度不同，组合可差 {best-worst:.2f}pt）")
print(f"  → 真实 per-trade 边缘（不受调度影响）: PF={s_a['pf']:.3f} 均值 {s_a['mean']:+.3f}%")

print("\n完成。", flush=True)
