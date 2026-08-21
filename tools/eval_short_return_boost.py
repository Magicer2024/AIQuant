# -*- coding: utf-8 -*-
"""
eval_short_return_boost.py —— 短线收益率提升对比实验（只读）
=============================================================
背景：短线胜率 50%+ 但单笔收益太低（实盘复盘 exit_return 均值 +1.03%，
而持仓期 max_return 均值 +5.16% —— 涨幅没被捕获）。

实验设计（与线上信号链完全同口径）：
  1. 全市场重建 pure_bottom_v2 候选：fs>=15 + 质量过滤 + 趋势闸门
     + 追高否决 + 扩展度否决 + RSI甜区（与 core/sync.py 写库链一致）；
  2. 每日按 fs 取 TopN（默认 8），T+1 开盘买入（+0.1% 滑点），
     卖出含 0.1% 滑点 + 佣金×2 + 印花税；
  3. 出场网格（收盘判定口径，与 evaluate_exit_by_prices 一致）：
     - 固定止盈族：stop × {fixed_tp} × hold
     - 移动止盈族：stop × launch × trail × hold
  4. 入场变体：RSI甜区开关 / 扩展度放宽 / 置信门控 / TopN
  5. 窗口：train 2024-01~2025-12 / test 2026-01~今 / 全窗

指标：n、胜率、净均值、PF、平均盈利/亏损、资金日均收益(年化)。
资金日均 = Σret / Σ持仓天数 × 250，衡量资金周转后的真实收益率。
"""
import os
import sys
import sqlite3
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd

from strategy.rec_filters import (quality_series, trend_gate_series,
                                  chase_filter_series, extension_filter_series)
from strategy.strategies import strategy_bottom_fishing_v2

sys.stdout.reconfigure(encoding="utf-8")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")

SIG = 15.0
SLIP = 0.001
COST = 0.0003 * 2 + 0.001          # 佣金双边 + 印花税
TOPN = 8
START = "2024-01-01"
TEST_START = "2026-01-01"
RECENT_START = "2026-07-01"        # 与实盘复盘窗口对齐的对账窗
EXCLUDE_BOARDS = ("300", "301", "688", "689")   # 用户仅主板可交易


def rsi14(close):
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = gain.rolling(14).mean()
    al = loss.rolling(14).mean()
    rs = ag / al.clip(lower=1e-9)
    return 100.0 - 100.0 / (1.0 + rs)


def simulate(rows, idx, cfg, entry):
    """收盘判定口径出场模拟。cfg: dict(mode,stop,tp,launch,trail,hold)。
    返回 (ret_net, reason, hold_days) 或 None。"""
    hold = cfg["hold"]
    if idx + hold >= len(rows):
        return None
    stop = cfg["stop"]
    mode = cfg["mode"]
    highest = None
    launched = False
    tline = None
    exit_price = None
    reason = None
    for j in range(1, hold + 1):
        r = rows[idx + j]
        hi, cl = r["high"], r["close"]
        if not cl or cl <= 0:
            return None
        if hi and (highest is None or hi > highest):
            highest = hi
        if mode == "trail" and not launched and highest is not None \
                and highest >= entry * (1 + cfg["launch"]):
            launched = True
        if mode == "trail" and launched:
            line = highest * (1 - cfg["trail"])
            if tline is None or line > tline:
                tline = line
        if j == 1:                      # T+1：买入日不可卖
            continue
        if cl <= entry * (1 - stop):
            exit_price, reason = cl, "stop"
            break
        if mode == "trail" and launched and tline is not None and cl <= tline:
            exit_price, reason = cl, "trail"
            break
        if mode == "fixed" and cl >= entry * (1 + cfg["tp"]):
            exit_price, reason = cl, "tp"
            break
        if j == hold:
            exit_price, reason = cl, "expire"
    if exit_price is None:
        return None
    ret = exit_price * (1 - SLIP) / entry - 1 - COST
    return ret, reason, j


class Stats:
    __slots__ = ("n", "sum", "pos", "gross_win", "gross_loss", "days",
                 "aw_n", "al_n", "reasons")

    def __init__(self):
        self.n = 0
        self.sum = 0.0
        self.pos = 0
        self.gross_win = 0.0
        self.gross_loss = 0.0
        self.days = 0
        self.aw_n = 0
        self.al_n = 0
        self.reasons = defaultdict(int)

    def add(self, ret, reason, hold_days):
        self.n += 1
        self.sum += ret
        self.pos += 1 if ret > 0 else 0
        if ret > 0:
            self.gross_win += ret
            self.aw_n += 1
        else:
            self.gross_loss += -ret
            self.al_n += 1
        self.days += hold_days
        self.reasons[reason] += 1

    def row(self):
        if not self.n:
            return None
        mean = self.sum / self.n * 100
        win = self.pos / self.n * 100
        pf = self.gross_win / self.gross_loss if self.gross_loss > 0 else float("inf")
        aw = self.gross_win / self.aw_n * 100 if self.aw_n else 0
        al = self.gross_loss / self.al_n * 100 if self.al_n else 0
        cap_day = self.sum / self.days * 250 * 100 if self.days else 0
        return dict(n=self.n, win=win, mean=mean, pf=pf, aw=aw, al=al,
                    cap_day=cap_day, reasons=dict(self.reasons))


def build_configs():
    cfgs = [("BASE 固定止损-6/止盈+10/持10 (现网近似)",
             dict(mode="fixed", stop=0.06, tp=0.10, hold=10))]
    for tp in (0.12, 0.15, 0.20):
        cfgs.append((f"固定 止损-6/止盈+{int(tp*100)}/持10",
                     dict(mode="fixed", stop=0.06, tp=tp, hold=10)))
    cfgs.append(("无止盈 止损-6/持10", dict(mode="fixed", stop=0.06, tp=9.9, hold=10)))
    cfgs.append(("无止盈 止损-5/持10", dict(mode="fixed", stop=0.05, tp=9.9, hold=10)))
    cfgs.append(("无止盈 止损-4/持10", dict(mode="fixed", stop=0.04, tp=9.9, hold=10)))
    for stop in (0.05, 0.06):
        for launch in (0.04, 0.06, 0.08, 0.10):
            for trail in (0.03, 0.04, 0.05, 0.08):
                for hold in (5, 10):
                    name = (f"移动 sl-{int(stop*100)} 启动+{int(launch*100)} "
                            f"回撤{int(trail*100)} 持{hold}")
                    cfgs.append((name, dict(mode="trail", stop=stop,
                                            launch=launch, trail=trail, hold=hold)))
    return cfgs


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("加载全市场日线…")
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close, volume, amount, pct_change "
        "FROM daily_price ORDER BY code, trade_date").fetchall()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(dict(r))
    ts_map = {r["code"]: (r["total_shares"] if r["total_shares"] and r["total_shares"] > 0
                          else None)
              for r in conn.execute("SELECT code, total_shares FROM stock_info")}
    conn.close()
    print(f"股票 {len(data)} 只")

    # ── 阶段1：逐股重建候选（各过滤分项单独保存，供入场变体复用）──
    CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".cache", "boost_cand.pkl")
    cand = cand_norsi = cand_ext18 = None
    if os.path.exists(CACHE):
        import pickle
        with open(CACHE, "rb") as f:
            cand, cand_norsi, cand_ext18 = pickle.load(f)
        print(f"从缓存加载候选池: base={len(cand)} 去RSI={len(cand_norsi)} "
              f"扩展18%={len(cand_ext18)}")
    if cand is None:
        print("重建 v2 候选池（fs>=15 + 质量 + 闸门 + 追高 + 扩展度 + RSI甜区）…")
        cand = []          # (date, code, fs, pct_above_ma20)
        cand_norsi = []
        cand_ext18 = []
        n_done = 0
        for code, rows in data.items():
            if len(rows) < 60:
                continue
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["trade_date"])
            sub = df[["open", "high", "low", "close", "volume", "amount", "pct_change"]].astype(float)
            try:
                v2 = strategy_bottom_fishing_v2(sub)["BUY_SCORE"]
                fs = v2 * (10.0 / 3.0) * 5.0
                q = quality_series(code, sub, ts_map.get(code))
                gate = trend_gate_series(sub)
                ch = chase_filter_series(sub)
                ext = extension_filter_series(sub)
                rsi = rsi14(sub["close"])
                e3 = (rsi >= 40) & (rsi <= 65)
                ma20 = sub["close"].rolling(20).mean()
                ext18 = sub["close"] <= ma20 * 1.18
                pct_ma20 = (sub["close"] / ma20 - 1) * 100
            except Exception:
                continue
            base = (fs >= SIG) & q & gate & ch
            for date, f in fs[base & ext & e3].items():
                if date.strftime("%Y-%m-%d") >= START:
                    cand.append((date.strftime("%Y-%m-%d"), code, float(f),
                                 float(pct_ma20.loc[date]) if pd.notna(pct_ma20.get(date)) else 0.0))
            for date, f in fs[base & ext].items():          # 去 RSI 甜区
                if date.strftime("%Y-%m-%d") >= START:
                    cand_norsi.append((date.strftime("%Y-%m-%d"), code, float(f),
                                       float(pct_ma20.loc[date]) if pd.notna(pct_ma20.get(date)) else 0.0))
            for date, f in fs[base & ext18 & e3].items():   # 扩展度放宽 18%
                if date.strftime("%Y-%m-%d") >= START:
                    cand_ext18.append((date.strftime("%Y-%m-%d"), code, float(f),
                                       float(pct_ma20.loc[date]) if pd.notna(pct_ma20.get(date)) else 0.0))
            n_done += 1
            if n_done % 500 == 0:
                print(f"  已处理 {n_done} 只…")
        print(f"候选: base={len(cand)}  去RSI={len(cand_norsi)}  扩展18%={len(cand_ext18)}")
        import pickle
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "wb") as f:
            pickle.dump((cand, cand_norsi, cand_ext18), f)
        print(f"候选池已缓存至 {CACHE}")

    cfgs = build_configs()
    REPORT = []

    def emit(s=""):
        print(s)
        REPORT.append(s)

    def run_pool(cand_list, topn, label, conf_gate=0.0, mainboard=True, rank_by="fs"):
        per_day = defaultdict(list)
        for item in cand_list:
            d, code, f = item[0], item[1], item[2]
            ext_pct = item[3] if len(item) > 3 else 0.0
            if mainboard and code.startswith(EXCLUDE_BOARDS):
                continue
            if f >= conf_gate:
                per_day[d].append((code, f, ext_pct))
        results = {}
        for name, cfg in cfgs:
            results[name] = {"full": Stats(), "train": Stats(), "test": Stats(),
                             "recent": Stats()}
        idx_cache = {}
        for d in sorted(per_day.keys()):
            if rank_by == "ext":
                # 现网复盘口径：低扩展度优先，再按融合分
                picks = sorted(per_day[d], key=lambda x: (x[2], -x[1], x[0]))[:topn]
            else:
                picks = sorted(per_day[d], key=lambda x: (-x[1], x[0]))[:topn]
            for code, _f, _e in picks:
                rows = data.get(code)
                if not rows:
                    continue
                imap = idx_cache.get(code)
                if imap is None:
                    imap = {r["trade_date"]: i for i, r in enumerate(rows)}
                    imap_keys = sorted(imap)
                    idx_cache[code] = (imap, imap_keys)
                imap, imap_keys = idx_cache[code]
                idx = imap.get(d)
                if idx is None:
                    import bisect
                    pos = bisect.bisect_left(imap_keys, d)
                    idx = imap[imap_keys[pos]] if pos < len(imap_keys) else -1
                if idx < 0 or idx + 1 >= len(rows):
                    continue
                entry = rows[idx + 1]["open"] * (1 + SLIP)
                if not entry or entry <= 0:
                    continue
                for name, cfg in cfgs:
                    out = simulate(rows, idx, cfg, entry)
                    if out is None:
                        continue
                    ret, reason, hd = out
                    results[name]["full"].add(ret, reason, hd)
                    bucket = "test" if d >= TEST_START else "train"
                    results[name][bucket].add(ret, reason, hd)
                    if d >= RECENT_START:
                        results[name]["recent"].add(ret, reason, hd)
        emit(f"\n{'='*118}")
        emit(f"[{label}]  TopN={topn} 主板only={mainboard} 置信门控={conf_gate}")
        emit(f"{'='*118}")
        hdr = (f"  {'配置':<34} {'窗口':<6} {'笔数':>5} {'胜率':>7} {'净均值':>8} "
               f"{'PF':>6} {'均盈':>7} {'均亏':>7} {'资金年化':>9}")
        emit(hdr)
        rows_out = []
        for name, _cfg in cfgs:
            for wk in ("full", "train", "test", "recent"):
                r = results[name][wk].row()
                if r and r["n"] >= 10:
                    rows_out.append((name, wk, r))
        # 先全量打印 base 与 train/test 拆分，其余按全窗均值排序打印
        shown = set()
        for name, wk, r in rows_out:
            if wk == "full":
                emit(f"  {name:<34} {'全窗':<6} {r['n']:>5} {r['win']:>6.1f}% "
                     f"{r['mean']:>+7.2f}% {r['pf']:>6.2f} {r['aw']:>+6.2f}% "
                     f"{-r['al']:>+6.2f}% {r['cap_day']:>+8.1f}%")
                shown.add(name)
        # 近期窗（2026-07 起，对账用）BASE 行
        for name, wk, r in rows_out:
            if wk == "recent" and name.startswith("BASE"):
                emit(f"  {name:<34} {'近期':<6} {r['n']:>5} {r['win']:>6.1f}% "
                     f"{r['mean']:>+7.2f}% {r['pf']:>6.2f} {r['aw']:>+6.2f}% "
                     f"{-r['al']:>+6.2f}% {r['cap_day']:>+8.1f}%")
        # test 窗口 Top12（按净均值）
        test_rows = [(n, r) for n, wk, r in rows_out if wk == "test"]
        test_rows.sort(key=lambda x: -x[1]["mean"])
        emit(f"\n  -- test 窗 (2026-01起) 按净均值 Top12 --")
        for name, r in test_rows[:12]:
            tr = next((rr for nn, wk, rr in rows_out if nn == name and wk == "train"), None)
            tr_s = f"train {tr['mean']:+.2f}%/{tr['win']:.0f}%" if tr else "train n/a"
            emit(f"  {name:<34} n={r['n']:>4} 胜率{r['win']:>5.1f}% 均值{r['mean']:>+6.2f}% "
                 f"PF {r['pf']:>4.2f} 资金年化{r['cap_day']:>+6.1f}% | {tr_s}")
        # 近期窗 Top8（2026-07 起）
        recent_rows = [(n, r) for n, wk, r in rows_out if wk == "recent"]
        recent_rows.sort(key=lambda x: -x[1]["mean"])
        emit(f"\n  -- 近期窗 (2026-07起, 对账窗) 按净均值 Top8 --")
        for name, r in recent_rows[:8]:
            emit(f"  {name:<34} n={r['n']:>4} 胜率{r['win']:>5.1f}% 均值{r['mean']:>+6.2f}% "
                 f"PF {r['pf']:>4.2f}")
        return results

    # ── 主实验：现网复盘口径（conf>=22 + 低扩展度排序 + Top4，与 recommend_outcome 一致）──
    run_pool(cand, 4, "现网口径 conf22+低扩展排序 Top4", conf_gate=22.0, rank_by="ext")
    # ── 对照：其余选股口径 ──
    run_pool(cand, 8, "融合分排序 Top8 fs>=15")
    run_pool(cand, 4, "融合分排序 Top4 fs>=22", conf_gate=22.0)
    run_pool(cand, 8, "融合分排序 Top8 fs>=22", conf_gate=22.0)
    # ── 入场放宽变体（均按现网复盘口径选股）──
    run_pool(cand_norsi, 4, "现网口径-去RSI甜区 Top4", conf_gate=22.0, rank_by="ext")
    run_pool(cand_ext18, 4, "现网口径-扩展18% Top4", conf_gate=22.0, rank_by="ext")

    OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       ".cache", "boost_results.txt")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    print(f"\n报告已写入 {OUT}")
    print("done.")


if __name__ == "__main__":
    main()
