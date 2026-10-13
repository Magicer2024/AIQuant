# -*- coding: utf-8 -*-
"""短线「弱市暴露控制」×「同日同风格集中度上限」双向验证（只读，不改生产数据）。

背景
----
2026-10-13 出场修正（移动止盈收盘启动 + 跳空止损按止损价成交）已落地，
面板窗（2026-07-20~10-12）均笔 -1.22% → -0.60%，3 年池 +1.11% → +1.32%。
但仍不达「均笔 +1%」目标，剩余亏损的两个放大器：
  1) **弱市暴露**：regime 连续偏冷/冰点期间仍在推抄底票（现有 market_gate 只在
     cold / 指数破 MA20×0.98 / 5 日动量 < -1.5% 的日子收缩，cool 单独不触发）；
  2) **同日同风格集中**：2026-09-21 一日 3 笔同风格前龙头，次日集体崩 → 5 笔
     贡献面板窗 -74.4pp 里的 -69.4pp。

口径保证（零漂移）
------------------
- 候选/过滤/排序 **直接 import** 线上实现：short_t1_filter_sql /
  short_market_gate_sql / short_observe_bottom_sql / short_order_clause
  （复用 tools/_diag_short_filter_big.py 的 build_query 骨架，仅把 rn<=3 放宽到
  rn<=pool_n，以便「过滤后回填」与线上一致）。
- 出场 **直接调用** core.outcome_tracker._short_exit_sim（含已落地的 lc+sf 修正）。
- 暴露/集中度变体只在「线上全链过滤之后」按 scan_date 内 rn 顺序做**名额裁剪**，
  等价于线上把条件写进 WHERE / ROW_NUMBER，不重排、不换排序键。
- 相关性簇只用 **信号日及之前** 20 个交易日的收盘收益（无未来函数）。

用法
----
    python tools/_eval_short_exposure_grid.py                      # 3 年，每 5 日采样
    python tools/_eval_short_exposure_grid.py --sample 1           # 3 年全量（慢）
    python tools/_eval_short_exposure_grid.py --start 2026-07-20 --end 2026-10-12 --sample 1
    python tools/_eval_short_exposure_grid.py --pool 6 --topn 3 --corr 0.6
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _diag_short_filter_big as fb                                    # noqa: E402


# ─────────────────────────── 1. 候选池（线上全链 + 放宽 rn） ───────────────────────────

def build_pool_query(conn, start_date, end_date, pool_n, sample_days=None):
    """线上全链过滤（conf_gate + T1 + market_gate + observe）后取每日前 pool_n 名。

    与 _diag_short_filter_big.build_query 的唯一差别：rn 上限放宽 + 多带
    pct_above_ma20 / rn 两列（供 Python 侧做名额裁剪与诊断）。
    """
    from config.strategy_params import get_param
    from core.outcome_tracker import (short_t1_filter_sql, short_market_gate_sql,
                                      short_observe_bottom_sql, short_order_clause)
    from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

    conds = ["s.fusion_score >= ?"]
    params = [float(get_param("short_conf_gate"))]
    for c, p in (short_t1_filter_sql(conn, start_date, end_date),
                 short_market_gate_sql(conn, start_date, end_date),
                 short_observe_bottom_sql()):
        if c != "1":
            conds.append(c)
            params.extend(p)

    board_filter = ""
    if MAIN_BOARD_ONLY:
        board_filter = "".join(
            f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
    gate_sql = " AND " + " AND ".join(conds)
    order = short_order_clause()

    if sample_days:
        ph = ",".join("?" * len(sample_days))
        sample_sql = f" AND s.scan_date IN ({ph})"
        head_params = list(sample_days)
    else:
        sample_sql = " AND s.scan_date >= ? AND s.scan_date <= ?"
        head_params = [start_date, end_date]

    sql = f"""
        SELECT code, name, scan_date, strategy, buy_price, stop_loss, take_profit,
               fusion_score, pct_above_ma20, rn
        FROM (
            SELECT s.code, s.name, s.scan_date, s.strategy, s.buy_price, s.stop_loss,
                   s.take_profit, s.fusion_score, s.pct_above_ma20,
                   ROW_NUMBER() OVER (PARTITION BY s.scan_date
                                      ORDER BY {order}) AS rn
            FROM stock_signal s
            WHERE 1=1{sample_sql}
              AND COALESCE(s.horizon, 'short') = 'short'
              AND s.buy_price IS NOT NULL AND s.buy_price > 0
              AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
              AND COALESCE(s.strategy, '') != '强势突破'
              AND COALESCE(s.strategy, '') != '缩量回踩'
              AND COALESCE(s.strategy, '') != '反转首日'
              {board_filter}
              {gate_sql}
        )
        WHERE rn <= ?
        ORDER BY scan_date ASC, rn ASC
    """
    return sql, [*head_params, *params, pool_n]


# ─────────────────────────── 2. 弱市暴露：regime 连续偏冷判定 ───────────────────────────

def regime_ctx(conn, start_date, end_date):
    """返回 (cc_days, cool_days, cold_days, streak, weak_prod)。

    - cc_days: regime ∈ {cold, cool} 的交易日集合（比线上 market_gate 更宽的口径）
    - streak:  {date: 截至当日「连续 cc」的天数}（按交易日历，非自然日）
    - cold_days / cool_days: 单档集合（供展示层 RECO_REGIME_CAP 对齐变体使用）
    - weak_prod: 线上 market_gate 已判为走弱的日子（cold ∪ 指数破位 ∪ 5日动量<-1.5%）
    """
    from core.market_regime import compute_regime_series
    from core.outcome_tracker import _short_weak_dates
    # regime 需要 lookback，起点前推 60 天保证窗口首日也有值
    lb = (date.fromisoformat(start_date) - timedelta(days=60)).isoformat()
    series = compute_regime_series(conn, lb, end_date)
    cc = {d for d, r in series.items() if r in ("cold", "cool")}
    cold = {d for d, r in series.items() if r == "cold"}
    cool = {d for d, r in series.items() if r == "cool"}
    days = sorted(series)
    streak = {}
    cur = 0
    for d in days:
        cur = cur + 1 if d in cc else 0
        streak[d] = cur
    weak_prod = _short_weak_dates(conn, start_date, end_date, series=series)
    return cc, cool, cold, streak, weak_prod


def corr_ctx(price_cache, lookback=20):
    """code -> {date: 近 lookback 日对数收益向量(list)}，只用 <= 当日的数据。"""
    out = {}
    for code, rows in price_cache.items():
        rows = sorted(rows, key=lambda r: r[0])
        closes = [r[2] for r in rows]
        dates = [r[0] for r in rows]
        rets = [None]
        for i in range(1, len(closes)):
            a, b = closes[i - 1], closes[i]
            rets.append(math.log(b / a) if a and b and a > 0 and b > 0 else None)
        m = {}
        for i, d in enumerate(dates):
            lo = i - lookback + 1
            if lo < 1:
                continue
            v = rets[lo:i + 1]
            if any(x is None for x in v):
                continue
            m[d] = v
        out[code] = m
    return out


def _pearson(a, b):
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


# ─────────────────────────── 3. 名额裁剪变体（等价于线上 WHERE/ROW_NUMBER 改造） ───────────────────────────

MOMENTUM = "隔日动量"


def _is_mom(p):
    return (p.get("strategy") or "") == MOMENTUM


def make_selector(spec, rc):
    """按变体定义返回 sel(day_picks_sorted_by_rn, scan_date) -> picks。

    rc = dict(cc=, cool=, cold=, streak=, cvecs=, corr_thr=) 为共享上下文。

    spec 键：
      block: None | 'cc' | 'cold' | 'cc2' | 'cc3'  —— 该判定命中日只留隔日动量
      cap_cc: None | int   —— cc 日总名额上限（暴露收缩而非全停）
      regime_cap: None | dict —— 与今日推荐展示层 RECO_REGIME_CAP 同构的
                    {'cold': 0, 'cool': 2} 名额天花板（其余档用 topn）
      style_cap: None | int —— 抄底类（非隔日动量）每日上限
      style_cap_all: None | int —— 每风格（动量 / 抄底各一组）每日上限
      no_backfill: bool —— 名额裁剪只在 rn<=topn 内做，不用 rn>topn 的候选回填
                    （对应最简 SQL 实现：单层 WHERE rn_style<=cap AND rn<=topn）
      corr_cap: bool —— 同日已选票 20 日收益相关 > corr_thr 的候选跳过（回填后续 rn）
    """
    cc, cool, cold = rc["cc"], rc["cool"], rc["cold"]
    streak, cvecs, corr_thr = rc["streak"], rc["cvecs"], rc["corr_thr"]
    block = spec.get("block")
    cap_cc = spec.get("cap_cc")
    regime_cap = spec.get("regime_cap")
    style_cap = spec.get("style_cap")
    style_cap_all = spec.get("style_cap_all")
    no_backfill = spec.get("no_backfill")
    corr_cap = spec.get("corr_cap")
    topn = spec.get("topn", 3)

    def blocked(d):
        if block == "cc":
            return d in cc
        if block == "cold":
            return d in cold
        if block == "cc2":
            return streak.get(d, 0) >= 2
        if block == "cc3":
            return streak.get(d, 0) >= 3
        return False

    def day_cap(d):
        limit = topn
        if cap_cc and d in cc:
            limit = min(limit, cap_cc)
        if regime_cap:
            if d in cold and "cold" in regime_cap:
                limit = min(limit, int(regime_cap["cold"]))
            elif d in cool and "cool" in regime_cap:
                limit = min(limit, int(regime_cap["cool"]))
        return limit

    def sel(day, d):
        pool = list(day)
        if blocked(d):
            pool = [p for p in pool if _is_mom(p)]
        limit = day_cap(d)
        if limit <= 0:
            return []
        if no_backfill:
            pool = pool[:topn]
        picks, n_bottom, n_mom, kept_vecs = [], 0, 0, []
        for p in pool:
            if len(picks) >= limit:
                break
            mom = _is_mom(p)
            if style_cap and not mom and n_bottom >= style_cap:
                continue
            if style_cap_all and ((n_mom if mom else n_bottom) >= style_cap_all):
                continue
            if corr_cap:
                v = (cvecs.get(p["code"]) or {}).get(d)
                if v is not None:
                    dup = False
                    for kv in kept_vecs:
                        c = _pearson(v, kv)
                        if c is not None and c > corr_thr:
                            dup = True
                            break
                    if dup:
                        continue
                    kept_vecs.append(v)
            picks.append(p)
            if mom:
                n_mom += 1
            else:
                n_bottom += 1
        return picks

    return sel


# ─────────────────────────── 4. 变体清单 ───────────────────────────

# 今日推荐展示层现成的 regime 名额天花板（config.RECO_REGIME_CAP["short"]）——
# 出场跟踪 / 复盘入库两处出口并未使用，是既有的口径分歧。
DISPLAY_REGIME_CAP = {"cold": 0, "cool": 2}


def variants(topn, corr_thr):
    v = {}
    v["base(线上全链)"] = dict(topn=topn)
    # 方向⓪：纯名额收缩（检验「第 3 名额是否本身负 alpha」）
    v["A0 全程名额=1"] = dict(topn=1)
    # 方向①：弱市暴露控制（逐档加严）
    v["A1 cold日只留动量"] = dict(topn=topn, block="cold")
    v["A2 cc日只留动量"] = dict(topn=topn, block="cc")
    v["A3 连冷2日只留动量"] = dict(topn=topn, block="cc2")
    v["A4 连冷3日只留动量"] = dict(topn=topn, block="cc3")
    v["A5 cc日名额=2"] = dict(topn=topn, cap_cc=2)
    v["A6 cc日名额=1"] = dict(topn=topn, cap_cc=1)
    v["A7 全程名额=2"] = dict(topn=2)
    # 方向②：同日同风格集中度上限
    v["C1 抄底同日≤2"] = dict(topn=topn, style_cap=2)
    v["C2 抄底同日≤1"] = dict(topn=topn, style_cap=1)
    v[f"C3 相关>{corr_thr}簇≤1"] = dict(topn=topn, corr_cap=True)
    # C4 = C1 的「不回填」实现（单层 SQL：rn_style<=2 AND rn<=3），落地成本最低；
    # 与 C1 的差异只在「被裁掉的名额是否用 rn>3 的候选补上」，用于选实现方式。
    v["C4 抄底≤2不回填"] = dict(topn=topn, style_cap=2, no_backfill=True)
    v["C5 每风格≤2"] = dict(topn=topn, style_cap_all=2)
    # 方向③：口径对齐——把展示层已有的 regime 名额天花板同步到跟踪/复盘出口
    v["E1 对齐展示层cap(cold0/cool2)"] = dict(
        topn=topn, regime_cap=DISPLAY_REGIME_CAP)
    # 组合
    v["D1 A3+C1"] = dict(topn=topn, block="cc2", style_cap=2)
    v["D2 A3+C3"] = dict(topn=topn, block="cc2", corr_cap=True)
    v["D3 A5+C1"] = dict(topn=topn, cap_cc=2, style_cap=2)
    v["D4 A5+C3"] = dict(topn=topn, cap_cc=2, corr_cap=True)
    v["D5 A2+C3"] = dict(topn=topn, block="cc", corr_cap=True)
    v["D6 A7+C1"] = dict(topn=2, style_cap=2)
    v["D7 E1+C1"] = dict(topn=topn, regime_cap=DISPLAY_REGIME_CAP, style_cap=2)
    v["D8 E1+A7"] = dict(topn=2, regime_cap=DISPLAY_REGIME_CAP)
    # 方向④：止损宽度上限（左尾）。2026-09-21/22 的 5 笔簇亏实际止损位是
    # -10.5%~-13.9%（ATR 自适应 k=2.5 放宽所致），不是集中度问题。
    # 锚点 = buy_price（信号日收盘价）——线上只能在写库时定止损，与生产可实现口径一致。
    for pct in (0.06, 0.08, 0.10):
        v[f"F{int(pct*100)} 止损上限{int(pct*100)}%"] = dict(
            topn=topn, stop_cap=pct)
    v["F8+C1"] = dict(topn=topn, stop_cap=0.08, style_cap=2)
    v["F8+A7"] = dict(topn=2, stop_cap=0.08)
    return v


def slot_report(conn, pool, price_cache, start_date, end_date):
    """按 rn（线上排序名次）归因：第 1/2/3/… 名额各自的期望收益。

    这是「名额收缩」与「同日集中度上限」两个方向的**共同根因检验**：
    若 rn=3 长期负 alpha 而 rn=1/2 为正，说明排序键在第 3 位之后失效，
    正解是把 short_top_n 收到 2（单参数、零新代码），而不是再叠过滤规则。
    """
    rows = fb.simulate(conn, pool, price_cache, start_date, end_date)
    key = {(p["scan_date"], p["code"]): p for p in pool}
    buckets = defaultdict(list)
    year_buckets = defaultdict(lambda: defaultdict(list))
    strat_buckets = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if not r.get("filled") or r["ret"] is None:
            continue
        p = key.get((r["scan_date"], r["code"]))
        if not p:
            continue
        buckets[p["rn"]].append(r["ret"])
        year_buckets[p["rn"]][r["scan_date"][:4]].append(r["ret"])
        strat_buckets[p["rn"]]["动量" if _is_mom(p) else "抄底"].append(r["ret"])
    print()
    print("=" * 118)
    print("按线上排序名次 rn 归因（rn=1 是当天最优先名额）")
    print("=" * 118)
    print(f"{'rn':>3}{'成交':>7}{'均笔%':>9}{'中位%':>8}{'胜率%':>7}{'合计%':>10}{'t':>7}"
          f"   动量均笔(n)      抄底均笔(n)      分年均笔")
    for rn in sorted(buckets):
        v = buckets[rn]
        s = summarize([dict(ret=x, filled=1) for x in v])
        mom = strat_buckets[rn].get("动量", [])
        bot = strat_buckets[rn].get("抄底", [])
        f = lambda xs: (f"{st.mean(xs):+.2f}(n{len(xs)})" if xs else "—")
        yrs = " ".join(f"{y}:{st.mean(z):+.2f}"
                       for y, z in sorted(year_buckets[rn].items()))
        print(f"{rn:>3}{s['n']:>7}{s['mean']:>+9.3f}{s['median']:>+8.3f}"
              f"{s['win']:>7.1f}{s['total']:>+10.1f}{s['t']:>+7.2f}"
              f"   {f(mom):<16} {f(bot):<16} {yrs}")
    print()


# ─────────────────────────── 5. 统计 ───────────────────────────

def summarize(rows):
    rets = [x["ret"] for x in rows if x.get("filled") and x["ret"] is not None]
    if not rets:
        return dict(n=0)
    n = len(rets)
    mean = st.mean(rets)
    sd = st.stdev(rets) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    return dict(n=n, mean=mean, median=st.median(rets), sd=sd,
                t=(mean / se if se > 0 else 0.0),
                win=sum(1 for v in rets if v > 0) / n * 100,
                stop=sum(1 for x in rows if x.get("hit_stop")) / n * 100,
                total=sum(rets))


def by_year(rows):
    d = defaultdict(list)
    for x in rows:
        if x.get("filled") and x["ret"] is not None:
            d[x["scan_date"][:4]].append(x["ret"])
    return {k: (len(v), st.mean(v)) for k, v in sorted(d.items())}


def paired_test(a, b):
    def byday(rows):
        d = defaultdict(list)
        for x in rows:
            if x.get("filled") and x["ret"] is not None:
                d[x["scan_date"]].append(x["ret"])
        return {k: st.mean(v) for k, v in d.items()}
    da, db = byday(a), byday(b)
    common = sorted(set(da) & set(db))
    if len(common) < 5:
        return None
    diffs = [da[k] - db[k] for k in common]
    m = st.mean(diffs)
    sd = st.stdev(diffs) if len(diffs) > 1 else 0.0
    se = sd / math.sqrt(len(diffs)) if len(diffs) > 1 else 0.0
    nz = [v for v in diffs if abs(v) > 1e-9]
    return dict(n_days=len(common), diff=m, t=(m / se if se > 0 else 0.0),
                win_days=sum(1 for v in diffs if v > 0) / len(diffs) * 100,
                n_diff=len(nz),
                diff_only=(st.mean(nz) if nz else 0.0),
                win_only=(sum(1 for v in nz if v > 0) / len(nz) * 100 if nz else 0.0))


def pick_days(picks):
    """入选组合的 (scan_date -> 代码集合)，用于统计「与线上不同的天数」。"""
    d = defaultdict(set)
    for p in picks:
        d[p["scan_date"]].add(p["code"])
    return d


def diff_days(pa, pb):
    da, db = pick_days(pa), pick_days(pb)
    days = set(da) | set(db)
    return sum(1 for d in days if da.get(d, set()) != db.get(d, set()))


def conn_max_date():
    c = sqlite3.connect(fb.DB)
    v = c.execute("SELECT MAX(scan_date) FROM stock_signal").fetchone()[0]
    c.close()
    return v


# ─────────────────────────── 6. 资金约束组合模拟 ───────────────────────────
# 为什么要这一层：个人账户 1 万元 × POSITION_PLAN_MAX_PCT=0.20 → 单笔 2000 元、
# 同时最多约 5 笔。而线上每日 3 笔推荐 × 平均持仓 6 个交易日 ≈ 18 笔并发，
# 资金吃不下 ⇒ 「每笔均值」与「收益之和」都不是用户真正拿到的钱。
# 本段按时间顺序模拟**槽位/现金约束**下的真实账户净值：同一天多只按 rn 优先，
# 槽位或现金不足则放弃该笔（等价于用户只能挑排在前面的做）。

def simulate_dated(conn, picks, price_cache, start_date, end_date):
    """与 _diag_short_filter_big.simulate 同一口径，但额外返回入场日/出场日/持仓天数。

    防漂移：调用方会用 fb.simulate 的 ret 做逐笔比对断言（见 pf_report）。
    """
    from config.strategy_params import get_param
    from strategy.exit_advisor import get_max_hold
    from core.outcome_tracker import _short_exit_sim

    trail_pct = get_param("short_trailing_pct")
    max_hold = get_max_hold("short") or 10
    pullback_on = bool(int(get_param("short_pullback_entry")))
    entry_window = max(1, int(get_param("short_entry_window_days")))

    out = []
    for p in picks:
        code, sd = p["code"], p["scan_date"]
        entry = p["buy_price"]
        if not entry or entry <= 0:
            continue
        series = price_cache.get(code)
        if not series:
            continue
        idx = None
        for i, (d, *_rest) in enumerate(series):
            if d > sd:
                idx = i
                break
        if idx is None:
            continue
        need = entry_window + max_hold + 1
        prices = series[idx: idx + need]
        if len(prices) < need:
            continue
        strategy = p.get("strategy") or ""
        pullback = pullback_on and strategy != "隔日动量"
        if pullback:
            fill_idx, exec_entry = None, None
            for j in range(min(entry_window, len(prices))):
                _d, _o, _c, _h, low = prices[j]
                if low is None:
                    continue
                if low <= entry:
                    exec_entry = round(min(float(entry), float(_o or entry)), 2)
                    fill_idx = j
                    break
            if fill_idx is None:
                out.append(dict(scan_date=sd, code=code, filled=0, reason="no_fill",
                                ret=None, rn=p.get("rn")))
                continue
            hold = prices[fill_idx:fill_idx + max_hold]
            entry_date = prices[fill_idx][0]
        else:
            _d, o, c, _h, _l = prices[0]
            exec_entry = float(o) if o else (float(c) or float(entry))
            hold = prices[:max_hold]
            entry_date = prices[0][0]
        hold_rows = [dict(trade_date=d, open=o, close=c, high=h, low=l)
                     for (d, o, c, h, l) in hold]
        reason, exit_date, ret, hit_stop, launched, done = _short_exit_sim(
            hold_rows, exec_entry, p["stop_loss"], p["take_profit"],
            trail_pct, max_hold)
        if not done:
            continue
        out.append(dict(scan_date=sd, code=code, filled=1, reason=reason, ret=ret,
                        hit_stop=hit_stop, rn=p.get("rn"), strategy=strategy,
                        entry_date=entry_date, exit_date=exit_date or hold_rows[-1]["trade_date"],
                        entry_price=exec_entry, hold=len(hold_rows)))
    return out


def pf_sim(rows, slots=5, per_pos=2000.0, cost_rt=0.0016, capital=10000.0):
    """槽位 + 现金约束下的账户模拟（固定单笔金额，不复利放大仓位）。

    同一天多笔按 rn 升序争抢槽位；现金不足 per_pos 则放弃。
    cost_rt = 往返成本（佣金万3×2 + 印花税千1 ≈ 0.16%）。
    返回 dict(净值/收益率/成交笔数/放弃笔数/最大回撤/平均并发)。
    """
    trades = [r for r in rows if r.get("filled") and r.get("ret") is not None
              and r.get("entry_date") and r.get("exit_date")]
    by_entry = defaultdict(list)
    for t in trades:
        by_entry[t["entry_date"]].append(t)
    for d in by_entry:
        by_entry[d].sort(key=lambda x: (x.get("rn") or 99, x["code"]))

    cash = capital
    open_pos = []          # list[(exit_date, invested, ret)]
    taken = skipped = 0
    equity_peak, max_dd = capital, 0.0
    conc = []
    for d in sorted(by_entry):
        # 先结算已到期（exit_date <= 当日）的持仓
        still = []
        for xd, inv, ret in open_pos:
            if xd <= d:
                cash += inv * (1 + (ret / 100.0) - cost_rt)
            else:
                still.append((xd, inv, ret))
        open_pos = still
        for t in by_entry[d]:
            if len(open_pos) >= slots or cash < per_pos:
                skipped += 1
                continue
            cash -= per_pos
            open_pos.append((t["exit_date"], per_pos, t["ret"]))
            taken += 1
        equity = cash + sum(inv for _xd, inv, _r in open_pos)
        equity_peak = max(equity_peak, equity)
        max_dd = max(max_dd, (equity_peak - equity) / equity_peak * 100)
        conc.append(len(open_pos))
    # 期末未平仓按成本计（保守：不计浮盈浮亏）
    final = cash + sum(inv for _xd, inv, _r in open_pos)
    return dict(final=final, ret_pct=(final / capital - 1) * 100, taken=taken,
                skipped=skipped, max_dd=max_dd,
                avg_conc=(sum(conc) / len(conc) if conc else 0.0),
                open_left=len(open_pos))


def pf_report(conn, variants_picks, price_cache, start_date, end_date,
              slots_list=(5,), per_pos=2000.0):
    """对每个变体跑资金约束组合模拟；先用 fb.simulate 校验 ret 口径零漂移。"""
    print()
    print("=" * 118)
    print(f"资金约束组合模拟（账户 1 万 / 单笔 {per_pos:.0f} 元 / "
          f"槽位 {'、'.join(str(s) for s in slots_list)} / 往返成本 0.16%）")
    print("=" * 118)
    hdr = (f"{'变体':<22}" + "".join(
        f"{'槽'+str(s)+'终值':>12}{'收益%':>9}{'回撤%':>8}" for s in slots_list)
        + f"{'成交':>6}{'放弃':>6}{'均笔%':>9}{'均并发':>8}")
    print(hdr)
    print("-" * 130)
    for name, picks in variants_picks.items():
        rows = simulate_dated(conn, picks, price_cache, start_date, end_date)
        # 口径校验：与 fb.simulate 的 ret 逐笔一致（防两套模拟漂移）
        ref = {(r["scan_date"], r["code"]): r["ret"] for r in
               fb.simulate(conn, picks, price_cache, start_date, end_date)
               if r.get("filled")}
        mine = {(r["scan_date"], r["code"]): r["ret"] for r in rows
                if r.get("filled")}
        assert set(ref) == set(mine), f"{name}: 样本集与线上模拟不一致"
        bad = [k for k in ref if abs((ref[k] or 0) - (mine[k] or 0)) > 1e-9]
        assert not bad, f"{name}: {len(bad)} 笔 ret 与线上模拟不一致"
        rets = [r["ret"] for r in rows if r.get("filled") and r["ret"] is not None]
        mean = st.mean(rets) if rets else 0.0
        cells = []
        r0 = None
        for s in slots_list:
            r = pf_sim(rows, slots=s, per_pos=per_pos)
            r0 = r0 or r
            cells.append(f"{r['final']:>12.0f}{r['ret_pct']:>+9.1f}"
                         f"{r['max_dd']:>8.1f}")
        print(f"{name:<22}" + "".join(cells)
              + f"{r0['taken']:>6}{r0['skipped']:>6}{mean:>+9.3f}"
              f"{r0['avg_conc']:>8.2f}", flush=True)
    print()
    print("注：终值含期末未平仓（按成本计，不计浮盈）；「放弃」= 因槽位/现金不足"
          "未能执行的推荐笔数——它才是用户真实的资金瓶颈证据。")
    print("     ret 已逐笔断言与 _diag_short_filter_big.simulate（线上口径）一致。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--topn", type=int, default=3)
    ap.add_argument("--pool", type=int, default=6, help="每日候选池深度（回填用）")
    ap.add_argument("--corr", type=float, default=0.6, help="同日相关簇阈值")
    ap.add_argument("--sample", type=int, default=5, help="交易日采样间隔（1=全量）")
    ap.add_argument("--slots", action="store_true",
                    help="额外输出按 rn 名次的期望归因（名额收缩的根因检验）")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略候选池缓存并重新执行 SQL（改了过滤/排序口径后必须加）")
    ap.add_argument("--pf", default=None,
                    help="资金约束组合模拟：逗号分隔的变体名子串（如 base,A0,A7,C1,D8）")
    ap.add_argument("--pf-slots", default="5",
                    help="组合模拟的并发槽位列表，逗号分隔（默认 5 = 1万/2000元）")
    args = ap.parse_args()

    end_date = args.end or str(conn_max_date())
    start_date = args.start or (date.fromisoformat(end_date)
                                - timedelta(days=int(365.25 * args.years))).isoformat()

    conn = sqlite3.connect(fb.DB)
    conn.row_factory = sqlite3.Row

    print("=" * 110)
    print(f"短线 弱市暴露 × 同日集中度 网格   窗口 {start_date} → {end_date}   "
          f"top_n={args.topn}  pool={args.pool}  corr>{args.corr}")
    print("=" * 110, flush=True)

    sample_days = None
    if args.sample > 1:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal "
            "WHERE scan_date >= ? AND scan_date <= ? "
            "AND COALESCE(horizon,'short')='short' ORDER BY scan_date",
            (start_date, end_date)).fetchall()]
        sample_days = days[::args.sample]
        print(f"交易日采样：{len(days)} → {len(sample_days)} 天", flush=True)

    cc, cool, cold, streak, weak_prod = regime_ctx(conn, start_date, end_date)
    win_days = sample_days or [r[0] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM stock_signal WHERE scan_date >= ? "
        "AND scan_date <= ? AND COALESCE(horizon,'short')='short'",
        (start_date, end_date)).fetchall()]
    n_cc = sum(1 for d in win_days if d in cc)
    n_cc2 = sum(1 for d in win_days if streak.get(d, 0) >= 2)
    n_cc3 = sum(1 for d in win_days if streak.get(d, 0) >= 3)
    n_weak = sum(1 for d in win_days if d in weak_prod)
    n_cool = sum(1 for d in win_days if d in cool)
    print(f"窗口内候选交易日 {len(win_days)}：线上走弱闸门已拦 {n_weak} | "
          f"cold/cool {n_cc}（cool {n_cool}）| 连冷≥2 {n_cc2} | 连冷≥3 {n_cc3}",
          flush=True)

    # 候选池缓存：T1 过滤含逐行相关子查询，3 年全量 SQL 数十分钟；变体调整不该重付
    cache_file = os.path.join(
        _ROOT, "logs", f"_exposure_pool_{start_date}_{end_date}"
                        f"_p{args.pool}_s{args.sample}.pkl")
    pool = None
    if not args.no_cache and os.path.exists(cache_file):
        import pickle
        with open(cache_file, "rb") as fh:
            pool = pickle.load(fh)
        print(f"候选池命中缓存 {cache_file}（{len(pool)} 条）", flush=True)
    if pool is None:
        sql, params = build_pool_query(conn, start_date, end_date, args.pool,
                                       sample_days)
        pool = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if not args.no_cache:
            import pickle
            with open(cache_file, "wb") as fh:
                pickle.dump(pool, fh)
    print(f"候选池（每日前 {args.pool}）共 {len(pool)} 条", flush=True)

    codes = sorted({p["code"] for p in pool})
    lb_start = (date.fromisoformat(start_date) - timedelta(days=60)).isoformat()
    price_cache = fb.load_prices(conn, codes, lb_start, end_date)
    print(f"价格缓存 {len(price_cache)} 只", flush=True)
    cvecs = corr_ctx(price_cache, lookback=20)
    rc = dict(cc=cc, cool=cool, cold=cold, streak=streak, cvecs=cvecs,
              corr_thr=args.corr)

    if args.slots:
        slot_report(conn, pool, price_cache, start_date, end_date)

    by_day = defaultdict(list)
    for p in pool:
        by_day[p["scan_date"]].append(p)
    for d in by_day:
        by_day[d].sort(key=lambda x: x["rn"])

    vs = variants(args.topn, args.corr)
    results = {}
    for name, spec in vs.items():
        sel = make_selector(spec, rc)
        picks = []
        for d in sorted(by_day):
            picks.extend(sel(by_day[d], d))
        cap = spec.get("stop_cap")
        if cap:
            # 止损价按信号日买点重新夹紧（只上移不下移，等价于线上写库时 cap）
            picks = [dict(p, stop_loss=(max(float(p["stop_loss"]),
                                            float(p["buy_price"]) * (1 - cap))
                                        if p.get("stop_loss") else p.get("stop_loss")))
                     for p in picks]
        rows = fb.simulate(conn, picks, price_cache, start_date, end_date)
        results[name] = (picks, rows)

    base_picks, base_rows = results["base(线上全链)"]
    print()
    hdr = (f"{'变体':<20}{'入选':>6}{'成交':>6}{'均笔%':>9}{'中位%':>8}{'胜率%':>7}"
           f"{'止损%':>7}{'合计%':>9}{'t':>7}  {'差异日':>6}{'diff%':>8}{'t':>7}"
           f"{'仅差异日均diff%':>16}{'差异日胜率%':>12}   分年均笔")
    print(hdr)
    print("-" * 150)
    for name in vs:
        picks, rows = results[name]
        s = summarize(rows)
        if not s["n"]:
            print(f"{name:<20}{len(picks):>6}"
                  f"{sum(1 for x in rows if x.get('filled')):>6}   无有效样本")
            continue
        pt = paired_test(rows, base_rows)
        dd = diff_days(picks, base_picks)
        yrs = " ".join(f"{y}:{m:+.2f}(n{n})" for y, (n, m) in by_year(rows).items())
        pt_s = (f"{dd:>6}{pt['diff']:>+8.3f}{pt['t']:>+7.2f}"
                f"{pt['diff_only']:>+16.3f}{pt['win_only']:>12.1f}"
                if pt else f"{dd:>6}{'—':>8}{'—':>7}{'—':>16}{'—':>12}")
        flag = ""
        if pt and abs(pt["t"]) > 2:
            flag = "  ★显著" if pt["diff"] > 0 else "  ✗显著劣化"
        print(f"{name:<20}{len(picks):>6}{s['n']:>6}{s['mean']:>+9.3f}"
              f"{s['median']:>+8.3f}{s['win']:>7.1f}{s['stop']:>7.1f}"
              f"{s['total']:>+9.1f}{s['t']:>+7.2f}  {pt_s}   {yrs}{flag}", flush=True)

    print()
    print("=" * 150)
    print("判读：diff% = 全窗口按日配对的当日均值差（含无差异日，摊薄后偏小）；"
          "「仅差异日均 diff%」才是该规则真实的单日作用力，配合「差异日」数看覆盖面。")
    print("      合计% = 窗口内所有成交笔收益之和（资金视角，名额收缩会同时压低它——"
          "均笔升 ≠ 赚得多，1 万本金场景必须两者一起看）。")
    print("      只有 diff>0、|t|>2、分年方向一致、且合计不明显缩水才算稳；"
          "仅在小窗取胜、3 年池不显著的变体一律视为过拟合，不落地。")
    print("=" * 150)

    if args.pf:
        keep = [n for n in vs if any(k in n for k in args.pf.split(","))] \
            or list(vs)
        pf_report(conn, {n: results[n][0] for n in keep}, price_cache,
                  start_date, end_date,
                  slots_list=tuple(int(x) for x in args.pf_slots.split(",")))

    conn.close()


if __name__ == "__main__":
    main()
