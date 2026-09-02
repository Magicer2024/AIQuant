# -*- coding: utf-8 -*-
"""backtest_short_live_1y.py —— 短线推荐策略「近一年」真实复盘口径回测（只读）

目标：用当前线上配置（SHORT_ENGINE=pure_bottom_v2 + 写库前过滤链 +
读时选股 + _evaluate_short 出场）对 stock_signal 历史信号做逐日 point-in-time 重放，
回答用户两个问题：① 胜率多少；② 近一年收益多少；③ 哪里需要调整。

口径对齐生产（保证可复现、与生产入库同口径）：
  - 候选源：stock_signal（horizon='short'），写库前过滤（趋势闸门/追高否决/
    扩展度否决/RSI甜区/质量过滤）已反映在存储信号里（最近一次全量重算 post-2026-08-22，
    含 PULLBACK_DIP）。
  - 读时选股（与 core/outcome_tracker.insert_new_outcomes 同口径）：
      fusion_score >= short_conf_gate(22)
      + T1 辅助过滤（恐慌日闸门 + MA5偏离，按信号日 regime 自动切换，
        隔日动量/缩量回踩豁免）
      + 排序 short_order_clause（隔日动量>缩量回踩>抄底，抄底按扩展度升序）
      + 取每日前 short_top_n(3)
      + 主板过滤（MAIN_BOARD_ONLY）+ ST/退 剔除 + 排除「强势突破」
      + 跳空守卫（T1_GAP_GUARD）：point-in-time 用次日开盘相对信号价涨幅 > 止盈启动线(+8%)
        则视为错过买点剔除（生产用 latest_price 快照，对远期历史有时代错位，此处改 point-in-time
        更诚实；差异仅在信号日强跳空票，量级小）
  - 出场（与 core/outcome_tracker._evaluate_short 完全一致）：
      entry_exec = 推荐日后首个交易日开盘（T+1 实盘可执行）
      launch = entry_exec*(1+short_take_profit=0.08) 或 推荐自带止盈价
      stop  = 推荐自带止损价 或 entry_exec*(1+short_stop_loss=-0.05)
      trailing: 持仓最高价达 launch 后启用，回撤线=最高价*(1-short_trailing_pct=0.03) 只上移
      收盘跌破 stop→stop_loss；launched 后收盘跌破回撤线→trailing_stop；持满 max_hold(10)→了结
  - T+N 收益（展示用）以信号日收盘(entry_price=buy_price)为基准

输出：
  1) 主指标：近一年 候选数 / T+1 OC 胜率&均值 / T+1 CC 胜率&均值 / 持仓出场 胜率&均值&PF / 盈亏比
  2) 近一年 3 槽位组合收益（等权，3 槽对应 short_top_n=3，冷市自然空仓）+ 最大回撤
  3) 分层：按 regime / 策略 / 扩展度桶 / 月份
  4) 读时过滤器消融（隔离 T1 闸门、跳空守卫贡献）
只读，不改库。
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

# 纯 stdlib 复刻 core.market_regime.compute_regime_series（避免 pandas 依赖，
# composite 公式/rolling5/阈值与生产完全一致，保证 T1 闸门 regime 同口径）
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
        SELECT trade_date,
               SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up,
               COUNT(*) AS total
        FROM daily_price
        WHERE trade_date >= date(?, '-12 days'){end_filter}
        GROUP BY trade_date ORDER BY trade_date
    """, params).fetchall()
    up_ratio = {r["trade_date"]: ((r["up"] or 0)/r["total"] if r["total"] else None)
                for r in rows}
    wrows = conn.execute(f"""
        SELECT d.trade_date,
               AVG(CASE WHEN d.close > d.ma20 THEN 1.0 ELSE 0.0 END) AS width
        FROM (
            SELECT code, trade_date, close,
                   AVG(close) OVER (
                       PARTITION BY code ORDER BY trade_date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                   ) AS ma20
            FROM daily_price
            WHERE trade_date >= date(?, '-30 days'){end_filter}
        ) d
        GROUP BY d.trade_date ORDER BY d.trade_date
    """, params).fetchall()
    width = {r["trade_date"]: float(r["width"] or 0) for r in wrows}
    slopes = []
    for icode in ("000300", "000905"):
        try:
            irows = conn.execute(f"""
                SELECT trade_date, close FROM index_daily
                WHERE code = ? AND trade_date >= date(?, '-20 days'){end_filter}
                ORDER BY trade_date
            """, (icode, *params)).fetchall()
        except Exception:
            irows = []
        closes = {r["trade_date"]: float(r["close"]) for r in irows if r["close"]}
        ds = sorted(closes)
        if len(ds) >= 6:
            s = {}
            for i in range(5, len(ds)):
                if closes[ds[i-5]]:
                    s[ds[i]] = closes[ds[i]]/closes[ds[i-5]] - 1
            slopes.append(s)
    idx_slope = {}
    if slopes:
        alld = set()
        for s in slopes:
            alld |= set(s)
        for d in alld:
            vals = [s[d] for s in slopes if d in s]
            if vals:
                idx_slope[d] = sum(vals)/len(vals)
    def roll5(d):
        dates = sorted(d)
        out = {}
        for i, dt in enumerate(dates):
            win = [d[x] for x in dates[max(0, i-4):i+1] if d[x] is not None]
            out[dt] = sum(win)/len(win) if win else 0.5
        return out
    ur_s = roll5(up_ratio)
    wd_s = roll5({k: (v if v is not None else 0.5) for k, v in width.items()})
    out = {}
    for d in sorted(set(ur_s) | set(wd_s) | set(idx_slope)):
        if d < start_date or (end_date and d > end_date):
            continue
        ur = ur_s.get(d); wd = wd_s.get(d)
        if ur is None or wd is None:
            continue
        sl = idx_slope.get(d)
        score = 50.0 + (ur-0.5)*100*0.4 + (wd-0.5)*100*0.4 + (sl*300 if sl is not None else 0.0)
        out[d] = regime_from_score(score)
    return out

DB = "core/quant.db"
START = "2025-08-25"
END = "2026-08-25"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# ── 参数（与生产同口径，支持 DB 覆盖层） ──
GATE = float(get_param("short_conf_gate"))
LAUNCH_RATIO = get_param("short_take_profit")
TRAIL_PCT = get_param("short_trailing_pct")
STOP_RATIO = get_param("short_stop_loss")
MAX_HOLD = 10
MKT_GATE = float(get_param("short_down_market_gate"))
DEV_MAX = float(get_param("short_dev_ma5_max"))
SHORT_TOP_N = max(1, int(get_param("short_top_n")))
EXT_SORT_DESC = int(get_param("short_ext_sort_desc"))
GAP_ENABLED = True  # T1_GAP_GUARD.enabled

print(f"[参数] gate={GATE} launch={LAUNCH_RATIO} trail={TRAIL_PCT} stop={STOP_RATIO} "
      f"max_hold={MAX_HOLD} top_n={SHORT_TOP_N} mkt_gate={MKT_GATE} dev_max={DEV_MAX} "
      f"ext_sort_desc={EXT_SORT_DESC}", flush=True)

# ── regime 序列（信号日窗口） ──
regime = compute_regime_series(conn, START, END)
active_days = sorted(d for d, r in regime.items() if r in ("cold", "cool"))
REGIME_OF = regime
print(f"[regime] 启用 T1 过滤的冰点/偏冷日: {len(active_days)}/{len(regime)}", flush=True)

# 启用日的全市场平均涨跌幅
mkt_avg = {}
if active_days and MKT_GATE < 99:
    ph = ",".join("?" * len(active_days))
    for r in conn.execute(
        f"SELECT trade_date, AVG(pct_change) m FROM daily_price "
        f"WHERE trade_date IN ({ph}) AND pct_change IS NOT NULL GROUP BY trade_date",
        active_days):
        mkt_avg[r["trade_date"]] = r["m"] or 0.0

# ── 基础候选（stock_signal short，窗口内） ──
board_sql = ""
if MAIN_BOARD_ONLY:
    board_sql = " AND " + " AND ".join(
        f"s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)

rows = conn.execute(f"""
    SELECT s.code, s.scan_date, s.strategy, s.buy_price, s.stop_loss,
           s.take_profit, s.fusion_score, s.pct_above_ma20 AS ext, s.name
    FROM stock_signal s
    WHERE s.scan_date >= ? AND s.scan_date <= ?
      AND COALESCE(s.horizon,'short') = 'short'
      AND s.buy_price IS NOT NULL AND s.buy_price > 0
      AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
      AND COALESCE(s.strategy,'') != '强势突破'
      {board_sql}
""", (START, END)).fetchall()
print(f"[候选] 基础候选行: {len(rows)}", flush=True)

# ── 预载价格序列（仅候选出现的 code，START-20 起） ──
distinct_codes = sorted({r["code"] for r in rows})
lo = (date.fromisoformat(START) - timedelta(days=20)).isoformat()
price_cache = {}
for code in distinct_codes:
    pr = conn.execute(
        "SELECT trade_date, open, close, high, low FROM daily_price "
        "WHERE code=? AND trade_date>=? ORDER BY trade_date",
        (code, lo)).fetchall()
    # 索引化：date -> (open,close,high,low)
    price_cache[code] = {p["trade_date"]: (p["open"], p["close"], p["high"], p["low"])
                         for p in pr}

def series_after(code, scan_date, n=15):
    pc = price_cache.get(code)
    if not pc:
        return []
    dates = sorted(d for d in pc if d > scan_date)
    out = []
    for d in dates[:n]:
        o, c, h, l = pc[d]
        out.append((d, o, c, h, l))
    return out

# MA5 偏离（仅启用日、非豁免策略需要）
dev5_cache = {}
if active_days and DEV_MAX < 99:
    need_codes = sorted({r["code"] for r in rows
                         if r["scan_date"] in active_days
                         and r["strategy"] not in ("隔日动量", "缩量回踩")})
    for code in need_codes:
        pc = price_cache.get(code)
        if not pc:
            continue
        sd = sorted(pc)
        closes = [pc[d][1] for d in sd]
        m5 = {}
        for i, d in enumerate(sd):
            if i >= 4:
                m5[d] = sum(closes[i-4:i+1]) / 5.0
        dev5_cache[code] = m5

# ── 逐信号评估（出场复刻 _evaluate_short） ──
def evaluate_pick(r):
    """返回 dict: entry_exec(次日开盘), exit_return, exit_reason, T+1OC, T+1CC,
       entry_close(信号日收盘), exit_date；不通过返回 None。"""
    code, scan = r["code"], r["scan_date"]
    entry_close = r["buy_price"]
    prices = series_after(code, scan, 15)
    if not prices:
        return None
    first = prices[0]
    exec_entry = float(first[1]) if first[1] else (float(first[2]) or entry_close)
    if not exec_entry or exec_entry <= 0:
        return None
    stop = r["stop_loss"]
    if (stop is None or stop <= 0):
        stop = exec_entry * (1 + STOP_RATIO)
    launch = None
    if LAUNCH_RATIO and 0 < LAUNCH_RATIO < 1.0:
        launch = exec_entry * (1 + LAUNCH_RATIO)
    elif r["take_profit"] and r["take_profit"] > 0:
        launch = r["take_profit"]
    # T+1 OC / CC
    oc = (prices[0][2] - prices[0][1]) / prices[0][1] * 100 if prices[0][1] else None
    cc = (prices[0][2] - entry_close) / entry_close * 100 if entry_close else None
    highest = None
    tline = None
    launched = False
    trail_ok = TRAIL_PCT is not None and 0.01 <= TRAIL_PCT < 1.0
    exit_ret = None
    exit_reason = None
    exit_date = None
    for j, p in enumerate(prices[:MAX_HOLD], start=1):
        close = p[2]
        if not close or close <= 0:
            break
        high = p[3] or close
        if highest is None or high > highest:
            highest = high
        if not launched and launch and highest >= launch:
            launched = True
        if launched and trail_ok:
            line = highest * (1 - TRAIL_PCT)
            if tline is None or line > tline:
                tline = line
        if j == 1:
            continue
        if stop and close <= stop:
            exit_reason = "stop_loss"
            exit_date = p[0]
            exit_ret = (close - exec_entry) / exec_entry * 100
            break
        if launched and tline is not None and close <= tline:
            exit_reason = "trailing_stop"
            exit_date = p[0]
            exit_ret = (close - exec_entry) / exec_entry * 100
            break
        if j == MAX_HOLD:
            exit_reason = "max_hold_days"
            exit_date = p[0]
            exit_ret = (close - exec_entry) / exec_entry * 100
    if exit_ret is None:
        # 数据不足持满：用最后可得收盘
        if prices:
            c = prices[-1][2]
            exit_ret = (c - exec_entry) / exec_entry * 100
            exit_reason = "data_short"
            exit_date = prices[-1][0]
    return {
        "code": code, "scan_date": scan, "strategy": r["strategy"],
        "ext": r["ext"] or 0.0, "fusion": r["fusion_score"] or 0.0,
        "exec_entry": exec_entry, "entry_close": entry_close,
        "oc": oc, "cc": cc, "exit_return": exit_ret, "exit_reason": exit_reason,
        "exit_date": exit_date,
        "regime": REGIME_OF.get(scan, "unknown"),
    }

# ── 过滤器（读时） ──
def t1_blocked(r):
    """T1 辅助过滤：冰点/偏冷日且非豁免策略，需满足 市场均涨跌<0 且 MA5偏离<=-2%"""
    d = r["scan_date"]
    if d not in active_days:
        return False
    if r["strategy"] in ("隔日动量", "缩量回踩"):
        return False
    if MKT_GATE < 99 and mkt_avg.get(d, 0) >= MKT_GATE:
        return True
    if DEV_MAX < 99:
        m5 = dev5_cache.get(r["code"], {}).get(d)
        if m5 is None:
            return True  # 缺 MA5 数据，保守拦截
        if (r["buy_price"] / m5 - 1) * 100 > DEV_MAX:
            return True
    return False

def gap_blocked(evp):
    """跳空守卫 point-in-time：次日开盘相对信号价涨幅 > 止盈启动线(+8%) → 错过买点不追"""
    if not GAP_ENABLED or evp is None:
        return False
    gap = (evp["exec_entry"] - evp["entry_close"]) / evp["entry_close"] * 100
    return gap > LAUNCH_RATIO * 100 + 0.5  # 近似 tp/entry 上限(+8%)

# ── 构建每日候选并施加过滤器 ──
# 先评估所有候选（出场），再做选择
evaluated = {}
for r in rows:
    evp = evaluate_pick(r)
    if evp is None:
        continue
    evaluated[(r["code"], r["scan_date"])] = (r, evp)

per_day = defaultdict(list)
for (code, scan), (r, evp) in evaluated.items():
    per_day[scan].append((r, evp))
print(f"[候选] 可评估信号日数: {len(per_day)}", flush=True)

# 排序键（与 short_order_clause 同口径）
def order_key(r, evp):
    if r["strategy"] == "隔日动量":
        pri = 0
    elif r["strategy"] == "缩量回踩":
        pri = 1
    else:
        pri = 2
    if EXT_SORT_DESC:
        return (pri, -evp["ext"], -evp["fusion"])
    return (pri, evp["ext"], -evp["fusion"])

# ── 评估截止（剔除尾部未满 10 交易日的信号，避免 data_short 虚高） ──
all_td = sorted(d for d in REGIME_OF)
EVAL_CUTOFF = all_td[-13] if len(all_td) >= 13 else all_td[0]
print(f"[截止] 评估窗口 {START} ~ {EVAL_CUTOFF}（剔除最后 12 交易日未满仓信号）", flush=True)

# ── 主回测（当前完整配置） ──
def select_day(scan, use_t1=True, use_gap=True, use_gate=True, top=SHORT_TOP_N,
               ext_cap=None):
    cands = per_day.get(scan, [])
    picked = []
    for r, evp in cands:
        if use_gate and (r["fusion_score"] or 0) < GATE:
            continue
        if use_t1 and t1_blocked(r):
            continue
        if use_gap and gap_blocked(evp):
            continue
        if ext_cap is not None and evp["ext"] > ext_cap:
            continue
        picked.append((r, evp))
    picked.sort(key=lambda x: order_key(x[0], x[1]))
    return picked[:top]

selected = []  # 最终推荐集合（每个元素 evp）
for scan in sorted(per_day):
    if scan > EVAL_CUTOFF:
        continue
    for r, evp in select_day(scan):
        selected.append(evp)
print(f"[主回测] 选入推荐: {len(selected)} 条", flush=True)

# ── 指标聚合 ──
def stats(evp_list, label):
    n = len(evp_list)
    if n == 0:
        print(f"  [{label}] n=0"); return
    oc = [e["oc"] for e in evp_list if e["oc"] is not None]
    cc = [e["cc"] for e in evp_list if e["cc"] is not None]
    ex = [e["exit_return"] for e in evp_list if e["exit_return"] is not None]
    def wr(xs):
        return sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else 0
    def mean(xs):
        return sum(xs) / len(xs) if xs else 0
    pos = sum(x for x in ex if x > 0)
    neg = -sum(x for x in ex if x < 0)
    pf = (pos / neg) if neg > 0 else float("inf")
    print(f"  [{label}] n={n}")
    print(f"    T+1 OC : 胜率 {wr(oc):5.1f}%  均值 {mean(oc):+6.3f}%")
    print(f"    T+1 CC : 胜率 {wr(cc):5.1f}%  均值 {mean(cc):+6.3f}%")
    print(f"    持仓出场: 胜率 {wr(ex):5.1f}%  均值 {mean(ex):+6.3f}%  PF={pf:.3f}")
    er = defaultdict(int)
    for e in evp_list:
        er[e["exit_reason"]] += 1
    print(f"    出场原因: " + "  ".join(f"{k}={v}({v/n*100:.0f}%)" for k, v in sorted(er.items())))

print("=" * 64)
print("【主指标】当前完整配置（近一年）")
print("=" * 64)
stats(selected, "全量")

# ── 近一年 3 槽位组合收益 ──
def portfolio(evp_list, slots=SHORT_TOP_N):
    # 槽位：占用从 entry 日(scan_date 次日) 到 exit_date
    # 简化：按 scan_date 顺序，自由槽位 FIFO 填入；每个槽等权 1/slots 资本
    # 权益曲线按交易日推进
    # 收集所有交易日
    all_dates = sorted({d for d in REGIME_OF})
    # 建 (scan_date -> [evp]) 
    by_day = defaultdict(list)
    for e in evp_list:
        by_day[e["scan_date"]].append(e)
    # 槽位状态: list of dict {exit_date, ret}
    occ = [None] * slots
    equity = 1.0
    curve = []
    # 预计算每条 evp 的 entry_date = scan_date 之后首个交易日；exit_date 已知
    for d in all_dates:
        # 1) 释放到期槽位（exit_date <= d 的在前一日已结算，这里用 exit_date < d 视为已释放）
        for i in range(slots):
            if occ[i] and occ[i]["exit_date"] < d:
                equity += (1.0 / slots) * occ[i]["ret"] / 100.0
                occ[i] = None
        # 2) 当日若有新推荐，填入空闲槽位
        for e in by_day.get(d, []):
            # 找空槽
            for i in range(slots):
                if occ[i] is None:
                    occ[i] = {"exit_date": e["exit_date"], "ret": e["exit_return"]}
                    break
        curve.append((d, equity))
    ret = equity - 1.0
    # 最大回撤
    peak = 1.0
    mdd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return ret * 100, mdd * 100

p_ret, p_mdd = portfolio(selected)
print(f"\n  [组合] 近一年 3 槽位等权组合收益: {p_ret:+6.2f}%  最大回撤: {p_mdd:6.2f}%")

# ── 分层分析 ──
print("\n" + "=" * 64)
print("【分层】按 regime")
print("=" * 64)
by_reg = defaultdict(list)
for e in selected:
    by_reg[e["regime"]].append(e)
for reg in ("cold", "cool", "neutral", "warm", "hot", "unknown"):
    if by_reg.get(reg):
        stats(by_reg[reg], f"regime={reg}")

print("\n" + "=" * 64)
print("【分层】按策略")
print("=" * 64)
by_st = defaultdict(list)
for e in selected:
    by_st[e["strategy"]].append(e)
for st in sorted(by_st):
    stats(by_st[st], f"strategy={st}")

print("\n" + "=" * 64)
print("【分层】按扩展度桶 (pct_above_ma20)")
print("=" * 64)
def ext_bucket(x):
    if x < 0.02: return "<2%"
    if x < 0.05: return "2-5%"
    if x < 0.08: return "5-8%"
    if x < 0.12: return "8-12%"
    return ">=12%"
by_ext = defaultdict(list)
for e in selected:
    by_ext[ext_bucket(e["ext"])].append(e)
for b in ("<2%", "2-5%", "5-8%", "8-12%", ">=12%"):
    if by_ext.get(b):
        stats(by_ext[b], f"ext={b}")

print("\n" + "=" * 64)
print("【分层】按月份")
print("=" * 64)
by_m = defaultdict(list)
for e in selected:
    by_m[e["scan_date"][:7]].append(e)
for m in sorted(by_m):
    stats(by_m[m], f"month={m}")

# ── 读时过滤器消融 ──
print("\n" + "=" * 64)
print("【消融】读时过滤器贡献（候选集/选股层面）")
print("=" * 64)
variants = {
    "无读时过滤(仅gate)": dict(use_t1=False, use_gap=False, use_gate=True),
    "完整(当前)":       dict(use_t1=True,  use_gap=True,  use_gate=True),
    "+扩展度否决@8%":   dict(use_t1=True,  use_gap=True,  use_gate=True, ext_cap=0.08),
    "+扩展度否决@5%":   dict(use_t1=True,  use_gap=True,  use_gate=True, ext_cap=0.05),
    "+扩展5% +关闭T1":  dict(use_t1=False, use_gap=True,  use_gate=True, ext_cap=0.05),
    "+扩展5% +关T1+关跳空": dict(use_t1=False, use_gap=False, use_gate=True, ext_cap=0.05),
}
for name, kw in variants.items():
    sel = []
    for scan in sorted(per_day):
        if scan > EVAL_CUTOFF:
            continue
        for r, evp in select_day(scan, **kw):
            sel.append(evp)
    print(f"\n  -- {name} (n={len(sel)}) --")
    stats(sel, name)
    if sel:
        pr, pm = portfolio(sel)
        print(f"    组合收益 {pr:+6.2f}%  最大回撤 {pm:6.2f}%")

print("\n完成。", flush=True)
