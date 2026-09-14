"""短线「高波动 regime 直接不出单」回测（只读，不改任何生产数据）

第三份退出/择时侧假设检验，接续 `tools/eval_short_risk_rules.py`（①ATR 止损 ②熔断）。
用户 2026-09-14 要求跑完最后一条待验证方向。

要回答的问题
------------
2026-07-20~09-10 短线组合 -0.87%（同窗口回测），诊断为「指数收平 + 中途剧烈洗盘」的
高波动 regime 下策略失效。那么：**在波动率高的日子直接不发单，能不能把这段的坑绕开？**

关键口径（吸取熔断被否决的教训）
-------------------------------
熔断被否决的核心证据是「改善全来自交易变少（-34%）＝降仓位，不是选票质量提升」。
所以本工具的主判指标是 **被屏蔽日 vs 保留日的逐信号平均收益**：
- 若被屏蔽日的信号本就明显更差 → 过滤器有**识别能力**（真信号）；
- 若两者接近 → 过滤器只是在**降仓位**（＝熔断的翻版，不应采纳）。

同时给出组合级每笔口径 + 累计复利 + 分半年一致性 + 单月依赖压力测试。

候选池说明
----------
`build_population` 已内置**弱市闸门 `short_market_gate_sql`**（`regime==cold` 或 上证/沪深300
跌破 MA20×0.98 或 5 日动量 < -1.5% 时，抄底类短线一律不发单）。也就是说「弱市不出单」
**已经在线上生效**；本工具测的是**正交的第二个维度：波动率高低**（趋势维度 vs 波动维度）。

用法
----
    python tools/eval_short_regime_filter.py            # 完整报告（秒级，复用行情缓存）
    python tools/eval_short_regime_filter.py --rebuild-regime   # 重算市场 regime 指标
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.db import get_conn                                       # noqa: E402
import eval_short_observe_rule as base                             # noqa: E402
import eval_short_risk_rules as risk                               # noqa: E402

RISK_CACHE = os.path.join(_ROOT, "logs", "_short_risk_sim.pkl")
PRICE_CACHE = os.path.join(_ROOT, "logs", "_short_risk_prices.pkl")
REGIME_CACHE = os.path.join(_ROOT, "logs", "_short_regime_frame.pkl")

MIN_PCT_DAYS = 120        # 分位数最短历史（前 120 个交易日不参与分位判定）
PCT_WIN = 500             # 分位窗口（约 2 年），滚动且只用当日及之前 → 无未来函数


# ───────────────────────── 1. 市场 regime 指标 ─────────────────────────

def _to_day(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).values.astype("datetime64[D]").astype(np.int64)


def build_market_frame(prices: dict) -> pd.DataFrame:
    """全市场等权口径的市场 regime 指标（按交易日）。

    只用 daily_price，覆盖到 2022-06（`index_daily` 仅从 2024-01 起，覆盖不足）。
      - mkt_ret   : 当日全市场个股涨跌幅的**中位数**（等权市场收益，抗极端值）
      - disp      : 当日个股涨跌幅的**横截面标准差**（离散度，越高越"各自乱跑"）
      - atr_med   : 当日个股 Wilder ATR14% 的**中位数**（市场波动水平，个股已算好）
      - vol20     : mkt_ret 的 20 日滚动标准差 ×√252（年化）
      - dd20      : 等权指数（(1+mkt_ret) 累乘）相对 20 日滚动最高的回撤
    """
    ds, rs, ats = [], [], []
    for p in prices.values():
        c = p["close"]
        if c.size < 2:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            r = c[1:] / c[:-1] - 1.0
        ok = np.isfinite(r)
        if not ok.any():
            continue
        ds.append(p["d"][1:][ok])
        rs.append(r[ok])
        ats.append(p["atr_pct"][1:][ok])
    raw = pd.DataFrame({
        "d": np.concatenate(ds),
        "r": np.concatenate(rs),
        "a": np.concatenate(ats),
    })
    g = raw.groupby("d")
    f = pd.DataFrame({
        "date": [str(np.datetime64(int(d), "D")) for d in g.groups.keys()],
        "mkt_ret": g["r"].median().to_numpy(),
        "disp": g["r"].std().to_numpy(),
        "atr_med": g["a"].median().to_numpy(),
        "n_stock": g["r"].size().to_numpy(),
    }).sort_values("date").reset_index(drop=True)

    f["vol20"] = f["mkt_ret"].rolling(20).std() * np.sqrt(252)
    eq = (1.0 + f["mkt_ret"]).cumprod()
    f["dd20"] = eq / eq.rolling(20).max() - 1.0
    return f


def add_rolling_pct(f: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """为每个指标加 `_pct` 列 = 当日值在过去 PCT_WIN 日内的分位（0~1，仅用当日及之前）。"""
    for c in cols:
        f[c + "_pct"] = f[c].rolling(PCT_WIN, min_periods=MIN_PCT_DAYS).apply(
            lambda w: float((w[:-1] < w[-1]).mean()) if len(w) > 1 else np.nan, raw=True)
    return f


def load_regime_frame(prices: dict, rebuild: bool = False) -> pd.DataFrame:
    if not rebuild and os.path.exists(REGIME_CACHE):
        with open(REGIME_CACHE, "rb") as fh:
            return pickle.load(fh)
    print("  计算市场 regime 指标（全市场等权口径）…")
    f = build_market_frame(prices)
    f = add_rolling_pct(f, ["vol20", "disp", "atr_med"])

    # 指数口径（2024-01 起，作为旁证）
    with get_conn() as conn:
        idx = pd.read_sql_query(
            "SELECT code, trade_date, close FROM index_daily ORDER BY code, trade_date", conn)
    if not idx.empty:
        piv = idx.pivot(index="trade_date", columns="code", values="close")
        for icode, tag in (("000852", "zz1000"), ("000001", "sh")):
            if icode not in piv.columns:
                continue
            c = pd.to_numeric(piv[icode], errors="coerce")
            r = c / c.shift(1) - 1.0
            f[f"{tag}_vol20"] = f["date"].map((r.rolling(20).std() * np.sqrt(252)).to_dict())
            eqi = (1.0 + r.fillna(0)).cumprod()
            f[f"{tag}_dd20"] = f["date"].map((eqi / eqi.rolling(20).max() - 1.0).to_dict())
        f = add_rolling_pct(f, [c for c in ("zz1000_vol20", "sh_vol20") if c in f.columns])
    f["d"] = _to_day(f["date"])
    os.makedirs(os.path.dirname(REGIME_CACHE), exist_ok=True)
    with open(REGIME_CACHE, "wb") as fh:
        pickle.dump(f, fh)
    return f


# ───────────────────────── 2. 过滤器网格 ─────────────────────────

# (标签, 指标列, 方向, 阈值)  —— 触及时「当日不发单」
FILTERS: list[tuple[str, str, str, float]] = [
    ("市场波动率 ≥70分位", "vol20_pct", ">=", 0.70),
    ("市场波动率 ≥80分位", "vol20_pct", ">=", 0.80),
    ("市场波动率 ≥90分位", "vol20_pct", ">=", 0.90),
    ("横截面离散度 ≥80分位", "disp_pct", ">=", 0.80),
    ("横截面离散度 ≥90分位", "disp_pct", ">=", 0.90),
    ("市场ATR中位 ≥80分位", "atr_med_pct", ">=", 0.80),
    ("市场ATR中位 ≥90分位", "atr_med_pct", ">=", 0.90),
    ("等权指数20日回撤 ≤-3%", "dd20", "<=", -0.03),
    ("等权指数20日回撤 ≤-5%", "dd20", "<=", -0.05),
    ("等权指数20日回撤 ≤-7%", "dd20", "<=", -0.07),
    ("中证1000波动率 ≥80分位", "zz1000_vol20_pct", ">=", 0.80),
    ("中证1000 20日回撤 ≤-5%", "zz1000_dd20", "<=", -0.05),
]


def blocked_days(regime: pd.DataFrame, col: str, cond: str, thr: float) -> set:
    if col not in regime.columns:
        return set()
    v = pd.to_numeric(regime[col], errors="coerce")
    m = (v >= thr) if cond == ">=" else (v <= thr)
    return set(regime.loc[m.fillna(False), "date"])


# 双条件组合：「高波动」单独无效（甚至反向），但「高波动 且 指数正在跌」才可能是真坏时段
COMBO_FILTERS: list[tuple[str, list[tuple[str, str, float]]]] = [
    ("波动高 且 指数回撤≤-3%", [("vol20_pct", ">=", 0.70), ("dd20", "<=", -0.03)]),
    ("波动高 且 指数回撤≤-5%", [("vol20_pct", ">=", 0.70), ("dd20", "<=", -0.05)]),
    ("波动高 且 中证1000回撤≤-5%", [("vol20_pct", ">=", 0.70), ("zz1000_dd20", "<=", -0.05)]),
    ("波动高 且 中证1000波动高", [("vol20_pct", ">=", 0.70), ("zz1000_vol20_pct", ">=", 0.80)]),
]


def combo_days(regime: pd.DataFrame, conds: list) -> set:
    sets = [blocked_days(regime, c, op, t) for c, op, t in conds]
    sets = [s for s in sets if s]
    return set.intersection(*sets) if len(sets) == len(conds) else set()


# ───────────────────────── 3. 评估 ─────────────────────────

def _per_trade(df: pd.DataFrame) -> dict:
    ev = df[(df["final"] == 1) & (df["filled"] == 1)]
    rets = pd.to_numeric(ev["ret"], errors="coerce").dropna()
    if rets.empty:
        return {"n": 0, "avg": None, "win": None, "pf": None, "stop": None,
                "worst": None, "days": 0}
    wins, losses = rets[rets > 0], rets[rets <= 0]
    return {
        "n": int(len(rets)),
        "avg": round(float(rets.mean()), 2),
        "win": round(float(len(wins) / len(rets) * 100), 1),
        "pf": round(float(wins.sum() / abs(losses.sum())), 2) if len(losses) else None,
        "stop": round(float((ev["exit_reason"] == "stop_loss").sum()) / len(rets) * 100, 1),
        "worst": round(float(rets.min()), 2),
        "days": int(df["scan_date"].nunique()),
    }


def _compound(picked: pd.DataFrame) -> float | None:
    """等权复利：按出场日排序把已结算单连乘（不再是 sum(单笔%) 的无意义口径）。"""
    ev = picked[(picked["final"] == 1) & (picked["filled"] == 1)].copy()
    if ev.empty:
        return None
    ev["_e"] = ev["exit_date"].fillna("9999-12-31")
    r = pd.to_numeric(ev.sort_values("_e")["ret"], errors="coerce").dropna() / 100.0
    return round(float((1.0 + r).prod() - 1.0) * 100, 1) if len(r) else None


def eval_filter(df: pd.DataFrame, name: str, blocked: set, top_n: int, asc: bool) -> dict:
    keep = df[~df["scan_date"].isin(blocked)]
    cut = df[df["scan_date"].isin(blocked)]
    kp, cp = _per_trade(keep), _per_trade(cut)
    m = risk.portfolio(keep, top_n, asc, name)
    picked = risk.portfolio_trades(keep, top_n, asc)
    return {
        "label": name,
        # ⚠ 必须按当前窗口内的交易日计数——用全局 len(blocked) 会把窗口外的高波动日也算进来
        "blocked_days": int(df["scan_date"].nunique() - keep["scan_date"].nunique()),
        "keep": kp, "cut": cp,
        "port": m, "cum": _compound(picked),
    }


def half_consistency(df: pd.DataFrame, blocked: set) -> tuple[int, int]:
    """分半年：保留日子集的逐信号均值 > 全样本同半年均值 → 记 1。返回 (胜出半年数, 总半年数)。"""
    keep, allv = df[~df["scan_date"].isin(blocked)], df
    win = tot = 0
    for h in sorted(allv["half"].unique()):
        a = _per_trade(allv[allv["half"] == h])["avg"]
        k = _per_trade(keep[keep["half"] == h])["avg"]
        if a is None or k is None:
            continue
        tot += 1
        win += int(k > a)
    return win, tot


def _f(v, s="{:.2f}"):
    return "--" if v is None else s.format(v)


def show_signal_table(df: pd.DataFrame, blocked_map: dict, title: str):
    print(f"\n=== ① 识别能力检验（逐信号平均收益，未成交不计）· {title} ===")
    tot_days = int(df["scan_date"].nunique())
    print(f"{'过滤器':<26}{'屏蔽日':>7}{'保留笔':>7}{'保留均值':>9}{'屏蔽笔':>7}"
          f"{'屏蔽均值':>9}{'差值':>8}{'屏蔽胜率':>9}{'屏蔽止损率':>11}")
    b = _per_trade(df)
    print(f"{'（基线·不屏蔽）':<26}{0:>7}{b['n']:>7}"
          f"{_f(b['avg'], '{:+.2f}%'):>9}{'--':>7}{'--':>9}{'--':>8}"
          f"{_f(b['win'], '{:.1f}%'):>9}{_f(b['stop'], '{:.1f}%'):>11}")
    for label, blocked in blocked_map.items():
        r = eval_filter(df, label, blocked, 1, True)
        k, c = r["keep"], r["cut"]
        diff = (None if (k["avg"] is None or c["avg"] is None) else round(c["avg"] - k["avg"], 2))
        print(f"{label:<26}{r['blocked_days']}/{tot_days:<4}{k['n']:>7}"
              f"{_f(k['avg'], '{:+.2f}%'):>9}{c['n']:>7}{_f(c['avg'], '{:+.2f}%'):>9}"
              f"{_f(diff, '{:+.2f}%'):>8}{_f(c['win'], '{:.1f}%'):>9}"
              f"{_f(c['stop'], '{:.1f}%'):>11}")
    print("\n  读法：『差值』= 屏蔽日均值 − 保留日均值。**负得越多 = 过滤器越有识别能力**；")
    print("        若接近 0 → 只是少做几笔（降仓位），不是选票质量提升（＝熔断被否决的同款陷阱）。")


def show_portfolio_table(cache: dict, variants: list, blocked_map: dict,
                         df_key: str, top_n: int, asc: bool, title: str):
    print(f"\n=== ② 组合级 每日top{top_n} · {title} ===")
    print(f"{'过滤器':<26}{'保留日':>7}{'成交':>7}{'日均':>6}{'每笔均':>9}{'中位':>8}"
          f"{'胜率':>8}{'盈亏比':>8}{'等权复利':>10}{'止损率':>8}{'半年胜':>8}")
    allv = cache[df_key]
    allv = allv[allv["final"] == 1]
    for label, blocked in [("（基线·不屏蔽）", set())] + list(blocked_map.items()):
        keep = allv[~allv["scan_date"].isin(blocked)]
        m = risk.portfolio(keep, top_n, asc, label)
        picked = risk.portfolio_trades(keep, top_n, asc)
        w, t = half_consistency(allv, blocked)
        print(f"{label:<26}{m['days']:>7}{m['n_fill']:>7}"
              f"{_f(m['per_day'], '{:.1f}'):>6}{_f(m['avg_ret'], '{:+.2f}%'):>9}"
              f"{_f(m['median_ret'], '{:+.2f}%'):>8}{_f(m['win_rate'], '{:.1f}%'):>8}"
              f"{_f(m['pf']):>8}{_f(_compound(picked), '{:+.0f}%'):>10}"
              f"{_f(m['stop_rate'], '{:.1f}%'):>8}{f'{w}/{t}':>8}")


def show_overlay(cache: dict, blocked_map: dict, top_n: int, asc: bool, title: str):
    """③ 已在 ATR2.5× 止损之上，再加 regime 过滤还有没有增量（决策关键）。

    若 ATR 止损已把大部分坑填掉，则 regime 过滤的边际价值可能为 0 → 不值得加复杂度。
    """
    key = "atr2.5_f5_c15" if "atr2.5_f5_c15" in cache else "base"
    allv = cache[key]
    allv = allv[allv["final"] == 1]
    print(f"\n=== ③ ATR2.5× 止损之上叠加 regime 过滤 · {title} ===")
    print(f"{'过滤器':<26}{'保留日':>7}{'成交':>7}{'每笔均':>9}{'中位':>8}{'胜率':>8}"
          f"{'盈亏比':>8}{'等权复利':>10}{'止损率':>8}")
    for label, blocked in [("（仅 ATR2.5×，无 regime 过滤）", set())] + list(blocked_map.items()):
        keep = allv[~allv["scan_date"].isin(blocked)]
        m = risk.portfolio(keep, top_n, asc, label)
        picked = risk.portfolio_trades(keep, top_n, asc)
        print(f"{label:<26}{m['days']:>7}{m['n_fill']:>7}{_f(m['avg_ret'], '{:+.2f}%'):>9}"
              f"{_f(m['median_ret'], '{:+.2f}%'):>8}{_f(m['win_rate'], '{:.1f}%'):>8}"
              f"{_f(m['pf']):>8}{_f(_compound(picked), '{:+.0f}%'):>10}"
              f"{_f(m['stop_rate'], '{:.1f}%'):>8}")


def show_time_consistency(df: pd.DataFrame, blocked_map: dict, labels: list, title: str):
    """④ 分半年看『屏蔽日均值 − 保留日均值』：负 = 该半年过滤有效。

    熔断被否决的第三条证据就是「时间上不一致」。若某个过滤器只在逆风段有效、
    在其余时段帮倒忙，它就不是规则，是对最近一次伤痛的过拟合。
    """
    halves = sorted(df["half"].unique())
    print(f"\n=== ④ 时间一致性（分半年 · 屏蔽日均值 − 保留日均值，负=过滤有效）· {title} ===")
    print(f"{'过滤器':<26}" + "".join(f"{h:>10}" for h in halves) + f"{'方向正确':>10}")
    for label in labels:
        bl = blocked_map.get(label, set())
        cells, ok, tot = [], 0, 0
        for h in halves:
            hdf = df[df["half"] == h]
            if hdf[hdf["final"] == 1].empty:
                cells.append("--"); continue
            k = _per_trade(hdf[~hdf["scan_date"].isin(bl)])
            c = _per_trade(hdf[hdf["scan_date"].isin(bl)])
            if k["avg"] is None or c["avg"] is None:
                cells.append("--"); continue
            tot += 1
            ok += int(c["avg"] < k["avg"])
            cells.append(f"{c['avg'] - k['avg']:+.2f}")
        print(f"{label:<26}" + "".join(f"{v:>10}" for v in cells) + f"{f'{ok}/{tot}':>10}")


def drop_month_test(df: pd.DataFrame, label: str, blocked: set,
                    top_n: int, asc: bool, drops: list[tuple[str, tuple]]):
    """⑤ 剔除关键月份后是否仍成立（单月依赖压力测试，最关键的证伪检验）。"""
    print(f"\n=== ⑤ 剔除关键月份 · {label} ===")
    allv = df[df["final"] == 1]
    for tag, prefixes in drops:
        keep_mask = allv["scan_date"].str.slice(0, 7).isin(list(prefixes))
        a = _per_trade(allv[~keep_mask])
        k = _per_trade(allv[(~keep_mask) & (~allv["scan_date"].isin(blocked))])
        pa = risk.portfolio(allv[~keep_mask], top_n, asc, "x")
        pk = risk.portfolio(allv[(~keep_mask) & (~allv["scan_date"].isin(blocked))], top_n, asc, "x")
        if a["avg"] is None or k["avg"] is None:
            print(f"  剔除 {tag}：样本不足"); continue
        print(f"  剔除 {tag:<12} 逐信号 保留 {k['avg']:+.2f}% vs 全样本 {a['avg']:+.2f}%"
              f"（差 {k['avg'] - a['avg']:+.2f}pp） ｜ 组合每笔 {pk['avg_ret']:+.2f}% vs {pa['avg_ret']:+.2f}%"
              f"（差 {pk['avg_ret'] - pa['avg_ret']:+.2f}pp）")


def month_stress(df: pd.DataFrame, label: str, blocked: set, top_n: int, asc: bool):
    """单月依赖压力测试：找出对结论贡献最大的月份，剔除后看优势是否还在。"""
    keep = df[~df["scan_date"].isin(blocked)]
    allv = df[df["final"] == 1]
    keepv = keep[keep["final"] == 1]
    months = sorted(set(allv["scan_date"].str.slice(0, 7)))
    contrib = {}
    for mo in months:
        a = _per_trade(allv[allv["scan_date"].str.startswith(mo)])["avg"]
        k = _per_trade(keepv[keepv["scan_date"].str.startswith(mo)])["avg"]
        if a is not None and k is not None:
            contrib[mo] = (k - a, k, a)
    if not contrib:
        return
    bad = sorted(contrib.items(), key=lambda kv: kv[1][0])[-3:]
    print(f"\n=== ③ 单月依赖压力测试 · {label} ===")
    print("  贡献最大的 3 个月（保留 − 全样本，逐信号均值）：")
    for mo, (d, k, a) in bad:
        print(f"    {mo}  保留 {k:+.2f}%  全样本 {a:+.2f}%  差 {d:+.2f}pp")
    for mo, _ in bad:
        sub_all = allv[~allv["scan_date"].str.startswith(mo)]
        sub_keep = keepv[~keepv["scan_date"].str.startswith(mo)]
        m_a = _per_trade(sub_all)["avg"]
        m_k = _per_trade(sub_keep)["avg"]
        pa = risk.portfolio(sub_all, top_n, asc, "x")
        pk = risk.portfolio(sub_keep, top_n, asc, "x")
        print(f"  剔除 {mo}：逐信号 全样本 {m_a:+.2f}% → 保留 {m_k:+.2f}%（差 {m_k - m_a:+.2f}pp）"
              f" ｜ 组合每笔 {pa['avg_ret']:+.2f}% → {pk['avg_ret']:+.2f}%")


# ───────────────────────── 4. 主流程 ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-regime", action="store_true")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--asc", type=int, default=1, choices=(0, 1))
    ap.add_argument("--win-start", default="2026-07-20")
    ap.add_argument("--end", default="2026-09-10")
    args = ap.parse_args()
    asc, top_n = bool(args.asc), args.top_n

    with open(PRICE_CACHE, "rb") as fh:
        prices = pickle.load(fh)
    regime = load_regime_frame(prices, args.rebuild_regime)
    with open(RISK_CACHE, "rb") as fh:
        cache = pickle.load(fh)

    base_df = cache["base"]
    ev = base_df[base_df["final"] == 1]
    print(f"\n候选池（**已含线上弱市闸门**）：{len(base_df):,} 条信号 / "
          f"{ev['scan_date'].nunique()} 个有效交易日 / {ev['scan_date'].min()} ~ {ev['scan_date'].max()}")
    print(f"市场 regime 指标覆盖：{regime['date'].min()} ~ {regime['date'].max()}"
          f"（分位需 {MIN_PCT_DAYS} 日预热）")
    b = _per_trade(ev)
    print(f"基线逐信号 均值 {b['avg']:+.2f}% / 胜率 {b['win']:.1f}% / 止损率 {b['stop']:.1f}%"
          f" ｜ 波动率中位 {regime['vol20'].median()*100:.1f}%(年化)  离散度中位 {regime['disp'].median()*100:.2f}%")

    blocked_map = {lab: blocked_days(regime, c, op, t) for lab, c, op, t in FILTERS}
    blocked_map.update({lab: combo_days(regime, conds) for lab, conds in COMBO_FILTERS})

    for lo, hi, tag in (("0000-01-01", "9999-12-31", "全期"),
                        (args.win_start, args.end, f"同窗口 {args.win_start}~{args.end}")):
        sub = base_df[(base_df["scan_date"] >= lo) & (base_df["scan_date"] <= hi)]
        if sub[sub["final"] == 1].empty:
            print(f"\n### {tag}：无样本"); continue
        print(f"\n{'='*100}\n### {tag}（终态 {int((sub['final']==1).sum()):,} 条）\n{'='*100}")
        show_signal_table(sub, blocked_map, tag)
        sub_cache = {k: v[(v["scan_date"] >= lo) & (v["scan_date"] <= hi)] for k, v in cache.items()}
        show_portfolio_table(sub_cache, [], blocked_map, "base", top_n, asc, tag)
        if asc:  # 线上现行升序；降序见 --asc 0
            print(f"\n--- 同表 · 扩展度降序（对照，若已决定改降序则以本表为准）---")
            show_portfolio_table(sub_cache, [], blocked_map, "base", top_n, False, tag)
        show_overlay(sub_cache, blocked_map, top_n, asc, tag)

    # ── ④⑤ 全期：时间一致性 + 剔除关键月份（决定性检验） ──
    KEY = ["市场波动率 ≥70分位", "市场ATR中位 ≥80分位", "等权指数20日回撤 ≤-7%",
           "中证1000波动率 ≥80分位", "波动高 且 指数回撤≤-5%", "波动高 且 中证1000回撤≤-5%"]
    print(f"\n{'#'*100}\n# 决定性问题：这套过滤器是「规则」还是「对最近一次伤痛的过拟合」？\n{'#'*100}")
    show_time_consistency(base_df, blocked_map, KEY, "全期")
    drops = [("2024-10", ("2024-10",)),
             ("2026-08", ("2026-08",)),
             ("2026-08+09", ("2026-08", "2026-09")),
             ("2024-10+2026-08+09", ("2024-10", "2026-08", "2026-09"))]
    for lab in ("市场波动率 ≥70分位", "中证1000波动率 ≥80分位"):
        drop_month_test(base_df, lab, blocked_map.get(lab, set()), top_n, asc, drops)

    # 同窗口表现最好的过滤器 → 全期做单月压力测试
    win_all = base_df[(base_df["scan_date"] >= args.win_start) & (base_df["scan_date"] <= args.end)]
    if not win_all[win_all["final"] == 1].empty:
        scored = []
        for lab, bl in blocked_map.items():
            if not bl:
                continue
            k = _per_trade(win_all[~win_all["scan_date"].isin(bl)])
            if k["n"] > 30:
                scored.append((k["avg"], lab, bl))
        if scored:
            scored.sort(reverse=True)
            avg, lab, bl = scored[0]
            print(f"\n{'='*100}\n### 同窗口最优过滤器『{lab}』全期压力测试\n{'='*100}")
            month_stress(base_df, lab, bl, top_n, asc)

    print("\n" + "=" * 100)
    print("说明：候选池已含线上弱市闸门（趋势维度）；本工具测的是正交的波动维度。")
    print("      分位数为滚动窗口且只用当日及之前 → 无未来函数；前 120 日不判定（不屏蔽）。")
    print("=" * 100)


if __name__ == "__main__":
    main()
