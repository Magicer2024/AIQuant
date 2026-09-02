# -*- coding: utf-8 -*-
"""tools/_eval_short_long.py —— 短线策略长周期检验 v2（只读，含改动前后对照 + 宽松闸门 + 隔日动量可执行出口）

窗口 2024-07-01~2026-08-10（index_daily=2024-01 / 龙虎榜=2024-07 限制下最长干净窗口）。

对照：旧基线 / 只去缩量回踩 / 去缩量回踩+激进闸门(现状) / 去缩量回踩+宽松闸门(拟改)
隔日动量：通用出场 vs T+2 收盘了结（A股 T+1 下最早可执行卖点）。
只读，不改库。用法：python tools/_eval_short_long.py
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

DB = "core/quant.db"
START = "2024-07-01"
END = "2026-08-26"

from core.market_regime import compute_regime_series
from core.outcome_tracker import _short_weak_dates as _weak_agg

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

GATE = float(get_param("short_conf_gate"))
LAUNCH_RATIO = get_param("short_take_profit")
TRAIL_PCT = get_param("short_trailing_pct")
STOP_RATIO = get_param("short_stop_loss")
MAX_HOLD = int(get_param("short_max_hold_days") or 10)
MKT_GATE = float(get_param("short_down_market_gate"))
DEV_MAX = float(get_param("short_dev_ma5_max"))
SHORT_TOP_N = max(1, int(get_param("short_top_n")))
EXT_SORT_DESC = int(get_param("short_ext_sort_desc"))

print(f"[参数] gate={GATE} launch={LAUNCH_RATIO} trail={TRAIL_PCT} stop={STOP_RATIO} "
      f"max_hold={MAX_HOLD} top_n={SHORT_TOP_N}", flush=True)

import pandas as pd
regime = compute_regime_series(conn, START, END)
active_days = sorted(d for d, r in regime.items() if r in ("cold", "cool"))
REGIME_OF = regime

def _weak_relaxed(conn, start, end):
    """宽松走弱判定：仅 cold regime 或 指数显著破位(>2%低于MA20) 或 5日动量显著转负(< -1.5%)。"""
    series = compute_regime_series(conn, start, end)
    weak = {d for d, r in series.items() if r == "cold"}
    end_filter = " AND trade_date <= ?" if end else ""
    for icode in ("000001", "000300"):
        rows = conn.execute(f"""
            SELECT trade_date, close FROM index_daily
            WHERE code=? AND trade_date >= date(?,'-40 days'){end_filter}
            ORDER BY trade_date
        """, (icode, start, end) if end else (icode, start)).fetchall()
        if not rows:
            continue
        df = pd.DataFrame([{"d": r["trade_date"], "c": float(r["close"])} for r in rows]).set_index("d")
        c = df["c"]; ma20 = c.rolling(20).mean(); mom5 = c / c.shift(5) - 1
        for d in sorted(series):
            if d not in c.index: continue
            cl = c.get(d); m = ma20.get(d); mo = mom5.get(d)
            if cl is None: continue
            if (m is not None and not pd.isna(m) and cl < m * 0.98) or \
               (mo is not None and not pd.isna(mo) and mo < -0.015):
                weak.add(d)
    return weak

# 旧版「激进」走弱：cold/cool 或 任一指数略破 MA20 或 5 日动量<0（复刻 2026-08-26 改动前）
def _weak_agg_old(conn, start, end):
    series = compute_regime_series(conn, start, end)
    weak = {d for d, r in series.items() if r in ("cold", "cool")}
    end_filter = " AND trade_date <= ?" if end else ""
    for icode in ("000001", "000300"):
        rows = conn.execute(f"""
            SELECT trade_date, close FROM index_daily
            WHERE code=? AND trade_date >= date(?,'-40 days'){end_filter} ORDER BY trade_date
        """, (icode, start, end) if end else (icode, start)).fetchall()
        if not rows: continue
        df = pd.DataFrame([{"d": r["trade_date"], "c": float(r["close"])} for r in rows]).set_index("d")
        c = df["c"]; ma20 = c.rolling(20).mean(); mom5 = c / c.shift(5) - 1
        for d in sorted(series):
            if d not in c.index: continue
            cl = c.get(d); m = ma20.get(d); mo = mom5.get(d)
            if cl is None: continue
            if (m is not None and not pd.isna(m) and cl < m) or \
               (mo is not None and not pd.isna(mo) and mo < 0):
                weak.add(d)
    return weak

WEAK_OLD = _weak_agg_old(conn, START, END)          # 改动前（激进）
WEAK_RELAXED = _weak_agg(conn, START, END)          # 生产现网（宽松）
print(f"[弱市日] 激进(改动前)={len(WEAK_OLD)}  宽松(现生产)={len(WEAK_RELAXED)}  / 总 {len(regime)}", flush=True)

mkt_avg = {}
if active_days and MKT_GATE < 99:
    ph = ",".join("?" * len(active_days))
    for r in conn.execute(
        f"SELECT trade_date, AVG(pct_change) m FROM daily_price "
        f"WHERE trade_date IN ({ph}) AND pct_change IS NOT NULL GROUP BY trade_date", active_days):
        mkt_avg[r["trade_date"]] = r["m"] or 0.0

board_sql = ""
if MAIN_BOARD_ONLY:
    board_sql = " AND " + " AND ".join(
        f"s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)

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
print(f"[候选] 基础候选行 = {len(rows)}；按策略 = "
      + str(dict((k, sum(1 for r in rows if r['strategy']==k)) for k in set(r['strategy'] for r in rows))),
      flush=True)

distinct_codes = sorted({r["code"] for r in rows})
lo = (date.fromisoformat(START) - timedelta(days=25)).isoformat()
price_cache = {}
for code in distinct_codes:
    pr = conn.execute("SELECT trade_date, open, close, high, low FROM daily_price "
                      "WHERE code=? AND trade_date>=? ORDER BY trade_date", (code, lo)).fetchall()
    price_cache[code] = {p["trade_date"]: (p["open"], p["close"], p["high"], p["low"]) for p in pr}

def series_after(code, scan_date, n=15):
    pc = price_cache.get(code)
    if not pc: return []
    dates = sorted(d for d in pc if d > scan_date)
    return [(d, *pc[d]) for d in dates[:n]]

dev5_cache = {}
if active_days and DEV_MAX < 99:
    need = {r["code"] for r in rows if r["scan_date"] in active_days
            and r["strategy"] not in ("隔日动量", "缩量回踩")}
    for code in need:
        pc = price_cache.get(code)
        if not pc: continue
        sd = sorted(pc); closes = [pc[d][1] for d in sd]
        dev5_cache[code] = {d: sum(closes[i-4:i+1]) / 5.0 for i, d in enumerate(sd) if i >= 4}

def evaluate_pick(r):
    code, scan = r["code"], r["scan_date"]
    entry_close = r["buy_price"]
    prices = series_after(code, scan, 15)
    if not prices: return None
    first = prices[0]
    exec_entry = float(first[1]) if first[1] else (float(first[2]) or entry_close)
    if not exec_entry or exec_entry <= 0: return None
    stop = r["stop_loss"]
    if stop is None or stop <= 0: stop = exec_entry * (1 + STOP_RATIO)
    launch = None
    if LAUNCH_RATIO and 0 < LAUNCH_RATIO < 1.0: launch = exec_entry * (1 + LAUNCH_RATIO)
    elif r["take_profit"] and r["take_profit"] > 0: launch = r["take_profit"]
    oc = (prices[0][2] - prices[0][1]) / prices[0][1] * 100 if prices[0][1] else None
    cc = (prices[0][2] - entry_close) / entry_close * 100 if entry_close else None
    # 可执行最快出口：买 T+1 开盘，最少持到 T+2（A股 T+1 限定），T+2 收盘了结
    t2close = None
    if len(prices) >= 2 and prices[0][1]:
        t2close = (prices[1][2] - prices[0][1]) / prices[0][1] * 100
    # 通用出场（移动止盈/止损/持满 MAX_HOLD）
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
            exit_reason = "stop_loss"; exit_date = p[0]; exit_ret = (close-exec_entry)/exec_entry*100; break
        if launched and tline is not None and close <= tline:
            exit_reason = "trailing_stop"; exit_date = p[0]; exit_ret = (close-exec_entry)/exec_entry*100; break
        if j == MAX_HOLD:
            exit_reason = "max_hold_days"; exit_date = p[0]; exit_ret = (close-exec_entry)/exec_entry*100
    if exit_ret is None and prices:
        c = prices[-1][2]; exit_ret = (c-exec_entry)/exec_entry*100; exit_reason = "data_short"; exit_date = prices[-1][0]
    return {"code": code, "scan_date": scan, "strategy": r["strategy"],
            "ext": r["ext"] or 0.0, "fusion": r["fusion_score"] or 0.0,
            "exec_entry": exec_entry, "entry_close": entry_close,
            "oc": oc, "cc": cc, "exit_return": exit_ret, "exit_reason": exit_reason,
            "exit_date": exit_date, "t2close": t2close, "regime": REGIME_OF.get(scan, "unknown")}

def t1_blocked(r):
    d = r["scan_date"]
    if d not in active_days: return False
    if r["strategy"] in ("隔日动量", "缩量回踩"): return False
    if MKT_GATE < 99 and mkt_avg.get(d, 0) >= MKT_GATE: return True
    if DEV_MAX < 99:
        m5 = dev5_cache.get(r["code"], {}).get(d)
        if m5 is None: return True
        if (r["buy_price"] / m5 - 1) * 100 > DEV_MAX: return True
    return False

def gap_blocked(evp):
    if evp is None: return False
    gap = (evp["exec_entry"] - evp["entry_close"]) / evp["entry_close"] * 100
    return gap > LAUNCH_RATIO * 100 + 0.5

evaluated = {}
for r in rows:
    evp = evaluate_pick(r)
    if evp is None: continue
    evaluated[(r["code"], r["scan_date"])] = (r, evp)
per_day = defaultdict(list)
for (code, scan), (r, evp) in evaluated.items():
    per_day[scan].append((r, evp))
print(f"[候选] 可评估信号日数 = {len(per_day)}", flush=True)

def order_key(r, evp):
    if r["strategy"] == "隔日动量": pri = 0
    elif r["strategy"] == "缩量回踩": pri = 1
    else: pri = 2
    return (pri, -evp["ext"] if EXT_SORT_DESC else evp["ext"], -evp["fusion"])

all_td = sorted(REGIME_OF)
EVAL_CUTOFF = all_td[-13] if len(all_td) >= 13 else all_td[0]
print(f"[截止] 评估窗口 {START} ~ {EVAL_CUTOFF}", flush=True)

def select_day(scan, no_pullback, gate_mode):
    cands = per_day.get(scan, [])
    picked = []
    for r, evp in cands:
        if no_pullback and r["strategy"] == "缩量回踩": continue
        if (r["fusion_score"] or 0) < GATE: continue
        if t1_blocked(r): continue
        if gap_blocked(evp): continue
        if gate_mode == "old" and scan in WEAK_OLD and r["strategy"] != "隔日动量": continue
        if gate_mode == "relaxed" and scan in WEAK_RELAXED and r["strategy"] != "隔日动量": continue
        picked.append((r, evp))
    picked.sort(key=lambda x: order_key(x[0], x[1]))
    return picked[:SHORT_TOP_N]

def run_variant(no_pullback, gate_mode):
    sel = []
    for scan in sorted(per_day):
        if scan > EVAL_CUTOFF: continue
        for r, evp in select_day(scan, no_pullback, gate_mode):
            sel.append(evp)
    return sel

def stats(evp_list, label):
    n = len(evp_list)
    if n == 0: print(f"  [{label}] n=0"); return
    oc = [e["oc"] for e in evp_list if e["oc"] is not None]
    ex = [e["exit_return"] for e in evp_list if e["exit_return"] is not None]
    t2 = [e["t2close"] for e in evp_list if e["t2close"] is not None]
    wr = lambda xs: (sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else 0)
    mean = lambda xs: (sum(xs) / len(xs) if xs else 0)
    pos = sum(x for x in ex if x > 0); neg = -sum(x for x in ex if x < 0)
    pf = (pos / neg) if neg > 0 else float("inf")
    er = defaultdict(int)
    for e in evp_list: er[e["exit_reason"]] += 1
    print(f"  [{label}] n={n} | T+1OC 胜率 {wr(oc):5.1f}% 均值 {mean(oc):+6.3f}% | "
          f"出场 胜率 {wr(ex):5.1f}% 均值 {mean(ex):+6.3f}% PF={pf:.3f} | "
          f"可执行T+2收 胜率 {wr(t2):5.1f}% 均值 {mean(t2):+6.3f}% | "
          + " ".join(f"{k}={v}" for k, v in sorted(er.items())))

def portfolio(evp_list, slots=SHORT_TOP_N):
    by_day = defaultdict(list)
    for e in evp_list: by_day[e["scan_date"]].append(e)
    occ = [None] * slots; equity = 1.0; curve = []
    for d in sorted(REGIME_OF):
        for i in range(slots):
            if occ[i] and occ[i]["exit_date"] < d:
                equity += (1.0 / slots) * occ[i]["ret"] / 100.0; occ[i] = None
        for e in by_day.get(d, []):
            for i in range(slots):
                if occ[i] is None:
                    occ[i] = {"exit_date": e["exit_date"], "ret": e["exit_return"]}; break
        curve.append((d, equity))
    ret = (equity - 1.0) * 100; peak = 1.0; mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq); mdd = min(mdd, eq / peak - 1.0)
    return ret, mdd * 100

variants = {
    "旧基线(缩量回踩, 无闸门)":        (False, "none"),
    "去缩量回踩(无闸门)":              (True,  "none"),
    "去缩量回踩+激进闸门(改动前)":     (True,  "old"),
    "去缩量回踩+宽松闸门(现生产)":     (True,  "relaxed"),
}
print("\n" + "=" * 84)
print(f"【长周期对照】{START} ~ {EVAL_CUTOFF}（等仓 {SHORT_TOP_N} 槽位，无交易成本）")
print("=" * 84)
res = {}
for name, (np_, gm) in variants.items():
    sel = run_variant(np_, gm); res[name] = sel
    print(f"\n  ▸ {name}  (n={len(sel)})")
    stats(sel, "全量")
    pr, pm = portfolio(sel)
    print(f"      组合收益 {pr:+6.2f}%  最大回撤 {pm:6.2f}%")

print("\n" + "=" * 84)
print("【隔日动量 出口方式对比】（各口径 n 相同）")
print("=" * 84)
for name, sel in res.items():
    mm = [e for e in sel if e["strategy"] == "隔日动量"]
    stats(mm, f"{name} (隔日动量)")

print("\n" + "=" * 84)
print("【宽松闸门口径 分层】")
print("=" * 84)
sel_r = res["去缩量回踩+宽松闸门(现生产)"]
by_st = defaultdict(list)
for e in sel_r: by_st[e["strategy"]].append(e)
for st in sorted(by_st): stats(by_st[st], f"strategy={st}")
by_reg = defaultdict(list)
for e in sel_r: by_reg[e["regime"]].append(e)
for reg in ("cold", "cool", "neutral", "warm", "hot", "unknown"):
    if by_reg.get(reg): stats(by_reg[reg], f"regime={reg}")
print("\n完成。", flush=True)
