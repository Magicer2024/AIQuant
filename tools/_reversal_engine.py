"""
tools/_reversal_engine.py —— 反转首日：组合规则 → 出场参数 → 四窗口回测（含 1 万元账户组合模拟）
==============================================================================================

接 tools/_mine_reversal_winners.py（共性挖掘）与 tools/_mine_reversal_strategy.py
（单因子阈值扫描 + 择时闸门检验）。本文件做**决策**部分：

  combos    组合规则对比（训练期定阈值 → 同日配对超额 + 真实出场口径）
  exit      出场参数网格（止损 × 止盈 × 持有期 × 移动止盈），训练期定参
  exit2     规则 × 出场 **联合**定参：固定档 + ATR 自适应档 + 持有期，跨 B3/B5/B7
  backtest  规则与参数**冻结**后跑 1M/3M/6M/1Y + 测试期 + 分年 + 分月，
            并模拟「1 万元账户、最多同时 2 只、按打分排序」的真实资金曲线
  calib     口径校正：按笔等权 → 按日等权（固定并发账户的真实期望），
            并检验各因子的「命中率」与「净收益」是否同向
  follow    报告 §8 三条后续方向的实证：F0 口径校正 / F1 分批止盈 /
            F2 建仓时点 / F3 并发数 × 资金规模（含「不排序」对照，把并发数的作用
            与排序键的作用分开）

═══ 三条评估口径（务必分清，混谈会得出相反结论）═══════════════════════════
  vsA3   同日配对超「全部反转首日信号 + 同一套出场」  → 我的**选股因子**加了多少值
  vs市场 同日配对超「全主板随便买一只 + 同一套出场」  → 这套东西**值不值得做**
  绝对   扣 A 股往返成本 0.10pp 后的单笔净收益        → 账户实际到手
⚠ 市场级特征（普涨日/信号多的日子）在 vsA3 与 vs市场 两个口径下**都拿不到超额**，
  只抬高绝对命中率 ⇒ 属 beta 择时，本脚本默认**不纳入**规则（B11/B12 仅作对照）。
⚠ 「绝对」还分两种等权口径，混谈会差出一个符号（见 stage_calib）：
    按笔等权 = 所有交易一票一权；按日等权 = 每天先取均值再对天平均。
    max_pos 固定的账户每天只能下 max_pos 注 ⇒ 它面对的是**按日等权**。
    vsA3 / vs市场 本来就在日级配对，不受影响；只有「绝对净收益」这一列会被高估。

用法：
  python tools/_reversal_engine.py combos
  python tools/_reversal_engine.py exit
  python tools/_reversal_engine.py calib   --rule A3,B5,B7 --stop -0.08 --take 0.20 --hold 8
  python tools/_reversal_engine.py follow  --rule A3,B5,B7 --stop -0.08 --take 0.20 --hold 8
  python tools/_reversal_engine.py backtest [--rule B3] [--stop -0.06] [--take 0.10]
                                           [--hold 10] [--arm 0.08] [--trail 0.05]
                                           [--max-pos 2] [--capital 10000]
                                           [--take1 0.10] [--w1 0.5] [--be-after1]
                                           [--eoff 0] [--eref open]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (PROJECT_ROOT, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools._mine_reversal_winners import (        # noqa: E402
    DB_PATH, SIG_START, SIG_END, TRAIN_END, HOLD_WIN, WIN_THR, COST_PP, load,
)
from tools._mine_reversal_strategy import (       # noqa: E402
    prep, stats_block, fmt_row, HDR, paired_daily, by_year, load_baseline,
)

PANEL_CACHE = os.path.join(PROJECT_ROOT, "data_cache", "_reversal_panel.npz")


# ══════════════════════════════════════════════════════════════════════════
# 向量化出场引擎（信号组与市场基线共用同一套数学，保证同口径可比）
# ══════════════════════════════════════════════════════════════════════════
R_STOP, R_TRAIL, R_TP, R_HOLD = 1, 2, 3, 4
REASON_NAME = {R_STOP: "止损", R_TRAIL: "移动止盈", R_TP: "止盈",
               R_HOLD: "到期了结", 0: "未结算"}


def _sim_core(get_ohlc, pc0, entry, sl, tp, n, max_hold, arm=None, trail=None,
              tp1=None, w1=0.5, be_after1=False):
    """逐日推进的向量化出场模拟。→ (收益%, 原因码, 了结日, 腿1是否成交)

    get_ohlc(j) → (o,h,l,c) 四个 shape=(n,) 数组，代表**第 j 个持仓日**行情（j 从 1 起）。
                  调用方负责把「建仓时点偏移」折进 j → 自然日的映射里。
    pc0         → 首个持仓日的前一交易日收盘，用于第 1 日的一字跌停判定。

    日内路径假设（保守且自洽）：
      · 卖出底线 = max(硬止损价, 已启动的移动止盈价) —— 价格下行先撞较高的那条；
      · 底线与止盈同日都碰到 ⇒ 判底线（先坏后好）；
      · 跳空越过挂单价 ⇒ 按开盘价成交（不臆造更好价格）；
      · 一字跌停（o=h=l 且较前收 <= -9.5%）当天卖不掉 ⇒ 顺延次日；
      · 到 max_hold 仍未触发 ⇒ 按当日收盘了结。

    ── 分批止盈（tp1 不为 None）───────────────────────────────────────────
    价格摸到 tp1 ⇒ 卖出 w1 仓位，剩余 (1-w1) 继续按 sl/tp 跑；
    最终收益 = w1×腿1 + (1-w1)×腿2。两条关键口径决定，都往**保守**方向取：
      · 同日 h 同时越过 tp1 与 tp ⇒ **两腿都算成交**（真实挂单就是 +10% 和 +20%
        两张限价单同时在场），不给「全仓吃到 +20%」的乐观解；
      · 腿1成交后若继续下跌撞到止损 ⇒ 剩余仓位照止损，不因为「已经落袋一半」而豁免。
    be_after1=True 时腿1成交后把剩余仓位的止损上移到建仓价（保本止损）。
    """
    ret = np.full(n, np.nan)
    rsn = np.zeros(n, dtype=np.int8)
    kexit = np.zeros(n, dtype=np.int8)
    exited = np.zeros(n, dtype=bool)
    good = np.isfinite(entry) & (entry > 0)
    peak = entry.copy()
    prev_c = pc0.copy()
    two_leg = tp1 is not None
    acc = np.zeros(n)                 # 已了结部分的加权收益（百分点）
    rem = np.ones(n)                  # 仍在场内的仓位权重
    leg1 = np.zeros(n, dtype=bool)    # 腿1（tp1）是否已成交
    for k in range(1, max_hold + 1):
        o, h, l, c = get_ohlc(k)
        act = (~exited) & good & np.isfinite(c)
        if act.any():
            # ⚠ 括号不能省：`&` 优先级高于 `<=`，漏括号会变成 bool & float
            with np.errstate(invalid="ignore", divide="ignore"):
                gapdn = (o / np.where(np.isfinite(prev_c) & (prev_c > 0),
                                      prev_c, np.nan) - 1) <= -0.095
            lock = (np.isfinite(o) & (o == h) & (o == l)
                    & np.isfinite(prev_c) & (prev_c > 0) & np.nan_to_num(gapdn))
            can = act & (~lock)
            floor = sl.copy()
            which = np.full(n, R_STOP, dtype=np.int8)
            if two_leg and be_after1:
                # 保本止损只对**已经落袋一半**的仓位生效
                floor = np.where(leg1, np.maximum(floor, entry), floor)
            if arm is not None and trail is not None:
                with np.errstate(invalid="ignore", divide="ignore"):
                    armed = (peak / entry - 1) >= arm
                tpx = peak * (1 - trail)
                use_t = armed & np.isfinite(tpx) & (tpx > floor)
                floor = np.where(use_t, tpx, floor)
                which = np.where(use_t, R_TRAIL, R_STOP).astype(np.int8)
            trig = can & np.isfinite(l) & (l <= floor)
            if trig.any():
                px = np.where(np.isfinite(o) & (o <= floor), o, floor)
                ret = np.where(trig, acc + rem * (px / entry - 1) * 100, ret)
                rsn = np.where(trig, which, rsn).astype(np.int8)
                kexit = np.where(trig, k, kexit).astype(np.int8)
                exited |= trig
            act2 = (~exited) & good & np.isfinite(c)
            trig2 = act2 & np.isfinite(h) & (h >= tp)
            if trig2.any():
                px2 = np.where(np.isfinite(o) & (o >= tp), o, tp)
                if two_leg:
                    # 腿1尚未成交的：本日两张限价单一起成交（保守，见 docstring）
                    px1 = np.where(np.isfinite(o) & (o >= tp1), o, tp1)
                    both = acc + w1 * (px1 / entry - 1) * 100 \
                        + (1 - w1) * (px2 / entry - 1) * 100
                    r_tot = np.where(leg1, acc + rem * (px2 / entry - 1) * 100, both)
                else:
                    r_tot = (px2 / entry - 1) * 100
                ret = np.where(trig2, r_tot, ret)
                rsn = np.where(trig2, R_TP, rsn).astype(np.int8)
                kexit = np.where(trig2, k, kexit).astype(np.int8)
                exited |= trig2
                if two_leg:
                    leg1 |= trig2      # 同日双成交也算腿1成交（只影响诊断口径，
                                       # 这些行已 exited，不会再参与保本止损判定）
            if two_leg:
                # 腿1：只卖 w1，**不算出场**，剩余仓位继续跑
                trig1 = ((~exited) & good & np.isfinite(c) & (~leg1)
                         & np.isfinite(h) & (h >= tp1))
                if trig1.any():
                    px1 = np.where(np.isfinite(o) & (o >= tp1), o, tp1)
                    acc = np.where(trig1, acc + w1 * (px1 / entry - 1) * 100, acc)
                    rem = np.where(trig1, 1 - w1, rem)
                    leg1 |= trig1
            if k == max_hold:
                act3 = (~exited) & good & np.isfinite(c)
                if act3.any():
                    ret = np.where(act3, acc + rem * (c / entry - 1) * 100, ret)
                    rsn = np.where(act3, R_HOLD, rsn).astype(np.int8)
                    kexit = np.where(act3, k, kexit).astype(np.int8)
                    exited |= act3
        peak = np.where(np.isfinite(h), np.fmax(peak, h), peak)
        prev_c = np.where(np.isfinite(c), c, prev_c)
    return ret, rsn, kexit, leg1


def _entry_cols(eoff: int, eref: str, max_hold: int) -> tuple[int, int]:
    """建仓时点 → (建仓日偏移 ke, 首个被监控日偏移 k0)，均以信号日 T 为 0。

    eoff=0, eref="open"  → ke=1, k0=1   T+1 开盘买，当天就开始盯止损（现网口径）
    eoff=0, eref="close" → ke=1, k0=2   T+1 收盘买，次日起盯
    eoff=1, eref="open"  → ke=2, k0=2   T+2 开盘买（等一天，看是否回踩）
    eoff=1, eref="close" → ke=2, k0=3   T+2 收盘买
    """
    ke = 1 + eoff
    k0 = ke + (1 if eref == "close" else 0)
    if k0 + max_hold - 1 > HOLD_WIN:
        raise SystemExit(
            f"前向数据只有 {HOLD_WIN} 日：建仓偏移 eoff={eoff}/{eref} + 持有 {max_hold} 日 "
            f"需要到 T+{k0 + max_hold - 1}，超界。请缩短持有期。")
    return ke, k0


def entry_ok(d: pd.DataFrame, eoff: int = 0, eref: str = "open") -> pd.Series:
    """建仓日是否真的能成交（剔一字板：整天 o=h=l 封死，挂单排队买不到）。

    ⚠ prep() 里的 `buyable` 只针对 T+1 开盘；换建仓时点后必须重算，
      否则会把「T+2 一字涨停买不到」的信号当成成交，凭空造出收益。
    """
    ke, _ = _entry_cols(eoff, eref, 1)
    e = d[f"{'c' if eref == 'close' else 'o'}{ke}"]
    return (e.notna() & (e > 0)
            & ~((d[f"o{ke}"] == d[f"h{ke}"]) & (d[f"o{ke}"] == d[f"l{ke}"])))


def sim_signals(d: pd.DataFrame, stop_pct=-0.06, take_pct=0.10, max_hold=10,
                ref="entry", arm=None, trail=None,
                atr_k=None, atr_lo=0.05, atr_hi=0.15,
                atr_tp_k=None, atr_tp_lo=0.10, atr_tp_hi=0.35,
                take1_pct=None, w1=0.5, be_after1=False,
                eoff=0, eref="open", with_leg1=False):
    """对信号表逐笔模拟出场 → (收益% Series, 原因码 Series, 持有日数 Series)。
    with_leg1=True 时多返回一个「腿1是否成交」的 bool Series。

    atr_k / atr_tp_k 不为 None 时改用**波动率自适应**档位：
        止损 = base × (1 − clip(atr_k × ATR14%/close, atr_lo, atr_hi))
        止盈 = base × (1 + clip(atr_tp_k × ATR14%/close, atr_tp_lo, atr_tp_hi))
    动机：规则本身**专挑高 ATR 的票**（atr_pct >= P60 ≈ 4.2%），固定 -6% 只有
    1.4 倍 ATR，训练期实测 50% 的单子被止损打掉 —— 档位必须跟着波动率走。

    take1_pct 不为 None 时启用分批止盈：摸到 +take1_pct 先卖 w1，剩余跑到 take_pct。
    eoff / eref 换建仓时点（见 _entry_cols）—— 用来检验「T+1 开盘直接买是不是
    为昨天那根阳线付了溢价」。
    """
    ke, k0 = _entry_cols(eoff, eref, max_hold)
    entry = d[f"{'c' if eref == 'close' else 'o'}{ke}"].to_numpy(float)
    cols = {p: [d[f"{p}{k0 + j - 1}"].to_numpy(float)
                for j in range(1, max_hold + 1)] for p in ("o", "h", "l", "c")}
    pc0 = d["close" if k0 == 1 else f"c{k0 - 1}"].to_numpy(float)
    base = entry if ref == "entry" else d["close"].to_numpy(float)
    apx = d["atr_pct"].to_numpy(float) / 100.0        # ATR14/close，信号日可知
    if atr_k is not None:
        sl = base * (1 - np.clip(atr_k * apx, atr_lo, atr_hi))
    else:
        sl = base * (1 + stop_pct)
    if atr_tp_k is not None:
        tp = base * (1 + np.clip(atr_tp_k * apx, atr_tp_lo, atr_tp_hi))
    else:
        tp = base * (1 + take_pct)
    if take1_pct is not None:
        tp1 = base * (1 + take1_pct)
        if not np.all(tp1 < tp):
            raise SystemExit(f"分批止盈要求第一腿低于第二腿：{take1_pct:+.0%} vs {take_pct:+.0%}")
    else:
        tp1 = None
    ret, rsn, kx, leg1 = _sim_core(
        lambda j: (cols["o"][j - 1], cols["h"][j - 1],
                   cols["l"][j - 1], cols["c"][j - 1]),
        pc0, entry, sl, tp, len(d), max_hold, arm, trail,
        tp1=tp1, w1=w1, be_after1=be_after1)
    out = (pd.Series(ret, index=d.index), pd.Series(rsn, index=d.index),
           pd.Series(kx, index=d.index))
    return out + (pd.Series(leg1, index=d.index),) if with_leg1 else out



def a3_day_series(a3: pd.DataFrame, ret: pd.Series | None = None,
                  **kw) -> pd.Series:
    """A3 基线：index=trade_date，values=当日**全部**反转首日信号在同一出场规则下的
    平均收益%。

    ⚠ 必须聚合到日级再交给 stats_block。传逐笔序列会 reindex 到策略自身的行，
      取到「同一笔交易自己」的收益，超额恒为 0（本文件曾犯过这个错）。
    """
    if ret is None:
        kw.pop("with_leg1", None)          # 这里只要收益，不要第 4 个返回值
        ret, _, _ = sim_signals(a3, **kw)
    return (pd.Series(ret.to_numpy(), index=a3["trade_date"].to_numpy())
            .groupby(level=0).mean())


def _load_panel():
    """全主板 O/H/L/C 面板矩阵 (n_dates, n_codes)，带磁盘缓存。"""
    if os.path.exists(PANEL_CACHE):
        z = np.load(PANEL_CACHE, allow_pickle=True)
        return (z["O"], z["H"], z["L"], z["C"],
                [str(x) for x in z["dates"]], [str(x) for x in z["codes"]])
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    df = pd.read_sql(
        "SELECT code, trade_date, open, high, low, close FROM daily_price "
        "WHERE trade_date >= ? AND trade_date <= ? "
        "  AND (code LIKE '60%' OR code LIKE '00%') ORDER BY trade_date",
        con, params=(SIG_START, SIG_END))
    con.close()
    df = df[(df["close"] > 0) & (df["open"] > 0)
            & (df["high"] > 0) & (df["low"] > 0)]
    dates = sorted(df["trade_date"].unique())
    codes = sorted(df["code"].unique())
    di = {x: i for i, x in enumerate(dates)}
    ci = {x: i for i, x in enumerate(codes)}
    r, q = df["trade_date"].map(di).to_numpy(), df["code"].map(ci).to_numpy()
    mats = []
    for col in ("open", "high", "low", "close"):
        M = np.full((len(dates), len(codes)), np.nan)
        M[r, q] = df[col].to_numpy(float)
        mats.append(M)
    os.makedirs(os.path.dirname(PANEL_CACHE), exist_ok=True)
    np.savez_compressed(PANEL_CACHE, O=mats[0], H=mats[1], L=mats[2], C=mats[3],
                        dates=np.array(dates), codes=np.array(codes))
    return mats[0], mats[1], mats[2], mats[3], dates, codes


def _panel_atr(H, L, C, n=14):
    """ATR14 矩阵（与 tools/_mine_reversal_winners.py 的 atr_pct 同口径：TR 简单均）。"""
    pc = np.vstack([np.full((1, C.shape[1]), np.nan), C[:-1]])
    with np.errstate(invalid="ignore"):
        tr = np.nanmax(np.stack([H - L, np.abs(H - pc), np.abs(L - pc)]), axis=0)
    return pd.DataFrame(tr).rolling(n, min_periods=n).mean().to_numpy()


def baseline_exit(stop_pct=-0.06, take_pct=0.10, max_hold=10,
                  arm=None, trail=None,
                  atr_k=None, atr_lo=0.05, atr_hi=0.15,
                  atr_tp_k=None, atr_tp_lo=0.10, atr_tp_hi=0.35,
                  take1_pct=None, w1=0.5, be_after1=False,
                  eoff=0, eref="open") -> pd.Series:
    """全主板「随便买一只」基线：任意日 T 按**同一建仓时点**入场 → **同一套**出场规则。

    返回 Series(index=信号日, values=当日全主板平均收益%)。
    ⚠ 基线也套同样的止盈止损（含 ATR 档位、分批止盈、建仓偏移）—— 否则「止损策略
      vs 裸持有市场」不可比（_diag_reversal_v5.py 当年就是这么比的，得出的
      「出场纪律有害」结论口径存疑）。
    """
    O, H, L, C, dates, codes = _load_panel()
    nd, nc = O.shape
    ke = 1 + eoff
    k0 = ke + (1 if eref == "close" else 0)
    m = nd - (k0 + max_hold - 1)
    if m <= 10:
        raise SystemExit("面板长度不足")
    E = (C if eref == "close" else O)[ke:ke + m]
    with np.errstate(invalid="ignore", divide="ignore"):
        if atr_k is not None or atr_tp_k is not None:
            A = _panel_atr(H, L, C)
            apx = np.where(C[0:m] > 0, A[0:m] / C[0:m], np.nan)
        if atr_k is not None:
            sl = E * (1 - np.clip(atr_k * apx, atr_lo, atr_hi))
        else:
            sl = E * (1 + stop_pct)
        if atr_tp_k is not None:
            tp = E * (1 + np.clip(atr_tp_k * apx, atr_tp_lo, atr_tp_hi))
        else:
            tp = E * (1 + take_pct)
        tp1 = E * (1 + take1_pct) if take1_pct is not None else None
        onebar = (E == H[ke:ke + m]) & (E == L[ke:ke + m])   # 建仓日一字板 ⇒ 买不到
    ret, _, _, _ = _sim_core(
        lambda j: (O[k0 + j - 1:k0 + j - 1 + m].ravel(),
                   H[k0 + j - 1:k0 + j - 1 + m].ravel(),
                   L[k0 + j - 1:k0 + j - 1 + m].ravel(),
                   C[k0 + j - 1:k0 + j - 1 + m].ravel()),
        C[k0 - 1:k0 - 1 + m].ravel(), E.ravel(),
        np.broadcast_to(sl, (m, nc)).ravel(), np.broadcast_to(tp, (m, nc)).ravel(),
        m * nc, max_hold, arm, trail,
        tp1=None if tp1 is None else np.broadcast_to(tp1, (m, nc)).ravel(),
        w1=w1, be_after1=be_after1)
    R = ret.reshape(m, nc)
    ok = np.isfinite(R) & (~onebar)                    # 剔建仓日一字板（买不到）
    with np.errstate(invalid="ignore"):
        mean = np.where(ok.sum(1) > 0, np.nansum(np.where(ok, R, 0), 1)
                        / np.maximum(ok.sum(1), 1), np.nan)
    return pd.Series(mean, index=dates[:m], name="mkt")


def calendar() -> list:
    _, _, _, _, dates, _ = _load_panel()
    return dates


# ══════════════════════════════════════════════════════════════════════════
# 组合规则（阈值一律由**训练期**分位数确定，测试期沿用同一批数字）
# ══════════════════════════════════════════════════════════════════════════
def train_thresholds(d: pd.DataFrame) -> dict:
    tr = d[d["period"] == "train"]
    q = lambda col, p: float(tr[col].quantile(p))          # noqa: E731
    z = lambda col: (float(tr[col].mean()),                # noqa: E731
                     float(tr[col].std(ddof=0)) or 1.0)
    T = {
        "ma60_p50": q("ma60_rel", .50), "ma60_p60": q("ma60_rel", .60),
        "ma60_p70": q("ma60_rel", .70),
        "atr_p50": q("atr_pct", .50), "atr_p60": q("atr_pct", .60),
        "atr_p70": q("atr_pct", .70),
        "vr_p30": q("vr", .30), "vr_p50": q("vr", .50),
        "dd_p80": q("dd_hi250", .80), "ret60_p80": q("ret60_p", .80),
        "z_ma60": z("ma60_rel"), "z_atr": z("atr_pct"), "z_vr": z("vr"),
    }
    sc = score_of(d, T)
    T["score_p60"] = float(sc.loc[tr.index].quantile(.60))
    T["score_p75"] = float(sc.loc[tr.index].quantile(.75))
    return T


def score_of(d: pd.DataFrame, T: dict) -> pd.Series:
    """三轴打分：趋势(ma60_rel) + 波动(atr_pct) − 爆量(vr)，训练期标准化常数。"""
    (m1, s1), (m2, s2), (m3, s3) = T["z_ma60"], T["z_atr"], T["z_vr"]
    return ((d["ma60_rel"].fillna(0) - m1) / s1 + (d["atr_pct"].fillna(0) - m2) / s2
            - (d["vr"].fillna(0) - m3) / s3)


def build_combos(d: pd.DataFrame, T: dict) -> dict:
    """{名称: 布尔掩码}。阈值全部来自 T（训练期定死），可对任意时间子集复用。"""
    S3 = (d["ma60_rel"] >= T["ma60_p60"]) & (d["atr_pct"] >= T["atr_p60"])
    sc = score_of(d, T)
    return {
        "A3": ("A3 裸信号（对照）", pd.Series(True, index=d.index)),
        "B1": (f"波动轴 atr>={T['atr_p60']:.2f}", d["atr_pct"] >= T["atr_p60"]),
        "B2": (f"趋势轴 ma60_rel>={T['ma60_p60']:.2f}", d["ma60_rel"] >= T["ma60_p60"]),
        "B3": (f"趋势+波动 ma60_rel>={T['ma60_p60']:.2f} & atr>={T['atr_p60']:.2f}", S3),
        "B4": (f"趋势+波动(宽P50) ma60>={T['ma60_p50']:.2f} & atr>={T['atr_p50']:.2f}",
               (d["ma60_rel"] >= T["ma60_p50"]) & (d["atr_pct"] >= T["atr_p50"])),
        "B5": (f"趋势+波动(严P70) ma60>={T['ma60_p70']:.2f} & atr>={T['atr_p70']:.2f}",
               (d["ma60_rel"] >= T["ma60_p70"]) & (d["atr_pct"] >= T["atr_p70"])),
        "B6": (f"B3+量比<={T['vr_p50']:.2f}", S3 & (d["vr"] <= T["vr_p50"])),
        "B7": (f"B3+近250日高 dd>={T['dd_p80']:.1f}", S3 & (d["dd_hi250"] >= T["dd_p80"])),
        "B8": (f"B3+前60日涨幅>={T['ret60_p80']:.1f}", S3 & (d["ret60_p"] >= T["ret60_p80"])),
        "B9": (f"三轴打分>=P60({T['score_p60']:.2f})", sc >= T["score_p60"]),
        "B10": (f"三轴打分>=P75({T['score_p75']:.2f})", sc >= T["score_p75"]),
        "B11": ("B3+择时 mkt_above_ma20>=40（对照：beta）",
                S3 & (d["mkt_above_ma20"] >= 40)),
        "B12": ("B3+择时 mkt_ret20>=2.5（对照：beta）",
                S3 & (d["mkt_ret20"] >= 2.5)),
    }


# ══════════════════════════════════════════════════════════════════════════
# Stage combos
# ══════════════════════════════════════════════════════════════════════════
def stage_combos(exit_kw=None) -> None:
    exit_kw = exit_kw or dict(stop_pct=-0.06, take_pct=0.10, max_hold=10)
    d = prep()
    T = train_thresholds(d)
    combos = build_combos(d, T)
    tr = d[d["period"] == "train"]
    nd = tr["trade_date"].nunique()
    mh = exit_kw["max_hold"]

    print("=" * 126)
    print(f"组合规则对比 · 训练期 {tr['trade_date'].min()} ~ {tr['trade_date'].max()}  "
          f"n={len(tr):,}  {nd} 个交易日")
    print(f"出场口径：T+1 开盘建仓 / 止损 {exit_kw['stop_pct']:+.0%} / "
          f"止盈 {exit_kw['take_pct']:+.0%} / 最长 {mh} 日")
    print("=" * 126)
    print("\n【3】目标标签口径（用户问题：10日内累计涨幅>10% 的命中率）")
    print(f"    {'规则':<46}{'笔数':>6}{'日均':>6}{'命中率%':>9}{'maxg10c%':>10}"
          f"{'超额pp':>8}{'t':>6}   分年(23/24/25)")
    for k, (nm, mask) in combos.items():
        sub = tr[mask.reindex(tr.index).fillna(False)]
        if len(sub) < 80:
            print(f"    {k} {nm:<44}{len(sub):>6}  样本过少")
            continue
        ex, t, _ = paired_daily(sub, "maxg10c", tr)
        yr = [paired_daily(sub[sub["year"] == y], "maxg10c", tr[tr["year"] == y])[0]
              for y in sorted(tr["year"].unique())]
        ok = all(np.isfinite(v) and v > 0 for v in yr)
        print(f"  {k} {nm:<44}{len(sub):>6}{len(sub)/nd:>6.2f}"
              f"{sub['win10'].mean()*100:>8.1f}%{sub['maxg10c'].mean():>9.2f}%"
              f"{ex:>+8.2f}{t:>+6.1f}   " + "/".join(f"{v:+.2f}" for v in yr)
              + ("  ★" if ok and np.isfinite(t) and t >= 2 else ""))

    print("\n【4】真实出场口径（净均值已扣往返成本 0.10pp）")
    mk = baseline_exit(**exit_kw)
    a3d = a3_day_series(tr, **exit_kw)
    print(HDR)
    res = {}
    for k, (nm, mask) in combos.items():
        sub = tr[mask.reindex(tr.index).fillna(False)]
        if len(sub) < 80:
            continue
        r, rsn, _ = sim_signals(sub, **exit_kw)
        s = stats_block(sub, r, a3d, mk.to_frame(f"r{mh}"), mh,
                        f"{k} {nm[:30]}", nd)
        res[k] = s
        print(fmt_row(s))
    print("\n    出场原因分布：")
    for k, (nm, mask) in combos.items():
        sub = tr[mask.reindex(tr.index).fillna(False)]
        if len(sub) < 80:
            continue
        _, rsn, _ = sim_signals(sub, **exit_kw)
        vc = rsn[rsn > 0].value_counts(normalize=True).sort_index()
        print(f"      {k:<5}" + "  ".join(
            f"{REASON_NAME.get(int(i), i)} {v*100:.1f}%" for i, v in vc.items()))
    return res


# ══════════════════════════════════════════════════════════════════════════
# Stage exit —— 出场参数网格（训练期定参）
# ══════════════════════════════════════════════════════════════════════════
def stage_exit(rule_key="B3") -> None:
    d = prep()
    T = train_thresholds(d)
    combos = build_combos(d, T)
    tr = d[d["period"] == "train"]
    nd = tr["trade_date"].nunique()
    nm_rule, mask_rule = combos[rule_key]
    _, mask_a3 = combos["A3"]
    sub = tr[mask_rule.reindex(tr.index).fillna(False)]
    a3 = tr[mask_a3.reindex(tr.index).fillna(False)]
    print("=" * 126)
    print(f"出场参数网格 · 训练期 · 规则 {rule_key}（{nm_rule}）  策略 n={len(sub):,}")
    print("  排序依据＝策略自身净均值与 vsA3 超额（两者都不需要市场基线，可全网格扫描）；")
    print("  网格跑完后再对 Top 候选补算「vs市场同出场基线」，避免 700 次全面板模拟。")
    print("=" * 126)

    STOPS = [-0.04, -0.05, -0.06, -0.07, -0.08, -0.10, -0.99]
    TAKES = [0.06, 0.08, 0.10, 0.12, 0.15, 0.20, 9.99]
    HOLDS = [3, 5, 8, 10]
    rows = []
    t0 = time.time()
    for mh in HOLDS:
        for sp in STOPS:
            for tk in TAKES:
                r, _, _ = sim_signals(sub, sp, tk, mh)
                a3d = a3_day_series(a3, stop_pct=sp, take_pct=tk, max_hold=mh)
                rr = r.dropna()
                if len(rr) < 80:
                    continue
                net = rr - COST_PP
                # ⚠ 配对基线必须先聚合到「交易日」级：逐笔 reindex 会取到自己
                day = (pd.DataFrame({"d": sub.loc[rr.index, "trade_date"].to_numpy(),
                                     "r": rr.to_numpy()})
                       .groupby("d")["r"].mean().to_frame("r"))
                day["b"] = a3d
                day = day.dropna()
                ex = day["r"] - day["b"]
                tex = (ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
                       if len(ex) > 3 and ex.std(ddof=1) > 0 else np.nan)
                yr = []
                for y in sorted(sub["year"].unique()):
                    e2 = ex[ex.index.str[:4] == y]
                    yr.append(e2.mean() if len(e2) >= 10 else np.nan)
                gp, gl = net[net > 0], net[net < 0]
                rows.append(dict(hold=mh, stop=sp, take=tk, n=len(rr),
                                 net=net.mean(), win=(net > 0).mean() * 100,
                                 pl=(gp.mean() / abs(gl.mean())) if len(gl) and len(gp) else np.nan,
                                 ex=ex.mean(), t=tex, yr=yr,
                                 yrs_ok=all(np.isfinite(v) and v > 0 for v in yr)))
    print(f"  网格 {len(rows)} 组，耗时 {time.time()-t0:.0f}s")
    g = pd.DataFrame(rows)

    def show(title, gg):
        print(f"\n  ── {title} ──")
        print(f"    {'持有':>4}{'止损':>7}{'止盈':>7}{'笔数':>7}{'净均值':>9}{'胜率':>8}"
              f"{'盈亏比':>7}{'vsA3':>8}{'t':>6}   分年vsA3")
        for _, r in gg.iterrows():
            print(f"    {int(r['hold']):>4}{r['stop']:>+7.0%}{r['take']:>+7.0%}"
                  f"{int(r['n']):>7}{r['net']:>8.2f}%{r['win']:>7.1f}%{r['pl']:>7.2f}"
                  f"{r['ex']:>+8.2f}{r['t']:>+6.1f}   "
                  + "/".join("nan" if not np.isfinite(v) else f"{v:+.2f}" for v in r["yr"])
                  + ("  ★" if r["yrs_ok"] else ""))

    show("按净均值 Top10", g.sort_values("net", ascending=False).head(10))
    show("按 vsA3 超额 Top10（且分年一致）",
         g[g["yrs_ok"]].sort_values("ex", ascending=False).head(10))
    show("按胜率 Top10", g.sort_values("win", ascending=False).head(10))

    # 移动止盈变体（在最优秀的固定档附近）
    print("\n  ── 移动止盈变体（浮盈达 arm 后启动，自峰值回撤 trail 清仓；止损兜底 -6%）──")
    print(f"    {'持有':>4}{'arm':>7}{'trail':>7}{'止盈':>7}{'笔数':>7}{'净均值':>9}"
          f"{'胜率':>8}{'盈亏比':>7}")
    for mh in (5, 8, 10):
        for arm, trail in ((0.06, 0.04), (0.08, 0.05), (0.10, 0.06), (0.08, 0.08)):
            r, _, _ = sim_signals(sub, -0.06, 9.99, mh, arm=arm, trail=trail)
            rr = r.dropna()
            if len(rr) < 80:
                continue
            net = rr - COST_PP
            gp, gl = net[net > 0], net[net < 0]
            print(f"    {mh:>4}{arm:>+7.0%}{trail:>7.0%}{'不设':>7}{len(rr):>7}"
                  f"{net.mean():>8.2f}%{(net>0).mean()*100:>7.1f}%"
                  f"{(gp.mean()/abs(gl.mean())) if len(gl) and len(gp) else float('nan'):>7.2f}")

    # Top3 补算市场基线
    print("\n  ── Top 候选补算「vs 全主板同出场基线」（同日配对）──")
    print(HDR)
    top = g[g["yrs_ok"]].sort_values("ex", ascending=False).head(3)
    top2 = g.sort_values("net", ascending=False).head(3)
    for _, r in pd.concat([top, top2]).drop_duplicates(
            subset=["hold", "stop", "take"]).iterrows():
        kw = dict(stop_pct=r["stop"], take_pct=r["take"], max_hold=int(r["hold"]))
        mk = baseline_exit(**kw)
        rr, _, _ = sim_signals(sub, **kw)
        s = stats_block(sub, rr, a3_day_series(a3, **kw),
                        mk.to_frame(f"r{int(r['hold'])}"),
                        int(r["hold"]),
                        f"持有{int(r['hold'])}/止损{r['stop']:+.0%}/止盈{r['take']:+.0%}",
                        nd)
        print(fmt_row(s))


# ══════════════════════════════════════════════════════════════════════════
# Stage exit2 —— 规则 × 出场 联合定参（含 ATR 自适应档位）
# ══════════════════════════════════════════════════════════════════════════
ENTRY_NAME = {(0, "open"): "T+1开盘", (0, "close"): "T+1收盘",
              (1, "open"): "T+2开盘", (1, "close"): "T+2收盘"}


def _enm(kw: dict) -> str:
    return ENTRY_NAME.get((kw.get("eoff", 0), kw.get("eref", "open")),
                          f"T+{1 + kw.get('eoff', 0)}{kw.get('eref', 'open')}")


def _kwn(kw: dict) -> str:
    """出场参数的紧凑中文描述。"""
    s = (f"ATR止损×{kw['atr_k']:.1f}" if kw.get("atr_k") is not None
         else f"止损{kw['stop_pct']:+.0%}")
    s += (f"/ATR止盈×{kw['atr_tp_k']:.1f}" if kw.get("atr_tp_k") is not None
          else ("/止盈不设" if kw["take_pct"] > 9 else f"/止盈{kw['take_pct']:+.0%}"))
    if kw.get("take1_pct") is not None:
        s += (f"（分批：+{kw['take1_pct']:.0%}卖{kw.get('w1', 0.5):.0%}"
              + ("·余保本" if kw.get("be_after1") else "") + "）")
    return s + f"/{kw['max_hold']}日"


def _exit_cell(sub: pd.DataFrame, a3: pd.DataFrame, **kw) -> dict:
    """一格出场参数的完整训练期指标（不含市场基线，故可全网格扫描）。"""
    kw2 = dict(kw)
    want_leg = kw2.pop("_want_leg", False)
    out = sim_signals(sub, with_leg1=want_leg, **kw2)
    r, rsn, kx = out[0], out[1], out[2]
    leg1 = out[3] if want_leg else None
    a3d = a3_day_series(a3, **kw2)
    rr = r.dropna()
    net = rr - COST_PP
    day = (pd.DataFrame({"d": sub.loc[rr.index, "trade_date"].to_numpy(),
                         "r": rr.to_numpy()})
           .groupby("d")["r"].mean().to_frame("r"))
    day["b"] = a3d
    day = day.dropna()
    ex = day["r"] - day["b"]
    sd = ex.std(ddof=1) if len(ex) > 3 else 0.0
    gp, gl = net[net > 0], net[net < 0]
    return dict(
        n=len(rr), net=float(net.mean()), win=float((net > 0).mean() * 100),
        pl=float(gp.mean() / abs(gl.mean())) if len(gl) and len(gp) else np.nan,
        med=float(net.median()), worst=float(net.min()),
        ex=float(ex.mean()) if len(ex) else np.nan,
        t=float(ex.mean() / (sd / np.sqrt(len(ex)))) if sd > 0 else np.nan,
        nday=len(ex),
        leg1=float(leg1.reindex(rr.index).mean() * 100) if leg1 is not None else np.nan,
        yr=[float(ex[ex.index.str[:4] == y].mean())
            if len(ex[ex.index.str[:4] == y]) >= 10 else np.nan
            for y in sorted(sub["year"].unique())],
        rsn=rsn, ret=r, kx=kx, kw=dict(kw2))


def _p_cell(tag: str, c: dict) -> str:
    star = ("  ★" if c["n"] >= 80 and all(np.isfinite(v) and v > 0 for v in c["yr"])
            and np.isfinite(c["t"]) and c["t"] >= 1.5 else "")
    return (f"    {tag:<40}{c['n']:>6}{c['net']:>8.2f}%{c['win']:>7.1f}%"
            f"{c['pl']:>7.2f}{c['ex']:>+8.2f}{c['t']:>+6.1f}   "
            + "/".join("nan" if not np.isfinite(v) else f"{v:+.2f}" for v in c["yr"])
            + star)


_H2 = (f"    {'出场':<40}{'笔数':>6}{'净均值':>9}{'胜率':>8}{'盈亏比':>7}"
       f"{'vsA3':>8}{'t':>6}   分年vsA3")


def stage_exit2(rules=("B3", "B5", "B7")) -> None:
    d = prep()
    T = train_thresholds(d)
    combos = build_combos(d, T)
    tr = d[d["period"] == "train"]
    _, mask_a3 = combos["A3"]
    a3 = tr[mask_a3.reindex(tr.index).fillna(False)]
    print("=" * 126)
    print(f"规则 × 出场 联合定参 · 训练期 {tr['trade_date'].min()} ~ {TRAIN_END}"
          f"  A3 基线 n={len(a3):,}")
    print("  网格只用「自身净均值 + 同日配对 vsA3」排序（两者都无需市场基线，可全扫）；")
    print("  跑完后对联合 Top 补算「vs 全主板同出场基线」（含 ATR 同口径），避免全面板模拟爆炸。")
    print("=" * 126)

    cells: list[tuple] = []
    for rk in rules:
        nm_rule, mask_rule = combos[rk]
        sub = tr[mask_rule.reindex(tr.index).fillna(False)]
        if len(sub) < 80:
            print(f"  {rk} 样本过少 (n={len(sub)})，跳过")
            continue
        print(f"\n【E1】{rk}（{nm_rule}） n={len(sub):,}  固定档 stop × take（持有 10 日）")
        print(_H2)
        for sp in (-0.05, -0.06, -0.07, -0.08, -0.10, -0.12, -0.99):
            for tk in (0.10, 0.12, 0.15, 0.20, 0.25, 9.99):
                kw = dict(stop_pct=sp, take_pct=tk, max_hold=10)
                c = _exit_cell(sub, a3, **kw)
                cells.append((rk, c))
                if tk in (0.15, 0.20, 9.99) or sp in (-0.08, -0.99):
                    print(_p_cell(_kwn(kw), c))

        print(f"\n【E2】{rk}  ATR 自适应档位（止损 = k×ATR14/close，夹在 5%~15%）")
        print(_H2)
        for ks in (1.5, 2.0, 2.5, 3.0):
            for kt in (2.0, 3.0, 4.0):
                for mh in (8, 10):
                    kw = dict(stop_pct=0, take_pct=0.10, max_hold=mh,
                              atr_k=ks, atr_tp_k=kt)
                    c = _exit_cell(sub, a3, **kw)
                    cells.append((rk, c))
                    if mh == 10 or ks == 2.5:
                        print(_p_cell(_kwn(kw), c))

        print(f"\n【E3】{rk}  持有期扫描（止损 -8% / 止盈 +20%）")
        print(_H2)
        for mh in (3, 5, 8, 10):
            kw = dict(stop_pct=-0.08, take_pct=0.20, max_hold=mh)
            c = _exit_cell(sub, a3, **kw)
            cells.append((rk, c))
            print(_p_cell(_kwn(kw), c))

    ok = [(rk, c) for rk, c in cells
          if c["n"] >= 200 and all(np.isfinite(v) and v > 0 for v in c["yr"])]
    print("\n" + "=" * 126)
    print("【E4】联合排序（要求：笔数>=200 且 分年 vsA3 全为正）")
    print("=" * 126)
    print("\n  ── 按 vsA3 超额 Top12 ──")
    print(_H2)
    top_ex = sorted(ok, key=lambda x: -x[1]["ex"])[:12]
    for rk, c in top_ex:
        print(_p_cell(f"{rk} {_kwn(c['kw'])}", c))
    print("\n  ── 按净均值 Top12 ──")
    print(_H2)
    top_net = sorted(ok, key=lambda x: -x[1]["net"])[:12]
    for rk, c in top_net:
        print(_p_cell(f"{rk} {_kwn(c['kw'])}", c))

    print("\n【E5】联合 Top5 补算「vs 全主板同出场基线」（同日配对，基线套同一档位）")
    print(HDR)
    seen = set()
    for rk, c in top_ex[:5]:
        key = (rk, tuple(sorted((k, v) for k, v in c["kw"].items())))
        if key in seen:
            continue
        seen.add(key)
        nm_rule, mask_rule = combos[rk]
        sub = tr[mask_rule.reindex(tr.index).fillna(False)]
        mk = baseline_exit(**c["kw"])
        mh = c["kw"]["max_hold"]
        s = stats_block(sub, c["ret"], a3_day_series(a3, **c["kw"]),
                        mk.to_frame(f"r{mh}"), mh, f"{rk} {_kwn(c['kw'])}",
                        tr["trade_date"].nunique())
        print(fmt_row(s))
        vc = c["rsn"][c["rsn"] > 0].value_counts(normalize=True).sort_index()
        print("        出场原因：" + "  ".join(
            f"{REASON_NAME.get(int(i), i)} {v*100:.1f}%" for i, v in vc.items())
            + f"   平均持有 {c['kx'][c['kx']>0].mean():.1f} 日")


# ══════════════════════════════════════════════════════════════════════════
# Stage backtest —— 四窗口 + 测试期 + 分年 + 1 万元账户组合模拟
# ══════════════════════════════════════════════════════════════════════════
def _windows(cal: list, max_hold: int) -> list:
    """窗口右端 = 最后一个「还有 max_hold 个完整交易日」的信号日，避免半窗偏差。"""
    end = cal[-(max_hold + 1)]
    ei = cal.index(end)
    out = []
    for lab, days in (("1个月", 21), ("3个月", 63), ("6个月", 126), ("1年", 250)):
        st = cal[max(0, ei - days + 1)]
        out.append((lab, st, end))
    return out


def stage_backtest(rule_key="B3", stop=-0.06, take=0.10, hold=10,
                   arm=None, trail=None, max_pos=2, capital=10000.0,
                   atr_k=None, atr_tp_k=None,
                   take1=None, w1=0.5, be_after1=False,
                   eoff=0, eref="open") -> None:
    d = prep()
    T = train_thresholds(d)
    combos = build_combos(d, T)
    cal = calendar()
    ci = {x: i for i, x in enumerate(cal)}
    nm_rule, mask_rule = combos[rule_key]
    _, mask_a3 = combos["A3"]
    d = d.assign(_rule=mask_rule.reindex(d.index).fillna(False),
                 _a3=mask_a3.reindex(d.index).fillna(False),
                 _score=score_of(d, T))
    # 前向数据不足的信号（窗口末尾）必须剔除，否则 maxg10c/出场都被静默截短
    ke, k0 = _entry_cols(eoff, eref, hold)
    d = d[d[f"c{k0 + hold - 1}"].notna()].copy()
    if (eoff, eref) != (0, "open"):
        # 换建仓时点 ⇒ 一字板过滤必须跟着换（prep 里的 buyable 只管 T+1 开盘）
        d = d[entry_ok(d, eoff, eref)].copy()
    kw = dict(stop_pct=stop, take_pct=take, max_hold=hold, arm=arm, trail=trail,
              atr_k=atr_k, atr_tp_k=atr_tp_k,
              take1_pct=take1, w1=w1, be_after1=be_after1, eoff=eoff, eref=eref)
    d["_ret"], d["_rsn"], d["_kx"] = sim_signals(d, **kw)
    d = d[d["_ret"].notna()].copy()
    d["_epx"] = d[f"{'c' if eref == 'close' else 'o'}{ke}"]     # 组合模拟用同一建仓价

    print("=" * 126)
    print(f"四窗口回测 · 规则 {rule_key}：{nm_rule}")
    print(f"出场：{_enm(kw)}建仓 / " + _kwn(kw)
          + (f" / 移动止盈 浮盈{arm:+.0%}启动·回撤{trail:.0%}清仓" if arm else ""))
    print(f"成本：每笔扣 {COST_PP:.2f}pp（佣金+印花税+滑点）   样本域：主板 60x/00x，"
          f"已剔 ST/退、{_enm(kw)}一字板、线上质量过滤")
    print("=" * 126)

    wins = _windows(cal, hold)
    eval_end = cal[-(hold + 1)]
    wins += [("测试期(样本外)", "2025-10-01", eval_end),
             ("训练期(样本内)", SIG_START, TRAIN_END),
             ("全样本", SIG_START, eval_end)]
    mk = baseline_exit(**kw).to_frame(f"r{hold}")     # 只算一次：与窗口无关
    # A3 基线也只需算一次：_ret 已对全体（含 A3）模拟过，直接按日聚合
    a3d = a3_day_series(d[d["_a3"]], ret=d.loc[d["_a3"], "_ret"])
    print(f"\n【5】窗口成绩单（策略 = {rule_key}；vsA3 = 同日配对超「全部反转首日同出场」；"
          f"vs市场 = 同日配对超「全主板随便买一只同出场」）")
    print(HDR)
    keep = []
    for lab, st, en in wins:
        w = d[(d["trade_date"] >= st) & (d["trade_date"] <= en)]
        wr = w[w["_rule"]]
        if len(wr) < 15:
            print(f"  {lab+' '+st+'~'+en:<34} 样本不足 (n={len(wr)})")
            continue
        # ⚠ 日均的分母必须是**窗口内的交易日总数**，不是「有信号的日子数」——
        #   用后者会把 0.68 只/交易日 显示成 1.81 只，直接误导仓位可行性判断。
        nd = max(1, sum(1 for x in cal if st <= x <= en))
        s = stats_block(wr, wr["_ret"], a3d, mk, hold,
                        f"{lab} {st}~{en}", nd)
        s["hit10"] = wr["win10"].mean() * 100
        s["fire_days"] = wr["trade_date"].nunique()
        s["ndays"] = nd
        # 按日等权 = 固定并发账户的真实期望口径（见 stage_calib）。「净均值」那一列
        # 是按笔等权，日均笔数波动大时会显著高估账户能拿到的数。
        s["net_day"] = float(wr.groupby("trade_date")["_ret"].mean().mean()) - COST_PP
        keep.append((lab, st, en, s, wr))
        print(fmt_row(s) + f"   命中10%+ {s['hit10']:.1f}%"
              f"   按日等权净 {s['net_day']:+.2f}%"
              f"   出信号 {s['fire_days']}/{s['ndays']} 天")

    print(f"\n【6】分年 / 分季度一致性（规则 {rule_key}，同出场参数）")
    print(f"    {'期间':<12}{'笔数':>6}{'日均':>6}{'净均值':>9}{'按日等权':>10}{'胜率':>8}"
          f"{'盈亏比':>7}{'vsA3':>8}{'vs市场':>8}{'命中10%+':>10}")
    for lab, keyf in (("分年", lambda x: x["trade_date"].str[:4]),
                      ("分季度", lambda x: x["trade_date"].str[:4] + "Q"
                       + ((x["trade_date"].str[5:7].astype(int) - 1) // 3 + 1).astype(str))):
        print(f"    ── {lab} ──")
        g = keyf(d)
        for per in sorted(g.unique()):
            w = d[g == per]
            wr = w[w["_rule"]]
            if len(wr) < 20:
                continue
            s = stats_block(wr, wr["_ret"], a3d, mk, hold, per,
                            max(1, w["trade_date"].nunique()))
            if s.get("n", 0) < 20:
                continue
            ndw = float(wr.groupby("trade_date")["_ret"].mean().mean()) - COST_PP
            print(f"    {per:<12}{s['n']:>6}{s['per_day']:>6.2f}{s['net']:>8.2f}%"
                  f"{ndw:>+9.2f}%{s['win']:>7.1f}%{s['pl']:>7.2f}{s['ex_a3']:>+8.2f}"
                  f"{s['ex_mkt']:>+8.2f}{wr['win10'].mean()*100:>9.1f}%")

    print(f"\n【7】出场原因分布（全样本 · 规则 {rule_key}）")
    wr = d[d["_rule"]]
    vc = wr["_rsn"].value_counts(normalize=True).sort_index()
    for i, v in vc.items():
        sub = wr[wr["_rsn"] == i]["_ret"] - COST_PP
        print(f"    {REASON_NAME.get(int(i), i):<10} {v*100:>5.1f}%   "
              f"n={len(sub):<5} 净均值 {sub.mean():+.2f}%")

    print(f"\n【8】¥{capital:,.0f} 账户组合模拟（最多同时 {max_pos} 只、同日多信号按三轴打分取前几名、"
          f"每仓 = 昨日权益/{max_pos}、A股整手取整、复利滚动、现金不足则跳过）")
    for lab, st, en in wins:
        port = portfolio(d, cal, ci, st, en, max_pos, capital, hold, ke=ke, k0=k0)
        if port is None:
            print(f"    {lab:<16} 样本不足")
            continue
        print(f"    {lab+' '+st+'~'+en:<34} 成交 {port['trades']:>3} 笔  "
              f"错过 {port['skipped']:>3} 笔  期末 ¥{port['equity']:>8,.0f}  "
              f"收益 {port['ret']:>+7.2f}%  最大回撤 {port['mdd']:>6.2f}%  "
              f"持仓日占比 {port['expo']*100:>5.1f}%  资金占用 {port['util']:>5.1f}%")
        print(f"    {'':<34} 成交那几笔：等权均净 {port['took']:>+6.2f}%  "
              f"资金加权均净 {port['took_vw']:>+6.2f}%  被跳过 {port['passed']:>+6.2f}%")

    print(f"\n【9】统计功效：这点边际在「¥{capital:,.0f} / {max_pos} 只并发」下测得出来吗？")
    print("    （单笔 σ 很大：结果集中在 −8%、+21%、+2% 三堆，不是一般的正态小波动）")
    avg_hold = float(d.loc[d["_rule"], "_kx"].clip(lower=1).mean())
    for lab, st, en in wins:
        w = d[(d["trade_date"] >= st) & (d["trade_date"] <= en)]
        wr = w[w["_rule"]]["_ret"] - COST_PP
        wr = wr.dropna()
        if len(wr) < 30:
            print(f"    {lab:<18} 样本不足 (n={len(wr)})")
            continue
        mu, sd = float(wr.mean()), float(wr.std(ddof=1))
        n_year = 245.0 / max(avg_hold, 1) * max_pos      # 一年能轮到几笔
        se1 = sd / np.sqrt(n_year) if n_year > 1 else np.nan
        # 账户层面的年度 1σ：sd 已是百分数，√n 笔独立交易、每笔占 1/max_pos 仓位
        acct_sd = sd * np.sqrt(n_year) / max_pos
        # 要在 1 年内达到 |t|>=2 ⇒ 需要 n >= (2σ/μ)²
        n_need = (2 * sd / mu) ** 2 if abs(mu) > 1e-9 else np.inf
        pos_need = n_need / max(245.0 / max(avg_hold, 1), 1e-9)
        print(f"    {lab:<18} n={len(wr):<5} μ={mu:+.2f}%  σ={sd:.2f}%  "
              f"单笔t={mu/(sd/np.sqrt(len(wr))):+.2f}   "
              f"{max_pos}只并发⇒年约{n_year:.0f}笔、单笔均值标准误±{se1:.2f}%、"
              f"账户年度1σ≈±{acct_sd:.0f}%")
        print(f"    {'':<18} 要把 μ 测到 2σ 显著需 {n_need:.0f} 笔 ⇒ "
              f"约 {pos_need:.0f} 只并发持仓（1 年内）"
              + ("  → 当前规模下**不可测**" if pos_need > max_pos * 4 else ""))


def _sig_prep(d: pd.DataFrame, cal: list, ci: dict, st: str, en: str,
              ke: int = 1, k0: int = 1, epx_col: str = "entry",
              rank_by: str | None = "_score") -> dict | None:
    """把窗口内的信号预处理成「按建仓日分桶的 dict 列表」，供 portfolio 复用。

    ⚠ 抽出来单算的原因：F3 要在同一窗口上跑 6 档并发 × 4 档资金 = 24 次组合模拟，
      每次都 iterrows() 一遍几千行会把扫描时间放大 24 倍。

    rank_by = 同日多信号时「先买谁」的排序键；None = 不排序（按代码顺序，等价于
      对收益无知的随机抓取）。F3 要同时跑两种，才能把「并发数的作用」与
      「排序键的作用」分开 —— 只跑 _score 会把两件事混在一起。
    """
    sig = d[(d["trade_date"] >= st) & (d["trade_date"] <= en) & d["_rule"]].copy()
    if len(sig) < 5:
        return None
    sig["spos"] = sig["trade_date"].map(ci)
    sig["epos"] = sig["spos"] + ke
    sig = sig[sig["epos"] < len(cal)]
    sig["exit_pos"] = sig["spos"] + k0 - 1 + sig["_kx"].clip(lower=1)
    sig = sig[sig["exit_pos"] < len(cal)]
    if not len(sig):
        return None
    ccols = [f"c{k}" for k in range(1, HOLD_WIN + 1)]
    if rank_by:
        sig = sig.sort_values(rank_by, ascending=False)
    recs = (sig[[c for c in ["spos", "epos", "exit_pos", "_ret", rank_by or "_ret",
                              epx_col] if c in sig.columns] + ccols]
            .to_dict("records"))
    by_entry: dict = {}
    for r in recs:
        by_entry.setdefault(int(r["epos"]), []).append(r)
    return dict(by_entry=by_entry, emin=int(sig["epos"].min()),
                emax=int(sig["exit_pos"].max()), epx_col=epx_col)


def portfolio(d: pd.DataFrame, cal: list, ci: dict, st: str, en: str,
              max_pos: int, capital: float, hold: int,
              ke: int = 1, k0: int = 1, pre: dict | None = None,
              rank_by: str | None = "_score") -> dict | None:
    """事件驱动资金曲线：信号日 T → 建仓日建仓 → 出场日落袋；每日按收盘价盯市。

    ke/k0 = 建仓日 / 首个被监控日相对信号日的偏移（见 _entry_cols）。
    建仓价用 `_epx`（若 stage_backtest 已按建仓时点算好），否则退回 T+1 开盘 `entry`。
    """
    if pre is None:
        pre = _sig_prep(d, cal, ci, st, en, ke, k0,
                        "_epx" if "_epx" in d.columns else "entry", rank_by)
    if pre is None:
        return None
    by_entry, epx_col = pre["by_entry"], pre["epx_col"]

    cash, open_pos, eq_curve = capital, [], []
    trades = skipped = held_days = 0
    deployed: list[float] = []
    took: list[float] = []          # 实际成交那几笔的净收益%（等权）
    took_w: list[tuple] = []        # (投入金额, 净收益%) —— 资金加权口径
    passed: list[float] = []        # 因仓位满/凑不足一手而放弃的那些信号
    mv_last = 0.0                   # 昨日收盘时的持仓市值（用于今日开盘定仓位）
    for p in range(pre["emin"], pre["emax"] + 1):
        # 1) 建仓：用**当日开盘前**可用现金（不含今日平仓回款）→ 偏保守
        #    仓位 = 昨日权益 / max_pos（固定比例）。
        #    ⚠ 不能用 cash/(空位数)：那样「只剩 1 个空位」时会把现金全押一只，
        #      仓位忽大忽小，资金加权收益与等权收益能差出 1 个数量级。
        eq_open = cash + mv_last
        for r in by_entry.get(p, []):
            if len(open_pos) >= max_pos:
                skipped += 1
                passed.append(r["_ret"] - COST_PP)
                continue
            amt = min(eq_open / max_pos, cash)
            epx = float(r[epx_col])
            # A 股整手：只能买 100 股的整数倍。1 万元账户下这一条**很要紧**——
            # 一只 30 元的票一手就 3000 元，凑不满的零头只能留在现金里吃 0 收益。
            lots = int(amt // (epx * 100))
            if lots < 1:
                skipped += 1
                passed.append(r["_ret"] - COST_PP)
                continue
            shares = lots * 100
            cost = shares * epx
            cash -= cost
            open_pos.append(dict(row=r, shares=shares, epx=epx))
            trades += 1
            took.append(r["_ret"] - COST_PP)
            took_w.append((cost, r["_ret"] - COST_PP))
        # 2) 平仓：exit_pos <= p 的今日了结（净收益已扣往返成本）
        still = []
        for pos in open_pos:
            if int(pos["row"]["exit_pos"]) <= p:
                cash += pos["shares"] * pos["epx"] \
                    * (1 + (pos["row"]["_ret"] - COST_PP) / 100)
            else:
                still.append(pos)
        open_pos = still
        if open_pos:
            held_days += 1
        # 3) 盯市（缺当日收盘则用建仓价占位）
        mv = 0.0
        for pos in open_pos:
            r = pos["row"]
            k = p - int(r["spos"])
            px = r[f"c{k}"] if 1 <= k <= HOLD_WIN else np.nan
            mv += pos["shares"] * (px if np.isfinite(px) else pos["epx"])
        eq_curve.append(cash + mv)
        deployed.append(mv)
        mv_last = mv

    if not eq_curve:
        return None
    eq = np.array(eq_curve)
    dep = np.array(deployed)
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min() * 100)
    took_arr = np.array(took) if took else np.array([np.nan])
    tw = np.array(took_w) if took_w else np.array([[np.nan, np.nan]])
    wsum = float(np.nansum(tw[:, 0]))
    return dict(trades=trades, skipped=skipped, equity=float(eq[-1]),
                ret=(eq[-1] / capital - 1) * 100, mdd=mdd,
                expo=held_days / len(eq), days=len(eq),
                # ⚠ 分母必须是**当日权益**，不是初始资金：账户赚了钱之后持仓市值
                #   自然超过初始本金，用初始本金会把占用率算成 107%、166% 这种
                #   >100% 的荒谬值（本文件曾犯过这个错）。
                util=float(np.mean(dep / np.maximum(eq, 1e-9))) * 100,
                took=float(np.nanmean(took_arr)),
                took_vw=float(np.nansum(tw[:, 0] * tw[:, 1]) / wsum) if wsum else np.nan,
                passed=float(np.mean(passed)) if passed else np.nan)


# ══════════════════════════════════════════════════════════════════════════
# Stage rank —— 同日多信号时「先买哪几只」的排序键（1 万元账户只能持 2 只，
#               排序键**就是策略的一部分**，选错会把 +0.7%/笔 做成账户亏损）
# ══════════════════════════════════════════════════════════════════════════
# (列名, 说明, 取大=True/取小=False)
RANK_KEYS = [
    ("_score", "三轴打分 趋势+波动−爆量", True),
    ("atr_pct", "ATR14/收盘%（波动）", True),
    ("atr_pct", "ATR14/收盘%（反向·挑低波）", False),
    ("ma60_rel", "偏离MA60%（趋势强度）", True),
    ("dd_hi250", "距250日高%（越近0越强）", True),
    ("ret60_p", "前60日涨幅%", True),
    ("vr", "量比（越低越温和）", False),
    ("ret", "信号日涨幅%", True),
    ("ret", "信号日涨幅%（反向·挑小阳）", False),
    ("ext20", "偏离MA20%（越低越不过热）", False),
    ("amt_yi", "成交额(亿)（流动性）", True),
    ("to_calc", "换手率%", True),
    ("body", "实体占比%", True),
    ("close_pos", "收盘在当日振幅位置", True),
]


def stage_rank(rule_key="B7", stop=-0.08, take=0.20, hold=8) -> None:
    """在**规则内部**比较各种同日排序键：取每日前 K 只，看净均值与稳定性。

    判定标准：训练期与测试期**同向**才算可用；只在训练期好的键＝过拟合。
    """
    d = prep()
    T = train_thresholds(d)
    combos = build_combos(d, T)
    nm_rule, mask_rule = combos[rule_key]
    d = d.assign(_rule=mask_rule.reindex(d.index).fillna(False),
                 _score=score_of(d, T))
    d = d[d[f"c{hold}"].notna()].copy()
    kw = dict(stop_pct=stop, take_pct=take, max_hold=hold)
    d["_ret"], d["_rsn"], d["_kx"] = sim_signals(d, **kw)
    d = d[d["_ret"].notna() & d["_rule"]].copy()
    d["_net"] = d["_ret"] - COST_PP
    tr = d[d["period"] == "train"]
    te = d[d["period"] != "train"]

    print("=" * 126)
    print(f"同日排序键对比 · 规则 {rule_key}（{nm_rule}） · 出场 {_kwn(kw)}")
    print(f"训练期 n={len(tr):,}（{tr['trade_date'].nunique()} 个信号日）   "
          f"测试期 n={len(te):,}（{te['trade_date'].nunique()} 个信号日）")
    print("  「全取」＝当日所有命中规则的股票等权（不限仓位数）；"
          "TopK ＝当日按该键排序只买前 K 只（1 万元账户的真实约束）")
    print("=" * 126)

    def topk(sub, col, desc, K):
        return (sub.sort_values(col, ascending=not desc)
                .groupby("trade_date", sort=False).head(K))

    def line(tag, a, b):
        def f(x):
            if len(x) < 15:
                return f"{len(x):>6}   样本不足      —      —  "
            n = x["_net"]
            gp, gl = n[n > 0], n[n < 0]
            pl = gp.mean() / abs(gl.mean()) if len(gl) and len(gp) else np.nan
            return (f"{len(x):>6}{n.mean():>9.2f}%{(n>0).mean()*100:>8.1f}%"
                    f"{pl:>7.2f}")
        return f"    {tag:<34}" + f(a) + " │" + f(b)

    print(f"    {'排序键':<32}{'n':>6}{'净均值':>9}{'胜率':>8}{'盈亏比':>7}"
          f" │{'n':>6}{'净均值':>9}{'胜率':>8}{'盈亏比':>7}")
    print(f"    {'':<32}{'── 训练期(样本内) ──':^32} │{'── 测试期(样本外) ──':^32}")
    print(line("全取（不限仓位）", tr, te))
    for col, lab, desc in RANK_KEYS:
        if col not in d.columns and col != "_score":
            continue
        for K in (1, 2, 3):
            tag = f"{lab} Top{K}" if K > 1 else f"{lab} Top1"
            print(line(tag, topk(tr, col, desc, K), topk(te, col, desc, K)))
        print()

    # 随机排序的分布（Top2 的运气成分有多大）
    print("  ── 随机取 2 只的净均值分布（1000 次抽样，训练期 / 测试期）──")
    rng = np.random.default_rng(7)
    for nm, sub in (("训练期", tr), ("测试期", te)):
        # 预取每日的净收益数组，避免 500×N 次 pandas 采样开销
        bags = [v["_net"].to_numpy() for _, v in sub.groupby("trade_date", sort=False)]
        bags = [b[np.isfinite(b)] for b in bags]
        bags = [b for b in bags if len(b)]
        if not bags:
            continue
        means = np.empty(1000)
        for i in range(1000):
            tot = cnt = 0.0
            for b in bags:
                k = min(2, len(b))
                pick = b if k == len(b) else b[rng.choice(len(b), k, replace=False)]
                tot += pick.sum()
                cnt += k
            means[i] = tot / max(cnt, 1)
        print(f"    {nm}: 均值 {means.mean():+.2f}%  中位 {np.median(means):+.2f}%  "
              f"5%分位 {np.percentile(means,5):+.2f}%  "
              f"95%分位 {np.percentile(means,95):+.2f}%  "
              f"单笔为负概率 {(means<0).mean()*100:.0f}%")


# ══════════════════════════════════════════════════════════════════════════
# Stage follow —— 报告 §8「如果还想继续挖」三条方向的实证
#   F1 分批止盈   F2 建仓时点   F3 并发数 × 资金规模（组合层是否收敛到单笔期望）
# 纪律沿用主报告：所有参数**只在训练期定**，选定后测试期一次性评估。
# ══════════════════════════════════════════════════════════════════════════
_MK_CACHE: dict = {}


def _mkt_base(kw: dict, hold: int) -> pd.DataFrame:
    """全主板同出场基线（全面板模拟，贵 ⇒ 按参数缓存）。"""
    key = tuple(sorted((k, v) for k, v in kw.items()))
    if key not in _MK_CACHE:
        _MK_CACHE[key] = baseline_exit(**kw).to_frame(f"r{hold}")
    return _MK_CACHE[key]


def _full_card(x: pd.DataFrame, sub: pd.DataFrame, a3s: pd.DataFrame,
               kw: dict, hold: int, label: str) -> dict:
    """一个变体在一个 period 上的完整成绩单（绝对 + vsA3 + vs市场，基线同口径）。

    额外算 net_day（按日等权净均值）—— 固定并发账户的真实期望口径，见 stage_calib。
    """
    if len(sub) < 20:
        return {"label": label, "n": len(sub)}
    r, _, _ = sim_signals(sub, **kw)
    s = stats_block(sub, r, a3_day_series(a3s, **kw), _mkt_base(kw, hold),
                    hold, label, max(1, x["trade_date"].nunique()))
    if s.get("n", 0) >= 20:
        rr = r.dropna()
        s["net_day"] = float(pd.Series(rr.to_numpy(),
                                       index=sub.loc[rr.index, "trade_date"].to_numpy())
                             .groupby(level=0).mean().mean()) - COST_PP
    return s


_F1H = (f"    {'方案':<44}{'笔数':>6}{'净均值':>9}{'胜率':>8}{'盈亏比':>7}"
        f"{'腿1成交':>9}{'vsA3':>8}{'t':>6}   分年vsA3")


def _f1row(tag: str, c: dict) -> str:
    star = ("  ★" if c["n"] >= 80 and all(np.isfinite(v) and v > 0 for v in c["yr"])
            and np.isfinite(c["t"]) and c["t"] >= 1.5 else "")
    l1 = "—" if not np.isfinite(c.get("leg1", np.nan)) else f"{c['leg1']:.1f}%"
    return (f"    {tag:<44}{c['n']:>6}{c['net']:>8.2f}%{c['win']:>7.1f}%"
            f"{c['pl']:>7.2f}{l1:>9}{c['ex']:>+8.2f}{c['t']:>+6.1f}   "
            + "/".join("nan" if not np.isfinite(v) else f"{v:+.2f}" for v in c["yr"])
            + star)


def stage_calib(rules=("A3", "B5", "B7"), stop=-0.08, take=0.20, hold=8) -> pd.DataFrame:
    """口径校正：把「每笔平均」换算成「每天每注平均」——后者才是固定并发账户的期望。

    为什么这一步是必需的（本轮最重要的发现）：
      §5 的「净均值」是**按笔等权**（所有交易一票一权）。但一个 max_pos 固定的账户
      每天最多只能下 max_pos 注，它面对的是**按日等权**的分布（每天先取当日均值，
      再对天平均）。两个口径在「日均笔数」波动大时会差出一个符号：
          B7 测试期  按笔 +0.69%/笔   →   按日 −0.11%/日注
      差额就是「收益在交易日之间的集中度」，本质上是 §2.1 说的 beta。
      vsA3 / vs市场 两个配对口径本来就在日级算，不受影响；受影响的只有**绝对收益**那一列。
    """
    d = prep()
    T = train_thresholds(d)
    combos = build_combos(d, T)
    d = d[d[f"c{hold}"].notna()].copy()
    kw = dict(stop_pct=stop, take_pct=take, max_hold=hold)
    d["_ret"], _, d["_kx"] = sim_signals(d, **kw)
    d = d[d["_ret"].notna()].copy()
    d["_net"] = d["_ret"] - COST_PP

    print("\n" + "=" * 132)
    print("【F0】口径校正：按笔等权 vs 按日等权（固定并发账户只能「每天下 max_pos 注」，属后者）")
    print(f"    出场 {_kwn(kw)} · 成本已扣 {COST_PP:.2f}pp/笔")
    print(f"    {'规则':<6}{'期':<6}{'笔数':>6}{'信号日':>7}{'日均笔数':>9}"
          f"{'按笔等权净':>12}{'按日等权净':>12}{'差=集中度':>11}{'日注为负天数':>13}")
    rows = []
    for rk in rules:
        m = combos[rk][1].reindex(d.index).fillna(False)
        for per, plab in (("train", "训练"), ("test", "测试")):
            x = d[m & ((d["period"] == "train") if per == "train"
                       else (d["period"] != "train"))]
            dm = x.groupby("trade_date")["_net"].mean()
            tw, dw = float(x["_net"].mean()), float(dm.mean())
            rows.append(dict(rule=rk, period=per, n=len(x), ndays=len(dm), tw=tw,
                             dw=dw, neg=float((dm < 0).mean() * 100)))
            print(f"    {rk:<6}{plab:<6}{len(x):>6}{len(dm):>7}{len(x)/max(len(dm),1):>9.2f}"
                  f"{tw:>11.2f}%{dw:>11.2f}%{dw-tw:>+10.2f}pp{(dm<0).mean()*100:>12.0f}%")
    print("    ⚠ 读法：「按日等权」才是把 ¥10,000 分成 max_pos 注、每天滚动的账户能拿到的期望。")
    print("      vsA3 / vs市场 两个配对口径本来就在日级聚合，不受此影响；只有**绝对净收益**这一列被高估。")

    print("\n    ── 集中度从哪来：按「当日 A3 信号数」分四档，看每档的按日等权净均值 ──")
    a3 = d[combos["A3"][1].reindex(d.index).fillna(False)]
    j = pd.concat([a3.groupby("trade_date").size().rename("n_sig"),
                   a3.groupby("trade_date")["_net"].mean().rename("net_day")],
                  axis=1).dropna()
    j["per"] = np.where(j.index <= TRAIN_END, "train", "test")
    for per, plab in (("train", "训练"), ("test", "测试")):
        s = j[j["per"] == per]
        q = pd.qcut(s["n_sig"], 4, labels=["信号最少1/4", "较少", "较多", "信号最多1/4"])
        print(f"      {plab}期：")
        for lev in q.cat.categories:
            v = s.loc[q == lev, "net_day"]
            print(f"        {lev:<12} 日均信号 {s.loc[q==lev,'n_sig'].mean():>5.1f}  "
                  f"天数 {len(v):>4}  按日等权净 {v.mean():>+6.2f}%")
        print("        ⇒ 训练期利润集中在信号最多的那 1/4 天（普涨日＝beta）；"
              "测试期这个结构**没有延续**，四档乱序 ⇒ 集中度本身也不稳定。")

    print("\n    ── 因子的「命中率」与「净收益」是否同向？（A3 池内五分位，这是选股有效/")
    print("       变现无效 的机制层解释：只有两者同向的因子才能进入变现层）")
    for col, lab, thr in (("dd_hi250", "距250日高%", -11.157),
                          ("atr_pct", "ATR14/收盘%", 4.209),
                          ("ma60_rel", "偏离MA60%", 6.10)):
        print(f"      ▼ {col}（{lab}）  B7 用的阈值 = {thr}")
        print(f"        {'分位':<8}{'训练n':>7}{'训练命中':>10}{'训练净':>9}"
              f"{'测试n':>8}{'测试命中':>10}{'测试净':>9}")
        q = pd.qcut(a3[col], 5, labels=["Q1低", "Q2", "Q3", "Q4", "Q5高"],
                    duplicates="drop")
        for lev in q.cat.categories:
            o = []
            for per in ("train", "test"):
                x = a3[(q == lev) & ((a3["period"] == "train") if per == "train"
                                     else (a3["period"] != "train"))]
                o += [len(x), x["win10"].mean() * 100, x["_net"].mean()]
            print(f"        {lev:<8}{o[0]:>7}{o[1]:>9.1f}%{o[2]:>8.2f}%"
                  f"{o[3]:>8}{o[4]:>9.1f}%{o[5]:>8.2f}%")
        for per, plab in (("train", "训练"), ("test", "测试")):
            x = a3[(a3["period"] == "train") if per == "train" else (a3["period"] != "train")]
            f = x[x[col] >= thr]
            print(f"        {plab}期 阈值过滤 ≥{thr}：n={len(f):>5}  "
                  f"命中10%+ {f['win10'].mean()*100:>5.1f}%（全池 {x['win10'].mean()*100:.1f}%）  "
                  f"按笔净 {f['_net'].mean():>+5.2f}%（全池 {x['_net'].mean():+.2f}%）  "
                  f"按日净 {f.groupby('trade_date')['_net'].mean().mean():>+5.2f}%"
                  f"（全池 {x.groupby('trade_date')['_net'].mean().mean():+.2f}%）")
    return pd.DataFrame(rows)


def stage_follow(rules=("A3", "B5", "B7"), stop=-0.08, take=0.20, hold=8,
                 poss=(2, 3, 5, 10, 20, 25),
                 caps=(10000.0, 25000.0, 50000.0, 125000.0)) -> None:
    d0 = prep()
    T = train_thresholds(d0)
    combos = build_combos(d0, T)
    cal = calendar()
    ci = {x: i for i, x in enumerate(cal)}
    ev_end = cal[-(hold + 1)]

    def sub_of(x, rk):
        return x[combos[rk][1].reindex(x.index).fillna(False)]

    base = d0[d0[f"c{hold}"].notna()].copy()
    base["_score"] = score_of(base, T)

    print("=" * 132)
    print("报告 §8 三条后续方向的实证 · 纪律：参数一律训练期定，测试期只评一次")
    print(f"冻结基线：止损{stop:+.0%} / 止盈{take:+.0%} / 最长 {hold} 日 · T+1 开盘建仓 · "
          f"每笔扣 {COST_PP:.2f}pp")
    print(f"训练期 {SIG_START}~{TRAIN_END}   测试期 2025-10-01~{ev_end}")
    print("=" * 132)

    # F0 口径校正：不先看这一步，后面所有「净均值」都会被高估
    stage_calib(rules, stop, take, hold)

    # ── F1 分批止盈 ────────────────────────────────────────────────────
    print("\n【F1】分批止盈（§8 建议②）：摸到 +X% 卖一部分、剩余跑到 +Y%")
    print("    口径：同日最高价同时越过 X 与 Y ⇒ **两腿都算成交**（真实挂单就是两张限价单")
    print("          同时在场），不给「全仓吃到 Y」的乐观解；腿1成交后若撞止损，剩余照止损。")
    print("    「余保本」= 腿1成交后把剩余仓位止损上移到建仓价。")
    print("    ★ = 笔数≥80 且 分年 vsA3 全为正 且 t≥1.5")
    f1_pick = {}
    for rk in rules:
        tr = base[base["period"] == "train"]
        trr, tra = sub_of(tr, rk), sub_of(tr, "A3")
        variants = [(None, take, 0.5, False)]          # [0] 恒为单腿基线
        for t1 in (0.08, 0.10, 0.12):
            for t2 in (0.20, 0.25):
                for wv in (0.5, 0.33):
                    for be in (False, True):
                        variants.append((t1, t2, wv, be))
        print(f"\n  ▼ {rk}：{combos[rk][0]}   训练期 n={len(trr):,}")
        print(_F1H)
        cells = []
        for t1, t2, wv, be in variants:
            kw = dict(stop_pct=stop, take_pct=t2, max_hold=hold)
            if t1 is None:
                tag = f"{rk} 单腿 →{t2:+.0%}（冻结基线）"
            else:
                tag = f"{rk} {t1:+.0%}卖{wv:.0%} →{t2:+.0%}" + ("·余保本" if be else "")
                kw.update(take1_pct=t1, w1=wv, be_after1=be)
            c = _exit_cell(trr, tra, _want_leg=True, **kw)
            cells.append((tag, c, kw, t1 is None))
            print(_f1row(tag, c))
        okc = [(tag, c, kw) for tag, c, kw, isb in cells
               if not isb and c["n"] >= 80 and np.isfinite(c["t"])
               and all(np.isfinite(v) and v > 0 for v in c["yr"])]
        f1_pick[rk] = (cells[0], max(okc, key=lambda x: x[1]["t"]) if okc else None)

    print("\n  ── F1 判定：训练期选出的分批档 vs 单腿基线，**测试期一次性评估** ──")
    print(HDR)
    for rk in rules:
        (btag, _, bkw, _), best = f1_pick[rk]
        cand = [(btag, bkw)] + ([(best[0], best[2])] if best else [])
        for tag, kw in cand:
            for per, plab in (("train", "训练"), ("test", "测试")):
                x = base[base["period"] == per]
                s = _full_card(x, sub_of(x, rk), sub_of(x, "A3"), kw, hold,
                               f"{tag} [{plab}]")
                print(fmt_row(s) + (f"   按日等权 {s['net_day']:+.2f}%"
                                    if "net_day" in s else ""))
        if best is None:
            print(f"  {rk:<34} （训练期没有任何分批档满足「分年全正 + t≥1.5」）")

    # ── F2 建仓时点 ────────────────────────────────────────────────────
    print("\n" + "=" * 132)
    print("【F2】建仓时点（§8 建议③）：T+1 开盘直接买，是不是在为昨天那根阳线付溢价？")
    print("    注：样本仍带 prep() 的「T+1 非一字板」过滤（那天封死的话整条 setup 已变味），")
    print("        T+2 建仓再额外剔 T+2 一字板。出场参数与 F1 基线完全相同。")
    F2V = [
        ("T+1 开盘（现网口径）", 0, "open", None),
        ("T+1 收盘（尾盘再决定）", 0, "close", None),
        ("T+2 开盘（多等一天）", 1, "open", None),
        ("T+2 收盘", 1, "close", None),
        ("T+2 开盘·仅 T+1 收阴（等到回踩才买）", 1, "open", "pullback"),
    ]
    print(f"    {'建仓时点':<38}{'规则':<6}{'期':<6}{'笔数':>6}{'按笔净':>9}{'按日净':>9}"
          f"{'胜率':>8}{'盈亏比':>7}{'vsA3':>8}{'t':>6}{'vs市场':>9}{'t':>6}")
    for lab, eoff, eref, extra in F2V:
        ke, k0 = _entry_cols(eoff, eref, hold)
        dd = base[base[f"c{k0 + hold - 1}"].notna()].copy()
        if (eoff, eref) != (0, "open"):
            dd = dd[entry_ok(dd, eoff, eref)].copy()
        if extra == "pullback":
            dd = dd[dd["c1"] < dd["close"]].copy()      # T+1 收阴 = 真的回踩了
        kw = dict(stop_pct=stop, take_pct=take, max_hold=hold, eoff=eoff, eref=eref)
        for rk in rules:
            for per, plab in (("train", "训练"), ("test", "测试")):
                x = dd[dd["period"] == per]
                s = _full_card(x, sub_of(x, rk), sub_of(x, "A3"), kw, hold, "")
                if s.get("n", 0) < 20:
                    print(f"    {lab:<38}{rk:<6}{plab:<6}{s.get('n',0):>6}   样本不足")
                    continue
                print(f"    {lab:<38}{rk:<6}{plab:<6}{s['n']:>6}{s['net']:>8.2f}%"
                      f"{s['net_day']:>+8.2f}%{s['win']:>7.1f}%{s['pl']:>7.2f}"
                      f"{s['ex_a3']:>+8.2f}{s['t_a3']:>+6.1f}"
                      f"{s['ex_mkt']:>+9.2f}{s['t_mkt']:>+6.1f}")
        print()

    # ── F3 并发数 × 资金规模 ───────────────────────────────────────────
    print("=" * 132)
    print("【F3】并发数 × 资金规模（§8 建议①）：把并发提上去，组合层会收敛到单笔期望吗？")
    kw0 = dict(stop_pct=stop, take_pct=take, max_hold=hold)
    print("\n  ── 先问物理上限：能同时持有几只，由「日均信号数 × 平均持有天数」决定，不由资金决定 ──")
    print(f"    {'规则':<8}{'日均信号':>10}{'平均持有':>10}{'可行并发上限':>14}"
          f"{'测试期净均值/笔':>18}{'把 μ 测到 2σ 需要':>20}")
    feas = {}
    for rk in rules:
        te = base[base["period"] != "train"]
        s = sub_of(te, rk)
        r, _, kx = sim_signals(s, **kw0)
        ok = r.notna()
        net = (r[ok] - COST_PP)
        nd = te["trade_date"].nunique()
        ah = float(kx[ok].clip(lower=1).mean())
        perday = ok.sum() / nd
        mu, sd = float(net.mean()), float(net.std(ddof=1))
        need = (2 * sd / mu) ** 2 if abs(mu) > 1e-9 else np.inf
        posneed = need / max(245.0 / max(ah, 1), 1e-9)
        feas[rk] = perday * ah
        print(f"    {rk:<8}{perday:>10.2f}{ah:>10.1f}{perday*ah:>14.1f}"
              f"{mu:>17.2f}%{posneed:>17.0f} 只"
              + ("  ← 信号量不够，加钱也买不到这么多只" if posneed > perday * ah * 1.5 else ""))

    for rk in rules:
        dd = base.copy()
        dd["_ret"], dd["_rsn"], dd["_kx"] = sim_signals(dd, **kw0)
        dd = dd[dd["_ret"].notna()].copy()
        dd["_epx"] = dd["entry"]
        dd["_rule"] = combos[rk][1].reindex(dd.index).fillna(False)
        capbig = caps[-1]
        for wl, st, en in (("测试期(样本外)", "2025-10-01", ev_end),
                           ("全样本", SIG_START, ev_end)):
            net_all = (dd.loc[dd["_rule"] & (dd["trade_date"] >= st)
                              & (dd["trade_date"] <= en), "_ret"] - COST_PP)
            ref = float(net_all.mean()) if len(net_all) else np.nan
            print(f"\n    ▼ {rk} · {wl} {st}~{en}   可行并发上限 ≈ {feas[rk]:.1f} 只   "
                  f"该窗口全部信号等权净均值 {ref:+.2f}%/笔（n={len(net_all)}）")
            pres = {rb: _sig_prep(dd, cal, ci, st, en, 1, 1, "_epx", rb)
                    for rb in ("_score", None)}
            if pres["_score"] is None:
                print("      样本不足")
                continue
            took_tb: dict = {}
            for rb, rbn, tag in (("_score", "同日按三轴打分取前几名（现网做法）", "a"),
                                 (None, "同日不排序（按代码顺序抓，对收益无知）", "b")):
                print(f"      ({tag}) {rbn}")
                print(f"        单元格 = 账户收益% / 资金占用% / 成交笔数 / 最大回撤%")
                print(f"    {'并发':<8}"
                      + "".join(f"{'¥'+format(int(c),','):>34}" for c in caps))
                res: dict = {}
                for mp in poss:
                    row = f"    {str(mp)+' 只':<8}"
                    for cap in caps:
                        p = portfolio(dd, cal, ci, st, en, mp, cap, hold, 1, 1,
                                      pre=pres[rb], rank_by=rb)
                        if p is None:
                            row += f"{'样本不足':>34}"
                            continue
                        res[(mp, cap)] = p["ret"]
                        if cap == capbig:
                            took_tb[(tag, mp)] = (p["took"], p["took_vw"], p["trades"])
                        row += (f"{p['ret']:>+11.2f}%/{p['util']:>5.1f}%/"
                                f"{p['trades']:>4}/{p['mdd']:>7.2f}%")
                    print(row)
                for cap in caps:
                    v = [res[(mp, cap)] for mp in poss if (mp, cap) in res]
                    if len(v) >= 2:
                        print(f"      ¥{cap:>9,.0f}：只改并发数 ⇒ 收益极差 "
                              f"{max(v)-min(v):>6.1f}pp（{min(v):+.1f}% ~ {max(v):+.1f}%）"
                              f"   平均 {np.mean(v):+.1f}%")
            print(f"      (c) 成交那几笔的**单笔质量**（¥{capbig:,.0f} 档）—— "
                  f"并发提上去，抓到的票会不会收敛到全体等权 {ref:+.2f}%？")
            print(f"    {'并发':<8}{'打分·等权':>12}{'打分·资金加权':>16}"
                  f"{'不排序·等权':>14}{'不排序·资金加权':>18}{'成交笔数':>12}")
            for mp in poss:
                a, b = took_tb.get(("a", mp)), took_tb.get(("b", mp))
                if not a and not b:
                    continue
                f = lambda v, i: f"{v[i]:+.2f}%" if v else "—"
                print(f"    {str(mp)+' 只':<8}{f(a,0):>12}{f(a,1):>16}"
                      f"{f(b,0):>14}{f(b,1):>18}"
                      f"{(str(a[2]) if a else '—')+'/'+(str(b[2]) if b else '—'):>12}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?", default="combos",
                    choices=["combos", "exit", "exit2", "rank", "backtest",
                             "calib", "follow"])
    ap.add_argument("--rule", default="B3")
    ap.add_argument("--stop", type=float, default=-0.06)
    ap.add_argument("--take", type=float, default=0.10)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--arm", type=float, default=None)
    ap.add_argument("--trail", type=float, default=None)
    ap.add_argument("--atr-k", type=float, default=None,
                    help="止损 = k×ATR14/close（夹 5%%~15%%），给了就覆盖 --stop")
    ap.add_argument("--atr-tp-k", type=float, default=None,
                    help="止盈 = k×ATR14/close（夹 10%%~35%%），给了就覆盖 --take")
    ap.add_argument("--max-pos", type=int, default=2)
    ap.add_argument("--capital", type=float, default=10000.0,
                    help="组合模拟的初始资金（默认 ¥10,000 = 用户实盘规模）")
    ap.add_argument("--take1", type=float, default=None,
                    help="分批止盈第一腿，如 0.10 = 摸到 +10%% 先卖一部分")
    ap.add_argument("--w1", type=float, default=0.5, help="第一腿卖出比例（默认半仓）")
    ap.add_argument("--be-after1", action="store_true",
                    help="第一腿成交后把剩余仓位止损上移到建仓价（保本）")
    ap.add_argument("--eoff", type=int, default=0,
                    help="建仓日相对 T+1 再往后推几天（0=T+1，1=T+2）")
    ap.add_argument("--eref", default="open", choices=["open", "close"],
                    help="按建仓日的开盘价还是收盘价成交")
    a = ap.parse_args()
    if a.stage == "combos":
        stage_combos()
    elif a.stage == "exit":
        stage_exit(a.rule)
    elif a.stage == "exit2":
        stage_exit2(tuple(x.strip() for x in a.rule.split(",")))
    elif a.stage == "rank":
        stage_rank(a.rule, a.stop, a.take, a.hold)
    elif a.stage == "calib":
        stage_calib(tuple(x.strip() for x in a.rule.split(",")),
                    a.stop, a.take, a.hold)
    elif a.stage == "follow":
        stage_follow(tuple(x.strip() for x in a.rule.split(",")),
                     a.stop, a.take, a.hold)
    else:
        stage_backtest(a.rule, a.stop, a.take, a.hold, a.arm, a.trail, a.max_pos,
                       capital=a.capital, atr_k=a.atr_k, atr_tp_k=a.atr_tp_k,
                       take1=a.take1, w1=a.w1, be_after1=a.be_after1,
                       eoff=a.eoff, eref=a.eref)
