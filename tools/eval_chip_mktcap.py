"""市值分层诊断：筹码峰（持仓成本分布）短线策略在**小市值**上是否更有效？

问题来源
--------
雪球《筹码峰9大应用策略》（xueqiu.com/1040910313/353342587）给出 9 条定性规则：
  ① 上峰不死，下跌不止      ② 单峰密集，主力参与（密集度越高、换手越充分越强）
  ③ 多峰林立，行情延续      ④ 多峰锁仓，强庄控盘
  ⑤ 下消上移，换庄接力      ⑥ 多峰齐消，主力出逃
  ⑦ 下峰锁定，上涨未尽      ⑧ 双峰对垒，高抛低吸
  ⑨ 顶格在消，强庄撤退

其中 ①②⑦ 可被本项目的筹码因子直接映射：
  ① → `pressure`（现价上方筹码占比）低 = 上方真空
  ② → `conc`（(P90-P10)/(P90+P10)，越小越集中）低 = 单峰密集
  ⑦ → `lock`（现价×0.85 以下筹码 / 现价以下筹码）高 = 底部锁仓未松动

⚠ 文章本身**没有**任何市值适用区间的论述——"小市值更适合筹码峰"是**用户的假设**，
   本工具存在的意义就是把它证真或证伪，而不是替它找理由。

核心陷阱（本工具的设计出发点）
------------------------------
候选池基线已呈"市值单调"：<50亿 +1.30% vs >1000亿 +0.46%（见 §1）。
若直接比较「小市值+筹码峰」与「大市值+筹码峰」，会把**小市值本身的天然优势**
误记为筹码峰的功劳。故本工具的主判据是**桶内增量**：

    Δ = 该市值桶内 (筹码优先排序 - 线上基线排序) 的每日 top-N 组合收益

Δ 随市值下降而增大 ⇒ 用户假设成立；Δ 与市值无关 ⇒ 筹码峰是全市场通用因子，
"小市值更适合"不成立。

口径
----
- 信号池 `logs/_short_observe_sim.pkl`（生产候选池 + 线上同口径出场状态机）；
- 市值 = `stock_info.total_shares × daily_price.close(scan_date)`（**当日点内**，
  与 `config/strategy_params.py::QUALITY_FILTER` 同口径；复算 流通市值 做稳健性）；
- 筹码因子 `logs/_chip_factors_{tag}.pkl`（由 `tools/eval_chip_factor.py` 生成，
  decay 可扫）；按 (code, scan_date) 对齐，只用当日及以前价量 → 无未来函数；
- ⚠ `ret` 单位是**百分数**（铁律 5）；单笔收益率**不可相加**（铁律 1）→ 只看
  平均每笔 / 胜率 / 止损率 / PF。

用法
----
    python tools/eval_chip_mktcap.py                       # 默认 decay=0.65
    python tools/eval_chip_mktcap.py --decay 0.35          # 换缓存，验稳健性
    python tools/eval_chip_mktcap.py --top-n 3 --is-end 2025-06-30
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pickle
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.db import get_conn                                                    # noqa: E402

LOGS = os.path.join(_ROOT, "logs")
SIM_CACHE = os.path.join(LOGS, "_short_observe_sim.pkl")
FACTORS = ["winner", "conc", "pressure", "cost_ratio", "lock", "center_shift"]

# 市值分桶（亿元）；300 是用户假设的切点，两侧各留细桶看梯度是否连续
CAP_EDGES = [0, 30, 50, 100, 200, 300, 500, 1000, 1e9]
CAP_LABELS = ["<30", "30-50", "50-100", "100-200", "200-300",
              "300-500", "500-1000", ">1000"]


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt(v, s="{:.2f}"):
    return s.format(v) if v is not None and np.isfinite(v) else "--"


# ───────────────────────── 数据装载 ─────────────────────────

def load_data(decay: float, decay_floor: float, tag: str | None = None):
    """返回 (H, EF, ev)。ev 含筹码因子 + 点内市值。

    tag：筹码缓存后缀。默认按 decay 组名；传 `calc` 可载入修正换手率口径的缓存
    （`_chip_factors_..._calc.pkl`，由 `eval_chip_factor.py --turnover calc` 生成）。
    """
    H = _load("_harness", os.path.join(_ROOT, "tools", "eval_short_observe_rule.py"))
    EF = _load("_chipeval", os.path.join(_ROOT, "tools", "eval_chip_factor.py"))

    tag = tag or f"d{decay:.2f}f{decay_floor:.3f}"
    cache = os.path.join(LOGS, f"_chip_factors_{tag}.pkl")
    if not os.path.exists(cache):
        raise SystemExit(f"缺筹码缓存 {cache}，先跑：python tools/eval_chip_factor.py "
                         f"--rebuild --decay {decay} --decay-floor {decay_floor}")

    sig = pickle.load(open(SIM_CACHE, "rb"))
    chip = pickle.load(open(cache, "rb"))
    print(f"信号池 {len(sig):,} 条  筹码缓存 {os.path.basename(cache)}（{len(chip):,} 行）")

    key_idx = pd.MultiIndex.from_arrays([sig["code"], sig["scan_date"]])
    j = chip.reindex(key_idx)
    for c in FACTORS:
        sig[c] = j[c].to_numpy()
    miss = int(j[FACTORS[0]].isna().sum())
    print(f"对齐成功 {len(sig) - miss:,} / {len(sig):,}（缺失 {miss:,}）")
    del chip, j

    # ⚠ final==1 保证局面完整，**不要**再按"最近 N 天"截断（铁律 2）
    ev = sig[sig["final"] == 1].copy()
    del sig

    # 点内市值：total_shares / circ_shares × scan_date 收盘
    with get_conn() as conn:
        sh = pd.read_sql_query(
            "SELECT code, total_shares, circ_shares FROM stock_info", conn)
        px = pd.read_sql_query(
            "SELECT code, trade_date, close FROM daily_price WHERE trade_date >= ?",
            conn, params=(ev["scan_date"].min(),))
    cmap = dict(zip(zip(px["code"], px["trade_date"]), px["close"]))
    ev["_close"] = [cmap.get((c, d)) for c, d in zip(ev["code"], ev["scan_date"])]
    ts = dict(zip(sh["code"], sh["total_shares"]))
    cs = dict(zip(sh["code"], sh["circ_shares"]))
    ev["_ts"] = ev["code"].map(ts)
    ev["_cs"] = ev["code"].map(cs)
    ev["mktcap"] = ev["_ts"] * ev["_close"] / 1e8          # 亿元，总市值
    ev["fmktcap"] = ev["_cs"] * ev["_close"] / 1e8         # 亿元，流通市值
    ev = ev[ev["mktcap"].notna() & (ev["mktcap"] > 0)].copy()
    print(f"终态样本 {len(ev):,}（{ev['scan_date'].min()} ~ {ev['scan_date'].max()}，"
          f"{ev['scan_date'].nunique()} 个交易日）\n")
    return H, EF, ev


def _sort_keys(d: pd.DataFrame) -> pd.DataFrame:
    """排序键：_p=扩展度, _f=fusion；线上基线 = _p↑,_f↓。"""
    d["_p"] = pd.to_numeric(d["pct_above_ma20"], errors="coerce").fillna(0)
    d["_f"] = pd.to_numeric(d["fusion_score"], errors="coerce").fillna(0)
    return d


K_BASE, A_BASE = ["_p", "_f"], [True, False]
K_CHIP, A_CHIP = ["conc", "_p", "_f"], [True, True, False]


# ───────────────────────── §1 市值分布与基线 ─────────────────────────

def s1_baseline(H, ev):
    print("=" * 78)
    print("§1 候选池市值分布与基线表现（⚠ 小市值天然占优 → 必须做桶内增量）")
    print("=" * 78)
    d = _sort_keys(ev.copy())
    d["_capb"] = pd.cut(d["mktcap"], CAP_EDGES, labels=CAP_LABELS)
    rows = [H.metrics(d, "全池基线")]
    for lb in CAP_LABELS:
        g = d[d["_capb"] == lb]
        if len(g) >= 50:
            rows.append(H.metrics(g, f"总市值 {lb} 亿"))
    H.show(rows, "市值分桶 · 逐笔口径基线")
    b = d.groupby("_capb", observed=True)["ret"].agg(["size", "mean"])
    print("\n  → 市值-收益单调性: " +
          " ".join(f"{lb}:{b.loc[lb, 'mean']:+.2f}%" for lb in b.index if lb in b.index))
    print("  → 结论：若小市值桶基线本身更高，则『小市值+筹码峰』的绝对收益必然更高，")
    print("     该比较**无信息量**；必须看桶内 Δ（§3）。")
    return d


# ───────────────────────── §2 桶内 conc 单调性（逐笔） ─────────────────────────

def s2_quintile(H, ev, key="conc"):
    print("\n" + "=" * 78)
    print(f"§2 各市值桶内 · {key} 五分档（逐笔口径，看止损率是否单调）")
    print("=" * 78)
    d = ev.copy()
    d["_capb"] = pd.cut(d["mktcap"], CAP_EDGES, labels=CAP_LABELS)
    v = pd.to_numeric(d[key], errors="coerce")
    for lb in CAP_LABELS:
        g = d[(d["_capb"] == lb) & v.notna()].copy()
        if len(g) < 300:
            continue
        # 桶内分位（不同市值桶的 conc 绝对水平不可比 → 必须桶内切）
        g["_q"] = pd.qcut(pd.to_numeric(g[key], errors="coerce"), 5,
                          labels=False, duplicates="drop")
        rows = [H.metrics(g, f"{lb}亿 全部")]
        stop_series = []
        for q, gq in g.groupby("_q"):
            m = H.metrics(gq, f"   └ {key} Q{int(q) + 1}")
            rows.append(m)
            if m["stop_rate"] is not None:
                stop_series.append(m["stop_rate"])
        H.show(rows, f"总市值 {lb} 亿 · {key} 五分档")
        if len(stop_series) == 5:
            mono = all(stop_series[i] <= stop_series[i + 1] + 0.6 for i in range(4))
            print(f"  → 止损率 Q1→Q5: {stop_series}  "
                  f"{'单调递增（集中→分散=风险↑，与全池一致）' if mono else '非单调'}")


# ───────────────────────── §3 桶内排序增量（核心） ─────────────────────────

def s3_increment(H, EF, ev, top_n: int, is_end: str):
    print("\n" + "=" * 78)
    print(f"§3 【核心】桶内排序增量 Δ —— 用户假设的直接检验（每日 top{top_n} 组合口径）")
    print("=" * 78)
    d = _sort_keys(ev.copy())
    d["_capb"] = pd.cut(d["mktcap"], CAP_EDGES, labels=CAP_LABELS)

    print(f"\n{'市值桶(亿)':<12}{'样本':>7}{'基线收益':>10}{'筹码优先':>10}{'Δ':>8}"
          f"{'基线止损':>9}{'筹码止损':>9}{'Δ止损':>8}{'选票重合':>9}")
    print("-" * 82)
    res = []
    for lb in CAP_LABELS:
        g = d[d["_capb"] == lb]
        if len(g) < 200:
            continue
        b = EF._portfolio_by(H, g, "", K_BASE, A_BASE, top_n, "base")
        c = EF._portfolio_by(H, g, "", K_CHIP, A_CHIP, top_n, "chip")
        if b["avg_ret"] is None or c["avg_ret"] is None:
            continue
        # 选票重合度：两个排序键每天各自挑的票有多像（解释 Δ 来源）
        pb = set(zip(*[g[g["final"] == 1].sort_values(K_BASE, ascending=A_BASE)
                       .groupby("scan_date").head(top_n)[x] for x in ("code", "scan_date")]))
        pc = set(zip(*[g[g["final"] == 1].sort_values(K_CHIP, ascending=A_CHIP)
                       .groupby("scan_date").head(top_n)[x] for x in ("code", "scan_date")]))
        ov = len(pb & pc) / max(1, len(pb)) * 100
        d_ret = c["avg_ret"] - b["avg_ret"]
        d_stop = (c["stop_rate"] - b["stop_rate"]) if (c["stop_rate"] is not None
                                                     and b["stop_rate"] is not None) else np.nan
        res.append((lb, len(g), b["avg_ret"], c["avg_ret"], d_ret,
                    b["stop_rate"], c["stop_rate"], d_stop, ov))
        print(f"{lb:<12}{len(g):>7}{b['avg_ret']:>+9.2f}%{c['avg_ret']:>+9.2f}%"
              f"{d_ret:>+7.2f}%{_fmt(b['stop_rate'], '{:.1f}%'):>9}"
              f"{_fmt(c['stop_rate'], '{:.1f}%'):>9}{_fmt(d_stop, '{:+.1f}pp'):>8}"
              f"{ov:>8.0f}%")

    if not res:
        return
    df = pd.DataFrame(res, columns=["cap", "n", "base", "chip", "d_ret",
                                    "stop_b", "stop_c", "d_stop", "ov"])
    print(f"\n  → Δ 与市值的相关性（Spearman）："
          f"{df['cap'].rank().corr(df['d_ret'].rank()):+.3f}"
          f"   （负数 = 市值越小 Δ 越大 → 支持用户假设）")

    # 两侧二分：<300 / ≥300
    print(f"\n{'二分':<16}{'样本':>7}{'基线':>9}{'筹码优先':>10}{'Δ':>8}"
          f"{'样本内Δ':>9}{'样本外Δ':>9}")
    print("-" * 72)
    for name, mask in (("<300亿", d["mktcap"] < 300), ("≥300亿", d["mktcap"] >= 300)):
        g = d[mask]
        b = EF._portfolio_by(H, g, "", K_BASE, A_BASE, top_n, "base")
        c = EF._portfolio_by(H, g, "", K_CHIP, A_CHIP, top_n, "chip")
        gi = g[g["scan_date"] <= is_end]
        go = g[g["scan_date"] > is_end]
        bi = EF._portfolio_by(H, gi, "", K_BASE, A_BASE, top_n, "b")
        ci = EF._portfolio_by(H, gi, "", K_CHIP, A_CHIP, top_n, "c")
        bo = EF._portfolio_by(H, go, "", K_BASE, A_BASE, top_n, "b")
        co = EF._portfolio_by(H, go, "", K_CHIP, A_CHIP, top_n, "c")
        di = (ci["avg_ret"] - bi["avg_ret"]) if (ci["avg_ret"] is not None
                                                and bi["avg_ret"] is not None) else np.nan
        do = (co["avg_ret"] - bo["avg_ret"]) if (co["avg_ret"] is not None
                                                and bo["avg_ret"] is not None) else np.nan
        print(f"{name:<16}{len(g):>7}{_fmt(b['avg_ret'], '{:+.2f}%'):>9}"
              f"{_fmt(c['avg_ret'], '{:+.2f}%'):>10}"
              f"{_fmt(c['avg_ret'] - b['avg_ret'], '{:+.2f}%'):>8}"
              f"{_fmt(di, '{:+.2f}%'):>9}{_fmt(do, '{:+.2f}%'):>9}")

    # 流通市值口径稳健性
    print(f"\n  稳健性 · 改用**流通市值**切 300亿：")
    d2 = d.assign(_capb=pd.cut(d["fmktcap"], CAP_EDGES, labels=CAP_LABELS))
    for name, mask in (("<300亿(流通)", d2["fmktcap"] < 300),
                       ("≥300亿(流通)", d2["fmktcap"] >= 300)):
        g = d2[mask]
        if len(g) < 200:
            continue
        b = EF._portfolio_by(H, g, "", K_BASE, A_BASE, top_n, "base")
        c = EF._portfolio_by(H, g, "", K_CHIP, A_CHIP, top_n, "chip")
        print(f"    {name:<14} n={len(g):>6}  基线 {_fmt(b['avg_ret'], '{:+.2f}%')}"
              f"  筹码优先 {_fmt(c['avg_ret'], '{:+.2f}%')}"
              f"  Δ {_fmt(c['avg_ret'] - b['avg_ret'], '{:+.2f}%')}")
    return df


# ───────────────────────── §4 单月压力测试 ─────────────────────────

def s4_monthly(H, EF, ev, top_n: int, mask_name="<300亿"):
    print("\n" + "=" * 78)
    print(f"§4 逐月压力测试 · {mask_name} 内 筹码优先 vs 基线（铁律 3）")
    print("=" * 78)
    d = _sort_keys(ev.copy())
    g = d[d["mktcap"] < 300] if mask_name.startswith("<") else d[d["mktcap"] >= 300]
    g = g.copy()
    g["_m"] = g["scan_date"].str.slice(0, 7)
    print(f"{'月份':<10}{'筹码优先':>10}{'基线':>10}{'价差':>9}{'新n':>7}{'基n':>7}")
    diffs = []
    for m, gm in sorted(g.groupby("_m")):
        a = EF._portfolio_by(H, gm, "", K_CHIP, A_CHIP, top_n, "new")
        b = EF._portfolio_by(H, gm, "", K_BASE, A_BASE, top_n, "base")
        if a["avg_ret"] is None or b["avg_ret"] is None:
            continue
        dd = a["avg_ret"] - b["avg_ret"]
        diffs.append(dd)
        print(f"{m:<10}{a['avg_ret']:>+10.2f}{b['avg_ret']:>+10.2f}{dd:>+9.2f}"
              f"{a['n_fill']:>7}{b['n_fill']:>7}")
    if diffs:
        arr = np.array(diffs)
        k = max(1, int(len(arr) * 0.1))
        print(f"\n  → 价差为正月份 {int((arr > 0).sum())}/{len(arr)}  "
              f"中位 {np.median(arr):+.2f}%  均值 {arr.mean():+.2f}%")
        print(f"  → 剔除贡献最大的 {k} 个月后均值 "
              f"{(arr.sum() - np.sort(arr)[-k:].sum()) / (len(arr) - k):+.2f}%")
    return diffs


# ───────────────────────── §5 换手率混淆 ─────────────────────────

def s5_turnover(H, EF, ev, top_n: int):
    print("\n" + "=" * 78)
    print("§5 混淆检验 · 小市值换手率天然更高，『conc 有效』是否只是换手率的分层？")
    print("=" * 78)
    d = ev.copy()
    with get_conn() as conn:
        px = pd.read_sql_query(
            "SELECT d.code, d.trade_date, d.volume, s.circ_shares "
            "FROM daily_price d JOIN stock_info s ON s.code = d.code "
            "WHERE d.trade_date >= ?", conn, params=(d["scan_date"].min(),))
    px["_to"] = px["volume"] / px["circ_shares"].replace(0, np.nan)
    tmap = dict(zip(zip(px["code"], px["trade_date"]), px["_to"]))
    d["_to"] = [tmap.get((c, sd)) for c, sd in zip(d["code"], d["scan_date"])]
    d = d[d["_to"].notna() & (d["_to"] > 0)].copy()
    print(f"有效样本 {len(d):,}  全池换手率中位 {d['_to'].median() * 100:.2f}%")

    print(f"\n{'区间':<22}{'n':>7}{'换手率中位':>12}{'conc中位':>11}")
    for name, m in (("<300亿", d["mktcap"] < 300), ("≥300亿", d["mktcap"] >= 300)):
        g = d[m]
        print(f"{name:<22}{len(g):>7}{g['_to'].median() * 100:>11.2f}%"
              f"{pd.to_numeric(g['conc'], errors='coerce').median():>11.3f}")

    print("\n  → 双变量交叉：市值 × 换手率桶 → 筹码优先 Δ（若 Δ 只在低换手列成立 = 混淆）")
    d = _sort_keys(d)
    d["_cap2"] = np.where(d["mktcap"] < 300, "<300亿", "≥300亿")
    d["_to2"] = pd.qcut(d["_to"], 2, labels=["低换手", "高换手"])
    print(f"  {'区间':<10}{'换手桶':<10}{'n':>7}{'Δ':>9}{'基线':>9}{'筹码优先':>10}")
    for cap in ("<300亿", "≥300亿"):
        for tob in ("低换手", "高换手"):
            g = d[(d["_cap2"] == cap) & (d["_to2"] == tob)]
            if len(g) < 100:
                continue
            b = EF._portfolio_by(H, g, "", K_BASE, A_BASE, top_n, "b")
            c = EF._portfolio_by(H, g, "", K_CHIP, A_CHIP, top_n, "c")
            print(f"  {cap:<10}{tob:<10}{len(g):>7}"
                  f"{_fmt(c['avg_ret'] - b['avg_ret'] if c['avg_ret'] is not None and b['avg_ret'] is not None else None, '{:+.2f}%'):>9}"
                  f"{_fmt(b['avg_ret'], '{:+.2f}%'):>9}{_fmt(c['avg_ret'], '{:+.2f}%'):>10}")


# ───────────────────────── §6 文章规则映射 ─────────────────────────

def s6_article_rules(H, EF, ev, top_n: int):
    print("\n" + "=" * 78)
    print("§6 雪球 9 条策略的可测映射（①上峰不死 ②单峰密集 ⑦下峰锁定）")
    print("=" * 78)
    d = ev.copy()
    q = lambda c, p: float(pd.to_numeric(d[c], errors="coerce").quantile(p))  # noqa: E731
    c30, p30, l50 = q("conc", 0.3), q("pressure", 0.3), q("lock", 0.5)
    rules = [
        ("", "基线（全池）"),
        (f"conc <= {c30:.4f}", f"② 单峰密集 conc≤P30 ({c30:.3f})"),
        (f"pressure <= {p30:.4f}", f"① 上方真空 pressure≤P30 ({p30:.3f})"),
        (f"lock >= {l50:.4f}", f"⑦ 下峰锁定 lock≥P50 ({l50:.3f})"),
        (f"conc <= {c30:.4f} and pressure <= {p30:.4f}",
         "②+① 单峰密集 & 上方真空"),
        (f"conc <= {c30:.4f} and lock >= {l50:.4f}", "②+⑦ 单峰密集 & 底部锁定"),
    ]
    print(f"\n{'规则':<34}{'<300亿 均收益':>14}{'止损':>8}{'n':>7}"
          f"{'≥300亿 均收益':>14}{'止损':>8}{'n':>7}")
    print("-" * 94)
    for expr, lb in rules:
        out = []
        for is_small in (True, False):
            g = d[d["mktcap"] < 300] if is_small else d[d["mktcap"] >= 300]
            gs = g.query(expr) if expr else g
            r = H.metrics(gs, lb)
            out.append(r)
        print(f"{lb:<34}{_fmt(out[0]['avg_ret'], '{:+.2f}%'):>14}"
              f"{_fmt(out[0]['stop_rate'], '{:.1f}%'):>8}{out[0]['n_fill']:>7}"
              f"{_fmt(out[1]['avg_ret'], '{:+.2f}%'):>14}"
              f"{_fmt(out[1]['stop_rate'], '{:.1f}%'):>8}{out[1]['n_fill']:>7}")

    print("\n  阈值改用**桶内分位**重算（避免全池阈值在小市值上过松/过紧）：")
    print(f"  {'规则':<34}{'<300亿 均收益':>14}{'n':>7}{'≥300亿 均收益':>14}{'n':>7}")
    print("-" * 82)
    for expr_name, builder in (
            ("② 单峰密集 conc≤桶内P30", lambda g, t: g["conc"] <= t["c"]),
            ("① 上方真空 pressure≤桶内P30", lambda g, t: g["pressure"] <= t["p"]),
            ("⑦ 下峰锁定 lock≥桶内P50", lambda g, t: g["lock"] >= t["l"]),
            ("②+① 密集 & 真空", lambda g, t: (g["conc"] <= t["c"]) & (g["pressure"] <= t["p"])),
            ("②+⑦ 密集 & 锁定", lambda g, t: (g["conc"] <= t["c"]) & (g["lock"] >= t["l"]))):
        out = []
        for lo, hi in ((0, 300), (300, 1e9)):
            g = d[(d["mktcap"] >= lo) & (d["mktcap"] < hi)]
            t = {"c": pd.to_numeric(g["conc"], errors="coerce").quantile(0.3),
                 "p": pd.to_numeric(g["pressure"], errors="coerce").quantile(0.3),
                 "l": pd.to_numeric(g["lock"], errors="coerce").quantile(0.5)}
            r = H.metrics(g[builder(g, t)], expr_name)
            out.append(r)
        print(f"  {expr_name:<32}{_fmt(out[0]['avg_ret'], '{:+.2f}%'):>14}"
              f"{out[0]['n_fill']:>7}{_fmt(out[1]['avg_ret'], '{:+.2f}%'):>14}"
              f"{out[1]['n_fill']:>7}")


def s7_decompose(H, EF, ev, top_n: int, is_end: str):
    """贡献分解 + 选中票画像 + 桶内相关 + Δ 的配对 t 统计。

    ① 2×2 分解：{全池, <300亿} × {基线排序, 筹码排序} → 市值收紧与筹码排序各贡献多少；
    ② 选中票画像：conc 排序若系统性挑到**更小市值**的票，则 Δ 只是 §1 小市值优势的马甲；
    ③ 桶内 conc vs 市值/换手率 的秩相关（判断因子与市值是否共线）；
    ④ 逐月 Δ 的配对 t 统计（32 个月的 mean/std → t），区分"稳健增量"与"噪音"。
    """
    print("\n" + "=" * 78)
    print("§7 贡献分解 · 选中票画像 · 桶内共线性 · Δ 显著性")
    print("=" * 78)

    d = _sort_keys(ev.copy())
    di = d[d["scan_date"] <= is_end]
    do = d[d["scan_date"] > is_end]

    def _run(g, ks, asc, lb):
        return EF._portfolio_by(H, g, "", ks, asc, top_n, lb)

    print(f"\n① 2×2 分解（每日 top{top_n}，全期 / 样本内 / 样本外）")
    print(f"{'方案':<30}{'全期':>9}{'样本内':>9}{'样本外':>9}{'全期止损':>10}")
    print("-" * 68)
    for name, mask in (("市值不收紧（现线上）", d["mktcap"] > 0),
                       ("市值收紧到 <300亿", d["mktcap"] < 300)):
        g = d[mask]
        gi = di[di["mktcap"] < 300] if "<300" in name else di
        go = do[do["mktcap"] < 300] if "<300" in name else do
        for sname, ks, asc in (("  + 基线排序", K_BASE, A_BASE),
                               ("  + 筹码优先排序", K_CHIP, A_CHIP)):
            a = _run(g, ks, asc, "a")
            i = _run(gi, ks, asc, "i")
            o = _run(go, ks, asc, "o")
            print(f"{name + sname:<30}{_fmt(a['avg_ret'], '{:+.2f}%'):>9}"
                  f"{_fmt(i['avg_ret'], '{:+.2f}%'):>9}{_fmt(o['avg_ret'], '{:+.2f}%'):>9}"
                  f"{_fmt(a['stop_rate'], '{:.1f}%'):>10}")

    print(f"\n② 选中票画像：conc 排序是否只是挑了更小市值的票？")
    with get_conn() as conn:
        px = pd.read_sql_query(
            "SELECT code, trade_date, volume, amount FROM daily_price WHERE trade_date >= ?",
            conn, params=(d["scan_date"].min(),))
        sh = pd.read_sql_query("SELECT code, circ_shares FROM stock_info", conn)
    cs = dict(zip(sh["code"], sh["circ_shares"]))
    px["_to"] = px["volume"] / px["code"].map(cs)
    tmap = dict(zip(zip(px["code"], px["trade_date"]), px["_to"]))
    amap = dict(zip(zip(px["code"], px["trade_date"]), px["amount"]))

    small = d[d["mktcap"] < 300].copy()
    small["_cap"] = small["mktcap"]
    small["_to"] = [tmap.get(k) for k in zip(small["code"], small["scan_date"])]
    small["_amt"] = [amap.get(k) for k in zip(small["code"], small["scan_date"])]
    print(f"  {'排序':<14}{'选中':>7}{'市值中位(亿)':>14}{'换手率中位':>12}{'成交额中位(万)':>16}")
    picks = {}
    for nm, ks, asc in (("基线", K_BASE, A_BASE), ("筹码优先", K_CHIP, A_CHIP)):
        p = small[small["final"] == 1].sort_values(ks, ascending=asc) \
            .groupby("scan_date", sort=True).head(top_n)
        picks[nm] = p
        print(f"  {nm:<14}{len(p):>7}{p['_cap'].median():>14.1f}"
              f"{np.nanmedian(p['_to']) * 100:>11.2f}%"
              f"{np.nanmedian(p['_amt']) / 1e4:>16.0f}")
    a, b = picks["筹码优先"], picks["基线"]
    print(f"  → 市值中位差 {a['_cap'].median() - b['_cap'].median():+.1f} 亿；"
          f"成交额中位差 {np.nanmedian(a['_amt']) / 1e4 - np.nanmedian(b['_amt']) / 1e4:+.0f} 万")
    print("     （市值显著更小 → Δ 是市值优势的马甲；持平或更高 → 筹码排序是独立贡献）")

    print(f"\n③ 桶内共线性：conc 与市值/换手率 的秩相关")
    for nm, g in (("<300亿", d[d['mktcap'] < 300]), ("≥300亿", d[d['mktcap'] >= 300])):
        g = g.copy()
        g["_to"] = [tmap.get(k) for k in zip(g["code"], g["scan_date"])]
        c = pd.to_numeric(g["conc"], errors="coerce")
        r_cap = c.rank().corr(g["mktcap"].rank())
        r_to = c.rank().corr(pd.to_numeric(g["_to"], errors="coerce").rank())
        print(f"  {nm:<10} rank(conc) vs rank(市值) = {r_cap:+.3f}   "
              f"rank(conc) vs rank(换手率) = {r_to:+.3f}")
    print("     （|r| < 0.3 → 无共线；conc 的效力不能被市值/换手率替代）")

    print(f"\n④ 逐月 Δ 的配对 t 统计（稳健性 vs 噪音）")
    for nm, g in (("<300亿", d[d["mktcap"] < 300]), ("≥300亿", d[d["mktcap"] >= 300])):
        g = g.copy()
        g["_m"] = g["scan_date"].str.slice(0, 7)
        diffs = []
        for _, gm in sorted(g.groupby("_m")):
            x = _run(gm, K_CHIP, A_CHIP, "x")
            y = _run(gm, K_BASE, A_BASE, "y")
            if x["avg_ret"] is not None and y["avg_ret"] is not None:
                diffs.append(x["avg_ret"] - y["avg_ret"])
        arr = np.array(diffs)
        if len(arr) > 2:
            se = arr.std(ddof=1) / np.sqrt(len(arr))
            t = arr.mean() / se if se > 0 else np.nan
            print(f"  {nm:<10} n月={len(arr):>3}  均值Δ={arr.mean():+.2f}%  "
                  f"标准差={arr.std(ddof=1):.2f}  t={t:+.2f}"
                  f"  {'显著（|t|>2）' if abs(t) > 2 else '不显著（|t|≤2）'}")


def s8_cut_sensitivity(H, EF, ev, top_n: int):
    """切点敏感性：Δ 是否只在 300亿 这一个切点上成立（刀尖结论 vs 平台结论）。

    ⚠ 库中 `stock_info.total_shares == circ_shares` 恒等（4419/4419，比值 1.000），
    故**无法**区分总市值/流通市值——本工具全程只有一种市值口径。
    """
    print("\n" + "=" * 78)
    print("§8 切点敏感性：把二分点从 100 挪到 800亿，Δ 与其显著性是否稳定")
    print("=" * 78)
    d = _sort_keys(ev.copy())
    d["_m"] = d["scan_date"].str.slice(0, 7)
    print(f"{'切点(亿)':<10}{'小侧n':>8}{'小侧Δ':>9}{'小侧t':>8}{'小侧剔3月':>11}"
          f"{'大侧n':>8}{'大侧Δ':>9}{'大侧t':>8}{'大侧剔3月':>11}")
    print("-" * 85)
    for cut in (100, 150, 200, 300, 400, 500, 800):
        out = []
        for lo, hi in ((0, cut), (cut, 1e9)):
            g = d[(d["mktcap"] >= lo) & (d["mktcap"] < hi)]
            n = len(g)
            a = EF._portfolio_by(H, g, "", K_CHIP, A_CHIP, top_n, "c")
            b = EF._portfolio_by(H, g, "", K_BASE, A_BASE, top_n, "b")
            delta = (a["avg_ret"] - b["avg_ret"]) if (a["avg_ret"] is not None
                                                     and b["avg_ret"] is not None) else np.nan
            md = []
            for _, gm in sorted(g.groupby("_m")):
                x = EF._portfolio_by(H, gm, "", K_CHIP, A_CHIP, top_n, "x")
                y = EF._portfolio_by(H, gm, "", K_BASE, A_BASE, top_n, "y")
                if x["avg_ret"] is not None and y["avg_ret"] is not None:
                    md.append(x["avg_ret"] - y["avg_ret"])
            arr = np.array(md)
            if len(arr) > 3:
                se = arr.std(ddof=1) / np.sqrt(len(arr))
                t = arr.mean() / se if se > 0 else np.nan
                k = max(1, int(len(arr) * 0.1))
                trim = (arr.sum() - np.sort(arr)[-k:].sum()) / (len(arr) - k)
            else:
                t, trim = np.nan, np.nan
            out.append((n, delta, t, trim))
        print(f"{cut:<10}{out[0][0]:>8}{_fmt(out[0][1], '{:+.2f}%'):>9}"
              f"{_fmt(out[0][2], '{:+.2f}'):>8}{_fmt(out[0][3], '{:+.2f}%'):>11}"
              f"{out[1][0]:>8}{_fmt(out[1][1], '{:+.2f}%'):>9}"
              f"{_fmt(out[1][2], '{:+.2f}'):>8}{_fmt(out[1][3], '{:+.2f}%'):>11}")
    print("\n  → 若『小侧 t 显著、大侧 t 不显著』在多个切点上重复出现 → 结论是平台而非刀尖")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decay", type=float, default=0.65)
    ap.add_argument("--decay-floor", type=float, default=0.003)
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--is-end", default="2025-06-30")
    ap.add_argument("--sections", default="123456",
                    help="要跑的章节，默认全部")
    ap.add_argument("--chip-tag", default="",
                    help="筹码缓存后缀。修正换手率口径用 'd0.65f0.003_calc'"
                         "（由 eval_chip_factor.py --turnover calc 生成）")
    args = ap.parse_args()

    H, EF, ev = load_data(args.decay, args.decay_floor, args.chip_tag or None)
    sec = set(args.sections)
    if "1" in sec:
        s1_baseline(H, ev)
    if "2" in sec:
        s2_quintile(H, ev)
    if "3" in sec:
        s3_increment(H, EF, ev, args.top_n, args.is_end)
    if "4" in sec:
        s4_monthly(H, EF, ev, args.top_n, "<300亿")
        s4_monthly(H, EF, ev, args.top_n, "≥300亿")
    if "5" in sec:
        s5_turnover(H, EF, ev, args.top_n)
    if "6" in sec:
        s6_article_rules(H, EF, ev, args.top_n)
    if "7" in sec:
        s7_decompose(H, EF, ev, args.top_n, args.is_end)
    if "8" in sec:
        s8_cut_sensitivity(H, EF, ev, args.top_n)


if __name__ == "__main__":
    main()
