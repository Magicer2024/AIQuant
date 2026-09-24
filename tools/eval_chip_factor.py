"""筹码峰（持仓成本分布）因子对短线选股是否有效的诊断工具 —— 只读，不改任何生产数据。

回答三个问题
------------
Q1 筹码因子在短线候选池里有没有**单调的选择力**？（分档看平均单笔收益/胜率）
Q2 它是不是 `pct_above_ma20`（扩展度）的**马甲**？—— 控制 ext 分桶后桶内是否仍单调。
   这是最关键的一问：若控制 ext 后消失，则筹码峰对现有策略无增量，不值得上。
Q3 收益差是**单月依赖**还是全期稳健？（项目铁律：必须逐月剔除压力测试）

口径
----
- 信号池 = `logs/_short_observe_sim.pkl`（生产链路候选池，出场用线上同口径
  `_short_exit_sim` 状态机，入场=回踩确认）——**直接复用，不重写数学**；
- 指标函数 `metrics` / `portfolio` 从 `tools/eval_short_observe_rule.py` 动态加载，
  保证与既有结论同口径可比；
- 筹码因子按 (code, scan_date) 对齐，只用 scan_date 当日及以前的价量 → **无未来函数**；
- ⚠ `ret` 单位是**百分数**（铁律 5）；⚠ 单笔收益率不可相加（铁律 1）→ 本工具只看
  平均每笔 / 胜率 / PF，不输出 `sum(单笔)`。

用法
----
    python tools/eval_chip_factor.py --rebuild          # 首次：算筹码因子并缓存
    python tools/eval_chip_factor.py                    # 复用缓存出报告
    python tools/eval_chip_factor.py --decay 0.35       # 扫衰减系数（关键自由参数）
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.db import get_conn                                                    # noqa: E402
from strategy.chip import (chip_factors, chip_matrix, turnover_series,          # noqa: E402
                           DEFAULT_DECAY, DEFAULT_DECAY_FLOOR, N_BINS)

LOGS = os.path.join(_ROOT, "logs")
SIM_CACHE = os.path.join(LOGS, "_short_observe_sim.pkl")
PRICE_START = "2021-06-01"          # 筹码 warmup：记忆期 ≤ ~330 日，留足 1.5 年
FACTORS = ["winner", "conc", "pressure", "cost_ratio", "lock", "center_shift"]


def _load_harness():
    """动态加载既有评估工具，复用其 metrics/portfolio（口径一致，不重写数学）。"""
    path = os.path.join(_ROOT, "tools", "eval_short_observe_rule.py")
    spec = importlib.util.spec_from_file_location("_harness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────── 1. 筹码因子计算（多进程） ─────────────────────────

def _chip_worker(payload):
    """子进程工作单元：单只股票原始数组 → 筹码因子矩阵。

    ⚠ decay / decay_floor 必须随 payload 传入：Windows spawn 下子进程会重新 import
    本模块，main() 里的 global 赋值**不会**继承，否则多进程跑出来的是默认参数结果。
    """
    code, d, hi, lo, cl, to, decay, decay_floor = payload
    df = pd.DataFrame({"high": hi, "low": lo, "close": cl, "turnover": to})
    C = chip_matrix(df["high"], df["low"], df["close"],
                    turnover_series(df, None), decay=decay, decay_floor=decay_floor)
    f = chip_factors(C, cl)
    return code, d, f[FACTORS].to_numpy(dtype=np.float32)


_DECAY = DEFAULT_DECAY
_DECAY_FLOOR = DEFAULT_DECAY_FLOOR
# 换手率口径：auto=旧口径（优先库 turnover）/ calc=volume/circ_shares（修正）/ db=只用库值
_TURNOVER_MODE = "auto"


def build_factors(codes: list[str], workers: int) -> pd.DataFrame:
    """返回 DataFrame，index=(code, trade_date)，列为 FACTORS。"""
    t0 = time.time()
    with get_conn() as conn:
        px = pd.read_sql_query(
            "SELECT code, trade_date, high, low, close, volume, turnover "
            "FROM daily_price WHERE trade_date >= ? ORDER BY code, trade_date",
            conn, params=(PRICE_START,))
        sh = pd.read_sql_query("SELECT code, circ_shares FROM stock_info", conn)
    px = px[px["code"].isin(set(codes))]
    shares = dict(zip(sh["code"], sh["circ_shares"]))
    print(f"[1/3] 行情 {len(px):,} 行 / {px['code'].nunique():,} 只 "
          f"（{PRICE_START} 起，用于筹码 warmup）  耗时 {time.time() - t0:.1f}s")

    # 换手率口径（2026-09-15 取证后修正）
    # 取证结论（tools/diag_price_data_quality.py）：
    #   · `circ_shares` 实为**流通股本**（分母正确，与 total_shares 是同一列的副本）；
    #   · `volume` / `amount` 可信（2025 年起复权影响 <2% 的行里，vwap 100% 落在当日区间内）；
    #   · `daily_price.turnover` **不可信** —— 约 38% 的行被低估 6.6~194 倍。
    # 旧口径优先采用库值 ⇒ 那些行换手率被压低 1~2 个数量级 ⇒ 筹码几乎不衰减、分布摊平
    # ⇒ `conc` 被系统性推高。故新增 `--turnover calc` 做修正口径的 A/B。
    #   auto（旧口径）：库值 >0 用之，否则 volume/circ_shares
    #   calc（修正）  ：一律 volume/circ_shares
    #   db  （对照）  ：只用库值，缺失落中位数
    vol = px["volume"].to_numpy(dtype=float)
    cs = px["code"].map(shares).to_numpy(dtype=float)
    calc = np.divide(vol, cs, out=np.full(len(px), np.nan), where=(cs > 0) & np.isfinite(cs))
    db_to = px["turnover"].to_numpy(dtype=float) / 100.0
    if _TURNOVER_MODE == "calc":
        to = calc
    elif _TURNOVER_MODE == "db":
        to = np.where(np.isfinite(db_to) & (db_to > 0), db_to, np.nan)
    else:
        to = np.where(np.isfinite(db_to) & (db_to > 0), db_to, calc)
    med = np.nanmedian(to)
    to = np.where(np.isfinite(to) & (to > 0), to, med if np.isfinite(med) else 0.02)
    px = px.assign(_to=np.clip(to, 0.0, 1.0))

    d_int = pd.to_datetime(px["trade_date"]).values.astype("datetime64[D]").astype(np.int64)
    px = px.assign(_d=d_int)

    payloads = []
    for code, g in px.groupby("code", sort=False):
        payloads.append((code, g["_d"].to_numpy(),
                         g["high"].to_numpy(float), g["low"].to_numpy(float),
                         g["close"].to_numpy(float), g["_to"].to_numpy(float),
                         _DECAY, _DECAY_FLOOR))
    del px

    print(f"[2/3] 计算筹码分布（{len(payloads):,} 只 × {workers} 进程）…")
    t0 = time.time()
    parts = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, (code, d, arr) in enumerate(
                ex.map(_chip_worker, payloads, chunksize=8), 1):
            parts.append(pd.DataFrame(arr, columns=FACTORS).assign(
                code=code, _d=d))
            if i % 500 == 0:
                print(f"      {i:,}/{len(payloads):,}  ({time.time() - t0:.0f}s)")
    out = pd.concat(parts, ignore_index=True)
    print(f"      完成，{len(out):,} 行，耗时 {time.time() - t0:.0f}s")

    # 日期 int64 → YYYY-MM-DD
    out["trade_date"] = pd.to_datetime(out["_d"], unit="D").dt.strftime("%Y-%m-%d")
    out = out.drop(columns=["_d"]).set_index(["code", "trade_date"])
    return out


# ───────────────────────── 2. 报告 ─────────────────────────

def _buckets(df: pd.DataFrame, col: str, n: int = 5):
    """按分位切成 n 档，返回 [(label, sub_df), ...]（低→高）。"""
    v = pd.to_numeric(df[col], errors="coerce")
    ok = df[v.notna()].copy()
    if len(ok) < n * 20:
        return []
    try:
        ok["_b"] = pd.qcut(pd.to_numeric(ok[col], errors="coerce"), n,
                           labels=False, duplicates="drop")
    except ValueError:
        return []
    out = []
    for b, g in ok.groupby("_b"):
        lo, hi = g[col].min(), g[col].max()
        out.append((f"Q{int(b) + 1} [{lo:.3f},{hi:.3f}]", g))
    return out


def report_single(H, ev, title="单变量分档（逐笔口径 · 筹码因子）"):
    """Q1：单变量分档，看是否单调。"""
    print(f"\n########## {title} ##########")
    rows = [H.metrics(ev, "全池基线")]
    for c in FACTORS:
        for lb, g in _buckets(ev, c):
            m = H.metrics(g, f"{c:<13}{lb}")
            rows.append(m)
    H.show(rows, f"{title}  (n={len(ev):,})")
    return rows


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """秩相关。不用 scipy.stats.spearmanr（项目未装 scipy）——
    Spearman 就是对秩做 Pearson，rank 后直接 corr 等价且无新依赖。"""
    ra, rb = a.rank(), b.rank()
    return float(ra.corr(rb))


def report_corr(ev):
    """因子间相关 + 与 ext 的秩相关（判断是否马甲）。"""
    print("\n########## 与 pct_above_ma20（扩展度）的秩相关 ##########")
    ext = pd.to_numeric(ev["pct_above_ma20"], errors="coerce")
    print(f"{'因子':<14}{'Spearman vs ext':>16}{'|r|判定':>12}")
    for c in FACTORS:
        v = pd.to_numeric(ev[c], errors="coerce")
        ok = v.notna() & ext.notna()
        if ok.sum() < 100:
            print(f"{c:<14}{'--':>16}")
            continue
        r = _spearman(v[ok], ext[ok])
        tag = "高度重叠" if abs(r) > 0.6 else ("中度" if abs(r) > 0.3 else "正交")
        print(f"{c:<14}{r:>+16.3f}{tag:>12}")


def report_incremental(H, ev, key="pressure", n_ext=3, n_fac=3):
    """Q2：控制 ext 分桶后，桶内筹码因子是否仍有效（增量检验，最关键）。"""
    print(f"\n########## 控制扩展度后的增量检验 · 因子={key} ##########")
    e = ev[pd.to_numeric(ev["pct_above_ma20"], errors="coerce").notna()].copy()
    if e.empty:
        print("  扩展度全空，跳过")
        return
    e["_extb"] = pd.qcut(pd.to_numeric(e["pct_above_ma20"], errors="coerce"),
                         n_ext, labels=False, duplicates="drop")
    lo_all = pd.to_numeric(e[key], errors="coerce").quantile(0.25)
    hi_all = pd.to_numeric(e[key], errors="coerce").quantile(0.75)
    rows, diffs = [], []
    for eb, ge in e.groupby("_extb"):
        lo, hi = ge["pct_above_ma20"].min(), ge["pct_above_ma20"].max()
        base = H.metrics(ge, f"ext桶{int(eb) + 1} [{lo:.3f},{hi:.3f}] 全部")
        rows.append(base)
        sel = ge[pd.to_numeric(ge[key], errors="coerce") <= lo_all]
        m = H.metrics(sel, f"     └ {key} 最低25%")
        rows.append(m)
        if m["avg_ret"] is not None and base["avg_ret"] is not None:
            diffs.append(round(m["avg_ret"] - base["avg_ret"], 2))
    H.show(rows, f"控制 ext 后 · {key}（最低25%档 vs 桶内基线）")
    if diffs:
        print(f"\n  → 各 ext 桶内「{key} 最低25%」相对桶内基线的平均单笔价差: {diffs}")
        print(f"  → 桶数 {len(diffs)}，方向为负 {sum(1 for x in diffs if x < 0)}/{len(diffs)}")
        print("     （若各桶内都同向 → 增量成立；若某桶翻转 → 该桶内是 ext 在起作用）")


def report_monthly(H, ev, expr: str, label: str, q=0.3):
    """Q3：逐月压力测试（铁律 3）——规则收益是否只靠少数月份。"""
    print(f"\n########## 逐月压力测试 · {label} ##########")
    sub = ev.copy()
    sub["_m"] = sub["scan_date"].str.slice(0, 7)
    sel = sub.query(expr) if expr else sub
    print(f"{'月份':<10}{'规则均值':>10}{'同期基线':>10}{'价差':>9}{'规则n':>8}{'基n':>8}")
    diffs = []
    for m, g in sorted(sub.groupby("_m")):
        gs = g.query(expr) if expr else g
        ra = pd.to_numeric(gs["ret"], errors="coerce").dropna()
        rb = pd.to_numeric(g["ret"], errors="coerce").dropna()
        if len(ra) < 5:
            continue
        d = float(ra.mean() - rb.mean())
        diffs.append((m, d))
        print(f"{m:<10}{ra.mean():>+10.2f}{rb.mean():>+10.2f}{d:>+9.2f}"
              f"{len(ra):>8}{len(rb):>8}")
    if diffs:
        pos = sum(1 for _, d in diffs if d > 0)
        arr = np.array([d for _, d in diffs])
        k = max(1, int(len(arr) * 0.1))
        top = np.sort(arr)[-k:]
        print(f"\n  → 价差为正月份: {pos}/{len(diffs)}  中位 {np.median(arr):+.2f}%  "
              f"均值 {arr.mean():+.2f}%")
        print(f"  → 剔除贡献最大的 {k} 个月后均值: "
              f"{(arr.sum() - top.sum()) / (len(arr) - k):+.2f}%"
              f"   （大幅下滑 = 单月依赖，不可上线）")


def candidate_rules(ev) -> list[tuple[str, str, str]]:
    """基于全期分位定义候选规则 → [(key, expr, label)]。"""
    q = lambda c, p: float(pd.to_numeric(ev[c], errors="coerce").quantile(p))  # noqa: E731
    c20, c40 = q("conc", 0.2), q("conc", 0.4)
    l20, l70 = q("lock", 0.2), q("lock", 0.7)
    r40, r85 = q("cost_ratio", 0.4), q("cost_ratio", 0.85)
    return [
        ("conc", f"conc <= {c20:.4f}", f"conc ≤ P20 ({c20:.3f}) 筹码集中"),
        ("conc", f"conc <= {c40:.4f}", f"conc ≤ P40 ({c40:.3f})"),
        ("lock", f"lock >= {l20:.4f} and lock <= {l70:.4f}",
         f"lock ∈ [P20,P70] ({l20:.3f}~{l70:.3f}) 适度锁仓"),
        ("cost_ratio", f"cost_ratio >= {r40:.4f} and cost_ratio <= {r85:.4f}",
         f"cost_ratio ∈ [P40,P85] ({r40:.3f}~{r85:.3f})"),
        ("winner", "winner <= 0.93", "winner ≤ 0.93 剔除全员浮盈"),
        ("conc", f"conc <= {c40:.4f} and cost_ratio >= {r40:.4f}",
         "conc≤P40 & cost_ratio≥P40 组合"),
        ("lock", f"lock >= {l20:.4f} and lock <= {l70:.4f} and conc <= {c40:.4f}",
         "lock适度 & conc≤P40 组合"),
    ]


def report_rules(H, ev, is_end: str, top_n: int):
    """候选规则总表：全期 / 样本内 / 样本外 + 组合口径。"""
    print("\n########## 候选规则总表（逐笔口径 · 单笔平均收益优先）##########")
    is_df = ev[ev["scan_date"] <= is_end]
    oos_df = ev[ev["scan_date"] > is_end]
    hdr = (f"{'规则':<38}{'全期':>9}{'胜率':>7}{'止损':>7}"
           f"{'样本内':>9}{'样本外':>9}{'信号':>8}")
    print(hdr)
    print("-" * len(hdr))
    for _, expr, label in candidate_rules(ev):
        a = H.metrics(ev.query(expr) if expr else ev, label)
        i = H.metrics(is_df.query(expr) if expr else is_df, label)
        o = H.metrics(oos_df.query(expr) if expr else oos_df, label)
        f = lambda v, s="{:.2f}": (s.format(v) if v is not None else "--")  # noqa: E731
        print(f"{label:<38}{f(a['avg_ret'], '{:+.2f}%'):>9}"
              f"{f(a['win_rate'], '{:.1f}%'):>7}{f(a['stop_rate'], '{:.1f}%'):>7}"
              f"{f(i['avg_ret'], '{:+.2f}%'):>9}{f(o['avg_ret'], '{:+.2f}%'):>9}"
              f"{a['n_fill']:>8}")

    print(f"\n########## 组合口径 · 每日 top{top_n}（线上排序：ext升序 + fusion降序）##########")
    rows = [H.portfolio(ev, "", top_n, True, "基线（现线上）")]
    for _, expr, label in candidate_rules(ev):
        if not expr:
            continue
        rows.append(H.portfolio(ev, expr, top_n, True, label))
    H.show_portfolio(rows, f"组合级 · 每日top{top_n} · 全期")

    rows = [H.portfolio(is_df, "", top_n, True, "基线（样本内）")]
    for _, expr, label in candidate_rules(ev):
        if expr:
            rows.append(H.portfolio(is_df, expr, top_n, True, label))
    H.show_portfolio(rows, f"组合级 · 每日top{top_n} · 样本内（≤{is_end}）")

    rows = [H.portfolio(oos_df, "", top_n, True, "基线（样本外）")]
    for _, expr, label in candidate_rules(ev):
        if expr:
            rows.append(H.portfolio(oos_df, expr, top_n, True, label))
    H.show_portfolio(rows, f"组合级 · 每日top{top_n} · 样本外（>{is_end}）")


def _portfolio_by(H, df: pd.DataFrame, expr: str, keys: list[str],
                  ascs: list[bool], top_n: int, label: str) -> dict:
    """自定义排序键的每日 top-N 组合口径（复刻 H.portfolio 的取数逻辑，只换排序）。"""
    pool = df.query(expr) if expr else df
    if pool.empty:
        return {"label": label, "n_sig": 0, "n_fill": 0, "days": 0, "per_day": 0,
                "fill_rate": None, "avg_ret": None, "median_ret": None,
                "win_rate": None, "pf": None, "avg_per_sig": None,
                "stop_rate": None, "cum_ret": None}
    pool = pool[pool["final"] == 1].copy()
    pool = pool.sort_values(keys, ascending=ascs)
    picked = pool.groupby("scan_date", sort=True).head(top_n)
    m = H.metrics(picked, label)
    days = int(picked["scan_date"].nunique())
    m.update(days=days, per_day=round(len(picked) / days, 2) if days else 0,
             cum_ret=round(float(picked.loc[picked["filled"] == 1, "ret"].dropna().sum()), 1))
    m["n_sig"] = int(len(pool))
    return m


def report_sort_variants(H, ev, top_n: int, is_end: str):
    """排序变体测试：过滤不改变每日 top-N 构成（只在池子边缘动刀），
    排序才能改变**真正会买的那几只**。筹码因子与 ext 正交 → 加进排序键有增量空间。

    ⚠ 组合口径是本项目最终判据：逐笔口径的池子平均改善会被 top-N 名额稀释。
    """
    print("\n########## 排序变体测试（组合口径 · 每日 top"
          f"{top_n}，这才是最终判据）##########")
    d = ev.copy()
    d["_cost_dev"] = (pd.to_numeric(d["cost_ratio"], errors="coerce") - 1.0).abs()
    d["_lock_dev"] = (pd.to_numeric(d["lock"], errors="coerce") - 0.27).abs()
    d["_p"] = pd.to_numeric(d["pct_above_ma20"], errors="coerce").fillna(0)
    d["_f"] = pd.to_numeric(d["fusion_score"], errors="coerce").fillna(0)

    variants = [
        ("基线 ext↑,fusion↓（现线上）", ["_p", "_f"], [True, False]),
        ("ext↑, conc↑, fusion↓", ["_p", "conc", "_f"], [True, True, False]),
        ("conc↑, ext↑, fusion↓", ["conc", "_p", "_f"], [True, True, False]),
        ("ext↑, |cost-1|↓, fusion↓", ["_p", "_cost_dev", "_f"], [True, True, False]),
        ("ext↑, |lock-0.27|↓, fusion↓", ["_p", "_lock_dev", "_f"], [True, True, False]),
        ("ext↑, conc↑, |cost-1|↓, fusion↓",
         ["_p", "conc", "_cost_dev", "_f"], [True, True, True, False]),
    ]
    for wname, wdf in (("全期", d), (f"样本内(≤{is_end})", d[d["scan_date"] <= is_end]),
                       (f"样本外(>{is_end})", d[d["scan_date"] > is_end])):
        if wdf.empty:
            continue
        rows = [_portfolio_by(H, wdf, "", ks, asc, top_n, lb)
                for lb, ks, asc in variants]
        H.show_portfolio(rows, f"排序变体 · 每日top{top_n} · {wname}")
    del d


def report_sort_diagnosis(H, ev, top_n: int):
    """conc 优先排序的上线前双关卡：
    ① 逐月压力测试（铁律 3）——收益差是否只靠少数月份；
    ② 选中票是否只是「低换手/低流动性」垃圾股（筹码集中≠主力控盘，可能是没人交易）。
    """
    d = ev.copy()
    d["_p"] = pd.to_numeric(d["pct_above_ma20"], errors="coerce").fillna(0)
    d["_f"] = pd.to_numeric(d["fusion_score"], errors="coerce").fillna(0)
    d["_m"] = d["scan_date"].str.slice(0, 7)
    K_NEW, A_NEW = ["conc", "_p", "_f"], [True, True, False]
    K_BASE, A_BASE = ["_p", "_f"], [True, False]

    print(f"\n########## 关卡① 逐月压力测试 · conc优先排序 vs 基线排序（每日top{top_n}）##########")
    print(f"{'月份':<10}{'筹码优先':>10}{'基线':>10}{'价差':>9}{'新n':>7}{'基n':>7}")
    diffs = []
    for m, g in sorted(d.groupby("_m")):
        a = _portfolio_by(H, g, "", K_NEW, A_NEW, top_n, "new")
        b = _portfolio_by(H, g, "", K_BASE, A_BASE, top_n, "base")
        if a["avg_ret"] is None or b["avg_ret"] is None:
            continue
        dd = a["avg_ret"] - b["avg_ret"]
        diffs.append((m, dd))
        print(f"{m:<10}{a['avg_ret']:>+10.2f}{b['avg_ret']:>+10.2f}{dd:>+9.2f}"
              f"{a['n_fill']:>7}{b['n_fill']:>7}")
    if diffs:
        arr = np.array([x for _, x in diffs])
        pos = int((arr > 0).sum())
        k = max(1, int(len(arr) * 0.1))
        print(f"\n  → 价差为正月份: {pos}/{len(arr)}  中位 {np.median(arr):+.2f}%  "
              f"均值 {arr.mean():+.2f}%")
        print(f"  → 剔除贡献最大的 {k} 个月后均值: "
              f"{(arr.sum() - np.sort(arr)[-k:].sum()) / (len(arr) - k):+.2f}%"
              f"   （大幅下滑 = 单月依赖）")

    print(f"\n########## 关卡② 选中票的活跃度特征（防止选到低流动性垃圾股）##########")
    picked = {}
    for nm, ks, asc in (("筹码优先", K_NEW, A_NEW), ("基线", K_BASE, A_BASE)):
        pool = d[d["final"] == 1].copy().sort_values(ks, ascending=asc)
        pk = pool.groupby("scan_date", sort=True).head(top_n)
        picked[nm] = set(zip(pk["code"], pk["scan_date"]))
    allpairs = sorted(picked["筹码优先"] | picked["基线"])
    if not allpairs:
        print("  无选中样本")
        return
    codes = sorted({c for c, _ in allpairs})
    with get_conn() as conn:
        ph = ",".join("?" * len(codes))
        px = pd.read_sql_query(
            f"SELECT code, trade_date, volume, close, amount FROM daily_price "
            f"WHERE code IN ({ph})", conn, params=codes)
        sh = pd.read_sql_query("SELECT code, circ_shares FROM stock_info", conn)
    sm = dict(zip(sh["code"], sh["circ_shares"]))
    px["_to"] = px["volume"] / px["code"].map(sm)
    tmap = dict(zip(zip(px["code"], px["trade_date"]), px["_to"]))
    amap = dict(zip(zip(px["code"], px["trade_date"]), px["amount"]))
    print(f"{'排序':<12}{'选中数':>8}{'换手率中位':>12}{'成交额中位(万)':>16}")
    for nm in ("筹码优先", "基线"):
        tos = [tmap.get(k) for k in picked[nm]]
        tos = [t for t in tos if t is not None and np.isfinite(t)]
        ams = [amap.get(k) for k in picked[nm]]
        ams = [a for a in ams if a is not None and np.isfinite(a)]
        print(f"{nm:<12}{len(picked[nm]):>8}"
              f"{(np.median(tos) * 100 if tos else float('nan')):>11.2f}%"
              f"{(np.median(ams) / 1e4 if ams else float('nan')):>16.0f}")
    print("  （换手率/成交额显著更低 → 所谓'筹码集中'只是没人交易的伪信号）")


def report_turnover_confound(H, ev, key="conc", n=3):
    """内生性检验：`conc` 会不会只是「低换手」的马甲？

    低换手 → 老筹码衰减慢 → 分布天然更集中。若 conc 的效力完全来自这一机制，
    则控制换手率分桶后 conc 应当失去区分度。桶内仍单调 = 独立贡献成立。
    """
    print(f"\n########## 关卡③ 内生性检验 · {key} 是否只是「低换手」的马甲 ##########")
    with get_conn() as conn:
        px = pd.read_sql_query(
            "SELECT d.code, d.trade_date, d.volume, s.circ_shares "
            "FROM daily_price d JOIN stock_info s ON s.code = d.code "
            "WHERE d.trade_date >= ?", conn, params=(ev["scan_date"].min(),))
    px["_to"] = px["volume"] / px["circ_shares"].replace(0, np.nan)
    tmap = dict(zip(zip(px["code"], px["trade_date"]), px["_to"]))
    e = ev.copy()
    e["_to"] = [tmap.get((c, d)) for c, d in zip(e["code"], e["scan_date"])]
    e = e[e["_to"].notna() & (e["_to"] > 0)]
    print(f"  有效样本 {len(e):,} / {len(ev):,}   换手率中位 {e['_to'].median() * 100:.2f}%")

    print(f"\n  ① 换手率单独分档（若低换手本身就好 → 存在混淆）")
    rows = [H.metrics(e, "全部")]
    for lb, g in _buckets(e, "_to", n):
        rows.append(H.metrics(g, f"换手率 {lb}"))
    H.show(rows, "换手率单变量分档")

    print(f"\n  ② 控制换手率后 · {key} 分档（桶内是否仍单调）")
    e["_tb"] = pd.qcut(e["_to"], n, labels=False, duplicates="drop")
    diffs = []
    for tb, gt in e.groupby("_tb"):
        lo, hi = gt["_to"].min() * 100, gt["_to"].max() * 100
        base = H.metrics(gt, f"换手率桶{int(tb) + 1} [{lo:.2f}%,{hi:.2f}%] 全部")
        rows = [base]
        means = []
        for lb, gf in _buckets(gt, key, n):
            m = H.metrics(gf, f"     └ {key} {lb}")
            rows.append(m)
            if m["avg_ret"] is not None:
                means.append(m["avg_ret"])
        H.show(rows, f"换手率桶{int(tb) + 1} 内 · {key} 分档")
        if len(means) == n:
            diffs.append(round(means[0] - means[-1], 2))   # 最低 conc 档 - 最高档
    if diffs:
        print(f"\n  → 各换手率桶内（{key} 最低档 - 最高档）价差: {diffs}")
        print(f"  → 同向桶数 {sum(1 for x in diffs if x > 0)}/{len(diffs)}"
              f"   （全部为正 = 排除换手率混淆，conc 有独立贡献）")


def main():
    global _DECAY, _DECAY_FLOOR, _TURNOVER_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="强制重算筹码因子")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--decay", type=float, default=DEFAULT_DECAY)
    ap.add_argument("--decay-floor", type=float, default=DEFAULT_DECAY_FLOOR)
    ap.add_argument("--turnover", choices=("auto", "calc", "db"), default="auto",
                    help="换手率口径：auto=旧口径(优先库值) / calc=volume/circ_shares（修正）"
                         " / db=只用库值。⚠ 库 turnover 约 38%% 的行被低估 1~2 个数量级，见 "
                         "tools/diag_price_data_quality.py")
    ap.add_argument("--key", default="conc", help="增量检验主因子")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--is-end", default="2025-06-30", help="样本内结束日")
    ap.add_argument("--tag", default="", help="缓存文件名后缀，便于多参数并存")
    args = ap.parse_args()

    _DECAY, _DECAY_FLOOR = args.decay, args.decay_floor
    _TURNOVER_MODE = args.turnover
    suffix = "" if args.turnover == "auto" else f"_{args.turnover}"
    tag = args.tag or f"d{args.decay:.2f}f{args.decay_floor:.3f}{suffix}"
    cache = os.path.join(LOGS, f"_chip_factors_{tag}.pkl")

    H = _load_harness()
    sig = pickle.load(open(SIM_CACHE, "rb"))
    codes = sorted(sig["code"].unique())
    print(f"信号池 {len(sig):,} 条 / {len(codes):,} 只（缓存 {os.path.basename(SIM_CACHE)}）")

    if args.rebuild or not os.path.exists(cache):
        chip = build_factors(codes, args.workers)
        with open(cache, "wb") as fh:
            pickle.dump(chip, fh)
        print(f"[3/3] 筹码因子缓存 → {cache}（{len(chip):,} 行）")
    else:
        chip = pickle.load(open(cache, "rb"))
        print(f"[1/3] 读取筹码缓存 {os.path.basename(cache)}（{len(chip):,} 行）——"
              f" 重算请加 --rebuild")

    # 对齐：信号 (code, scan_date) ← 筹码因子
    key_idx = pd.MultiIndex.from_arrays([sig["code"], sig["scan_date"]])
    j = chip.reindex(key_idx)
    miss = int(j[FACTORS[0]].isna().sum())
    for c in FACTORS:
        sig[c] = j[c].to_numpy()
    print(f"      对齐成功 {len(sig) - miss:,} / {len(sig):,}（缺失 {miss:,}）")

    # ⚠ final==1 保证局面完整，**不要**再按"最近 N 天"截断（铁律 2）
    ev = sig[sig["final"] == 1].copy()
    print(f"终态样本 {len(ev):,}（{ev['scan_date'].min()} ~ {ev['scan_date'].max()}，"
          f"{ev['scan_date'].nunique()} 个交易日）")

    report_corr(ev)
    report_single(H, ev)
    report_incremental(H, ev, args.key)
    report_rules(H, ev, args.is_end, args.top_n)
    report_sort_variants(H, ev, args.top_n, args.is_end)
    report_sort_diagnosis(H, ev, args.top_n)
    report_turnover_confound(H, ev, args.key)
    # 逐月压力测试：取候选规则里最强的两条
    rules = candidate_rules(ev)
    for _, expr, label in (rules[0], rules[2]):
        report_monthly(H, ev, expr, label)


if __name__ == "__main__":
    main()
