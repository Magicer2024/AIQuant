"""tools/eval_topk_pick.py —— 「每日 buy 池怎么挑 top10」的排序键离线评估

为什么需要它
────────────
deep_tracker.sync_from_scan 每天从 stock_deep_signal 的 buy 池里取前 N(=10) 只建跟踪单。
buy 池每天 40~90 只，top10 怎么挑直接决定整个跟踪面板的收益。历史上一度只用
`pct_above_ma20 ASC`（越贴 MA20 越优先）单键排序，丢弃了规则分等更强区分度的信息。

本脚本在**同一 buy 池**上，用**完全复刻 deep_tracker 的入场/出场规则**模拟收益，
横向对比多个排序键，给出可验证的取舍依据。

口径（与 strategy/deep_tracker.py 严格一致，改动任一侧都要同步改这里）
──────────────────────────────────────────────────────────────────
· 入场：信号日 T 收盘价 = sig_entry。T+1 起 entry_window_days(5) 个交易日内，
        low <= sig_entry 才成交，成交价 = min(sig_entry, open)；窗口内没回踩 → expired。
· 出场：从建仓日（含）起，j=1 当日不判（A股 T+1）：
        close <= sig_stop → stop_loss
        曾触及 launch(entry×1.08) 且 close <= 最高价×(1-0.03) → trailing_stop
        j >= max_hold(10) → max_hold_days（按当日收盘了结）
· 收益：(出场价 - 成交价) / 成交价 × 100

无未来函数：所有特征都用 as_of=scan_date 截断的行情重算（analyze_stock(as_of=...)）。

用法
────
    python tools/eval_topk_pick.py                    # 全部扫描日
    python tools/eval_topk_pick.py --days 30          # 最近 30 个扫描日
    python tools/eval_topk_pick.py --procs 12 --topn 10
    python tools/eval_topk_pick.py --out reports/xx.json

输出：控制台对比表 + 可选 JSON（--out）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics as st
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.strategy_params import DEEP_TRACK, get_param  # noqa: E402
from core.db import DB_PATH  # noqa: E402

# ── 与 deep_tracker 同步的参数 ──────────────────────────────
def _p(key, fb, default):
    v = DEEP_TRACK.get(key)
    if v is not None:
        return v
    try:
        v = get_param(fb)
    except Exception:
        v = None
    return default if v is None else v


ENTRY_WINDOW = int(_p("entry_window_days", "entry_window_days", 5) or 5)
MAX_HOLD = int(_p("max_hold_days", "short_max_hold_days", 10) or 10)
TP_PCT = float(_p("take_profit_pct", "short_take_profit", 0.08) or 0.08)
TRAIL_PCT = float(_p("trailing_pct", "short_trailing_pct", 0.03) or 0.03)
FALLBACK_STOP = abs(float(_p("stop_loss_pct", "short_stop_loss", -0.06) or -0.06))


# ─────────────────────────────────────────────
# 1. 特征重算（多进程 worker，spawn 安全）
# ─────────────────────────────────────────────
_wconn = None


def _conn():
    global _wconn
    if _wconn is None:
        _wconn = sqlite3.connect(DB_PATH, timeout=30)
        _wconn.row_factory = sqlite3.Row
        _wconn.execute("PRAGMA busy_timeout=10000")
    return _wconn


def _feat_one(args):
    """重算单只股票在 as_of 日的深析特征（无未来函数）。

    ⚠ 特征提取是**零额外计算成本**的：analyze_stock(light=True) 内部已经跑完了
      trend_state / analyze_volume_price / detect_rhythm 三个子模块，这里只是把
      它们的返回值全部取出来。早期版本只取了 score/frac20/risk_pct/atr_pct/ext，
      量价（OBV、背离、量比）与趋势（MACD、RSI、regime）等白白丢弃了 —— 这些正是
      「top4 精排」需要的正向指标弹药。
    """
    code, as_of = args
    try:
        from strategy.stock_deep import analyze_stock
        r = analyze_stock(_conn(), code, lookback=180, recent_days=0,
                          light=True, as_of=as_of)
        if r.get("insufficient"):
            return None
        sig = r.get("signal") or {}
        ap = sig.get("action_plan") or {}
        tr = r.get("trend") or {}
        rh = r.get("rhythm") or {}
        vp = r.get("volume_price") or {}
        entry = ap.get("entry_price")
        if not entry:
            return None
        return {
            "code": code,
            "as_of": as_of,
            "level": sig.get("level"),
            # ── 规则分（既有维度）──
            "score": sig.get("score"),
            "base_score": sig.get("base_score"),
            "frac20": sig.get("frac20"),
            "risk_pct": sig.get("risk_pct"),
            "atr_pct": sig.get("atr_pct"),
            "rr": ap.get("risk_reward_ratio"),
            # ── 趋势 / 位置 ──
            "ext": tr.get("pct_above_ma20"),
            "ext60": tr.get("pct_above_ma60"),
            "ext_ema": tr.get("pct_above_ema20"),
            "macd": tr.get("macd"),
            "rsi14": tr.get("rsi14"),
            "rsi_zone": tr.get("rsi_zone"),
            "regime": tr.get("regime"),
            "alignment": tr.get("alignment"),
            "volatility": tr.get("volatility"),
            # ── 量价（原脚本完全未使用）──
            "divergence": vp.get("divergence"),
            "obv_trend": vp.get("obv_trend"),
            "obv_grad_pct": vp.get("obv_grad_pct"),
            "updown_vol": vp.get("updown_volume_ratio"),
            "volume_ratio": vp.get("volume_ratio"),
            "health": vp.get("health"),
            # ── 节奏 ──
            "rhythm_hint": rh.get("position_hint") or "",
            "pattern": rh.get("pattern"),
            "current_leg": rh.get("current_leg"),
            "position_pct": rh.get("position_pct"),
            "up_amp_avg": rh.get("up_amp_avg"),
            "down_amp_avg": rh.get("down_amp_avg"),
            "cycle_avg_days": rh.get("cycle_avg_days"),
            # ── 交易执行 ──
            "entry": entry,
            "stop": ap.get("stop_loss"),
            "tp": ap.get("take_profit"),
        }
    except Exception:
        return None


def build_features(dates_codes: dict, procs: int) -> dict:
    """dates_codes: {scan_date: [code,...]} → {scan_date: {code: feat}}"""
    tasks = [(c, d) for d, cs in dates_codes.items() for c in cs]
    out = {d: {} for d in dates_codes}
    if not tasks:
        return out
    done = 0
    with ProcessPoolExecutor(max_workers=procs) as ex:
        futs = [ex.submit(_feat_one, t) for t in tasks]
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if done % 500 == 0:
                print(f"    ... 特征 {done}/{len(tasks)}", flush=True)
            if r and r.get("level") == "buy":
                out[r["as_of"]][r["code"]] = r
    return out


# ─────────────────────────────────────────────
# 2. 模拟（复刻 deep_tracker 入场/出场）
# ─────────────────────────────────────────────
def _prices_after(conn, code, start_date, limit):
    return conn.execute(
        """SELECT trade_date, open, high, low, close FROM daily_price
           WHERE code = ? AND trade_date > ? ORDER BY trade_date ASC LIMIT ?""",
        (code, start_date, limit)).fetchall()


def _prices_from(conn, code, start_date, limit):
    return conn.execute(
        """SELECT trade_date, open, high, low, close FROM daily_price
           WHERE code = ? AND trade_date >= ? ORDER BY trade_date ASC LIMIT ?""",
        (code, start_date, limit)).fetchall()


def simulate(conn, f: dict) -> dict:
    """对单只候选跑完整入场+出场，返回 {status, ret, hold_tdays, reason}。"""
    code, sig_entry = f["code"], f["entry"]
    # ── 入场 ──
    ps = _prices_after(conn, code, f["as_of"], ENTRY_WINDOW + 2)
    if not ps:
        return {"status": "no_data"}
    filled = None
    for idx, p in enumerate(ps):
        if idx >= ENTRY_WINDOW:
            break
        if p["low"] is None:
            continue
        if p["low"] <= sig_entry:
            open_ = p["open"] or sig_entry
            filled = {
                "date": p["trade_date"],
                "px": round(min(sig_entry, open_), 2),
            }
            break
    if not filled:
        if len(ps) >= ENTRY_WINDOW:
            return {"status": "expired"}
        return {"status": "pending"}

    entry_px, entry_date = filled["px"], filled["date"]
    stop = f["stop"] if (f["stop"] and f["stop"] > 0) else entry_px * (1 - FALLBACK_STOP)
    launch = entry_px * (1 + TP_PCT)

    # ── 出场 ──
    seq = _prices_from(conn, code, entry_date, MAX_HOLD + 5)
    if len(seq) < 2:
        return {"status": "holding", "ret": None}
    highest, tline, launched = None, None, False
    for j, p in enumerate(seq[:MAX_HOLD], start=1):
        close, high = p["close"], (p["high"] or p["close"])
        if not close or close <= 0:
            break
        if highest is None or high > highest:
            highest = high
        if not launched and highest >= launch:
            launched = True
        if launched:
            line = highest * (1 - TRAIL_PCT)
            if tline is None or line > tline:
                tline = line
        if j == 1:
            continue
        reason = None
        if close <= stop:
            reason = "stop_loss"
        elif launched and tline is not None and close <= tline:
            reason = "trailing_stop"
        elif j >= MAX_HOLD:
            reason = "max_hold_days"
        if reason:
            return {"status": "closed", "ret": (close - entry_px) / entry_px * 100,
                    "hold_tdays": j - 1, "reason": reason}
    return {"status": "holding", "ret": None}


# ─────────────────────────────────────────────
# 3. 排序键定义
# ─────────────────────────────────────────────
def _g(d, k, default):
    v = d.get(k)
    return default if v is None else v


def key_ext_asc(f):
    """现状基线：贴 MA20 越近越优先（只用一个维度）。"""
    return (-_g(f, "ext", 999), f["code"])


def key_score_desc(f):
    """规则分降序。"""
    return (-_g(f, "score", -99), f["code"])


def key_score_frac20(f):
    """规则分降序 + 区间位置低者优先（低吸）。"""
    return (-_g(f, "score", -99), _g(f, "frac20", 1.0), f["code"])


def key_score_ext(f):
    """规则分降序 + 贴 MA20 者优先。"""
    return (-_g(f, "score", -99), _g(f, "ext", 999), f["code"])


def key_score_risk(f):
    """规则分降序 + 止损窄者优先（小风险敞口）。"""
    return (-_g(f, "score", -99), _g(f, "risk_pct", 99), f["code"])


def key_score_frac20_ext(f):
    """规则分降序 + 区间位置低 + 贴 MA20。"""
    return (-_g(f, "score", -99),
            _g(f, "frac20", 1.0) + max(0.0, _g(f, "ext", 999)) / 20.0,
            f["code"])


def _rhythm_bonus(f):
    """上升初期/中段加分，末期与回调减分（与 _rhythm_adjust 同向）。"""
    h = f.get("rhythm_hint") or ""
    if "初期" in h:
        return 0 if "回调" in h else -2
    if "中段" in h:
        return 0 if "回调" in h else -1
    if "末期" in h:
        return 1
    if "末段" in h:
        return 0
    return 0


def key_score_rhythm(f):
    """规则分降序 + 节奏阶段（上升初期优先）。"""
    return (-_g(f, "score", -99), _rhythm_bonus(f), _g(f, "frac20", 1.0), f["code"])


def key_combo(f):
    """复合分：规则分为主 + 区间位置 + 节奏 + 波动率惩罚（越大越优先）。"""
    s = _g(f, "score", 0)
    frac = _g(f, "frac20", 0.5)
    rb = {"初期": 0.0, "中段": -0.5, "末期": -1.5, "末段": -0.5}.get(
        (f.get("rhythm_hint") or "")[-2:], 0.0)
    if "回调" in (f.get("rhythm_hint") or ""):
        rb = -1.0 if "初期" in (f.get("rhythm_hint") or "") else -0.5
    atr = _g(f, "atr_pct", 3.0)
    return -(s * 1.0 - frac * 1.5 + rb - max(0.0, atr - 4.0) * 0.15)


def key_frac20(f):
    """纯区间位置低吸：frac20 越小越优先（分层归因里唯一单调的特征）。"""
    return (_g(f, "frac20", 1.0), f["code"])


def key_frac20_ext(f):
    """区间位置为主 + 贴 MA20 次之。"""
    return (_g(f, "frac20", 1.0), _g(f, "ext", 999), f["code"])


def key_score6_frac20(f):
    """规则分封顶 6（score=7 仅 20 只且表现最差，避免极端票占坑）+ 位置低吸。"""
    return (-min(_g(f, "score", 0), 6), _g(f, "frac20", 1.0), f["code"])


def key_score6_ext(f):
    """规则分封顶 6 + 贴 MA20。"""
    return (-min(_g(f, "score", 0), 6), _g(f, "ext", 999), f["code"])


def key_hi_frac20(f):
    """高分档优先（score>=6 一档，其余一档）+ 档内位置低吸。"""
    return (0 if _g(f, "score", 0) >= 6 else 1, _g(f, "frac20", 1.0), f["code"])


def key_hi_ext(f):
    """高分档优先 + 档内贴 MA20。"""
    return (0 if _g(f, "score", 0) >= 6 else 1, _g(f, "ext", 999), f["code"])


def key_frac20_score6(f):
    """位置为主 + 规则分封顶 6 次之。"""
    return (_g(f, "frac20", 1.0), -min(_g(f, "score", 0), 6), f["code"])


def key_pos_guard(f):
    """位置低吸 + 排除区间顶部（frac>=0.66 分层为负，硬否决）。"""
    return (0 if _g(f, "frac20", 1.0) < 0.66 else 1,
            _g(f, "frac20", 1.0), _g(f, "ext", 999), f["code"])


# ─────────────────────────────────────────────
# 3'. top4 精排候选：多维正向指标打分器（Q 系列）
# ─────────────────────────────────────────────
# 设计依据 = 全样本分层归因（诊断 6/7，buy 池 4432 只，62 扫描日，topn=4）：
#   ext60<0         : +1.33% / 胜率 61.0% / PF 1.48   （价处 MA60 下方 = 长周期低位）
#   ext60>=15%      : -1.14% / 胜率 53.7% / PF 0.82
#   regime 震荡偏多  : +1.11% / 胜率 60.1% / PF 1.38
#   regime 多头上升  : -0.61% / 胜率 52.6% / PF 0.86   ← 追涨是负贡献
#   pattern 下行阴跌 : +1.56% / 胜率 62.6% / PF 1.83
#   pattern 慢牛上行 : -0.06% / PF 0.99
#   volume_ratio>=2 : -1.36% / 胜率 50.6% / PF 0.71   ← 爆量追高，强否决
#   volume_ratio .8~1.2 : +0.77% / 胜率 59.4% / PF 1.23
#   obv_grad -2~2%  : +1.11% / 胜率 58.8% / PF 1.36   ← OBV 走平优于上行(+0.06%/PF1.01)
#   frac20 越低越好  : <0.33 最强；>=0.66 为 -0.50% / PF 0.88
# 统一机制（与既有「buy 选出的已是启动票，后劲不足」结论同向）：
#   **买「还没启动、位置低、无人问津」的，不买「已启动、量价齐升、趋势漂亮」的。**
def _q_raw(f, w_frac=1.2):
    """综合质量分（越大越优先）。分量权重取自上述归因均值，非拍脑袋。

    w_frac 可调是为了做**权重扫描**：frac20 是最强的连续信号，但权重过大就退化成
    纯 frac20 单键（I 键实测仅 +0.84%），必然存在最优点。扫描 + 诊断联合判断，
    避免一路加到拟合噪音上。
    """
    s = 0.0
    frac = f.get("frac20")
    if frac is not None:
        s += w_frac * (1.0 - min(1.0, max(0.0, frac)))   # 连续低吸，主力权重
    ext60 = f.get("ext60")
    if ext60 is not None and ext60 < 0:
        s += 1.0
    if f.get("regime") == "震荡偏多":
        s += 0.8
    if f.get("pattern") == "下行阴跌":
        s += 0.6
    obv = f.get("obv_grad_pct")
    if obv is not None and -2.0 <= obv <= 2.0:
        s += 0.5
    vr = f.get("volume_ratio")
    if vr is not None and vr >= 2.0:
        s -= 1.5
    atr = f.get("atr_pct")
    if atr is not None and atr > 4.0:
        s -= 0.4 * (atr - 4.0)
    return s


def key_q1_lowpos(f):
    """Q1 位置优先：长周期低位(ext60<0) → 20 日区间低分位 → 贴 MA20。"""
    e60 = f.get("ext60")
    return (0 if (e60 is not None and e60 < 0) else 1,
            _g(f, "frac20", 1.0), _g(f, "ext", 999), f["code"])


def key_q2_combo(f):
    """Q2 综合质量分（全指标加权，见 _q_raw）。"""
    return (-_q_raw(f), f["code"])


def key_q3_contrarian(f):
    """Q3 逆向精选：MA60 下方 + 区间低分位 + 量比贴近 1（既不爆量也不死水）。"""
    e60 = f.get("ext60")
    vr = f.get("volume_ratio")
    return (0 if (e60 is not None and e60 < 0) else 1,
            _g(f, "frac20", 1.0),
            abs((vr if vr is not None else 1.0) - 1.0),   # 越接近 1 越靠前
            f["code"])


def key_q4_guard(f):
    """Q4 硬否决 + 排序：先剔除爆量(权重2)/区间顶部(权重1)，档内再按质量分排。"""
    vr = f.get("volume_ratio")
    ban = 0
    if vr is not None and vr >= 2.0:
        ban += 2
    if _g(f, "frac20", 0.5) >= 0.66:
        ban += 1
    return (ban, -_q_raw(f), f["code"])


def key_q5_pure(f):
    """Q5 纯连续质量分：只留连续型分量（位置 + 波动惩罚），不依赖类别字段。

    用于检验「类别加分是否只是过拟合」—— 若 Q5 与 Q2 接近，说明机制稳健。
    """
    frac = _g(f, "frac20", 0.5)
    e60 = _g(f, "ext60", 999)
    atr = _g(f, "atr_pct", 3.0)
    s = (1.2 * (1.0 - min(1.0, max(0.0, frac)))
         + 1.0 * (1.0 if e60 < 0 else 0.0)
         - 0.4 * max(0.0, atr - 4.0))
    return (-s, f["code"])


def key_q6_score_tier(f):
    """Q6 = Q2 + 高分档优先。

    Q2 完全没用 score 字段，而 score 是已验证维度（诊断 1/5）：
      score=5 -0.05%/PF0.99（占 89.3%）｜score=6 +0.62%/PF1.16｜score=7 -1.18%（仅 21 只）
    → 必须**封顶成两档**（>=6 vs 其余），不能 score DESC，否则 21 只极端票霸榜。
    """
    return (0 if _g(f, "score", 0) >= 6 else 1, -_q_raw(f), f["code"])


def key_q7_strong_low(f):
    """Q7 = Q2 强低吸：frac20 权重 1.2 → 2.2。

    frac20 是归因里最强且最单调的连续信号（<0.33 最强；>=0.66 为 -0.50%/PF0.88），
    加大权重检验"低吸"还能不能再榨出收益。实测：+1.52% → +1.72%，PF 1.79 → 1.91。
    """
    return (-_q_raw(f, 2.2), f["code"])


def key_q10(f):
    """Q10 权重扫描：frac20 权重 → 3.2。"""
    return (-_q_raw(f, 3.2), f["code"])


def key_q11(f):
    """Q11 权重扫描：frac20 权重 → 4.5。

    这是**过拟合探针**：权重过大就退化成纯 frac20 单键（I 键实测仅 +0.84%），
    所以 Q11 应当比 Q7 差。若 Q11 反而更好，说明是拟合噪音而非真实机制。
    """
    return (-_q_raw(f, 4.5), f["code"])


def key_q12(f):
    """Q12 分档版低吸：frac20<0.33 单独成档（归因最强组），档内再按质量分排。"""
    frac = f.get("frac20")
    tier = 0 if (frac is not None and frac < 0.33) else 1
    return (tier, -_q_raw(f, 2.2), f["code"])


def key_q8_ext60_ban(f):
    """Q8 = Q2 + ext60 高位硬否决。

    归因：ext60<0 → +1.33%/61.0%/PF1.48；0~5% → -0.49%；5~15% → -0.71%；>=15% → -1.14%。
    → 不只在 ext60<0 时加分，还把 ext60>=5% 的直接沉底。
    """
    e60 = f.get("ext60")
    ban = 1 if (e60 is not None and e60 >= 5.0) else 0
    return (ban, -_q_raw(f), f["code"])


def key_q9_full(f):
    """Q9 = Q6 + Q8 合并：ext60 高位否决 → 高分档 → 质量分。"""
    e60 = f.get("ext60")
    ban = 1 if (e60 is not None and e60 >= 5.0) else 0
    return (ban, 0 if _g(f, "score", 0) >= 6 else 1, -_q_raw(f), f["code"])


KEYS: list[tuple[str, Callable]] = [
    ("A 现状: ext ASC", key_ext_asc),
    ("B score DESC", key_score_desc),
    ("C score DESC + frac20 ASC", key_score_frac20),
    ("D score DESC + ext ASC", key_score_ext),
    ("E score DESC + risk ASC", key_score_risk),
    ("F score + frac20 + ext", key_score_frac20_ext),
    ("G score DESC + 节奏", key_score_rhythm),
    ("H 复合 combo", key_combo),
    ("I frac20 ASC", key_frac20),
    ("J frac20 ASC + ext", key_frac20_ext),
    ("K score6↓ + frac20 ASC", key_score6_frac20),
    ("L score6↓ + ext ASC", key_score6_ext),
    ("M 高分档 + frac20 ASC", key_hi_frac20),
    ("N 高分档 + ext ASC", key_hi_ext),
    ("O frac20 ASC + score6↓", key_frac20_score6),
    ("P 顶部否决 + 低吸", key_pos_guard),
    # ── top4 精排候选：多维正向指标（量价/趋势/节奏），见 _q_raw 的归因依据 ──
    ("Q1 位置优先 ext60+frac20", key_q1_lowpos),
    ("Q2 综合质量分", key_q2_combo),
    ("Q3 逆向精选 缩量低吸", key_q3_contrarian),
    ("Q4 硬否决+质量分", key_q4_guard),
    ("Q5 纯连续质量分", key_q5_pure),
    # ── 第二轮：围绕 Q2 做增量（补上 Q2 漏掉的 score，以及更严的低位否决）──
    ("Q6 Q2+高分档", key_q6_score_tier),
    ("Q7 Q2+强低吸", key_q7_strong_low),
    ("Q8 Q2+ext60否决", key_q8_ext60_ban),
    ("Q9 高分档+ext60否决+Q2", key_q9_full),
    # ── 第三轮：frac20 权重扫描（1.2 / 2.2 / 3.2 / 4.5）+ 分档版 ──
    ("Q10 低吸w3.2", key_q10),
    ("Q11 低吸w4.5(过拟合探针)", key_q11),
    ("Q12 低吸分档+质量分", key_q12),
]


# ─────────────────────────────────────────────
# 4. 主流程
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只取最近 N 个扫描日（0=全部）")
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--topn", type=int, default=10)
    ap.add_argument("--out", default="", help="结果 JSON 输出路径")
    ap.add_argument("--no-diag", action="store_true", help="跳过过拟合诊断输出")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM stock_deep_signal ORDER BY scan_date")]
    if args.days:
        dates = dates[-args.days:]
    # ⚠ 这里**不能**按"扫描日后需有 MAX_HOLD+ENTRY_WINDOW 个交易日"整批过滤日期：
    # 那样会把最近十几个扫描日里**已经因止损/移动止盈提前出场**的单一起丢掉，
    # 只剩"走完全程"的样本 → 与 deep_track 的 status='closed' 口径不一致，均值会系统性偏低
    # （首次实现就踩了这个坑：同选股下算出 -0.35%，而真实 deep_track 是 +1.37%）。
    # 正确做法：所有扫描日都模拟，只把 status=='closed' 的计入统计 —— 未走完的单本来就会
    # 返回 holding/pending 被自动排除，且这个排除对两个排序键是**对称**的，不影响对比公平性。
    last_mkt = conn.execute("SELECT MAX(trade_date) d FROM daily_price").fetchone()["d"]
    usable = []
    for d in dates:
        n = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) c FROM daily_price WHERE trade_date > ?",
            (d,)).fetchone()["c"]
        if n >= 2:      # 至少能推进：一天入场 + 一天持仓判定，否则纯属浪费计算
            usable.append(d)
    print(f"扫描日 {len(dates)} 个，纳入 {len(usable)} 个（后续交易日 >=2）；"
          f"统计口径 = 已出场(closed)，与 deep_track 一致；全市场最新交易日 {last_mkt}")

    # buy 池名单
    dates_codes = {}
    for d in usable:
        cs = [r[0] for r in conn.execute(
            "SELECT code FROM stock_deep_signal WHERE scan_date=? AND level='buy'", (d,))]
        if cs:
            dates_codes[d] = cs
    total = sum(len(v) for v in dates_codes.values())
    print(f"buy 池：{len(dates_codes)} 天 / {total} 只，开始重算特征（{args.procs} 进程）...")

    feats = build_features(dates_codes, args.procs)
    got = sum(len(v) for v in feats.values())
    print(f"特征完成：{got}/{total} 只通过 buy 复判")

    # 模拟缓存：同一 (date, code) 只算一次，排序键之间共享
    cache = {}
    def sim(d, f):
        k = (d, f["code"])
        if k not in cache:
            cache[k] = simulate(conn, f)
        return cache[k]

    # 每天每键的收益集合：{key: {date: [ret,...]}}
    per_day = {name: {} for name, _ in KEYS}
    stat_rows = []
    for name, kf in KEYS:
        rets, holds, reasons = [], [], []
        picked = filled = 0
        for d in sorted(feats):
            pool = list(feats[d].values())
            if not pool:
                continue
            pool.sort(key=kf)
            day_rets = []
            for f in pool[:args.topn]:
                picked += 1
                r = sim(d, f)
                if r["status"] == "closed":
                    filled += 1
                    rets.append(r["ret"])
                    day_rets.append(r["ret"])
                    holds.append(r.get("hold_tdays") or 0)
                    reasons.append(r.get("reason"))
            per_day[name][d] = day_rets
        if not rets:
            continue
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x <= 0]
        pf = (sum(wins) or 0.0) / (abs(sum(losses)) or 1e-9)
        stat_rows.append({
            "key": name, "n": len(rets),
            "fill_rate": filled / picked * 100 if picked else 0,
            "mean": st.mean(rets), "median": st.median(rets),
            "win": len(wins) / len(rets) * 100, "pf": pf, "avg_hold": st.mean(holds),
            "sl": reasons.count("stop_loss") / len(reasons) * 100,
            "exp": reasons.count("max_hold_days") / len(reasons) * 100,
        })

    print("\n" + "=" * 92)
    print(f"{'排序键':<28}{'笔数':>6}{'成交率%':>9}{'均值%':>9}{'中位%':>9}"
          f"{'胜率%':>8}{'PF':>7}{'均持':>6}{'止损%':>7}{'到期%':>7}")
    print("=" * 92)
    for s in stat_rows:
        print(f"{s['key']:<28}{s['n']:>6}{s['fill_rate']:>9.1f}{s['mean']:>9.2f}"
              f"{s['median']:>9.2f}{s['win']:>8.1f}{s['pf']:>7.2f}"
              f"{s['avg_hold']:>6.1f}{s['sl']:>7.1f}{s['exp']:>7.1f}")
    print("=" * 92)

    # ── 诊断：区分度 / 稳定性 / topn 敏感性 / 逐日配对 ──
    if not args.no_diag:
        _diag(feats, per_day, sim, args)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"params": {"entry_window": ENTRY_WINDOW, "max_hold": MAX_HOLD,
                                  "tp_pct": TP_PCT, "trail_pct": TRAIL_PCT,
                                  "topn": args.topn, "days": len(feats)},
                       "results": [{k: (round(v, 3) if isinstance(v, float) else v)
                                    for k, v in s.items()} for s in stat_rows]},
                      fh, ensure_ascii=False, indent=2)
        print(f"\n已写入 {args.out}")


def _band_diag(feats, sim, field: str, bands: list, names: list):
    """按某特征分桶，全样本跑模拟，看收益是否随该特征单调变化。"""
    buckets = {n: [] for n in names}
    for d in feats:
        for f in feats[d].values():
            v = f.get(field)
            if v is None:
                continue
            for (lo, hi), n in zip(bands, names):
                if (lo is None or v >= lo) and v < hi:
                    r = sim(d, f)
                    if r["status"] == "closed":
                        buckets[n].append(r["ret"])
                    break
    print(f"    · {field}")
    for n in names:
        v = buckets[n]
        if len(v) < 10:
            print(f"        {n:<16} 样本不足（{len(v)}）")
            continue
        wins = [x for x in v if x > 0]
        pf = (sum(wins) or 0.0) / (abs(sum(x for x in v if x <= 0)) or 1e-9)
        print(f"        {n:<16} n={len(v):>5}  均值={st.mean(v):>7.2f}%  "
              f"中位={st.median(v):>7.2f}%  胜率={len(wins) / len(v) * 100:>5.1f}%  PF={pf:>5.2f}")


def _cat_diag(feats, sim, field: str, min_n: int = 10):
    """按**类别型**特征分组，buy 池全样本跑模拟，看各组收益差异。

    与 _band_diag（数值分桶）互补：量价与趋势的产出大多是类别（底背离/上行/金叉），
    不能用数值分桶。这一步用于筛出「哪些指标真的有正向区分度」，避免凭直觉定权重。
    """
    buckets: dict = {}
    for d in feats:
        for f in feats[d].values():
            v = f.get(field)
            if v is None or v == "":
                continue
            r = sim(d, f)
            if r["status"] == "closed":
                buckets.setdefault(str(v), []).append(r["ret"])
    rows = []
    for k, v in buckets.items():
        if len(v) < min_n:
            continue
        wins = [x for x in v if x > 0]
        pf = (sum(wins) or 0.0) / (abs(sum(x for x in v if x <= 0)) or 1e-9)
        rows.append((k, len(v), st.mean(v), st.median(v),
                     len(wins) / len(v) * 100, pf))
    if not rows:
        print(f"    · {field}: 样本不足")
        return
    rows.sort(key=lambda x: -x[2])
    print(f"    · {field}")
    for k, n, mean, med, win, pf in rows:
        print(f"        {k:<20} n={n:>5}  均值={mean:>7.2f}%  "
              f"中位={med:>7.2f}%  胜率={win:>5.1f}%  PF={pf:>5.2f}")


def _diag(feats, per_day, sim, args):
    """过拟合排查：特征区分度 + 逐日配对 + 时间稳定性 + topn 敏感性。"""
    # 1) buy 池内 score 分布（score 没有区分度的话，排序键 B~H 都无从谈起）
    from collections import Counter
    cnt = Counter()
    for d in feats:
        for f in feats[d].values():
            cnt[f.get("score")] += 1
    tot = sum(cnt.values()) or 1
    print("\n【诊断 1】buy 池规则分分布（决定 score 做主键有没有意义）")
    for s in sorted(k for k in cnt if k is not None):
        print(f"    score={s:<3} {cnt[s]:>5} 只  {cnt[s] / tot * 100:>5.1f}%")
    # 同分层内 ext 的离散度（决定 ext 做 tie-break 有没有意义）
    for s in sorted(k for k in cnt if k is not None):
        exts = [f["ext"] for d in feats for f in feats[d].values()
                if f.get("score") == s and f.get("ext") is not None]
        if len(exts) >= 20:
            print(f"    score={s} 层内 ext: n={len(exts)} "
                  f"min={min(exts):.1f} p25={st.quantiles(exts, n=4)[0]:.1f} "
                  f"med={st.median(exts):.1f} max={max(exts):.1f}")

    # 2) 逐日配对：候选键 vs 基线，看是不是靠少数几天撑起来的
    #    ⚠ 基线取 **N 高分档 + ext ASC**（当前真正落地在 deep_tracker 的排序键），
    #    而不是 A（历史基线，早已不用）。这样才能直接读出「新方案相对现状」的改进。
    base = "N 高分档 + ext ASC"
    print("\n【诊断 2】逐日配对（候选键日均值 − 基线日均值，基线 = N 当前落地键）")
    print(f"    {'排序键':<28}{'胜出天数':>9}{'日均差%':>10}{'t值':>8}")
    for name, _ in KEYS:
        if name == base:
            continue
        diffs = []
        for d in sorted(feats):
            a = per_day[base].get(d) or []
            b = per_day[name].get(d) or []
            if a and b:
                diffs.append(st.mean(b) - st.mean(a))
        if len(diffs) < 3:
            continue
        wd = sum(1 for x in diffs if x > 0)
        m = st.mean(diffs)
        sd = st.stdev(diffs) if len(diffs) > 1 else 0
        t = m / (sd / len(diffs) ** 0.5) if sd > 0 else float("inf")
        print(f"    {name:<28}{wd:>6}/{len(diffs):<3}{m:>10.2f}{t:>8.2f}")

    # 3) 时间稳定性：前后半段分别算
    ds = sorted(feats)
    half = len(ds) // 2
    print("\n【诊断 3】时间稳定性（前半段 / 后半段均值%）")
    print(f"    {'排序键':<28}{'前段':>10}{'后段':>10}")
    for name, _ in KEYS:
        segs = []
        for seg in (ds[:half], ds[half:]):
            r = [x for d in seg for x in (per_day[name].get(d) or [])]
            segs.append(st.mean(r) if r else float("nan"))
        print(f"    {name:<28}{segs[0]:>10.2f}{segs[1]:>10.2f}")

    # 5) 特征分层归因：全样本（不取 top10）看各特征值区间的收益差异。
    #    这一步决定"排序键的机制是否真实存在" —— 若 score 高低对收益毫无影响，
    #    那么 D 键相对 A 键的优势就只是样本噪音。
    print("\n【诊断 5】特征分层归因（buy 池全样本，不取 top10）")
    _band_diag(feats, sim, "score", [(None, 5.5), (5.5, 6.5), (6.5, 99)],
               ["score=5", "score=6", "score>=7"])
    _band_diag(feats, sim, "ext", [(None, 1.0), (1.0, 3.0), (3.0, 6.0), (6.0, 99)],
               ["ext<1", "ext 1~3", "ext 3~6", "ext>=6"])
    _band_diag(feats, sim, "frac20", [(None, 0.33), (0.33, 0.66), (0.66, 99)],
               ["frac<0.33", "frac .33~.66", "frac>=0.66"])
    _band_diag(feats, sim, "risk_pct", [(None, 4.0), (4.0, 7.0), (7.0, 99)],
               ["risk<4%", "risk 4~7%", "risk>=7%"])
    _band_diag(feats, sim, "atr_pct", [(None, 2.5), (2.5, 4.0), (4.0, 99)],
               ["atr<2.5%", "atr 2.5~4%", "atr>=4%"])

    # 6) 量价 / 趋势 / 节奏的**类别**归因。
    #    这些特征原脚本根本没提取（见 _feat_one），是 top4 精排的正向指标候选池。
    #    全部走 sim 缓存，同一 (date, code) 只模拟一次，额外开销仅是分组统计。
    print("\n【诊断 6】量价·趋势·节奏 类别归因（buy 池全样本，筛正向指标）")
    for fld in ("divergence", "obv_trend", "health", "macd", "regime",
                "rsi_zone", "pattern", "current_leg", "volatility"):
        _cat_diag(feats, sim, fld)

    print("\n【诊断 7】量价·位置·盈亏比 数值归因（buy 池全样本）")
    _band_diag(feats, sim, "updown_vol", [(None, 0.8), (0.8, 1.0), (1.0, 1.2), (1.2, 99)],
               ["涨跌量比<0.8", "0.8~1.0", "1.0~1.2", ">=1.2"])
    _band_diag(feats, sim, "volume_ratio", [(None, 0.8), (0.8, 1.2), (1.2, 2.0), (2.0, 99)],
               ["量比<0.8", "0.8~1.2", "1.2~2", ">=2"])
    _band_diag(feats, sim, "obv_grad_pct", [(None, -2.0), (-2.0, 2.0), (2.0, 10.0), (10.0, 99)],
               ["obv斜率<-2%", "-2~2%", "2~10%", ">=10%"])
    _band_diag(feats, sim, "ext60", [(None, 0), (0, 5.0), (5.0, 15.0), (15.0, 99)],
               ["ext60<0", "0~5%", "5~15%", ">=15%"])
    _band_diag(feats, sim, "rsi14", [(None, 40), (40, 55), (55, 70), (70, 99)],
               ["rsi<40", "40~55", "55~70", ">=70"])
    _band_diag(feats, sim, "rr", [(None, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 99)],
               ["盈亏比<1.5", "1.5~2", "2~2.5", ">=2.5"])

    # 4) topn 敏感性
    print("\n【诊断 4】topn 敏感性（均值%）")
    ns = [5, 8, 10, 15, 20]
    print(f"    {'排序键':<28}" + "".join(f"{n:>9}" for n in ns))
    for name, kf in KEYS:
        cells = []
        for n in ns:
            r = []
            for d in ds:
                pool = sorted(feats[d].values(), key=kf)[:n]
                for f in pool:
                    x = sim(d, f)
                    if x["status"] == "closed":
                        r.append(x["ret"])
            cells.append(st.mean(r) if r else float("nan"))
        print(f"    {name:<28}" + "".join(f"{c:>9.2f}" for c in cells))


if __name__ == "__main__":
    main()
