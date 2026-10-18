"""
tools/_mine_reversal_strategy.py —— 反转首日：共性归纳 → 筛选规则 → 止盈止损 → 四窗口回测
==========================================================================================

接 tools/_mine_reversal_winners.py 的挖掘结论，做三件事：
  rule     在**训练期**（<=2025-09-30）搜索个股筛选规则（同日配对市场中性口径）
  exit     在训练期网格搜索止盈/止损/最长持有期
  backtest 规则与出场参数**冻结**后，跑 1M/3M/6M/1Y + 测试期 + 分年，只评估不回头改

═══ 挖掘阶段的两条结论（决定了本脚本的评估口径）═══════════════════════════
① 市场级特征（当日全市场涨幅 / 站上MA20占比 / 当日信号数 / 市场20日累计）
   对「10日内涨超10%」的绝对命中率影响巨大（14.5% → 33.0%），
   但它们在**同一天内是常数**，同日配对超额恒为 0 ⇒ 那是 beta（择时），不是选股 alpha。
   ⇒ 本脚本对择时闸门单独用「绝对收益 + 同期市场基线」评估，不与选股因子混谈。
② 个股级特征里真正有市场中性 alpha 的是同一族：**中期趋势强 + 波动大 + 换手高**
   （ret60_p / ma60_rel / ma120_rel / pos250 / dd_hi250 / atr_pct / to_calc，
    最高档同日配对超额 +0.86 ~ +1.22pp，t=+2.3 ~ +3.0）。
   ⇒ 即「反转首日」在**本来就强势的股票**上是中继，在弱势股上是抢反弹，前者才是 alpha。
   ⚠ 市值/换手用的 circ_shares、total_shares 是**当前快照**（非 point-in-time），
     含未来信息 ⇒ 一律不作为规则因子，只作参考。

用法：
  python tools/_mine_reversal_strategy.py baseline   # 建市场基线（同入场/同持有期）
  python tools/_mine_reversal_strategy.py rule       # 训练期规则搜索
  python tools/_mine_reversal_strategy.py exit       # 训练期出场参数网格
  python tools/_mine_reversal_strategy.py backtest   # 四窗口回测（含测试期）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd
from scipy import stats

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools._mine_reversal_winners import (  # noqa: E402
    DB_PATH, LOAD_START, SIG_START, SIG_END, TRAIN_END, HOLD_WIN, WIN_THR,
    COST_PP, FEATURE_LABELS, load, paired_daily, by_year,
)

BASE_CACHE = os.path.join(PROJECT_ROOT, "data_cache", "_reversal_mkt_baseline.parquet")


# ══════════════════════════════════════════════════════════════════════════
# Stage 0 · 市场基线（与策略同入场口径：T+1 开盘买 → T+k 收盘卖，剔 T+1 一字板）
# ══════════════════════════════════════════════════════════════════════════
def baseline() -> None:
    t0 = time.time()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    df = pd.read_sql(
        "SELECT code, trade_date, open, high, low, close FROM daily_price "
        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY code, trade_date",
        con, params=(SIG_START, SIG_END))
    con.close()
    df = df[~df["code"].str.startswith(("688", "689", "8", "9"))].copy()
    df = df[(df["close"] > 0) & (df["open"] > 0)].reset_index(drop=True)
    df["mb"] = df["code"].str.startswith(("60", "00"))
    G = df.groupby("code", sort=False)
    o1 = G["open"].shift(-1)
    h1 = G["high"].shift(-1)
    l1 = G["low"].shift(-1)
    tradable = ~((o1 == h1) & (o1 == l1)) & o1.notna() & (o1 > 0)
    rows = {"trade_date": df["trade_date"], "mb": df["mb"].to_numpy(), "ok": tradable.to_numpy()}
    for k in range(1, HOLD_WIN + 1):
        ck = G["close"].shift(-k)
        rows[f"r{k}"] = ((ck / o1 - 1) * 100).to_numpy()
    b = pd.DataFrame(rows)
    b = b[b["ok"] & b["mb"]]                       # 主板口径（用户可交易域）
    agg = b.groupby("trade_date")[[f"r{k}" for k in range(1, HOLD_WIN + 1)]].mean()
    agg["n_univ"] = b.groupby("trade_date").size()
    agg = agg.reset_index()
    agg.to_parquet(BASE_CACHE, index=False)
    print(f"✅ 市场基线（主板 T+1开盘→T+k收盘，剔一字板）{len(agg)} 个交易日  "
          f"{time.time()-t0:.0f}s → {BASE_CACHE}")
    print(f"   T+1 裸持有均值 {agg['r1'].mean():+.3f}%   T+5 {agg['r5'].mean():+.3f}%   "
          f"T+10 {agg['r10'].mean():+.3f}%   日均样本 {agg['n_univ'].mean():.0f} 只")


def load_baseline() -> pd.DataFrame:
    if not os.path.exists(BASE_CACHE):
        raise SystemExit("缺市场基线，先跑：python tools/_mine_reversal_strategy.py baseline")
    return pd.read_parquet(BASE_CACHE).set_index("trade_date")


# ══════════════════════════════════════════════════════════════════════════
# 出场模拟器（真实口径：T+1 开盘建仓，盘中止损/止盈，跌停一字卖不掉则顺延）
# ══════════════════════════════════════════════════════════════════════════
def simulate_exit(d: pd.DataFrame, stop_pct: float, take_pct: float,
                  max_hold: int, ref: str = "entry") -> pd.Series:
    """逐笔模拟出场，返回收益 %（未扣成本）。

    ref='entry' → 止损/止盈以**建仓价(T+1开盘)**为基准（真实挂单口径）
    ref='close' → 以**信号日收盘**为基准（线上 stock_signal.stop_loss 的口径）

    保守约定：
      · 同一天同时触及止损与止盈 ⇒ 判止损（先坏后好）
      · 跳空越过止损/止盈价 ⇒ 按开盘价成交（不臆造更好的价格）
      · 一字跌停（o=h=l 且较前收 <= -9.5%）⇒ 当天卖不掉，顺延到下一日
      · 到 max_hold 仍未触发 ⇒ 按当日收盘了结
    """
    n = len(d)
    entry = d["entry"].to_numpy(float)
    c0 = d["close"].to_numpy(float)
    base = entry if ref == "entry" else c0
    sl = base * (1 + stop_pct)
    tp = base * (1 + take_pct)
    O = {k: d[f"o{k}"].to_numpy(float) for k in range(1, max_hold + 1)}
    H = {k: d[f"h{k}"].to_numpy(float) for k in range(1, max_hold + 1)}
    L = {k: d[f"l{k}"].to_numpy(float) for k in range(1, max_hold + 1)}
    C = {k: d[f"c{k}"].to_numpy(float) for k in range(1, max_hold + 1)}
    PC = {1: c0}
    for k in range(2, max_hold + 1):
        PC[k] = C[k - 1]

    out = np.full(n, np.nan)
    reason = np.array([""] * n, dtype=object)
    for i in range(n):
        e = entry[i]
        if not np.isfinite(e) or e <= 0:
            continue
        for k in range(1, max_hold + 1):
            o, h, l, c = O[k][i], H[k][i], L[k][i], C[k][i]
            if not np.isfinite(c):
                break
            lock = np.isfinite(o) and (o == h) and (o == l) \
                and np.isfinite(PC[k][i]) and PC[k][i] > 0 \
                and (o / PC[k][i] - 1) <= -0.095          # 一字跌停：卖不掉
            if not lock:
                if np.isfinite(l) and l <= sl[i]:
                    px = min(o, sl[i]) if np.isfinite(o) and o <= sl[i] else sl[i]
                    out[i] = (px / e - 1) * 100
                    reason[i] = "stop_loss"
                    break
                if np.isfinite(h) and h >= tp[i]:
                    px = max(o, tp[i]) if np.isfinite(o) and o >= tp[i] else tp[i]
                    out[i] = (px / e - 1) * 100
                    reason[i] = "take_profit"
                    break
            if k == max_hold:
                out[i] = (c / e - 1) * 100
                reason[i] = "max_hold_days"
    return pd.Series(out, index=d.index), pd.Series(reason, index=d.index)


# ══════════════════════════════════════════════════════════════════════════
# 通用统计
# ══════════════════════════════════════════════════════════════════════════
def prep() -> pd.DataFrame:
    d = load()
    d = d[d["main_board"] & d["valid10"] & d["buyable"] & d["entry"].notna()
          & (d["entry"] > 0)].copy()
    return d


def stats_block(sub: pd.DataFrame, ret: pd.Series, a3_day: pd.Series,
                mktbl: pd.DataFrame, max_hold: int, label: str,
                ndays_total: int) -> dict:
    """一套完整成绩单：绝对收益 + 同日配对超额(vs A3) + 同日配对超额(vs 市场)。

    ⚠ 两个基线都必须是**按交易日聚合后的序列**，不能是逐笔序列：
      逐笔 reindex 会取到「同一笔交易自己」，超额恒为 0（本文件曾犯过这个错）。
    a3_day : index=trade_date, values=当日**全部**反转首日信号在同一出场规则下的平均收益
    mktbl  : index=trade_date, 需含列 r{max_hold}=当日全主板同出场基线平均收益
    """
    r = ret.dropna()
    if len(r) < 20:
        return {"label": label, "n": len(r)}
    net = r - COST_PP
    dates = sub.loc[r.index, "trade_date"]
    tmp = pd.DataFrame({"d": dates.to_numpy(), "r": r.to_numpy()}).dropna()
    day_mean = tmp.groupby("d")["r"].mean()

    def _paired(base: pd.Series):
        both = pd.concat([day_mean.rename("s"), base.rename("b")], axis=1).dropna()
        if len(both) < 5:
            return np.nan, np.nan, len(both)
        dd = both["s"] - both["b"]
        sd = dd.std(ddof=1)
        return dd.mean(), (dd.mean() / (sd / np.sqrt(len(dd))) if sd > 0 else np.nan), len(dd)

    ex_a3, t_a3, n_a3 = _paired(a3_day)
    ex_mk, t_mk, n_mk = _paired(mktbl[f"r{max_hold}"])
    win = (net > 0).mean() * 100
    gp, gl = net[net > 0], net[net < 0]
    return {
        "label": label, "n": len(r), "per_day": len(r) / max(1, ndays_total),
        "days": dates.nunique(), "n_a3": n_a3, "n_mk": n_mk,
        "gross": r.mean(), "net": net.mean(), "median": net.median(),
        "win": win, "pf": (gp.sum() / abs(gl.sum())) if len(gl) else np.inf,
        "pl": (gp.mean() / abs(gl.mean())) if len(gl) and len(gp) else np.nan,
        "avg_win": gp.mean() if len(gp) else np.nan,
        "avg_loss": gl.mean() if len(gl) else np.nan,
        "ex_a3": ex_a3, "t_a3": t_a3, "ex_mkt": ex_mk, "t_mkt": t_mk,
        "max_dd": net.min(), "best": net.max(),
    }


def fmt_row(s: dict) -> str:
    if s.get("n", 0) < 20:
        return f"  {s['label']:<34} 样本不足 (n={s.get('n',0)})"
    return (f"  {s['label']:<34}{s['n']:>6}{s['per_day']:>7.2f}{s['net']:>8.2f}%"
            f"{s['win']:>7.1f}%{s['pl']:>7.2f}{s['ex_a3']:>9.2f}{s['t_a3']:>7.1f}"
            f"{s['ex_mkt']:>9.2f}{s['t_mkt']:>7.1f}")


HDR = (f"  {'方案':<34}{'笔数':>6}{'日均':>7}{'净均值':>9}{'胜率':>7}{'盈亏比':>7}"
       f"{'vsA3':>9}{'t':>7}{'vs市场':>9}{'t':>7}")


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 · 训练期规则搜索
# ══════════════════════════════════════════════════════════════════════════
# 挖掘阶段筛出的候选轴（同日配对 |t|>=2 且非市值快照类）
CAND = {
    # 轴 A：中期趋势强度/位置
    "ret60_p": "hi", "ma60_rel": "hi", "ma120_rel": "hi",
    "pos250": "hi", "dd_hi250": "hi", "ret120_p": "hi",
    # 轴 B：波动/活跃
    "atr_pct": "hi",
    # 轴 C：量能形态
    "vr": "lo", "ext20": "hi", "up_shadow": "lo", "vshrink": "hi",
    # 轴 D：当日强度
    "ret": "hi", "close_pos": "hi",
}
DAY_FILTERS = {
    "mkt_above_ma20": [30, 40, 50, 60],
    "mkt_pct": [0.0, 0.5, 1.0],
    "n_signal_mb": [8, 12, 17],
    "mkt_ret20": [0.0, 2.5],
}


def rule() -> None:
    d = prep()
    tr = d[d["period"] == "train"]
    print("=" * 118)
    print(f"规则搜索 · 训练期 {tr['trade_date'].min()} ~ {tr['trade_date'].max()}  "
          f"n={len(tr):,}   （测试期 n={len(d[d['period']=='test']):,} 本轮不看）")
    print("=" * 118)

    # ── 0. 候选特征相关性（挑独立轴，避免堆叠同源因子）─────────────────────
    cols = list(CAND)
    cm = tr[cols].corr(method="spearman")
    print("\n【0】候选特征 Spearman 相关阵（|ρ|>0.5 视为同源，规则里只取一个代表）")
    print("        " + "".join(f"{c[:9]:>10}" for c in cols))
    for r in cols:
        marks = "".join(("    ·   " if abs(cm.loc[r, c]) < 0.3 else
                         ("   ***  " if abs(cm.loc[r, c]) > 0.6 else "   *   "))
                        if r != c else "    —   " for c in cols)
        print(f"  {r:<8}" + "".join(f"{cm.loc[r,c]:>10.2f}" for c in cols))
    print("  （· 弱相关 <0.3 | * 中等相关 | *** 同源 >0.6）")

    # ── 1. 单因子阈值扫描（同日配对超额）──────────────────────────────────
    print("\n【1】单因子阈值扫描 · 同日配对超额（pp）  [目标列见表头]")
    print("    超额口径 = 该子集当日 maxg10c 均值 − 当日全部反转首日信号均值")
    print(f"    {'因子':<16}{'阈值':>22}{'笔数':>7}{'日均':>6}{'命中率%':>9}"
          f"{'maxg10c':>9}{'超额pp':>8}{'t':>6}   分年超额(2023/24/25)")
    picks = {}
    for f, direction in CAND.items():
        lab = FEATURE_LABELS.get(f, f)
        qs = [0.50, 0.60, 0.70, 0.80] if direction == "hi" else [0.50, 0.40, 0.30, 0.20]
        for q in qs:
            thr = tr[f].quantile(q)
            if not np.isfinite(thr):
                continue
            sub = tr[tr[f] >= thr] if direction == "hi" else tr[tr[f] <= thr]
            if len(sub) < 300:
                continue
            ex, t, nd = paired_daily(sub, "maxg10c", tr)
            yr = []
            for y in sorted(tr["year"].unique()):
                e2, _, _ = paired_daily(sub[sub["year"] == y], "maxg10c",
                                        tr[tr["year"] == y])
                yr.append(e2)
            ok_year = all(np.isfinite(v) and v > 0 for v in yr)
            star = "★" if (np.isfinite(t) and t >= 2.0 and ok_year) else \
                   ("☆" if np.isfinite(t) and t >= 1.5 else "")
            print(f"    {lab:<16}{f'{f} >= {thr:.3f}' if direction=='hi' else f'{f} <= {thr:.3f}':>22}"
                  f"{len(sub):>7}{len(sub)/tr['trade_date'].nunique():>6.2f}"
                  f"{sub['win10'].mean()*100:>8.1f}%{sub['maxg10c'].mean():>8.2f}%"
                  f"{ex:>+8.2f}{t:>+6.1f}   "
                  + "/".join(f"{v:+.2f}" for v in yr) + f"  {star}")
            if star == "★":
                picks.setdefault(f, []).append((q, thr, ex, t))

    print("\n    ★ = t>=2.0 且训练期每一年超额同号为正（项目铁律：分年一致）")

    # ── 2. 择时闸门（市场级）：绝对口径 + 同期市场基线 ────────────────────
    print("\n【2】择时闸门（市场级特征）· 绝对口径  ⚠ 这是 beta 择时，不是选股 alpha")
    mk = load_baseline()
    trm = tr.merge(mk[["r10"]], left_on="trade_date", right_index=True, how="left")
    nd_all = tr["trade_date"].nunique()
    print(f"    {'闸门':<26}{'交易日':>7}{'笔数':>7}{'命中率%':>9}{'maxg10c%':>10}"
          f"{'c10ret%':>9}{'同期市场T+10%':>15}{'差pp':>8}")
    base_hit = tr["win10"].mean() * 100
    print(f"    {'（无闸门：全部反转首日）':<26}{nd_all:>7}{len(tr):>7}{base_hit:>8.1f}%"
          f"{tr['maxg10c'].mean():>9.2f}%{tr['c10ret'].mean():>8.2f}%"
          f"{trm['r10'].mean():>14.2f}%{tr['c10ret'].mean()-trm['r10'].mean():>+8.2f}")
    for f, thrs in DAY_FILTERS.items():
        for thr in thrs:
            sub = trm[trm[f] >= thr]
            if len(sub) < 200:
                continue
            print(f"    {f'{f} >= {thr}':<26}{sub['trade_date'].nunique():>7}{len(sub):>7}"
                  f"{sub['win10'].mean()*100:>8.1f}%{sub['maxg10c'].mean():>9.2f}%"
                  f"{sub['c10ret'].mean():>8.2f}%{sub['r10'].mean():>14.2f}%"
                  f"{sub['c10ret'].mean()-sub['r10'].mean():>+8.2f}")

    # ── 3. 组合规则与真实出场评估 → 见 tools/_reversal_engine.py ──────────
    print("\n【3】组合规则 / 出场参数 / 四窗口回测：见 tools/_reversal_engine.py")
    print("    （阈值全部由训练期分位数定死，测试期只评估一次）")


