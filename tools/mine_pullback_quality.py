# -*- coding: utf-8 -*-
"""mine_pullback_quality.py —— 「缩量回踩低吸」假设只读挖掘（Phase 1 go/no-go）

2026-08-22 用户交付外部方案，核心假设：
  H1 「top-N + 过滤链误杀」：现网每日仅取 Top3，全候选池可能更优
  H2 「缩量回踩质量」：量缩至 5日均量 60% 内 + 回踩 MA10 企稳的票，
     短线期望优于现网抄底链（动量/抄底分天然偏好已涨多的票）

口径纪律（2026-08-22 desc 被推翻的教训）：全部用真实复盘口径——
entry=次日开盘、止损=信号日收盘×(1+short_stop_loss)、启动线 entry×(1+8%)、
回撤 3% 移动止盈、T+1、持满 10 天收盘了结（与 _evaluate_short 同源）。
窗口：train ≤2025-12-31 / test 2026+ / w6m 2026-02-20+。
只读脚本，不改库；go/no-go 结论写回会话。
"""
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

from config.strategy_params import get_param

DB = r"core/quant.db"
W6M = "2026-02-20"
TRAIN_END = "2025-12-31"
START = "2024-01-01"
BOARD_EX = ("300", "301", "688", "689")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

STOP = float(get_param("short_stop_loss"))
LAUNCH = float(get_param("short_take_profit"))
TRAIL = float(get_param("short_trailing_pct"))
HOLD = 10
GATE = float(get_param("short_conf_gate"))

print("加载 daily_price ...", flush=True)
px = conn.execute(
    "SELECT code, trade_date, open, high, low, close, volume "
    "FROM daily_price ORDER BY code, trade_date").fetchall()
data = defaultdict(list)
for r in px:
    if r["code"].startswith(BOARD_EX):
        continue
    data[r["code"]].append(dict(r))
print(f"  主板股票数: {len(data)}", flush=True)

idx_cache = {}


def get_idx(code, d):
    imap = idx_cache.get(code)
    if imap is None:
        imap = {r["trade_date"]: i for i, r in enumerate(data[code])}
        idx_cache[code] = imap
    return imap.get(d)


def simulate_exit(code, idx, sig_close):
    """真实复盘口径方案A模拟，返回 (ret%, 出场原因) 或 None（数据不足）。"""
    rows = data[code]
    if idx + HOLD >= len(rows):
        return None
    r1 = rows[idx + 1]
    entry = r1["open"] or r1["close"]
    if not entry or entry <= 0:
        return None
    stop = sig_close * (1 + STOP)          # 信号自带止损口径：信号日收盘定死
    launch = entry * (1 + LAUNCH)
    highest = tline = None
    launched = False
    trail_ok = TRAIL is not None and 0.01 <= TRAIL < 1.0
    for j in range(1, HOLD + 1):
        p = rows[idx + j]
        close = p["close"]
        if not close or close <= 0:
            return None
        high = p["high"] or close
        if highest is None or high > highest:
            highest = high
        if not launched and highest >= launch:
            launched = True
        if launched and trail_ok:
            line = highest * (1 - TRAIL)
            if tline is None or line > tline:
                tline = line
        if j == 1:                          # T+1 买入日不可卖
            continue
        if close <= stop:
            return (close / entry - 1) * 100, "stop"
        if launched and tline is not None and close <= tline:
            return (close / entry - 1) * 100, "trail"
        if j == HOLD:
            return (close / entry - 1) * 100, "expire"
    return None


def t1oc(code, idx):
    """T+1 OC（次日开盘买→当日收盘卖），快速全池筛选用。"""
    rows = data[code]
    if idx + 1 >= len(rows):
        return None
    o = rows[idx + 1]["open"]
    c = rows[idx + 1]["close"]
    if not o or o <= 0 or not c:
        return None
    return (c / o - 1) * 100


class Buckets:
    def __init__(self):
        self.d = {w: [0, 0.0, 0] for w in ("train", "test", "w6m")}

    def add(self, date, ret):
        wks = ["train" if date <= TRAIN_END else "test"]
        if date >= W6M:
            wks.append("w6m")
        for w in wks:
            s = self.d[w]
            s[0] += 1
            s[1] += ret
            s[2] += 1 if ret > 0 else 0

    def report(self, label):
        print(f"[{label}]", flush=True)
        for w in ("train", "test", "w6m"):
            n, tot, wins = self.d[w]
            if n:
                print(f"  {w:<6} n={n:<6} 均值={tot/n:+.3f}% 胜率={wins/n*100:.1f}%",
                      flush=True)
            else:
                print(f"  {w:<6} n=0", flush=True)


# ─────────────────────────────────────────────
# Part A · H1「top-N 误杀」：现网候选池（fusion>=22）全池 vs Top3
# ─────────────────────────────────────────────
print("\n===== Part A · H1 top-N 误杀检验（fusion>=22 池，方案A出口）=====", flush=True)
sigs = conn.execute("""
    SELECT s.code, s.scan_date, s.strategy, s.fusion_score,
           s.pct_above_ma20 AS ext, s.price
    FROM stock_signal s
    WHERE s.scan_date >= ? AND COALESCE(s.horizon,'short')='short'
      AND s.buy_price > 0 AND s.fusion_score >= ?
      AND COALESCE(s.strategy,'') != '强势突破'
      AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
      AND s.code NOT LIKE '300%' AND s.code NOT LIKE '301%'
      AND s.code NOT LIKE '688%' AND s.code NOT LIKE '689%'
""", (START, GATE)).fetchall()
print(f"候选行(stock_signal fusion>={GATE:.0f}): {len(sigs)}", flush=True)

pool_t1 = Buckets()
pool_a = Buckets()
top3_a = Buckets()
per_day = defaultdict(list)
for s in sigs:
    code, d = s["code"], s["scan_date"]
    if code not in data:
        continue
    idx = get_idx(code, d)
    if idx is None or idx + 1 >= len(data[code]):
        continue
    v = t1oc(code, idx)
    if v is not None:
        pool_t1.add(d, v)
    per_day[d].append((code, idx, s["price"], s["ext"] or 0,
                       s["fusion_score"] or 0, s["strategy"]))

# 全池方案A（抽样：每日期前 30 条按 fusion 序，避免 13万行全模拟过慢）
for d, cands in per_day.items():
    cands_sorted = sorted(cands, key=lambda c: -c[4])
    for code, idx, price, ext, fs, strat in cands_sorted[:30]:
        out = simulate_exit(code, idx, price)
        if out:
            pool_a.add(d, out[0])
    # Top3：现网口径（隔日动量优先，其余 ext 升序）
    top = sorted(cands, key=lambda c: (0 if c[5] == "隔日动量" else 1, c[3], -c[4]))[:3]
    for code, idx, price, ext, fs, strat in top:
        out = simulate_exit(code, idx, price)
        if out:
            top3_a.add(d, out[0])

pool_t1.report("全池 T+1 OC（全量）")
pool_a.report("全池 方案A（每日 fusion 前30 抽样）")
top3_a.report("Top3 现网口径 方案A（隔日动量优先+ext升序）")

# ─────────────────────────────────────────────
# Part B · H2「缩量回踩质量」：全市场主板挖掘
# ─────────────────────────────────────────────
print("\n===== Part B · H2 缩量回踩质量挖掘（全市场主板，方案A出口）=====", flush=True)
variants = {
    "V0 回踩企稳": {},
    "V1 +量缩60%": {"shrink": 0.6},
    "V2 +趋势闸门": {"shrink": 0.6, "gate": True},
    "V3 +前期涨幅": {"shrink": 0.6, "gate": True, "rally": True},
}
buckets = {k: Buckets() for k in variants}
t1b = {k: Buckets() for k in variants}
cand_count = {k: 0 for k in variants}

n_done = 0
for code, rows in data.items():
    n_done += 1
    if len(rows) < 60:
        continue
    closes = pd.Series([r["close"] for r in rows], dtype=float)
    vols = pd.Series([r["volume"] for r in rows], dtype=float)
    ma10 = closes.rolling(10).mean().to_numpy()
    ma20 = closes.rolling(20).mean().to_numpy()
    vol5 = vols.rolling(5).mean().to_numpy()
    hi20 = closes.rolling(20).max().to_numpy()
    cl = closes.to_numpy()
    dates = [r["trade_date"] for r in rows]
    for i in range(30, len(rows) - HOLD - 1):
        d = dates[i]
        if d < START:
            continue
        c, lo = rows[i]["close"], rows[i]["low"]
        v = rows[i]["volume"]
        if not c or c <= 0 or not lo or not v:
            continue
        m10, m20, v5 = ma10[i], ma20[i], vol5[i]
        if m10 != m10 or m20 != m20 or v5 != v5 or v5 <= 0:  # NaN check
            continue
        # 回踩 MA10 企稳：当日触及 MA10±1% 且收盘站稳 MA10 上方（含贴线 +1% 内）
        if not (lo <= m10 * 1.01 and c >= m10 * 0.99):
            continue
        shrink_ok = v <= v5 * 0.6
        gate_ok = m20 > ma20[i - 5] and c > m20              # MA20 向上且站上
        rally_ok = (cl[i - 20:i].max() / cl[i - 20] - 1) >= 0.10 if cl[i - 20] > 0 else False
        for name, cond in variants.items():
            if "shrink" in cond and not shrink_ok:
                continue
            if cond.get("gate") and not gate_ok:
                continue
            if cond.get("rally") and not rally_ok:
                continue
            cand_count[name] += 1
            tv = t1oc(code, i)
            if tv is not None:
                t1b[name].add(d, tv)
            out = simulate_exit(code, i, c)
            if out:
                buckets[name].add(d, out[0])

for name in variants:
    print(f"\n[{name}] 候选数={cand_count[name]} "
          f"(日均≈{cand_count[name]/660:.1f})", flush=True)
    t1b[name].report(f"{name} T+1 OC")
    buckets[name].report(f"{name} 方案A全周期")

# ─────────────────────────────────────────────
# Part C · 决定性检验：V3 条件在现有 fusion>=22 池内部是否有效
#   有效 → 链内叠加过滤/加分（不换引擎）；几乎无命中 → 需独立信号线（需授权）
# ─────────────────────────────────────────────
print("\n===== Part C · V3 条件在 fusion 池内部分桶（方案A出口）=====", flush=True)
hit, miss = Buckets(), Buckets()
hit_n = 0
ind_cache = {}
for d, cands in per_day.items():
    for code, idx, price, ext, fs, strat in cands:
        rows = data[code]
        if idx < 30:
            continue
        ind = ind_cache.get(code)
        if ind is None:
            closes = pd.Series([r["close"] for r in rows], dtype=float)
            vols = pd.Series([r["volume"] for r in rows], dtype=float)
            ind = (closes.to_numpy(),
                   closes.rolling(10).mean().to_numpy(),
                   closes.rolling(20).mean().to_numpy(),
                   vols.rolling(5).mean().to_numpy())
            ind_cache[code] = ind
        cl, ma10a, ma20a, vol5a = ind
        m10, m20, v5 = ma10a[idx], ma20a[idx], vol5a[idx]
        c, lo, v = rows[idx]["close"], rows[idx]["low"], rows[idx]["volume"]
        if m10 != m10 or m20 != m20 or v5 != v5 or v5 <= 0:
            continue
        v3 = (lo <= m10 * 1.01 and c >= m10 * 0.99          # 回踩企稳
              and v <= v5 * 0.6                              # 量缩
              and m20 > ma20a[idx - 5] and c > m20           # 趋势闸门
              and cl[idx - 20] > 0
              and (cl[idx - 20:idx].max() / cl[idx - 20] - 1) >= 0.10)
        out = simulate_exit(code, idx, price)
        if not out:
            continue
        if v3:
            hit_n += 1
            hit.add(d, out[0])
        else:
            miss.add(d, out[0])
print(f"fusion 池内 V3 命中: {hit_n} 条（日均≈{hit_n/660:.2f}）", flush=True)
hit.report("fusion池 × V3命中")
miss.report("fusion池 × V3未命中")
