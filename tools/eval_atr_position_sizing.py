"""ATR 反算仓位（风险平价）对比回测：等权 vs「w ∝ 1/止损宽度」——只读，不改生产。

问题
----
ATR 自适应止损落地后，**同一交易日内不同候选的止损宽度可相差 3 倍**（下限 5% ~ 上限 15%）。
等权买入 = 每笔「跌到止损」时对组合的伤害相差 3 倍。ATR 反算仓位让
**每笔的目标风险相等**（风险平价）：

    wᵢ ∝ 1 / 止损宽度ᵢ        其中 止损宽度 = clamp(k×ATR14%, floor, cap)
                              → 反向宽度有界（1/0.15 ~ 1/0.05，比值 ≤ 3×）

口径（严格遵守用户约束）
----
- 测试系统 = **每只股每买日 1 手，不做资金约束**，故只统计**收益比例**，
  **不折算任何金额**。所有指标都是比率口径（%）。本工具不产出任何 ¥ 数字。
- 权重在**当日篮子内**归一化（当日总仓位 = 100%），**单票不设上限**（用户指定）。
  `--weight-cap` 可做对照臂，用来检验「不设上限」是否真的无害（集中度诊断）。
- 未成交（no_fill）**不进入篮子**：没成交就没有仓位，篮子只含实际成交笔。
- ⚠ 复利/MDD 列是「若每日满仓轮动」的**参考**指标 —— 真实持仓期最长达 10 日、
  批次重叠，它不是真实资金曲线。**结论请以「平均每笔篮子收益 / 资本加权胜率」为准。**

数据来源（复用已验证的回放，不重写任何入场/出场数学）
----
- `--source short`：读 `logs/_short_risk_sim.pkl`（`tools/eval_short_risk_rules.py` 产出，
  含线上同口径的 ATR 止损回放结果）→ 秒级。
- `--source deep` ：`import eval_deep_atr_stop`，复刻 deep_track 的
  `_try_fill` + `_run_exit`（`sim_one` 直接返回 `pct` = 该臂止损宽度）。

用法
----
    python tools/eval_atr_position_sizing.py                          # 短线线（已落地的 ATR 2.5×）
    python tools/eval_atr_position_sizing.py --rule base              # 短线线对照组
    python tools/eval_atr_position_sizing.py --weight-cap 0.40        # 加一条「单票≤40%」对照
    python tools/eval_atr_position_sizing.py --source deep --scope top4 --arm cap8
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

SHORT_CACHE = os.path.join(_ROOT, "logs", "_short_risk_sim.pkl")
RULE_DEFAULT = "atr2.5_f5_c15"          # 短线线 2026-09-14 已落地的 ATR 参数
TRADING_DAYS = 244                      # A股年化交易日


# ───────────────────────── 1. 权重方案 ─────────────────────────

def cap_renorm(w: np.ndarray, cap: float, iters: int = 100) -> np.ndarray:
    """单票权重上限 + 迭代再归一（超出部分按剩余权重比例回填）。

    设上限只为**对照诊断**；主口径 `cap=None` 表示**单票不设上限**（用户指定）。
    """
    w = np.asarray(w, dtype=float).copy()
    if not cap or cap <= 0 or cap >= 1.0 or len(w) == 0:
        return w
    for _ in range(iters):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = ~over
        if not free.any():
            break
        s = float(w[free].sum())
        if s <= 0:
            w[free] = 1.0 / len(w)
            break
        w[free] += excess * w[free] / s
    return w


def norm_weights(widths, scheme: str, cap: float | None = None) -> np.ndarray:
    """当日篮子的权重（归一化，Σw = 1）。

    - `eq`   : 等权 1/n（基线）
    - `rva`  : **ATR 反算**（用户原意）w ∝ 1/ATR%_raw —— 不做夹逼，保留真实波动差异
    - `rp`   : 风险平价 w ∝ 1/止损宽度（= 夹逼后的 k×ATR，比值上限 3×）
    - `rp05` : 半平价   w ∝ 1/√止损宽度（更温和，防过度集中）

    `rva` 与 `rp` 的差别是**关键**：止损宽度被 clamp 到 [5%,15%] 后，
    ATR 2% 与 2.5% 的票同权重、ATR 6% 与 7% 的票同权重 → 真实波动差异被抹平。
    如果风险确实来自波动，`rva` 才是正确口径。

    宽度缺失/非正 → 该篮子整体回退等权（绝不因脏数据静默变成资金集中）。
    """
    wd = np.asarray(widths, dtype=float)
    n = len(wd)
    if n == 0:
        return np.zeros(0)
    if scheme == "eq" or not np.all(np.isfinite(wd)) or np.any(wd <= 0):
        w = np.full(n, 1.0 / n)
    else:
        inv = 1.0 / wd
        if scheme in ("rp05", "rva05"):
            inv = np.sqrt(inv)
        w = inv / inv.sum()
    return cap_renorm(w, cap) if cap else w


# ───────────────────────── 2. 交易日篮子 ─────────────────────────

def cohorts(trades: pd.DataFrame, scheme: str, cap: float | None = None) -> pd.DataFrame:
    """把一个交易日的成交笔聚成一个「篮子」，返回逐日篮子表。

    `day_ret` = Σ wᵢ·rᵢ（权重和=1）→ 与「平均每笔」同量纲，可直接跨方案比较；
    `n_win/n` 是**笔胜率**（与权重无关 → 各方案必须相同，可作口径自检）；
    `w_win` 是**资本口径胜率**（赢家占用的资金份额）；`wmax/hhi` 是集中度诊断。
    """
    recs = []
    for d, g in trades.groupby("scan_date", sort=True):
        r = g["ret"].to_numpy(dtype=float)
        # rva* = ATR 反算（用原始 ATR）；rp* = 止损宽度反算（用夹逼后的宽度）
        col = "atr" if scheme.startswith("rva") else "width"
        vals = (g[col].to_numpy(dtype=float) if col in g.columns
                else g["width"].to_numpy(dtype=float))
        if not np.all(np.isfinite(vals)) or np.any(vals <= 0):
            vals = g["width"].to_numpy(dtype=float)
        w = norm_weights(vals, scheme, cap)
        pos = r > 0
        wr = w * r
        recs.append({
            "scan_date": d, "n": len(g), "day_ret": float(w @ r),
            "wmax": float(w.max()) if len(w) else 0.0,
            "hhi": float((w * w).sum()) if len(w) else 0.0,
            "w_win": float(w[pos].sum()),
            "gain": float(wr[pos].sum()), "loss": float(-wr[~pos].sum()),
            "n_win": int(pos.sum()),
        })
    return pd.DataFrame(recs)


def stats(cf: pd.DataFrame, label: str) -> dict:
    """比率口径指标组（无金额）。"""
    if cf is None or cf.empty:
        return {"label": label, "days": 0, "n_trade": 0, "avg_basket": None,
                "compound": None, "mdd": None, "vol": None, "sharpe": None,
                "win_cap": None, "win_trade": None, "pf": None, "wmax_med": None,
                "wmax_p95": None, "per_day": None}
    r = cf["day_ret"].to_numpy(dtype=float)
    n = len(r)
    eq = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    loss = float(cf["loss"].sum())
    return {
        "label": label,
        "days": n,
        "n_trade": int(cf["n"].sum()),
        "per_day": round(float(cf["n"].mean()), 2),
        "avg_basket": round(float(r.mean()) * 100, 3),                    # 平均每篮（≈平均每笔、资本口径）
        "compound": round((float(eq[-1]) - 1.0) * 100, 1),                # 参考：每日满仓轮动复利
        "mdd": round(float((eq / peak - 1.0).min()) * 100, 2),            # 参考
        "vol": round(sd * np.sqrt(TRADING_DAYS) * 100, 2),
        "sharpe": round(float(r.mean()) / sd * np.sqrt(TRADING_DAYS), 2) if sd > 0 else None,
        "win_cap": round(float(cf["w_win"].mean()) * 100, 2),             # 资本加权胜率
        "win_trade": round(float(cf["n_win"].sum()) / float(cf["n"].sum()) * 100, 2),
        "pf": round(float(cf["gain"].sum()) / loss, 3) if loss > 0 else None,
        "wmax_med": round(float(cf["wmax"].median()) * 100, 1),
        "wmax_p95": round(float(cf["wmax"].quantile(0.95)) * 100, 1),
    }


def pairwise(a: pd.DataFrame, b: pd.DataFrame, label: str) -> dict:
    """逐日配对差（a − b），同一批交易日才可比。"""
    m = (a.set_index("scan_date")["day_ret"].rename("a").to_frame()
         .join(b.set_index("scan_date")["day_ret"].rename("b"), how="inner"))
    if m.empty:
        return {"label": label, "n": 0, "diff": None, "t": None, "win": None}
    d = (m["a"] - m["b"]).to_numpy(dtype=float)
    n = len(d)
    sd = float(d.std(ddof=1)) if n > 1 else 0.0
    t = float(d.mean()) / (sd / np.sqrt(n)) if sd > 0 else 0.0
    return {"label": label, "n": n, "diff": round(float(d.mean()) * 100, 3),
            "t": round(t, 2), "win": round(float((d > 0).mean()) * 100, 1)}


# ───────────────────────── 3. 打印 ─────────────────────────

def show(rows: list[dict], title: str):
    print(f"\n=== {title} ===")
    print(f"{'仓位方案':<24}{'交易日':>6}{'笔数':>7}{'每日笔':>7}"
          f"{'平均每篮':>10}{'胜率(资本)':>11}{'胜率(笔)':>9}{'PF':>7}"
          f"{'复利*':>10}{'最大回撤*':>10}{'夏普':>7}{'最大单票(中/95)':>16}")
    for r in rows:
        f = lambda v, s="{:.2f}": (s.format(v) if v is not None else "--")  # noqa: E731
        wm = "--" if r["wmax_med"] is None else f"{r['wmax_med']:.1f}/{r['wmax_p95']:.1f}%"
        print(f"{r['label']:<24}{r['days']:>6}{r['n_trade']:>7}"
              f"{f(r['per_day'], '{:.1f}'):>7}"
              f"{f(r['avg_basket'], '{:+.3f}%'):>10}"
              f"{f(r['win_cap'], '{:.1f}%'):>11}{f(r['win_trade'], '{:.1f}%'):>9}"
              f"{f(r['pf']):>7}"
              f"{f(r['compound'], '{:+.0f}%'):>10}{f(r['mdd'], '{:.1f}%'):>10}"
              f"{f(r['sharpe']):>7}{wm:>16}")


def show_pair(rows: list[dict], title: str):
    print(f"\n--- {title}（逐日配对，同一批交易日）---")
    print(f"{'对比':<32}{'配对数':>8}{'平均日差':>11}{'t值':>9}{'胜出日占比':>12}")
    for r in rows:
        f = lambda v, s="{:.2f}": (s.format(v) if v is not None else "--")  # noqa: E731
        print(f"{r['label']:<32}{r['n']:>8}{f(r['diff'], '{:+.3f}%'):>11}"
              f"{f(r['t'], '{:+.2f}'):>9}{f(r['win'], '{:.1f}%'):>12}")


# ───────────────────────── 4. 数据来源 ─────────────────────────

def trades_short(rule: str, top_n: int, asc: bool) -> pd.DataFrame:
    """短线线：读缓存 → 线上排序取每日 top_n → 只留成交笔。"""
    import eval_short_risk_rules as rk
    with open(rk.CACHE_FILE, "rb") as fh:
        cache = pickle.load(fh)
    if rule not in cache:
        raise SystemExit(f"rule '{rule}' 不在缓存中，可选：{list(cache.keys())}")
    df = cache[rule]
    picked = rk._pick_daily(df, top_n, asc)          # final==1 域（含未成交）
    picked = picked[picked["filled"] == 1]           # 只留实际成交
    # ⚠ 缓存里的 ret 是**百分数**（如 2.5 表示 +2.5%）。此处统一转成小数，
    #   否则 cumprod(1+r) 会指数爆炸（历史踩坑：复利列打出 1e300+）。
    t = pd.DataFrame({
        "scan_date": picked["scan_date"].to_numpy(),
        "code": picked["code"].to_numpy(),
        "ret": pd.to_numeric(picked["ret"], errors="coerce").fillna(0.0).to_numpy(float) / 100.0,
        "width": pd.to_numeric(picked["stop_pct_used"], errors="coerce").to_numpy(float),
        # 短线缓存的 atr_pct 已是小数（atr/close）→ 直接用
        "atr": pd.to_numeric(picked["atr_pct"], errors="coerce").to_numpy(float),
    })
    t = t[np.isfinite(t["width"].to_numpy(float)) & (t["width"] > 0)]
    print(f"数据源：短线线缓存 rule={rule} · 每日 top{top_n} · 成交 {len(t):,} 笔 / "
          f"{t['scan_date'].nunique()} 个交易日")
    print(f"止损宽度：中位 {t['width'].median()*100:.2f}% "
          f"· P05 {t['width'].quantile(0.05)*100:.2f}% · P95 {t['width'].quantile(0.95)*100:.2f}% "
          f"· 宽度比(P95/P05) {t['width'].quantile(0.95)/t['width'].quantile(0.05):.2f}×")
    a = t["atr"][np.isfinite(t["atr"].to_numpy(float)) & (t["atr"].to_numpy(float) > 0)]
    if len(a):
        print(f"原始 ATR14%：中位 {a.median()*100:.2f}% · P05 {a.quantile(0.05)*100:.2f}% "
              f"· P95 {a.quantile(0.95)*100:.2f}% · 波动比(P95/P05) "
              f"{a.quantile(0.95)/a.quantile(0.05):.2f}×  ← ATR 反算的真实分散度")
    return t


def trades_deep(arm_key: str, scope: str) -> pd.DataFrame:
    """个股深度：复用 eval_deep_atr_stop 的候选池/行情/单笔回放。"""
    import eval_deep_atr_stop as dp
    ew, mh = dp._entry_window(), dp._max_hold()
    arm = next((a for a in dp.ARMS if a[0] == arm_key), None)
    if arm is None:
        raise SystemExit(f"arm '{arm_key}' 不存在，可选：{[a[0] for a in dp.ARMS]}")
    _, alabel, mode, kw = arm
    print(f"数据源：个股深度 scope={scope} · 臂={arm_key}（{alabel}）"
          f" · 回踩窗口 {ew} 日 · 持仓上限 {mh} 日")
    pop = dp.load_population([scope])
    codes = sorted({c for (_, c) in pop[scope]})
    prices = dp.load_prices(codes)
    recs = []
    for sig in pop[scope].values():
        p = prices.get(sig["code"])
        if p is None or not dp.complete_domain(sig, p, ew, mh):
            continue
        out = dp.sim_one(sig, p, mode, kw, ew, mh)
        if out is None:
            continue
        ret, _reason, _hold, pct, _filled = out
        # sim_one 返回的 ret 同样是百分数 → 转小数（与短线线口径统一）
        # 深度的 atr_pct 是百分数（stop_pct_for 里 /100），这里一并转小数
        _a = sig.get("atr_pct")
        recs.append({"scan_date": sig["scan_date"], "code": sig["code"],
                     "ret": float(ret) / 100.0, "width": float(pct),
                     "atr": (float(_a) / 100.0) if (_a and np.isfinite(float(_a)) and float(_a) > 0)
                            else float(pct)})
    t = pd.DataFrame(recs)
    if not t.empty:
        t = t[np.isfinite(t["width"].to_numpy(float)) & (t["width"] > 0)]
    print(f"  成交 {len(t):,} 笔 / {t['scan_date'].nunique() if len(t) else 0} 个交易日")
    if not t.empty:
        print(f"止损宽度：中位 {t['width'].median()*100:.2f}% "
              f"· P95 {t['width'].quantile(0.95)*100:.2f}% "
              f"· 宽度比(P95/P05) {t['width'].quantile(0.95)/t['width'].quantile(0.05):.2f}×")
    return t


# ───────────────────────── 5. 主流程 ─────────────────────────

def schemes_for(cap: float | None) -> list[tuple[str, str, float | None]]:
    """(key, label, cap)。主口径 cap=None = 单票不设上限。"""
    out = [("eq", "等权 1/n（基线）", None),
           ("rva", "ATR 反算 w∝1/ATR%", None),
           ("rp", "止损宽度反算 w∝1/宽度", None),
           ("rp05", "半平价 w∝1/√宽度", None)]
    if cap:
        out.append(("rp", f"止损宽度反算 + 单票≤{cap:.0%}", cap))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("short", "deep"), default="short")
    ap.add_argument("--rule", default=RULE_DEFAULT, help="短线线缓存变体名")
    ap.add_argument("--arm", default="base", help="deep 臂 key（见 eval_deep_atr_stop.ARMS）")
    ap.add_argument("--scope", default="top4", help="deep scope: top4/top20/buy/all")
    ap.add_argument("--top-n", type=int, default=3, help="短线线每日笔数（线上 top3）")
    ap.add_argument("--asc", type=int, default=1, choices=(0, 1), help="扩展度升序=1（线上）")
    ap.add_argument("--weight-cap", type=float, default=None,
                    help="对照臂：单票权重上限（如 0.4）；不填=不设上限（用户指定）")
    ap.add_argument("--win-start", default="2026-07-20", help="同窗口起点")
    ap.add_argument("--is-end", default="2025-06-30", help="样本内结束日")
    args = ap.parse_args()

    if args.source == "short":
        trades = trades_short(args.rule, args.top_n, bool(args.asc))
    else:
        trades = trades_deep(args.arm, args.scope)
    if trades.empty:
        raise SystemExit("无成交样本")
    # 量纲自检：ret 必须是小数（单笔收益中位数不可能超过 50%）
    _med = float(trades["ret"].median())
    if abs(_med) > 0.5:
        raise SystemExit(f"ret 量纲可疑（中位 {_med:.3f}，应为小数）—— 检查数据源单位换算")
    print(f"单笔收益：中位 {_med*100:+.2f}% · 均值 {trades['ret'].mean()*100:+.2f}%")

    schemes = schemes_for(args.weight_cap)

    spans = [("0000-01-01", "9999-12-31", "全期"),
             (args.win_start, "9999-12-31", f"同窗口（≥{args.win_start}）"),
             ("0000-01-01", args.is_end, f"样本内（≤{args.is_end}）"),
             (args.is_end, "9999-12-31", f"样本外（>{args.is_end}）")]

    for lo, hi, tag in spans:
        sub = trades[(trades["scan_date"] >= lo) & (trades["scan_date"] <= hi)]
        if len(sub) < 20:
            print(f"\n### {tag}：样本不足（{len(sub)} 笔），跳过")
            continue
        print(f"\n{'='*118}\n### {tag} · {len(sub):,} 笔 / {sub['scan_date'].nunique()} 个交易日"
              f" / 每日 {len(sub)/sub['scan_date'].nunique():.1f} 笔\n{'='*118}")
        cfs, rows = {}, []
        for key, label, cap in schemes:
            cf = cohorts(sub, key, cap)
            cfs[label] = cf
            rows.append(stats(cf, label))
        show(rows, f"仓位方案对比 · {tag}")

        eq_label = schemes[0][1]
        pairs = [pairwise(cfs[l], cfs[eq_label], f"{l} − 等权")
                 for l in [s[1] for s in schemes[1:]]]
        show_pair(pairs, "相对等权的增量")

        # 集中度提示：主口径「单票不设上限」，需把集中度代价显式暴露出来
        hot = [r for r in rows if r["wmax_p95"] is not None and r["wmax_p95"] > 45]
        for hr in hot:
            print(f"  ⚠ 集中度：{hr['label']} 的 P95 单票权重达 {hr['wmax_p95']:.1f}%"
                  f"（等权基准 {rows[0]['wmax_p95']:.1f}%）→ 加 --weight-cap 0.4 可看收紧后的差异。")

    # ── 稳定性格局：分半年 各方案 vs 等权 ──
    cmp_schemes = [("rva", "ATR反算"), ("rp", "宽度反算"), ("rp05", "半平价")]
    print(f"\n{'='*118}\n### 分半年稳定性（平均每篮收益，各方案 vs 等权）\n{'='*118}")
    tr = trades.copy()
    tr["half"] = tr["scan_date"].str.slice(0, 4) + "H" + np.where(
        tr["scan_date"].str.slice(5, 7).astype(int) <= 6, "1", "2")
    print(f"{'半年':<10}{'笔数':>7}{'等权':>10}"
          + "".join(f"{lb:>12}{'增量':>10}" for _, lb in cmp_schemes))
    win_cnt = {k: 0 for k, _ in cmp_schemes}
    halves = 0
    for h, g in tr.groupby("half", sort=True):
        e = stats(cohorts(g, "eq"), "eq")["avg_basket"]
        if e is None:
            continue
        halves += 1
        cells = ""
        for key, _lb in cmp_schemes:
            v = stats(cohorts(g, key), key)["avg_basket"]
            if v is None:
                cells += f"{'--':>12}{'--':>10}"
                continue
            win_cnt[key] += 1 if v > e else 0
            cells += f"{v:>+11.2f}%{v - e:>+9.2f}%"
        print(f"{h:<10}{len(g):>7}{e:>+9.2f}%{cells}")
    full = {k: (stats(cohorts(trades, k), k)["avg_basket"]
                - stats(cohorts(trades, "eq"), "eq")["avg_basket"]) for k, _ in cmp_schemes}
    print(f"  分半年胜出计数（共 {halves} 个半年）: "
          + " / ".join(f"{lb} {win_cnt[k]}" for k, lb in cmp_schemes))
    print(f"  全期平均每篮增量: " + " / ".join(f"{lb} {full[k]:+.3f}%" for k, lb in cmp_schemes))

    # ── 单月依赖压力测试（项目铁律：先问「是不是某一个月撑起来的」）──
    print(f"\n{'='*118}\n### 单月依赖压力测试（逐个剔除单月后，各方案是否仍优于等权）\n{'='*118}")
    tr2 = trades.copy()
    tr2["ym"] = tr2["scan_date"].str.slice(0, 7)
    print(f"{'剔除月份':<12}{'剩余笔数':>10}"
          + "".join(f"{lb:>14}" for _, lb in cmp_schemes))
    neg = {k: 0 for k, _ in cmp_schemes}
    for ym, _g in tr2.groupby("ym", sort=True):
        rest = tr2[tr2["ym"] != ym]
        if len(rest) < 20:
            continue
        e = stats(cohorts(rest, "eq"), "eq")["avg_basket"]
        cells = ""
        for key, _lb in cmp_schemes:
            d = stats(cohorts(rest, key), key)["avg_basket"] - e
            neg[key] += 1 if d <= 0 else 0
            cells += f"{d:>+13.3f}%"
        print(f"{ym:<12}{len(rest):>10}{cells}")
    print("  出现「≤0」的月数: "
          + " / ".join(f"{lb} {neg[k]}" for k, lb in cmp_schemes)
          + "（0 = 结论不依赖任何单月）")

    print("\n注：* 复利/最大回撤为「若每日满仓轮动」的参考指标（持仓期重叠，非真实资金曲线）；"
          "\n    结论请以「平均每篮收益」「资本加权胜率」为准。本工具全程只输出比率，不涉及金额。")


if __name__ == "__main__":
    main()
