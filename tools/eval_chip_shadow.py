"""影子对照评估：筹码优先排序 vs 线上基线排序（前向记录，只读）。

背景
----
筹码集中度作第一排序键在历史样本上有效（样本外 +0.91% → +1.34%、止损率 37% → 23%），
但 26k 历史样本已被多轮挖掘、IS/OOS 切分不再干净，且 Δ 对样本期敏感
（子样本 Δ +0.87% → +0.47%，成因是样本构成）。故改用**前向记录**：
core/outcome_tracker.insert_new_outcomes 每日用**完全相同的 WHERE**、
只把 ORDER BY 换成 chip_conc 优先，把同样 top_n 写进 recommend_outcome_shadow。

两组天然对齐（同日、同池、同过滤链）⇒ 唯一变量是排序键。

本工具的回答
------------
1. 覆盖窗口与样本量（够不够看）
2. **重叠度**：影子组里有多少票也在基线组里 —— 重叠 100% = 改排序根本没动任何东西
3. 逐笔口径：T+1/3/5、止损率、胜率、PF（含 no_fill 占比）
4. **按日配对检验**（主判据）：两组同日同池 ⇒ 配对消掉日效应，功效远高于非配对
5. 差集分解：只在影子组 / 只在基线组 的票各自表现（这才是"改排序动了哪几只"）
6. **功效分析**：当前样本能检测出的最小效应量 + 要达标还需多少交易日
7. 单周压力测试（铁律 3）

⚠ 纪律：样本不足时**不给方向性结论**。本工具会明确打印"证据不足"，不要读成"没效果"。

用法
----
    python tools/eval_chip_shadow.py
    python tools/eval_chip_shadow.py --refresh   # 先跑一次线上评估把收益填上
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import get_conn                     # noqa: E402

OUT = os.path.join(ROOT, "logs", "_chip_shadow_eval.txt")

# 判定门槛（先写死，攒够样本再谈）
MIN_DAYS = 20        # 少于 20 个交易日：只看不判
MIN_TRADES = 40      # 两组各少于 40 笔：只看不判
ALPHA = 0.05

_SQL = """
    SELECT scan_date, code, strategy, entry_price,
           {conc}
           {extra}
           t1_return, t3_return, t5_return,
           max_return, min_return, hit_stop, exit_reason, exit_return,
           evaluated_at
    FROM {table}
    WHERE COALESCE(horizon, 'short') = 'short'
      AND entry_price > 0
      AND scan_date >= ?
    ORDER BY scan_date, code
"""


def load(table: str, start: str, extra: str = "", conc: str = "") -> pd.DataFrame:
    """⚠ chip_conc 只在影子表里（线上表故意不加该列），故按表注入 `conc` 列名，
    否则对 recommend_outcome 会 `no such column: chip_conc`。"""
    with get_conn() as conn:
        return pd.read_sql_query(
            _SQL.format(table=table, extra=extra, conc=conc), conn,
            params=(start,))


def _stats(df: pd.DataFrame, label: str) -> dict:
    """一组交易的统计。收益口径 = exit_return（可交易口径：回踩入场+止损+移动止盈）。"""
    n = len(df)
    nf = int((df["exit_reason"] == "no_fill").sum())
    tr = df[df["exit_reason"] != "no_fill"]
    r = pd.to_numeric(tr["exit_return"], errors="coerce").dropna()
    win = (r > 0).mean() if len(r) else np.nan
    loss = r[r < 0]
    gain = r[r > 0]
    pf = (gain.sum() / abs(loss.sum())) if len(loss) and abs(loss.sum()) > 0 else np.nan
    return {
        "label": label, "n": n, "n_fill": len(r), "n_no_fill": nf,
        "no_fill_pct": nf / n * 100 if n else np.nan,
        "avg_exit": r.mean() if len(r) else np.nan,
        "med_exit": r.median() if len(r) else np.nan,
        "win_pct": win * 100 if len(r) else np.nan,
        "stop_pct": (tr["hit_stop"] == 1).mean() * 100 if len(tr) else np.nan,
        "pf": pf,
        "avg_t1": pd.to_numeric(tr["t1_return"], errors="coerce").mean(),
        "avg_t3": pd.to_numeric(tr["t3_return"], errors="coerce").mean(),
        "avg_t5": pd.to_numeric(tr["t5_return"], errors="coerce").mean(),
        "std": r.std() if len(r) > 1 else np.nan,
        "_r": r,
    }


def _welch(a, b) -> tuple[float, float]:
    """Welch t 检验（不假定等方差），返回 (t, 双尾 p)。不依赖 scipy。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return np.nan, np.nan
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = np.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return np.nan, np.nan
    t = (a.mean() - b.mean()) / se
    df = (va / len(a) + vb / len(b)) ** 2 / (
        (va / len(a)) ** 2 / (len(a) - 1) + (vb / len(b)) ** 2 / (len(b) - 1))
    p = 2 * (1 - _t_cdf(abs(t), df))
    return t, p


def _t_cdf(t: float, df: float) -> float:
    """Student-t 累积分布（用不完全 Beta 的连分式近似，够用）。"""
    if df <= 0:
        return np.nan
    x = df / (df + t * t)
    ib = _betainc(df / 2, 0.5, x)
    return 1 - 0.5 * ib


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta I_x(a,b)，连分式（Numerical Recipes 6.4）。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = (_lgamma(a) + _lgamma(b) - _lgamma(a + b))
    front = np.exp(a * np.log(x) + b * np.log(1 - x) - lbeta) / a
    if x < (a + 1) / (a + b + 2):
        return front * _cf(a, b, x)
    return 1 - np.exp(b * np.log(1 - x) + a * np.log(x) - lbeta) / b * _cf(b, a, 1 - x)


def _cf(a: float, b: float, x: float, itmax: int = 200, eps: float = 3e-9) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _lgamma(x: float) -> float:
    from math import lgamma
    return lgamma(x)


def mde(n1: int, n2: int, sigma: float, power: float = 0.80) -> float:
    """两样本 t 检验的最小可检测效应（pp）。σ 为单笔收益标准差。"""
    if not (np.isfinite(sigma) and sigma > 0 and n1 > 1 and n2 > 1):
        return np.nan
    z_a = 1.959964                      # α=0.05 双尾
    z_b = {0.80: 0.841621, 0.90: 1.281552}.get(power, 0.841621)
    return (z_a + z_b) * sigma * np.sqrt(1.0 / n1 + 1.0 / n2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="先跑一次线上+影子评估，把收益填上再统计")
    ap.add_argument("--days", type=int, default=120, help="回看窗口（自然日）")
    args = ap.parse_args()

    buf = io.StringIO()

    def w(s=""):
        print(s)
        buf.write(s + "\n")

    if args.refresh:
        from core.outcome_tracker import evaluate_outcomes
        print("刷新评估中…")
        evaluate_outcomes()
        evaluate_outcomes("recommend_outcome_shadow")

    from datetime import timedelta
    start = (date.today() - timedelta(days=args.days)).isoformat()

    with get_conn() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='recommend_outcome_shadow'").fetchone()
    if not exists:
        w("!! recommend_outcome_shadow 表不存在 —— 先跑一次每日链路（init_db + insert_new_outcomes）")
        io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        return

    chip = load("recommend_outcome_shadow", start, "in_baseline,",
                conc="chip_conc,")
    base = load("recommend_outcome", start)

    # ⚠ 残渣防御：合法旁路行的 chip_conc 必然非 NULL（写入起点 ≥ chip_ready）。
    # 出现 NULL 行 ⇒ 实验前/调试期写入的**基线副本**，会污染对照与重叠度统计，剔除。
    n_junk = int(chip["chip_conc"].isna().sum()) if not chip.empty else 0
    if n_junk:
        chip = chip[chip["chip_conc"].notna()].copy()

    w("=" * 78)
    w("筹码优先排序 · 前向对照（影子组 vs 线上基线组）")
    w("=" * 78)
    if chip.empty:
        w("影子组暂无记录 —— 前向记录刚启动，等每日链路累积。")
        io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        return

    days = sorted(set(chip["scan_date"]))
    cb, bb = _stats(chip, "筹码优先"), _stats(base, "线上基线")

    w("")
    w(f"【覆盖】{days[0]} ~ {days[-1]}  共 {len(days)} 个交易日  "
      f"（窗口起点 {start}）")
    if n_junk:
        w(f"⚠ 已剔除 {n_junk} 行 chip_conc 为 NULL 的旁路残渣（实验前/调试期写入的")
        w("  基线副本）。若持续出现 ⇒ outcome_tracker 的自愈清理没生效，")
        w("  先跑 tests/_verify_chip_shadow.py --write 排查。")
    n_ev = int(chip["evaluated_at"].notna().sum())
    w(f"【评估进度】影子组 {n_ev}/{len(chip)} 条已出终态"
      f"（未终态 = 持仓未走完，属正常）")

    w("")
    w("【样本量】")
    w("  （已终态 = 已算出 exit_return；未终态 = 持仓未走完，属正常，不是缺陷）")
    w(f"{'组':>10} {'笔数':>7} {'已终态':>8} {'no_fill':>8} {'未终态':>8} "
      f"{'no_fill%':>9}")
    for s in (cb, bb):
        # ⚠ 不能把 (笔数 - 已终态) 当 no_fill：那是「no_fill + 未终态」的混合，
        #   会让人误以为大量未成交（此处三列分开列，避免误读）。
        unsettled = s["n"] - s["n_no_fill"] - s["n_fill"]
        w(f"{s['label']:>10} {s['n']:>7} {s['n_fill']:>8} {s['n_no_fill']:>8} "
          f"{unsettled:>8} {s['no_fill_pct']:>8.1f}%")

    # ── 重叠度：最关键的先看 ──
    ov = chip["in_baseline"].mean() * 100
    w("")
    w("【① 重叠度】改排序到底动了哪几只")
    w(f"影子组中同时在基线组里的票：{ov:.1f}%")
    w(f"  纯影子（基线没选）：{int((chip['in_baseline'] == 0).sum())} 条")
    if ov >= 99.0:
        if len(chip) >= 10:
            w("  ⚠ 重叠接近 100% ⇒ 排序改动几乎没有改变任何选票，")
            w("    Δ 必然趋零且**不能据此判定无效**（不是结论，是没做实验）。")
            w("    需检查 chip_conc 是否全表同值/全 NULL，或候选池本身无区分度。")
        else:
            w("  （样本过少，重叠度暂不具判别力 —— 别读成高重叠）")

    # ── 逐笔口径 ──
    def _n(v, wd: int, spec: str = ".3f") -> str:
        """nan → '—'：早期样本必然大量未终态，打印 nan 会被误读成"效果为 0"。"""
        try:
            ok = np.isfinite(v)
        except TypeError:
            ok = False
        return (f"{v:{spec}}" if ok else "—").rjust(wd)

    w("")
    w("【② 逐笔口径】收益 = exit_return（回踩入场+止损+移动止盈的可交易口径）")
    w(f"{'组':>10} {'均值%':>8} {'中位%':>8} {'胜率%':>7} {'止损率%':>8} "
      f"{'PF':>6} {'T+1%':>7} {'T+3%':>7} {'T+5%':>7}")
    for s in (cb, bb):
        w(f"{s['label']:>10} {_n(s['avg_exit'], 8)} {_n(s['med_exit'], 8)} "
          f"{_n(s['win_pct'], 7, '.1f')} {_n(s['stop_pct'], 8, '.1f')} "
          f"{_n(s['pf'], 6, '.2f')} {_n(s['avg_t1'], 7)} {_n(s['avg_t3'], 7)} "
          f"{_n(s['avg_t5'], 7)}")
    if len(cb["_r"]) >= 2 and len(bb["_r"]) >= 2:
        t_w, p_w = _welch(cb["_r"], bb["_r"])
        w(f"  Welch t 检验（非配对）：Δ = {cb['avg_exit'] - bb['avg_exit']:+.3f}pp，"
          f"t = {t_w:+.2f}，p = {p_w:.3f}")
    else:
        w("  Welch t 检验：两组需各 ≥2 笔已终态 —— 当前样本不足，跳过")

    # ── 按日配对（主判据）──
    w("")
    w("【③ 按日配对检验（主判据）】两组同日同池抽取 ⇒ 配对可消掉日效应")
    dc = chip[chip["exit_reason"] != "no_fill"].groupby("scan_date")["exit_return"].mean()
    db = base[base["exit_reason"] != "no_fill"].groupby("scan_date")["exit_return"].mean()
    pair = pd.concat([dc.rename("chip"), db.rename("base")], axis=1).dropna()
    if len(pair) >= 3:
        d = pair["chip"] - pair["base"]
        t_p, p_p = _welch(d, np.zeros(len(d)))      # 单样本 t（等价配对 t）
        w(f"可配对交易日 {len(pair)} 个")
        w(f"日均：筹码 {pair['chip'].mean():+.3f}% / 基线 {pair['base'].mean():+.3f}%"
          f"  → Δ = {d.mean():+.3f}pp")
        w(f"配对 t = {t_p:+.2f}，p = {p_p:.3f}，"
          f"Δ 标准差 {d.std(ddof=1):.3f}pp，"
          f"Δ>0 的天数 {int((d > 0).sum())}/{len(d)}")
    else:
        w(f"可配对交易日仅 {len(pair)} 个 —— 不足以检验")

    # ── 差集分解 ──
    w("")
    w("【④ 差集分解】改排序真正新增/剔除的票表现如何")
    base_keys = set(zip(base["scan_date"], base["code"]))
    chip_keys = set(zip(chip["scan_date"], chip["code"]))
    for nm, keys in (("纯影子（新进）", chip_keys - base_keys),
                     ("纯基线（被剔）", base_keys - chip_keys),
                     ("两组交集", chip_keys & base_keys)):
        src = (chip if nm.startswith("纯影子") else
               base if nm.startswith("纯基线") else chip)
        sub = src[[k in keys for k in zip(src["scan_date"], src["code"])]]
        sub = sub[sub["exit_reason"] != "no_fill"]
        r = pd.to_numeric(sub["exit_return"], errors="coerce").dropna()
        if len(r):
            w(f"  {nm:>14}  n={len(r):>4}  均值 {r.mean():+.3f}%  "
              f"中位 {r.median():+.3f}%  胜率 {(r>0).mean()*100:.0f}%")
        else:
            w(f"  {nm:>14}  无样本")

    # ── 功效分析：最诚实的一段 ──
    w("")
    w("【⑤ 功效分析】当前样本能识别多大的效应？")
    both = pd.concat([cb["_r"], bb["_r"]])
    sigma = float(both.std()) if len(both) > 1 else np.nan
    n1, n2 = max(cb["n_fill"], 1), max(bb["n_fill"], 1)
    m = mde(n1, n2, sigma)
    w(f"单笔收益标准差 σ = {sigma:.3f}pp（两组合并）" if np.isfinite(sigma)
      else "单笔收益标准差 σ = —（已终态样本 <2 笔，无法估计）")
    if np.isfinite(m):
        w(f"当前 n = {n1} / {n2}  ⇒ 80% 功效下的**最小可检测效应 MDE ≈ {m:.3f}pp**")
    else:
        w(f"当前 n = {n1} / {n2}  ⇒ MDE 暂不可估（σ 未知）—— 先攒样本")
    if np.isfinite(m):
        w(f"  即：真实效应小于 {m:.2f}pp 时，当前样本**检不出来**。")
        for target in (0.87, 0.5, 0.3):
            need = (2.802 * sigma / target) ** 2 * 2      # n1=n2 对称估计
            need_days = need / max(1.0, (n1 + n2) / max(len(pair), 1))
            w(f"  要识别 +{target:.2f}pp 需每天每组各 ~{need/2:.0f} 笔 → "
              f"约 {need_days:.0f} 个交易日（按当前日均出票率）")
    w("  参照：历史回测的 Δ 是 +0.87pp（全期）/ +0.99pp（样本外）。")

    # ── 是否达到判定门槛 ──
    w("")
    w("【⑥ 判定门槛】")
    ok_days = len(pair) >= MIN_DAYS
    ok_n = cb["n_fill"] >= MIN_TRADES and bb["n_fill"] >= MIN_TRADES
    w(f"交易日 {len(pair)}/{MIN_DAYS} {'✅' if ok_days else '❌'}"
      f"  样本 {cb['n_fill']}/{bb['n_fill']}（各需 ≥{MIN_TRADES}）"
      f" {'✅' if ok_n else '❌'}")
    if ok_days and ok_n:
        verdict = "样本达标，可按 Δ 符号与 p 值给结论"
    else:
        verdict = ("**证据不足 —— 不得据此判定筹码排序有效或无效**。"
                   "当前仅够检查链路是否正常（填充率/重叠度/收益是否已评估）。")
    w(f"⇒ {verdict}")

    # ── 单周压力测试（铁律 3）──
    w("")
    w("【⑦ 单周压力测试】剔除最好/最差的一周后 Δ 是否还成立")
    if len(pair) >= 6:
        d = (pair["chip"] - pair["base"])
        wk = pd.to_datetime(pair.index).isocalendar().week
        ywk = pd.to_datetime(pair.index).isocalendar().year
        g = d.groupby([ywk, wk])
        means = g.mean()
        mx = means.idxmax()
        mn = means.idxmin()
        w(f"共 {len(means)} 个自然周；Δ 周均值中位 {means.median():+.3f}pp，"
          f"周均值>0 的周数 {int((means > 0).sum())}/{len(means)}")
        w(f"  剔除最好周（{mx[0]}W{mx[1]:02d}，{means.loc[mx]:+.3f}pp）后 "
          f"Δ = {means.drop(mx).mean():+.3f}pp")
        w(f"  剔除最差周（{mn[0]}W{mn[1]:02d}，{means.loc[mn]:+.3f}pp）后 "
          f"Δ = {means.drop(mn).mean():+.3f}pp")
        w(f"  同时剔除两端后 Δ = {means.drop([mx, mn]).mean():+.3f}pp")
    else:
        w("  周数不足，跳过")

    w("")
    w("=" * 78)
    w("⚠ 本报告仅供研究参考，不构成个人投资建议。样本不足时禁止据此开关排序。")

    io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
    print(f"\n[写出] {OUT}")


if __name__ == "__main__":
    main()
