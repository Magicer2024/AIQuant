"""short 过滤链**大样本** leave-one-out 归因（替代 45 天小样本结论）。

背景
----
2026-09-19 首次诊断（tools/_diag_short_filter_live.py）只有 45 个交易日 / 113 笔，
结论「全链 −1.03% vs 无过滤 +0.77%」功效不足。而 stock_signal 实有 **8102 个
交易日 / 48.5 万条** short 信号（1993 至今），近 3 年也有 738 个交易日。
本脚本在**近 3 年**上重做同样实验，且用与线上完全一致的过滤函数与出场数学。

设计要点
--------
1. 过滤函数直接 import 线上实现（short_t1_filter_sql / short_market_gate_sql /
   short_observe_bottom_sql / short_order_clause），绝不重写 ⇒ 口径零漂移。
2. 出场数学直接复用 core.outcome_tracker._short_exit_sim（回踩入场逻辑在
   本脚本内复刻 _evaluate_short 的实现），不重写。
3. 每个组合**一次性** SQL 取全窗口 top-N（ROW_NUMBER PARTITION BY scan_date），
   而非按天循环；价格按需按 code 批量拉取并缓存。
4. leave-one-out：全开 / 全关 / 逐个关掉其中一个，共 N+2 个组合；
   另加「逐个单开」用于看每个过滤器的独立作用。

用法
----
    python tools/_diag_short_filter_big.py [--years 3] [--topn 3] [--pairs]

输出：每个组合的 笔数 / 成交数 / 均值 / 中位 / 胜率 / 止损率 / 均值 t 统计量，
以及**按日配对**的全链 vs 对照组差值检验（主判据）。
"""

import argparse
import math
import sqlite3
import statistics as st
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")


# ─────────────────────────────────────────────
# 组合定义：每个过滤器一个开关
# ─────────────────────────────────────────────

FILTERS = ["conf_gate", "t1", "market_gate", "observe"]

PRESETS = {
    "全链(线上)":      dict(conf_gate=True,  t1=True,  market_gate=True,  observe=True),
    "无过滤":          dict(conf_gate=False, t1=False, market_gate=False, observe=False),
    "去掉 conf_gate":  dict(conf_gate=False, t1=True,  market_gate=True,  observe=True),
    "去掉 t1":         dict(conf_gate=True,  t1=False, market_gate=True,  observe=True),
    "去掉 market_gate":dict(conf_gate=True,  t1=True,  market_gate=False, observe=True),
    "去掉 observe":    dict(conf_gate=True,  t1=True,  market_gate=True,  observe=False),
    "单开 conf_gate":  dict(conf_gate=True,  t1=False, market_gate=False, observe=False),
    "单开 t1":         dict(conf_gate=False, t1=True,  market_gate=False, observe=False),
    "单开 market_gate":dict(conf_gate=False, t1=False, market_gate=True,  observe=False),
    "单开 observe":    dict(conf_gate=False, t1=False, market_gate=False, observe=True),
}


def build_query(conn, start_date, end_date, topn, flags, sample_days=None):
    """构造一次性取全窗口 top-N 的 SQL（口径与 outcome_tracker.insert_new_outcomes 一致）。

    :param sample_days: 可选的交易日白名单。**性能关键**——T1 过滤的
        `(SELECT AVG(pct_change) FROM daily_price WHERE trade_date=s.scan_date)`
        是逐行相关子查询，3 年 14.5 万行会跑 40 分钟以上。按交易日采样
        （每 N 日取 1）把扫描行数降到 1/N，**不改变任何过滤语义**（过滤器
        都是按 scan_date 独立判定），只是减少样本日数。
    """
    from config.strategy_params import get_param
    from core.outcome_tracker import (
        short_t1_filter_sql, short_market_gate_sql,
        short_observe_bottom_sql, short_order_clause,
    )
    from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

    conds, params = [], []
    if flags["conf_gate"]:
        conds.append("s.fusion_score >= ?")
        params.append(float(get_param("short_conf_gate")))
    if flags["t1"]:
        c, p = short_t1_filter_sql(conn, start_date, end_date)
        if c != "1":
            conds.append(c)
            params.extend(p)
    if flags["market_gate"]:
        c, p = short_market_gate_sql(conn, start_date, end_date)
        if c != "1":
            conds.append(c)
            params.extend(p)
    if flags["observe"]:
        c, p = short_observe_bottom_sql()
        if c != "1":
            conds.append(c)
            params.extend(p)

    board_filter = ""
    if MAIN_BOARD_ONLY:
        board_filter = "".join(
            f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)

    gate_sql = (" AND " + " AND ".join(conds)) if conds else ""
    order = short_order_clause()
    if sample_days:
        ph = ",".join("?" * len(sample_days))
        sample_sql = f" AND s.scan_date IN ({ph})"
        head_params = list(sample_days)
    else:
        sample_sql = " AND s.scan_date >= ? AND s.scan_date <= ?"
        head_params = [start_date, end_date]
    sql = f"""
        SELECT code, scan_date, strategy, buy_price, stop_loss, take_profit, fusion_score
        FROM (
            SELECT s.code, s.scan_date, s.strategy, s.buy_price, s.stop_loss,
                   s.take_profit, s.fusion_score,
                   ROW_NUMBER() OVER (PARTITION BY s.scan_date
                                      ORDER BY {order}) AS rn
            FROM stock_signal s
            WHERE 1=1{sample_sql}
              AND COALESCE(s.horizon, 'short') = 'short'
              AND s.buy_price IS NOT NULL AND s.buy_price > 0
              AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
              AND COALESCE(s.strategy, '') != '强势突破'
              AND COALESCE(s.strategy, '') != '缩量回踩'
              {board_filter}
              {gate_sql}
        )
        WHERE rn <= ?
        ORDER BY scan_date ASC
    """
    return sql, [*head_params, *params, topn]


def load_prices(conn, codes, start_date, end_date):
    """批量拉取候选票的价格序列（含 end_date 之后的出场缓冲）。"""
    cache = {}
    if not codes:
        return cache
    # 出场最多需要 15(entry window) + max_hold + 缓冲
    buf_end = _shift(end_date, 40)
    for i in range(0, len(codes), 300):
        chunk = codes[i:i + 300]
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(f"""
            SELECT code, trade_date, open, close, high, low
            FROM daily_price
            WHERE code IN ({ph}) AND trade_date >= ? AND trade_date <= ?
            ORDER BY code, trade_date ASC
        """, (*chunk, start_date, buf_end)).fetchall()
        for r in rows:
            cache.setdefault(r["code"], []).append(
                (r["trade_date"], r["open"], r["close"], r["high"], r["low"]))
    return cache


def _shift(date_str, days):
    from datetime import date, timedelta
    d = date.fromisoformat(date_str) + timedelta(days=days)
    return d.isoformat()


def simulate(conn, picks, price_cache, start_date, end_date):
    """对选中的每笔做回踩入场 + 线上出场模拟，返回逐笔结果 list[dict]。"""
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
        # 取信号日之后的行情
        idx = None
        for i, (d, *_rest) in enumerate(series):
            if d > sd:
                idx = i
                break
        if idx is None:
            continue
        # 需要 window + max_hold + 1 行才能定终态
        need = entry_window + max_hold + 1
        prices = series[idx: idx + need]
        if len(prices) < need:
            continue   # 数据未走完 ⇒ 非终态，剔除（保证样本完整）

        strategy = p["strategy"] or ""
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
                out.append(dict(scan_date=sd, code=code, filled=0,
                                reason="no_fill", ret=None))
                continue
            hold = prices[fill_idx:fill_idx + max_hold]
        else:
            _d, o, c, _h, _l = prices[0]
            exec_entry = float(o) if o else (float(c) or float(entry))
            hold = prices[:max_hold]

        # _short_exit_sim 需要支持下标访问的对象
        hold_rows = [dict(trade_date=d, open=o, close=c, high=h, low=l)
                     for (d, o, c, h, l) in hold]
        reason, _xd, ret, hit_stop, launched, done = _short_exit_sim(
            hold_rows, exec_entry, p["stop_loss"], p["take_profit"],
            trail_pct, max_hold)
        if not done:
            continue
        out.append(dict(scan_date=sd, code=code, filled=1, reason=reason,
                        ret=ret, hit_stop=hit_stop))
    return out


def stat(pairs):
    """pairs: list[dict(ret, hit_stop)]，已成交部分。"""
    rets = [x["ret"] for x in pairs if x.get("filled") and x["ret"] is not None]
    if not rets:
        return dict(n=0)
    n = len(rets)
    mean = st.mean(rets)
    sd = st.stdev(rets) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    t = mean / se if se > 0 else 0.0
    stop = sum(1 for x in pairs if x.get("filled") and x.get("hit_stop"))
    return dict(n=n, mean=mean, median=st.median(rets), sd=sd, t=t,
                win=sum(1 for v in rets if v > 0) / n * 100,
                stop=stop / n * 100)


def paired_test(a, b):
    """按 scan_date 配对，比较两组合的当日均值差（a - b），返回配对 t 检验。"""
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
    return dict(n_days=len(common), diff=m,
                t=(m / se) if se > 0 else 0.0,
                sd=sd,
                win_days=sum(1 for v in diffs if v > 0) / len(diffs) * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--topn", type=int, default=3)
    ap.add_argument("--end", default=None)
    ap.add_argument("--pairs", action="store_true",
                    help="额外输出全链 vs 各组合的按日配对检验")
    ap.add_argument("--sample", type=int, default=5,
                    help="交易日采样间隔（1=全量，很慢；5=每 5 个交易日取 1）")
    ap.add_argument("--skip-single", action="store_true",
                    help="跳过「单开 X」组合（T1 子查询极慢，且归因价值低）")
    args = ap.parse_args()

    from datetime import date, timedelta
    end_date = args.end or str(conn_max_date())
    start_date = (date.fromisoformat(end_date)
                  - timedelta(days=int(365.25 * args.years))).isoformat()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 96)
    print(f"short 过滤链大样本 leave-one-out 归因   窗口 {start_date} → {end_date}"
          f"   top_n={args.topn}")
    print("=" * 96)

    # ⚠ 性能：load_prices 是主要开销（每个 code 拉 3 年+缓冲的 OHLC）。
    # 各组合的 WHERE 都是「无过滤」WHERE 的子集 ⇒ 候选票 code 并集可由一次
    # 全量拉取覆盖。先跑完所有 SQL 收集并集，**只拉一次价格**再逐个模拟。
    # （初版每组合各拉一遍，10 组合 → 16 分钟仍未跑完。）
    # 交易日采样（性能）：T1 的逐行相关子查询在 3 年全量上要 40 分钟
    sample_days = None
    if args.sample > 1:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal "
            "WHERE scan_date >= ? AND scan_date <= ? "
            "AND COALESCE(horizon,'short')='short' ORDER BY scan_date",
            (start_date, end_date)).fetchall()]
        sample_days = days[::args.sample]
        print(f"交易日采样：{len(days)} 天 → 每 {args.sample} 天取 1 = "
              f"{len(sample_days)} 天", flush=True)

    presets = {k: v for k, v in PRESETS.items()
               if not (args.skip_single and k.startswith("单开"))}
    print(f"收集各组合候选（{len(presets)} 组）...", flush=True)
    all_picks = {}
    all_codes = set()
    for name, flags in presets.items():
        sql, params = build_query(conn, start_date, end_date, args.topn, flags,
                                  sample_days)
        picks = [dict(r) for r in conn.execute(sql, params).fetchall()]
        all_picks[name] = picks
        all_codes.update(p["code"] for p in picks)
        print(f"  {name:<18} 入选 {len(picks):>6}", flush=True)
    print(f"候选票去重 {len(all_codes)} 只，拉取价格 ...", flush=True)
    cache = load_prices(conn, sorted(all_codes), start_date, end_date)
    print(f"价格缓存 {len(cache)} 只，开始逐笔模拟 ...\n", flush=True)

    results = {}
    for name, flags in presets.items():
        picks = all_picks[name]
        rows = simulate(conn, picks, cache, start_date, end_date)
        results[name] = rows
        s = stat(rows)
        nfill = sum(1 for x in rows if x.get("filled"))
        if s["n"]:
            print(f"{name:<18} 入选 {len(picks):>5}  成交 {nfill:>5}  "
                  f"均值 {s['mean']:+7.3f}%  中位 {s['median']:+7.3f}%  "
                  f"胜率 {s['win']:5.1f}%  止损 {s['stop']:5.1f}%  t={s['t']:+6.2f}")
        else:
            print(f"{name:<18} 入选 {len(picks):>5}  成交 {nfill:>5}  无有效样本")

    if args.pairs:
        print()
        print("=" * 96)
        print("按日配对检验（负 diff = 该组合劣于全链；|t|>2 视为显著）")
        print("=" * 96)
        base = results["全链(线上)"]
        for name in presets:
            if name == "全链(线上)":
                continue
            r = paired_test(results[name], base)
            if r:
                flag = "显著" if abs(r["t"]) > 2 else "不显著"
                print(f"{name:<18} vs 全链  diff {r['diff']:+7.3f}%  "
                      f"t={r['t']:+6.2f} ({flag})  "
                      f"{r['n_days']:>4} 日  优于全链的天数占比 {r['win_days']:5.1f}%")

    conn.close()


def conn_max_date():
    c = sqlite3.connect(DB)
    v = c.execute("SELECT MAX(scan_date) FROM stock_signal").fetchone()[0]
    c.close()
    return v


if __name__ == "__main__":
    main()
