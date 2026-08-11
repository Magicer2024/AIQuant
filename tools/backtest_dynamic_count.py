"""
backtest_dynamic_count.py —— 短线推荐数量动态调整回测（8→4，差日子出0）

目的：验证"每天固定8个"改为"每天最多4个、最少0个"对真实组合收益的影响，
并回答能否达到 周2% / 月10% / 年50% 的目标。

口径完全对齐线上（避免回测/推荐分裂）：
  - 引擎：strategy_bottom_fishing_v2（现网 SHORT_ENGINE="pure_bottom_v2"）
  - 候选 = (FUSION>=15) & 质量 & 趋势闸门 & 追高否决 & 扩展度否决 & E3(RSI[40,65])
  - FUSION = BUY_SCORE × (10/3) × 5
  - 入场 = T+1 开盘；出场 = 止损-6% / 止盈+10% / 持仓3日（现网最佳配置）
  - 组合：满仓等权，当前所有未平仓头寸均分资金；按真实止盈/止损日内触发记账

三类场景：
  S0 基线     ：每天取候选 Top8（现状）
  S1 截断     ：每天取候选 Top4（仅砍数量，其余不变）
  S2 截断+置信 ：每天最多 Top4，但仅 FUSION>=GATE 才推荐；弱日自然出 0~少量

附加诊断：
  - 按候选排名分桶（1-4 / 5-8 / 9-12 / 13+）看排序是否有选择力（Top4 是否明显优于后段）
  - 按 cap(4/8/12) 看每日篮子的平均单信号收益（per-cycle 均值）
  - 推导"达到年50%所需的 per-cycle 均值"

窗口：2020-01-01 起（近6年，相关性更高；全历史含早期不流通A股噪声大）
纯读取，不写库。
"""
import sys, os, json, sqlite3
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from strategy.strategies import strategy_bottom_fishing_v2
from strategy.rec_filters import (quality_series, trend_gate_series,
                                  chase_filter_series, extension_filter_series)

DB = os.path.join(PROJECT_ROOT, "core", "quant.db")
SIG = 15.0
MA_N = 20
STOP = 0.06
TAKE = 0.10
MAXHOLD = 3
WINDOW_START = "2020-01-01"
TOPN_BASE = 8
TOPN_PROP = 4
FUSION_GATE = 22.0   # S2 置信度门槛：仅融合分>=此值才推荐（弱日候选稀疏→自然出0~少量）

conn = sqlite3.connect(DB)
df_all = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, amount, pct_change "
    "FROM daily_price WHERE trade_date >= ?", conn, params=(WINDOW_START,))
df_all["trade_date"] = pd.to_datetime(df_all["trade_date"])
groups = {c: g.sort_values("trade_date").reset_index(drop=True)
          for c, g in df_all.groupby("code")}
ts = {r[0]: (r[1] if r[1] and r[1] > 0 else None)
      for r in conn.execute("SELECT code, total_shares FROM stock_info").fetchall()}
conn.close()

def fusion_from_buyscore(bs):
    return bs * (10.0 / 3.0) * 5.0

def rsi14(close):
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0); loss = (-delta).clip(lower=0.0)
    ag = gain.rolling(14).mean(); al = loss.rolling(14).mean()
    rs = ag / al.clip(lower=1e-9)
    return 100.0 - 100.0 / (1.0 + rs)

def simulate_series(g, idx, maxhold, stop, take):
    """返回 (entry_date, exit_date, {date: day_return}) 或 None。
    day_return 用收盘价记账，止盈/止损触发日改用触发价相对前一日收盘的收益。"""
    if idx + maxhold + 1 >= len(g):
        return None
    entry = float(g["open"].iloc[idx + 1])
    if not entry or entry <= 0:
        return None
    entry_date = g["trade_date"].iloc[idx + 1]
    prev_close = entry
    daily = {}
    for off in range(1, maxhold + 1):
        hi = float(g["high"].iloc[idx + off])
        lo = float(g["low"].iloc[idx + off])
        cl = float(g["close"].iloc[idx + off])
        d = g["trade_date"].iloc[idx + off]
        if hi >= entry * (1 + take):
            daily[d] = entry * (1 + take) / prev_close - 1.0
            return entry_date, d, daily
        if lo <= entry * (1 - stop):
            daily[d] = entry * (1 - stop) / prev_close - 1.0
            return entry_date, d, daily
        daily[d] = cl / prev_close - 1.0
        prev_close = cl
    return entry_date, g["trade_date"].iloc[idx + maxhold], daily

# ---- 收集所有候选信号（v2 + 全部过滤器 + E3） ----
# 每条：signal_date, code, fusion, rank(当日降序1-based), realized(序列), entry_date, exit_date, daily
candidates = []   # (signal_date, code, fusion, daily_dict, entry_date, exit_date)
per_day = {}      # signal_date -> list of (fusion, daily_dict, entry_date, exit_date)

n_stocks = 0
for code, g in groups.items():
    g = g.copy(); g.index = g["trade_date"]
    if len(g) < 30:
        continue
    n_stocks += 1
    b2 = strategy_bottom_fishing_v2(g)["BUY_SCORE"]
    f2 = fusion_from_buyscore(b2)
    q = quality_series(code, g, ts.get(code))
    gs = trend_gate_series(g)
    ch = chase_filter_series(g)
    ex = extension_filter_series(g)
    rsi = rsi14(g["close"].astype(float))
    e3 = (rsi >= 40) & (rsi <= 65)
    ma20 = g["close"].astype(float).rolling(MA_N).mean()
    dev_series = (g["close"].astype(float) / ma20 - 1.0)
    mask = (f2 >= SIG) & q & gs & ch & ex & e3
    if not mask.any():
        continue
    sub = f2[mask].sort_values(ascending=False)
    for rank, (sig_date, fusion_val) in enumerate(sub.items(), start=1):
        idx = g.index.get_loc(sig_date)
        res = simulate_series(g, idx, MAXHOLD, STOP, TAKE)
        if res is None:
            continue
        entry_date, exit_date, daily = res
        dev_val = float(dev_series.loc[sig_date]) if sig_date in dev_series.index else 0.0
        rec = (sig_date, code, float(fusion_val), rank, daily, entry_date, exit_date, dev_val)
        candidates.append(rec)
        per_day.setdefault(sig_date, []).append(rec)

print(f"处理股票数 = {n_stocks}, 窗口 {WINDOW_START}+")
print(f"有效候选信号总数 = {len(candidates)}, 信号日数 = {len(per_day)}")

# ============ 诊断1：按候选排名分桶（排序选择力） ============
def bucket_stats(pairs):
    """pairs: list of realized_total (frac)"""
    if not pairs:
        return (0, float("nan"), 0)
    arr = np.array(pairs)
    return (len(arr), float(np.mean(arr) * 100), float((arr > 0).mean() * 100))

buckets = {"1-4": [], "5-8": [], "9-12": [], "13+": []}
ext_buckets = {"<0%(MA20下)": [], "0-4%": [], "4-8%": [], ">8%": []}
for sig_date, recs in per_day.items():
    for (_, _, fusion, rank, daily, _, _, dev) in recs:
        tot = sum(daily.values())
        if rank <= 4:
            buckets["1-4"].append(tot)
        elif rank <= 8:
            buckets["5-8"].append(tot)
        elif rank <= 12:
            buckets["9-12"].append(tot)
        else:
            buckets["13+"].append(tot)
        if dev < 0:
            ext_buckets["<0%(MA20下)"].append(tot)
        elif dev < 0.04:
            ext_buckets["0-4%"].append(tot)
        elif dev < 0.08:
            ext_buckets["4-8%"].append(tot)
        else:
            ext_buckets[">8%"].append(tot)

# ============ 诊断2：每日篮子均值 by cap ============
def basket_mean_by_cap(cap):
    means = []
    for sig_date, recs in per_day.items():
        recs_sorted = sorted(recs, key=lambda r: r[2], reverse=True)
        top = recs_sorted[:cap]
        if not top:
            continue
        rets = [sum(r[4].values()) for r in top]
        means.append(np.mean(rets))
    return float(np.mean(means) * 100) if means else float("nan"), len(means)

# ============ 组合模拟 ============
def sort_key(r, selector):
    if selector == "ext":
        return r[7]          # 低扩展度优先（升序）
    return -r[2]             # 融合分降序（默认）

def simulate_portfolio(cap, gate=None, selector="fusion"):
    """gate: 若设，仅 fusion>=gate 的候选才入池（弱日自然出0~少量）。
    selector: 'fusion'=按融合分降序取前N；'ext'=按扩展度(距MA20)升序取前N。
    返回 equity 时间序列 与 每笔收益列表。"""
    # 构建交易日历
    all_dates = sorted({d for recs in per_day.values() for r in recs for d in r[4].keys()})
    equity = [1.0]
    eq_by_date = [all_dates[0]]
    open_pos = {}
    new_by_entry = {}
    for sig_date, recs in per_day.items():
        recs_sorted = sorted(recs, key=lambda r: sort_key(r, selector))
        taken = 0
        for r in recs_sorted:
            if cap is not None and taken >= cap:
                break
            if gate is not None and r[2] < gate:
                continue
            new_by_entry.setdefault(r[5], []).append(r)
            taken += 1
    for d in all_dates:
        open_pos = {k: v for k, v in open_pos.items() if v[6] >= d}
        for r in new_by_entry.get(d, []):
            open_pos[r[5]] = r
        rs = [r[4][d] for r in open_pos.values() if d in r[4]]
        port_r = float(np.mean(rs)) if rs else 0.0
        equity.append(equity[-1] * (1 + port_r))
        eq_by_date.append(d)
    per_trade = []
    for sig_date, recs in per_day.items():
        recs_sorted = sorted(recs, key=lambda r: sort_key(r, selector))
        taken = 0
        for r in recs_sorted:
            if cap is not None and taken >= cap:
                break
            if gate is not None and r[2] < gate:
                continue
            per_trade.append(sum(r[4].values()))
            taken += 1
    return equity, eq_by_date, per_trade

def portfolio_stats(equity, eq_by_date, per_trade):
    eq = np.array(equity)
    n_days = len(eq) - 1
    years = n_days / 252.0
    total_ret = eq[-1] / eq[0] - 1
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    # 周/月收益分布（按交易日切片：5日=周, 21日=月）
    weekly = []; monthly = []
    arr = eq[1:]  # 每日权益（已含首日1.0）
    for i in range(0, len(arr), 5):
        if i + 5 <= len(arr):
            weekly.append(arr[i + 5 - 1] / arr[i] - 1)
    for i in range(0, len(arr), 21):
        if i + 21 <= len(arr):
            monthly.append(arr[i + 21 - 1] / arr[i] - 1)
    pt = np.array(per_trade)
    return {
        "n_days": n_days, "years": round(years, 2),
        "total_ret": total_ret * 100, "cagr": cagr * 100,
        "per_trade_mean": float(pt.mean()) * 100 if len(pt) else float("nan"),
        "per_trade_win": float((pt > 0).mean()) * 100 if len(pt) else float("nan"),
        "n_trades": len(pt),
        "weekly_mean": float(np.mean(weekly)) * 100 if weekly else float("nan"),
        "weekly_ge2pct": float((np.array(weekly) >= 0.02).mean()) * 100 if weekly else float("nan"),
        "monthly_mean": float(np.mean(monthly)) * 100 if monthly else float("nan"),
        "monthly_ge10pct": float((np.array(monthly) >= 0.10).mean()) * 100 if monthly else float("nan"),
        "max_dd": max_drawdown(eq) * 100,
    }

def max_drawdown(eq):
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    return float(dd.min())

# ============ 运行 ============
print("\n" + "=" * 78)
print("诊断1：候选按排名分桶（排序是否有选择力）")
print("=" * 78)
print(f"  {'桶':<8} {'样本数':>8} {'均值/笔':>9} {'胜率':>8}")
for b, pairs in buckets.items():
    n, m, w = bucket_stats(pairs)
    print(f"  {b:<8} {n:>8} {m:>+8.3f}% {w:>7.1f}%")

print("\n  按扩展度(距MA20偏离)分桶：低扩展度=未来空间大，是否更优？")
print(f"  {'桶':<12} {'样本数':>8} {'均值/笔':>9} {'胜率':>8}")
for b, pairs in ext_buckets.items():
    n, m, w = bucket_stats(pairs)
    print(f"  {b:<12} {n:>8} {m:>+8.3f}% {w:>7.1f}%")

print("\n" + "=" * 78)
print("诊断2：每日篮子平均单信号收益 by cap（per-cycle 均值）")
print("=" * 78)
for cap in (4, 8, 12):
    m, ndays = basket_mean_by_cap(cap)
    print(f"  cap={cap:<3} 每日篮子均值 {m:>+7.3f}%  (信号日数 {ndays})")

print("\n" + "=" * 78)
print("组合模拟：S0基线(Top8) / S1截断(Top4) / S2截断+置信门控(Top4,GATE=%.0f)" % FUSION_GATE)
print("=" * 78)
scenarios = {
    "S0 基线 Top8(融合分)": (TOPN_BASE, None, "fusion"),
    "S1 截断 Top4(融合分)": (TOPN_PROP, None, "fusion"),
    "S2 截断+置信 Top4(融合分>=%.0f)" % FUSION_GATE: (TOPN_PROP, FUSION_GATE, "fusion"),
    "S3 截断 Top4(低扩展度)": (TOPN_PROP, None, "ext"),
    "S4 截断+置信 Top4(低扩展度>=%.0f)" % FUSION_GATE: (TOPN_PROP, FUSION_GATE, "ext"),
}
print("  注：S3/S4 选择标准为『距MA20偏离最小(未来空间最大)』取前4，而非融合分前4")
stats = {}
for name, (cap, gate, selector) in scenarios.items():
    eq, eqd, pt = simulate_portfolio(cap, gate, selector)
    s = portfolio_stats(eq, eqd, pt)
    stats[name] = s
    print(f"\n  [{name}]")
    print(f"    交易日 {s['n_days']} ({s['years']}年)  笔数 {s['n_trades']}")
    print(f"    累计收益 {s['total_ret']:>+7.1f}%   年化(CAGR) {s['cagr']:>+7.1f}%")
    print(f"    单笔均值 {s['per_trade_mean']:>+6.3f}%  单笔胜率 {s['per_trade_win']:>5.1f}%")
    print(f"    周均 {s['weekly_mean']:>+5.2f}%   周>=2%占比 {s['weekly_ge2pct']:>5.1f}%")
    print(f"    月均 {s['monthly_mean']:>+5.2f}%   月>=10%占比 {s['monthly_ge10pct']:>5.1f}%")
    print(f"    最大回撤 {s['max_dd']:>7.1f}%")

# ============ 目标缺口推导 ============
print("\n" + "=" * 78)
print("目标缺口：达到 年50% 所需的 per-cycle(3日篮子) 均值")
print("=" * 78)
cycles_per_yr = 252 / MAXHOLD
need_cycle = (1.50 ** (1 / cycles_per_yr) - 1) * 100
print(f"  每年约 {cycles_per_yr:.0f} 个3日周期；年50% → 每周期需 {need_cycle:+.3f}%")
print(f"  当前 S0 篮子均值 vs 需求：见诊断2（cap=8）")
for cap in (4, 8):
    m, _ = basket_mean_by_cap(cap)
    print(f"    cap={cap}: 实测 {m:+.3f}%  vs 需求 {need_cycle:+.3f}%  → "
          f"{'达标' if m >= need_cycle else '缺口 %.3f%%' % (need_cycle - m)}")

# ============ 写报告 ============
report = {
    "window": WINDOW_START,
    "config": {"engine": "pure_bottom_v2", "SIG": SIG, "E3": True,
               "STOP": STOP, "TAKE": TAKE, "MAXHOLD": MAXHOLD},
    "n_candidates": len(candidates), "n_signal_days": len(per_day),
    "rank_buckets": {b: {"n": len(p),
                         "mean_pct": bucket_stats(p)[1],
                         "win_pct": bucket_stats(p)[2]} for b, p in buckets.items()},
    "ext_buckets": {b: {"n": len(p),
                        "mean_pct": bucket_stats(p)[1],
                        "win_pct": bucket_stats(p)[2]} for b, p in ext_buckets.items()},
    "basket_mean_by_cap": {str(c): basket_mean_by_cap(c)[0] for c in (4, 8, 12)},
    "scenarios": stats,
    "need_cycle_pct_for_50pct_yr": need_cycle,
}
out_path = os.path.join(PROJECT_ROOT, "tools", "backtest_dynamic_count_report.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"\n报告已写入 {out_path}")
print("完成。")
