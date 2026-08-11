"""
tools/calibrate_short_topn.py —— 短线推荐 top-N 数量校准回测（Phase 1，只读不写库）

目标：验证「短线推荐每天 0~4 个」能否达到 周2% / 月10% / 年50% 的收益预期。
回答四个问题：
  Q1 排序选择力：fusion_score 排序后 rank 1-4 / 5-8 / 9-16 / 17+ 的 T+1/T+3 OC 收益
                 ——「砍到 4 个」是否正期望、第 5~8 名是否拖累
  Q2 分数门槛  ：fusion_score 分档收益曲线 → 「宁缺毋滥」门槛候选值
  Q3 推荐组合  ：G8(现状) vs G4 vs G2 vs G4+门槛 的周/月收益与 2%/10% 达标率
  Q4 引擎组合  ：复用 backtest.engine 组合口径（PF/年化/回撤），作为资金效率参考
  Q5 大盘冷暖  ：沪深300 MA20 斜率分 warm/cold，看冷市下 top-4 是否应减到 0

口径说明（与 tools/diag_short_reco.py / tools/eval_fusion_mode.py 对齐）：
  OC = 信号日次日开盘买入（现实可买），出场 = 止损/止盈/最长持仓（收盘确认，与引擎一致）
  费率 = 佣金万三双向 + 印花税千一卖出 + 滑点千一（引擎默认值）
  信号源 = stock_signal horizon='short'（线上推荐 = 当日全部 short 信号按 fusion_score top-N）
  窗口 = 2024-01 ~ 2026-08（regime 指数自 2024-01-02 起）
"""
import os
import sys
import sqlite3
from datetime import datetime
from collections import defaultdict

import pandas as pd

# 项目根入 sys.path（tests 约定一致），供 Q4 复用 backtest.engine
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")

START = "2024-01-01"
END = "2026-08-11"

# 出场参数（与 config.strategy_params TUNABLE_PARAMS 当前值一致）
STOP_LOSS = -0.06      # 短线止损 -6%
TAKE_PROFIT = 0.10     # 短线止盈 +10%
MAX_HOLD = 3           # 最长持仓 3 个交易日
# 费率（与 backtest/engine.py 默认一致）
SLIPPAGE = 0.001
COMMISSION = 0.0003
STAMP = 0.001

regime_of: dict = {}   # 交易日 -> warm/cold/?


def load_signals(conn, strategies=("短线融合", "隔日动量"),
                 main_board_only=True):
    """现网 short 信号（2024 起），含当日 rank。

    strategies：只取现网写库策略（默认排除旧引擎'历史短线'信号，避免混代污染）；
    main_board_only：与 routes/investor.py 线上推荐同口径（小资金仅推主板）。
    """
    sql = """
        SELECT scan_date, code, name, fusion_score
        FROM stock_signal
        WHERE horizon = 'short'
          AND scan_date BETWEEN ? AND ?
          AND fusion_score IS NOT NULL
    """
    args: list = [START, END]
    if strategies:
        sql += " AND strategy IN ({})".format(",".join("?" * len(strategies)))
        args += list(strategies)
    if main_board_only:
        from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES
        if MAIN_BOARD_ONLY:
            sql += "".join(f" AND code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
    sql += " ORDER BY scan_date ASC, fusion_score DESC"
    rows = conn.execute(sql, args).fetchall()
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["scan_date"]].append(dict(r))
    # 打 rank
    for d, lst in by_date.items():
        for i, r in enumerate(lst):
            r["rank"] = i + 1
    return by_date


def load_prices(conn, codes):
    """有信号股票的全量日线（open/high/low/close）。"""
    px = {}
    # 分块 IN 查询避免 SQL 变量数超限
    codes = list(codes)
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT code, trade_date, open, high, low, close, volume
            FROM daily_price
            WHERE code IN ({ph})
            ORDER BY code, trade_date
            """, chunk).fetchall()
        for r in rows:
            px.setdefault(r["code"], []).append(dict(r))
    return px


def rsi14(closes):
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g = sum(gains[:14]) / 14.0
    avg_l = sum(losses[:14]) / 14.0
    for i in range(14, len(gains)):
        avg_g = (avg_g * 13 + gains[i]) / 14.0
        avg_l = (avg_l * 13 + losses[i]) / 14.0
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))


def find_idx(rows, date):
    for i, r in enumerate(rows):
        if r["trade_date"] >= date:
            return i
    return -1


def trade_return(rows, sig_idx, stop=STOP_LOSS, tp=TAKE_PROFIT,
                 max_hold=MAX_HOLD, mode="managed"):
    """单笔交易收益（%）：次日开盘买入 → 出场。

    mode="managed"：止损/止盈/最长持仓收盘卖出（与 backtest.engine 一致，收盘确认）；
    mode="hold"   ：纯持有 max_hold 天，第 max_hold 日收盘卖出（无止损止盈，
                     用于 rank/分档的排序选择力评估，与 diag_short_reco 的 OC 口径对齐）。
    返回 (ret_pct, exit_date, exit_reason) 或 None（无后续行情）。
    """
    if sig_idx + 1 >= len(rows):
        return None
    nxt = rows[sig_idx + 1]
    entry = nxt["open"] * (1 + SLIPPAGE)
    if not entry or entry <= 0:
        return None
    if mode == "hold":
        j = sig_idx + max_hold
        if j >= len(rows):
            return None
        close = rows[j]["close"]
        if not close or close <= 0:
            return None
        ret = close * (1 - SLIPPAGE) / entry - 1
        return (ret * 100, rows[j]["trade_date"], "hold")
    for off in range(1, max_hold + 1):
        j = sig_idx + off
        if j >= len(rows):
            return None
        close = rows[j]["close"]
        if not close or close <= 0:
            continue
        ret = close * (1 - SLIPPAGE) / entry - 1
        if ret <= stop:
            return (ret * 100, rows[j]["trade_date"], "stop_loss")
        if ret >= tp:
            return (ret * 100, rows[j]["trade_date"], "take_profit")
        if off == max_hold:
            return (ret * 100, rows[j]["trade_date"], "max_hold")
    return None


def net_of_cost(ret_pct):
    """毛收益 → 净收益（佣金双向 + 印花税卖出 + 滑点已在价格里）"""
    return ret_pct - (COMMISSION * 2 + STAMP) * 100


def analyze_q1_q2(by_date, px, regime_of):
    """Q1 rank 分组 / Q2 分档 的 T+1 / T+3 OC 收益（单笔口径，含费）。"""
    buckets = {
        "rank": {"1-4": [], "5-8": [], "9-16": [], "17+": []},
        "fs": {"<18": [], "18-20": [], "20-22": [], "22-25": [],
               "25-28": [], "28-32": [], "32+": []},
    }
    regime_buckets = {"rank": defaultdict(list)}
    def add_bucket(store, key, oc1, oc3, reg):
        store[key].append((oc1, oc3, reg))
    for d in sorted(by_date.keys()):
        reg = regime_of.get(d, "?")
        for r in by_date[d]:
            rows = px.get(r["code"])
            if not rows:
                continue
            idx = find_idx(rows, d)
            if idx < 0 or idx + 1 >= len(rows):
                continue
            oc1 = trade_return(rows, idx, max_hold=1, mode="hold")
            oc3 = trade_return(rows, idx, max_hold=3, mode="hold")
            if oc1 is None:
                continue
            rk = r["rank"]
            rkey = "1-4" if rk <= 4 else ("5-8" if rk <= 8 else
                   ("9-16" if rk <= 16 else "17+"))
            add_bucket(buckets["rank"], rkey, oc1[0], oc3[0] if oc3 else None, reg)
            add_bucket(regime_buckets["rank"], f"{reg}|{rkey}",
                       oc1[0], oc3[0] if oc3 else None, reg)
            fs = r["fusion_score"]
            fkey = ("<18" if fs < 18 else
                    "18-20" if fs < 20 else
                    "20-22" if fs < 22 else
                    "22-25" if fs < 25 else
                    "25-28" if fs < 28 else
                    "28-32" if fs < 32 else "32+")
            add_bucket(buckets["fs"], fkey, oc1[0], oc3[0] if oc3 else None, reg)
    return buckets, regime_buckets


def report_bucket(store, title):
    print(f"\n--- {title} ---")
    print(f"  {'分组':<8}{'n':>7}{'T+1胜率':>9}{'T+1均值':>9}{'T+3胜率':>9}"
          f"{'T+3均值':>9}{'净均值(含费)':>12}")
    for key, lst in store.items():
        if not lst:
            continue
        o1 = [x[0] for x in lst]
        o3 = [x[1] for x in lst if x[1] is not None]
        win1 = sum(1 for v in o1 if v > 0) / len(o1) * 100
        win3 = sum(1 for v in o3 if v > 0) / len(o3) * 100 if o3 else 0
        net1 = sum(net_of_cost(v) for v in o1) / len(o1)
        print(f"  {key:<8}{len(o1):>7}{win1:>8.1f}%{sum(o1)/len(o1):>+8.3f}%"
              f"{win3:>8.1f}%{sum(o3)/len(o3):>+8.3f}%{net1:>+11.3f}%")


def weekly_monthly(per_trade, scan_date_col, ret_col):
    """按推荐日 ISO 周 / 年月聚合等权收益，输出分布与达标率。"""
    wk = defaultdict(list)
    mo = defaultdict(list)
    for t in per_trade:
        dt = datetime.strptime(str(t[scan_date_col])[:10], "%Y-%m-%d")
        iso = dt.isocalendar()
        wk[(iso[0], iso[1])].append(t[ret_col])
        mo[(dt.year, dt.month)].append(t[ret_col])
    def summ(grp, label, target):
        keys = sorted(grp.keys())
        vals = [sum(grp[k]) / len(grp[k]) for k in keys]
        if not vals:
            return
        n = len(vals)
        hit = sum(1 for v in vals if v >= target) / n * 100
        vals_sorted = sorted(vals)
        med = vals_sorted[n // 2]
        cum = 1.0
        for v in vals:
            cum *= (1 + v / 100)
        print(f"    {label}: {n} 期 | 均值 {sum(vals)/n:+.2f}% | 中位 {med:+.2f}% "
              f"| 达标率(≥{target}%) {hit:.0f}% | 最好 {max(vals):+.2f}% "
              f"| 最差 {min(vals):+.2f}% | 累计复利 {cum*100-100:+.1f}%")
        return cum
    print(f"  [周收益目标 2% / 月收益目标 10%]")
    summ(wk, "按周", 2.0)
    summ(mo, "按月", 10.0)


def analyze_q3(by_date, px, groups):
    """Q3 推荐组合等权：每天 top-N（可选分数门槛），组内等权，周/月聚合。"""
    for label, n, fs_min in groups:
        per_trade = []
        for d in sorted(by_date.keys()):
            lst = sorted(by_date[d], key=lambda r: (-r["fusion_score"], r["code"]))
            picks = [r for r in lst if (fs_min is None or r["fusion_score"] >= fs_min)][:n]
            for r in picks:
                rows = px.get(r["code"])
                if not rows:
                    continue
                idx = find_idx(rows, d)
                if idx < 0:
                    continue
                t = trade_return(rows, idx)
                if t is None:
                    continue
                per_trade.append({"date": d, "ret": net_of_cost(t[0])})
        print(f"\n=== Q3 推荐组合等权: {label}（每日最多 {n} 只"
              + (f"，fusion_score≥{fs_min}" if fs_min else "") + "）===")
        if not per_trade:
            print("    无样本")
            continue
        all_ret = [t["ret"] for t in per_trade]
        win = sum(1 for v in all_ret if v > 0) / len(all_ret) * 100
        n_days = len({t["date"] for t in per_trade})
        print(f"    总样本 {len(all_ret)} 笔 / {n_days} 个推荐日 | 单笔胜率 {win:.1f}% "
              f"| 单笔均值 {sum(all_ret)/len(all_ret):+.3f}%")
        weekly_monthly(per_trade, "date", "ret")


def analyze_q4(by_date, codes, conn):
    """Q4 引擎组合口径（复用 VisualBacktestEngine，含停牌补齐）。"""
    from backtest.engine import BacktestParams, VisualBacktestEngine, _add_indicators
    print("\n=== Q4 引擎组合口径（资金效率参考，含费/滑点/持仓上限）===")
    # 价格面板：信号股 2023-06 起（留指标 warmup）
    px_all = load_prices(conn, codes)
    px_by_code = {}
    for code, rows in px_all.items():
        df = pd.DataFrame(rows)
        df["_dt"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("_dt").sort_index()
        px_by_code[code] = df
    calendar = pd.DatetimeIndex(sorted({d for df in px_by_code.values() for d in df.index}))
    price_cols = ["open", "high", "low", "close", "volume"]
    stock_data, info_map = {}, {}
    for code, df in px_by_code.items():
        gk = df[price_cols].reindex(calendar[calendar >= df.index[0]])
        gk[price_cols] = gk[price_cols].ffill()
        stock_data[code] = _add_indicators(gk[price_cols])
        info_map[code] = {"code": code, "name": str(code)}

    # 信号矩阵：signal 日 True；fusion_score 用信号日值
    sig_days = defaultdict(set)
    score_by_day = defaultdict(dict)
    for d, lst in by_date.items():
        for r in lst:
            sig_days[r["code"]].add(pd.Timestamp(d))
            score_by_day[d][r["code"]] = r["fusion_score"]
    signals = {}
    for code, df in stock_data.items():
        s = pd.Series(False, index=df.index)
        for d in sig_days.get(code, set()):
            if d in df.index:
                s.loc[d] = True
        signals[code] = s
        fs = pd.Series(0.0, index=df.index)
        for d, m in score_by_day.items():
            if code in m:
                td = pd.Timestamp(d)
                if td in df.index:
                    fs.loc[td] = m[code]
        stock_data[code]["fusion_score"] = fs

    for label, n in [("G8 现状top-8", 8), ("G4 方案top-4", 4), ("G2 极端top-2", 2)]:
        params = BacktestParams(
            start_date=START, end_date=END, initial_cash=1_000_000.0,
            max_holdings=4 * n, max_buy_per_day=n, buy_timing="next_day_open",
            take_profit_pct=TAKE_PROFIT, stop_loss_pct=STOP_LOSS,
            max_hold_days=MAX_HOLD, exclude_st=True, exclude_kcb=True,
            exclude_cyb=True,
        )
        engine = VisualBacktestEngine(params)
        r = engine._run_portfolio(stock_data, signals, info_map)
        m = engine._compute_metrics(r["equity_curve"], r["trades"], r["final_assets"])
        print(f"  {label:<14} 年化 {m['annual_return']*100:+6.2f}% | 总收益 "
              f"{m['total_return']*100:+7.2f}% | 最大回撤 {m['max_drawdown']*100:6.1f}%"
              f" | PF {m['profit_factor']:5.2f} | 胜率 {m['win_rate']*100:5.1f}%"
              f" | {m['total_trades']} 笔")


def analyze_q5(regime_buckets, by_date):
    """Q5 大盘冷暖下 top-4 vs top-8 的表现。"""
    print("\n=== Q5 大盘冷暖（沪深300 MA20 斜率）===")
    rk = regime_buckets["rank"]
    for reg in ("warm", "cold", "?"):
        for rk_key in ("1-4", "5-8"):
            lst = rk.get(f"{reg}|{rk_key}", [])
            if not lst:
                continue
            o1 = [x[0] for x in lst]
            win = sum(1 for v in o1 if v > 0) / len(o1) * 100
            net1 = sum(net_of_cost(v) for v in o1) / len(o1)
            print(f"  {reg:<5} rank {rk_key:<4} n={len(o1):>6} | T+1胜率 {win:5.1f}% "
                  f"| T+1均值 {sum(o1)/len(o1):+.3f}% | 净均值 {net1:+.3f}%")
    # 冷市每日信号数（top-4 是否经常凑不满 4 个）
    cold_days = [d for d, reg in regime_of.items() if reg == "cold"]
    if cold_days:
        cnts = [len(by_date[d]) for d in cold_days]
        lt4 = sum(1 for c in cnts if c <= 4) / len(cnts) * 100
        print(f"  冷市交易日 {len(cnts)} 个 | 当日信号≤4个的占比 {lt4:.0f}% "
              f"| 冷市日均信号 {sum(cnts)/len(cnts):.0f} 个")


def analyze_q6(conn, px):
    """Q6 真实线上推荐验证：recommend_outcome = 当前 stock_signal 每日每组 Top8
    （主板过滤、fusion_score 降序）。用次日开盘重算收益（与 Q1-Q5 同口径，
    不用表内以 buy_price 为基准的乐观口径），rank 1-4 vs 5-8 + 周/月收益。
    """
    print("\n" + "=" * 76)
    print("Q6 真实线上推荐验证（recommend_outcome short，"
          "2026-05-25 ~ 2026-08-11，每日 Top8 主板）")
    print("=" * 76)
    rows = conn.execute(
        "SELECT code, scan_date, fusion_score, stop_loss, take_profit "
        "FROM recommend_outcome WHERE horizon='short' "
        "ORDER BY scan_date, fusion_score DESC").fetchall()
    per_day = defaultdict(list)
    for r in rows:
        per_day[r["scan_date"]].append(dict(r))
    rank_buckets = {"1-4": [], "5-8": []}
    per_trade = []
    n_days = 0
    for d in sorted(per_day.keys()):
        lst = per_day[d]
        if len(lst) < 8:
            continue
        n_days += 1
        for i, r in enumerate(lst[:8]):
            rows_px = px.get(r["code"])
            if not rows_px:
                continue
            idx = find_idx(rows_px, d)
            if idx < 0:
                continue
            t1 = trade_return(rows_px, idx, max_hold=1, mode="hold")
            ex = trade_return(rows_px, idx, max_hold=3, mode="managed")
            rk = "1-4" if i < 4 else "5-8"
            if t1:
                rank_buckets[rk].append(t1[0])
            if ex:
                per_trade.append({"date": d, "ret": net_of_cost(ex[0])})
    print(f"  有效推荐日 {n_days} 个（每天 Top8 完整）")
    print(f"  {'分组':<8}{'n':>6}{'T+1胜率':>9}{'T+1均值':>9}{'净均值':>9}")
    for rk, lst in rank_buckets.items():
        if not lst:
            continue
        win = sum(1 for v in lst if v > 0) / len(lst) * 100
        net = sum(net_of_cost(v) for v in lst) / len(lst)
        print(f"  {rk:<8}{len(lst):>6}{win:>8.1f}%{sum(lst)/len(lst):>+8.3f}%"
              f"{net:>+8.3f}%")
    print("  [管理出场（止损/止盈/3天）周/月收益]")
    weekly_monthly(per_trade, "date", "ret")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    by_date = load_signals(conn)
    print(f"[数据] 窗口 {START} ~ {END}：{sum(len(v) for v in by_date.values())} 条信号，"
          f"{len(by_date)} 个推荐日")
    codes = {r["code"] for lst in by_date.values() for r in lst}
    print(f"[数据] 涉及股票 {len(codes)} 只，加载日线…")
    px = load_prices(conn, codes)
    print(f"[数据] 日线加载完成 {sum(len(v) for v in px.values())} 行\n")

    # regime：沪深300 MA20 斜率
    global regime_of
    regime_of = {}
    try:
        idx = conn.execute(
            "SELECT trade_date, close FROM index_daily WHERE code='000300' "
            "ORDER BY trade_date").fetchall()
        idd = pd.DataFrame([dict(r) for r in idx]).set_index("trade_date")["close"]
        ma20 = idd.rolling(20).mean()
        slope = ma20 >= ma20.shift(5)
        for d in by_date.keys():
            try:
                regime_of[d] = "warm" if bool(slope.loc[d]) else "cold"
            except KeyError:
                regime_of[d] = "?"
    except Exception as e:
        print(f"[warn] regime 计算失败: {e}")

    buckets, regime_buckets = analyze_q1_q2(by_date, px, regime_of)
    print("=" * 76)
    print("Q1 排序选择力（单笔 OC 口径，含费净收益列）")
    print("=" * 76)
    report_bucket(buckets["rank"], "按当日 fusion_score 排名分组")
    print("\n" + "=" * 76)
    print("Q2 fusion_score 分档（宁缺毋滥门槛候选）")
    print("=" * 76)
    report_bucket(buckets["fs"], "按 fusion_score 分档")

    print("\n" + "=" * 76)
    print("Q3 推荐组合等权：周/月收益 vs 目标（周≥2%、月≥10%）")
    print("=" * 76)
    analyze_q3(by_date, px, [
        ("G8 现状(每天8只)", 8, None),
        ("G4 方案(每天最多4只)", 4, None),
        ("G2 极端(每天最多2只)", 2, None),
        ("G4+门槛25(宁缺毋滥)", 4, 25.0),
    ])

    print("\n" + "=" * 76)
    analyze_q5(regime_buckets, by_date)

    print("\n" + "=" * 76)
    analyze_q6(conn, px)

    print("\n" + "=" * 76)
    analyze_q4(by_date, codes, conn)

    conn.close()
    print("\n[done] 只读回测完成，未写任何库。")


if __name__ == "__main__":
    main()
