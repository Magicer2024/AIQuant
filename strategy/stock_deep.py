"""
strategy/stock_deep.py —— 个股深度分析引擎（量价关系 + 涨跌节奏 + 买卖建议）

面向"单只个股"的深度分析，供首页「个股深度 · 买卖建议」面板使用。
切换个股后，基于该股自己的历史走势，输出：
  1. 趋势状态（均线排列 / MACD / 波动率）
  2. 量价关系（放量上涨 / 缩量回调 / 量价背离 / 主力资金倾向）
  3. 涨跌节奏（用 ATR 阈值做 zigzag 摆动点切分上/下腿，得出涨跌周期、当前所处阶段）
  4. 近期买卖建议（逐日规则信号时间线 + 当前可执行建议：入场/止损/止盈）

设计原则：只用该股自己的历史（不引入大盘横截面），识别"这只股票的节奏"。
所有判断为规则驱动、可解释，输出中文 reason/risk，不追求黑箱预测。
"""
import json
import os
import time
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from strategy.indicators import calc_all_indicators
from strategy.exit_advisor import evaluate_exit_by_prices, get_max_hold
from config.strategy_params import DEEP_EMA20_AUX as _EMA20_AUX_CFG
from config.strategy_params import DEEP_TRACK

# 环境变量支持 A/B 验证（tools/_eval_deep_buypoints.py）：DEEP_EMA20_AUX=1 强制开启、
# 0 强制关闭，否则用配置默认（enabled=False = 基线）。模块加载时读取一次即可。
_EMA20_AUX_ENV = os.environ.get("DEEP_EMA20_AUX")
if _EMA20_AUX_ENV in ("1", "true", "True", "yes", "on"):
    _EMA20_AUX = dict(_EMA20_AUX_CFG, enabled=True)
elif _EMA20_AUX_ENV in ("0", "false", "False", "no", "off"):
    _EMA20_AUX = dict(_EMA20_AUX_CFG, enabled=False)
else:
    _EMA20_AUX = _EMA20_AUX_CFG


# ─────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────
def load_history(conn, code: str, lookback: int = 260,
                 as_of: Optional[str] = None) -> Optional[pd.DataFrame]:
    """读取个股截至 as_of（默认最新）的最近 lookback 个交易日日线，按日期升序。

    返回 df（trade_date 为字符串索引，含 close/open/high/low/volume/amount/pct_change/…）。
    数据不足回退为整段历史（保证长周期均线可算）。

    ⚠ as_of 是历史回补的生命线：不传就永远取【最新】数据，这样拿去回补历史扫描日
      等于让每一天都"预知未来"（look-ahead bias），胜率会虚高到完全不可信。
      回补时必须传 as_of=该扫描日，使指标只由当时可见的行情算出。
    """
    sql = """
        SELECT trade_date, open, high, low, close, volume, amount, pct_change, turnover,
               fusion_score, vol_score, ma_score, diverge_score, bottom_score, whale_score
        FROM daily_price
        WHERE code = ? AND close IS NOT NULL
    """
    params: list = [code]
    if as_of:
        sql += " AND trade_date <= ?"
        params.append(as_of)
    sql += " ORDER BY trade_date ASC"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.drop_duplicates(subset="trade_date", keep="last").set_index("trade_date")
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_change",
                "turnover", "fusion_score"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_index()
    if len(df) > lookback:
        df = df.iloc[-lookback:]
    return df


def _calc_kdj_safe(high: pd.Series, low: pd.Series, close: pd.Series,
                   n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """NaN 安全的 KDJ（修复 indicators.calc_kdj 热启动期 NaN 污染递归的问题）。

    calc_kdj 的 rsv 在前 n-1 日为 NaN（rolling 未满窗），递归式因此被污染成 NaN。
    这里把热启动期 rsv 填充为中性 50，保证 KDJ 在暖机后稳定输出。
    """
    low_min = low.rolling(window=n).min()
    high_max = high.rolling(window=n).max()
    rsv = (close - low_min) / (high_max - low_min + 1e-10) * 100
    rsv = rsv.fillna(50.0)   # 暖机期视为中性
    k = pd.Series(index=close.index, dtype=float)
    d = pd.Series(index=close.index, dtype=float)
    k.iloc[0] = 50.0
    d.iloc[0] = 50.0
    for i in range(1, len(close)):
        k.iloc[i] = (m1 - 1) / m1 * k.iloc[i - 1] + 1 / m1 * rsv.iloc[i]
        d.iloc[i] = (m2 - 1) / m2 * d.iloc[i - 1] + 1 / m2 * k.iloc[i]
    j = 3 * k - 2 * d
    return pd.DataFrame({"KDJ_K": k, "KDJ_D": d, "KDJ_J": j}, index=close.index)


def _compute(df: pd.DataFrame) -> pd.DataFrame:
    """在原始 df 基础上叠加全套技术指标，返回新 df（保留原始列）。

    calc_all_indicators 要求 open/high/low/close/volume 列。
    """
    base = df[["open", "high", "low", "close", "volume"]].astype(float)
    ind = calc_all_indicators(base.copy())
    out = df.copy()
    for col in ind.columns:
        out[col] = ind[col].values
    # 覆盖 NaN 污染的 KDJ（indicators.calc_kdj 的热启动 bug）
    kdj = _calc_kdj_safe(base["high"], base["low"], base["close"])
    for col in kdj.columns:
        out[col] = kdj[col].values
    return out


# ─────────────────────────────────────────────
# 涨跌节奏：zigzag 摆动点切分上/下腿
# ─────────────────────────────────────────────
def _zigzag_pivots(close: np.ndarray, threshold: float):
    """基于 ATR 阈值的 zigzag 摆动点检测。

    返回 [(index, price, kind), ...]，kind ∈ {'low', 'high'}，按 index 升序且交替。
    threshold 为"确认一个摆动点所需的最小反向幅度"（绝对价格）。
    """
    n = len(close)
    if n < 2:
        return []
    pivots = []
    direction = 0          # 0 未定, 1 上升, -1 下降
    high_i, low_i = 0, 0
    for i in range(1, n):
        p = close[i]
        if direction == 0:
            # 尚未确认方向：跟踪极值，出现 >= threshold 的移动即确认首个摆动点
            if p >= close[high_i]:
                high_i = i
            if p <= close[low_i]:
                low_i = i
            if p - close[low_i] >= threshold:
                direction = 1
                pivots.append((low_i, float(close[low_i]), "low"))
                high_i = i
            elif close[high_i] - p >= threshold:
                direction = -1
                pivots.append((high_i, float(close[high_i]), "high"))
                low_i = i
        elif direction == 1:
            if p >= close[high_i]:
                high_i = i
            elif close[high_i] - p >= threshold:
                pivots.append((high_i, float(close[high_i]), "high"))
                low_i = i
                direction = -1
        else:  # direction == -1
            if p <= close[low_i]:
                low_i = i
            elif p - close[low_i] >= threshold:
                pivots.append((low_i, float(close[low_i]), "low"))
                high_i = i
                direction = 1
    return pivots


def detect_rhythm(df: pd.DataFrame) -> dict:
    """识别该股的涨跌节奏。

    用 zigzag 摆动点把走势切成交替的"上腿/下腿"，度量每条腿的长度与幅度，
    从中归纳出节奏模式、周期长度、以及当前所处周期阶段。
    """
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    if n < 20:
        return {"insufficient": True}

    atr = df["ATR"].to_numpy(dtype=float)
    atr_valid = atr[~np.isnan(atr)]
    median_atr = float(np.nanmedian(atr_valid)) if len(atr_valid) else float(np.nan)
    median_close = float(np.nanmedian(close))
    # 阈值：max(2.5×ATR, 3% 中位价)，保证切出的腿是"有意义"的波段
    thr = max(2.5 * median_atr, 0.03 * median_close)
    if not np.isfinite(thr) or thr <= 0:
        thr = 0.03 * median_close

    pivots = _zigzag_pivots(close, thr)
    if len(pivots) < 3:
        # 数据太少或波动过小，无法切分 → 视为"窄幅盘整"
        return {
            "pattern": "窄幅盘整", "desc": "波动过小，难以切分出明显的涨跌波段",
            "up_leg_avg_days": None, "down_leg_avg_days": None,
            "cycle_avg_days": None, "up_amp_avg": None, "down_amp_avg": None,
            "current_leg": "盘整", "position_hint": "无趋势波段",
            "swing_high": float(np.nanmax(close)) if n else None,
            "swing_low": float(np.nanmin(close)) if n else None,
            "recent_legs": [],
        }

    # 由相邻摆动点构成"腿"：每段 legs 有 start_idx/end_idx/kind/len_days/amp
    legs = []
    for i in range(len(pivots) - 1):
        a = pivots[i]
        b = pivots[i + 1]
        kind = "up" if a[2] == "low" else "down"
        amp = (b[1] - a[1]) / a[1] if a[1] else 0.0
        legs.append({
            "start_idx": a[0], "end_idx": b[0],
            "start_price": round(a[1], 2), "end_price": round(b[1], 2),
            "kind": kind, "len_days": b[0] - a[0],
            "amp": round(amp * 100, 2),
        })
    legs = legs[-(30):]   # 只看最近 ~30 条腿，避免远古数据主导

    up_legs = [l for l in legs if l["kind"] == "up"]
    down_legs = [l for l in legs if l["kind"] == "down"]
    avg = lambda xs: float(np.mean(xs)) if xs else None
    up_len = avg([l["len_days"] for l in up_legs])
    down_len = avg([l["len_days"] for l in down_legs])
    up_amp = avg([abs(l["amp"]) for l in up_legs])
    down_amp = avg([abs(l["amp"]) for l in down_legs])

    cycle = (up_len + down_len) if (up_len and down_len) else (up_len or down_len)

    # 当前腿：最后一条腿；若最后一个摆动点是 high 且之后价格仍上行，视为上升进行中
    last_pivot = pivots[-1]
    cur_idx = last_pivot[0]
    cur_kind = "up" if last_pivot[2] == "low" else "down"
    cur_age = int(n - 1 - cur_idx)
    # 当前腿在典型周期中的位置
    position_pct = (cur_age / cycle * 100) if cycle and cycle > 0 else None
    if cur_kind == "up":
        position_hint = (
            "上升初期" if (position_pct is not None and position_pct < 40) else
            "上升中段" if (position_pct is not None and position_pct < 80) else "上升末期"
        )
    else:
        position_hint = (
            "回调初期" if (position_pct is not None and position_pct < 40) else
            "回调中段" if (position_pct is not None and position_pct < 80) else "回调末段"
        )

    # 节奏模式归类
    if up_amp and down_amp:
        if up_amp > down_amp * 1.5 and up_len and up_len >= down_len:
            pattern = "慢牛上行"        # 涨的幅度大、时长长，跌的浅
        elif down_amp > up_amp * 1.5:
            pattern = "下行阴跌"        # 跌的比涨的多
        elif up_len and up_len <= 4:
            pattern = "快节奏波段"      # 涨得快、腿短，题材/游资风
        else:
            pattern = "波段震荡"        # 涨跌交替、幅度接近
    else:
        pattern = "盘整蓄势"

    desc = _rhythm_desc(pattern, up_len, down_len, up_amp, down_amp, cur_kind, position_hint)

    return {
        "pattern": pattern,
        "desc": desc,
        "up_leg_avg_days": round(up_len, 1) if up_len else None,
        "down_leg_avg_days": round(down_len, 1) if down_len else None,
        "cycle_avg_days": round(cycle, 1) if cycle else None,
        "up_amp_avg": round(up_amp, 1) if up_amp else None,
        "down_amp_avg": round(down_amp, 1) if down_amp else None,
        "current_leg": "上升" if cur_kind == "up" else "回调",
        "current_leg_age": cur_age,
        "position_pct": round(position_pct, 0) if position_pct is not None else None,
        "position_hint": position_hint,
        "swing_high": float(np.nanmax(close)),
        "swing_low": float(np.nanmin(close)),
        "_pivots": [[int(p[0]), round(float(p[1]), 2), p[2]] for p in pivots],  # 供逐日节奏修正
        "recent_legs": [
            {"kind": l["kind"], "start": df.index[l["start_idx"]],
             "end": df.index[l["end_idx"]], "days": l["len_days"],
             "amp": l["amp"], "start_price": l["start_price"], "end_price": l["end_price"]}
            for l in legs[-(6):]
        ],
    }


def _rhythm_desc(pattern, up_len, down_len, up_amp, down_amp, cur_kind, position_hint) -> str:
    parts = []
    if up_len and down_len:
        parts.append(f"平均上涨波段约 {up_len:.1f} 天、回调约 {down_len:.1f} 天，一个涨跌周期约 "
                     f"{up_len + down_len:.1f} 天")
    if up_amp and down_amp:
        parts.append(f"上涨波平均 +{up_amp:.1f}%、回调波平均 -{down_amp:.1f}%")
    if cur_kind and position_hint:
        parts.append(f"当前处于{'上升' if cur_kind == 'up' else '回调'}波段、{position_hint}")
    if pattern == "慢牛上行":
        parts.append("涨多跌少、重心上移，属于偏积极的节奏")
    elif pattern == "下行阴跌":
        parts.append("跌多涨少、重心下移，偏防守节奏")
    elif pattern == "快节奏波段":
        parts.append("涨跌切换快、弹性大，适合短线但需盯盘")
    elif pattern == "波段震荡":
        parts.append("涨跌交替、幅度接近，宜波段低吸高抛")
    else:
        parts.append("以盘整蓄势为主，等待方向选择")
    return "；".join(parts)


# ─────────────────────────────────────────────
# 量价关系
# ─────────────────────────────────────────────
def analyze_volume_price(df: pd.DataFrame) -> dict:
    """量价关系：放量上涨/缩量回调健康度、量比、OBV 趋势与背离、主力倾向。"""
    if len(df) < 20:
        return {"insufficient": True}
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    pct = df["pct_change"].astype(float)
    recent = df.iloc[-60:]

    r_pct = recent["pct_change"].astype(float)
    r_vol = recent["volume"].astype(float)

    up_mask = r_pct > 0
    down_mask = r_pct < 0
    up_days = int(up_mask.sum())
    down_days = int(down_mask.sum())
    up_vol = float(r_vol[up_mask].mean()) if up_days else None
    down_vol = float(r_vol[down_mask].mean()) if down_days else None
    updown_ratio = (up_vol / down_vol) if (up_vol and down_vol and down_vol > 0) else None

    latest_vr = float(df.iloc[-1]["VOLUME_RATIO"]) if "VOLUME_RATIO" in df.columns and not np.isnan(df.iloc[-1]["VOLUME_RATIO"]) else None

    # OBV 趋势：近 20 日 OBV 斜率（基准用 OBV 绝对量级，容差 2%）
    obv = df["OBV"].astype(float)
    obv20 = obv.iloc[-20:].to_numpy(dtype=float)
    obv_grad = float((obv20[-1] - obv20[0]) / abs(obv20[0] or 1) * 100) if len(obv20) >= 2 else 0.0
    obv_trend = "上行" if obv_grad > 2 else ("下行" if obv_grad < -2 else "走平")

    # 量价背离：近 20 日价格创新高但 OBV 未同步 → 顶背离；价格创新低但 OBV 未同步新低 → 底背离
    r20 = df.iloc[-20:]
    p20 = r20["close"].to_numpy(dtype=float)
    o20 = r20["OBV"].to_numpy(dtype=float)
    divergence = "无"
    if len(p20) >= 10 and p20.max() > p20.min():
        p_now = p20[-1]
        p_max, p_min = float(p20.max()), float(p20.min())
        o_max, o_min = float(o20.max()), float(o20.min())
        # 价格处于20日高位区（>=95% 区间），但 OBV 处于低位区（<=60% 区间）
        if p_now >= p_min + 0.95 * (p_max - p_min) and o20[-1] <= o_min + 0.60 * (o_max - o_min):
            divergence = "顶背离"
        # 价格处于20日低位区（<=5% 区间），但 OBV 处于高位区（>=40% 区间）
        elif p_now <= p_min + 0.05 * (p_max - p_min) and o20[-1] >= o_min + 0.40 * (o_max - o_min):
            divergence = "底背离"

    # 判断健康度
    if updown_ratio is not None and updown_ratio > 1.2 and up_days >= down_days:
        health = "放量上攻为主"          # 涨的时候放量 → 买盘积极
    elif updown_ratio is not None and updown_ratio < 0.8:
        health = "缩量上涨为主"          # 涨的时候缩量 → 上涨动能不足，防顶背离
    else:
        health = "量能多空均衡"

    note = "；".join([
        s for s in [
            f"近 60 日上涨日平均量能 / 下跌日平均量能 ≈ {updown_ratio:.2f}" if updown_ratio else None,
            f"最新量比 {latest_vr:.2f}" if latest_vr else None,
            f"OBV 近 20 日{'上行' if obv_trend == '上行' else ('下行' if obv_trend == '下行' else '走平')}",
            f"量价{'呈现' if divergence else '未见'}{divergence}",
        ] if s
    ])

    return {
        "volume_ratio": round(latest_vr, 2) if latest_vr else None,
        "updown_volume_ratio": round(updown_ratio, 2) if updown_ratio else None,
        "up_vol": round(up_vol, 0) if up_vol else None,
        "down_vol": round(down_vol, 0) if down_vol else None,
        "obv_trend": obv_trend,
        "obv_grad_pct": round(obv_grad, 2),
        "divergence": divergence,
        "health": health,
        "note": note,
    }


# ─────────────────────────────────────────────
# 趋势状态
# ─────────────────────────────────────────────
def trend_state(df: pd.DataFrame) -> dict:
    row = df.iloc[-1]
    close = float(row["close"])
    ma5 = float(row["MA5"]) if not np.isnan(row["MA5"]) else np.nan
    ma10 = float(row["MA10"]) if not np.isnan(row["MA10"]) else np.nan
    ma20 = float(row["MA20"]) if not np.isnan(row["MA20"]) else np.nan
    ma60 = float(row["MA60"]) if not np.isnan(row["MA60"]) else np.nan
    dif, dea, hist = (float(row["MACD_DIF"]), float(row["MACD_DEA"]), float(row["MACD_HIST"]))
    rsi = float(row["RSI14"]) if not np.isnan(row["RSI14"]) else np.nan
    k, d, j = (float(row["KDJ_K"]), float(row["KDJ_D"]), float(row["KDJ_J"]))
    atr = float(row["ATR"]) if not np.isnan(row["ATR"]) else np.nan

    # 均线排列
    if not np.isnan(ma20) and not np.isnan(ma60) and close > ma20 > ma60:
        alignment = "多头排列(价>MA20>MA60)"
        regime = "多头上升"
    elif not np.isnan(ma20) and not np.isnan(ma60) and close < ma20 < ma60:
        alignment = "空头排列(价<MA20<MA60)"
        regime = "空头下行"
    elif not np.isnan(ma20) and close > ma20:
        alignment = "站上MA20"
        regime = "震荡偏多"
    else:
        alignment = "跌破MA20"
        regime = "震荡偏空"

    # MACD
    if dif > dea and hist > 0:
        macd = "金叉多头(红柱)"
    elif dif < dea and hist < 0:
        macd = "死叉空头(绿柱)"
    elif dif > dea:
        macd = "DIF上行"
    else:
        macd = "DIF下行"

    # 波动率（ATR%）
    atr_pct = (atr / close * 100) if atr and np.isfinite(atr) and close else None
    vol_label = ("高波动" if atr_pct and atr_pct > 4 else
                 "中波动" if atr_pct and atr_pct > 2 else "低波动") if atr_pct else "未知"

    rsi_zone = None
    if not np.isnan(rsi):
        rsi_zone = "超买(>70)" if rsi > 70 else ("超卖(<30)" if rsi < 30 else "中性")

    pct_above_ma20 = ((close / ma20 - 1) * 100) if not np.isnan(ma20) and ma20 else None
    pct_above_ma60 = ((close / ma60 - 1) * 100) if not np.isnan(ma60) and ma60 else None
    # EMA20 参考（P0-1.4）：近端加权均线，供面板显示与短端动能判断
    ema20 = float(row["EMA20"]) if ("EMA20" in df.columns and not np.isnan(row["EMA20"])) else np.nan
    pct_above_ema20 = ((close / ema20 - 1) * 100) if not np.isnan(ema20) and ema20 else None

    return {
        "regime": regime,
        "alignment": alignment,
        "close": round(close, 2),
        "ma5": round(ma5, 2) if not np.isnan(ma5) else None,
        "ma20": round(ma20, 2) if not np.isnan(ma20) else None,
        "ma60": round(ma60, 2) if not np.isnan(ma60) else None,
        "ema20": round(ema20, 2) if not np.isnan(ema20) else None,
        "pct_above_ma20": round(pct_above_ma20, 2) if pct_above_ma20 is not None else None,
        "pct_above_ma60": round(pct_above_ma60, 2) if pct_above_ma60 is not None else None,
        "pct_above_ema20": round(pct_above_ema20, 2) if pct_above_ema20 is not None else None,
        "macd": macd,
        "dif": round(dif, 3), "dea": round(dea, 3), "hist": round(hist, 3),
        "rsi14": round(rsi, 1) if not np.isnan(rsi) else None,
        "rsi_zone": rsi_zone,
        "kdj": {"k": round(k, 1), "d": round(d, 1), "j": round(j, 1)},
        "atr": round(atr, 2) if atr and np.isfinite(atr) else None,
        "atr_pct": round(atr_pct, 2) if atr_pct else None,
        "volatility": vol_label,
    }


# ─────────────────────────────────────────────
# 规则信号：逐日评估（供当前建议 + 近期时间线复用）
# ─────────────────────────────────────────────
_LEVEL_META = {
    "buy":  ("建议买入", "🟢"),
    "add":  ("可加仓", "🟡"),
    "hold": ("观望持有", "🔵"),
    "sell": ("偏空·观望/减仓", "🔴"),
}


def _range_pos(df: pd.DataFrame, i: int) -> dict:
    """近 20 日收盘区间位置 frac20 ∈ [0,1] 与是否创 20 日收盘新高。

    用于把买点从「追涨确认」拉回「中低位低吸」：位置越高，追高风险越大。
    只用当日及之前的收盘价，无未来数据（区别于 _rhythm_at 的 zigzag 周期位置）。
    """
    w = df["close"].iloc[max(0, i - 19):i + 1].to_numpy(dtype=float)
    w = w[~np.isnan(w)]
    if len(w) == 0:
        return {"frac20": None, "new_high": False}
    lo20, hi20 = float(np.nanmin(w)), float(np.nanmax(w))
    close = float(df.iloc[i]["close"])
    if hi20 <= lo20:
        return {"frac20": None, "new_high": False}
    frac20 = (close - lo20) / (hi20 - lo20)
    new_high = bool(close >= hi20 * (1 - 1e-9))
    return {"frac20": frac20, "new_high": new_high}


def _signal_at(df: pd.DataFrame, i: int):
    """在指标 df 的第 i 行评估规则信号，返回 (score, reasons, risks)。"""
    row = df.iloc[i]
    close = float(row["close"])
    ma20 = float(row["MA20"]) if not np.isnan(row["MA20"]) else np.nan
    ma60 = float(row["MA60"]) if not np.isnan(row["MA60"]) else np.nan
    dif, dea, hist = float(row["MACD_DIF"]), float(row["MACD_DEA"]), float(row["MACD_HIST"])
    rsi = float(row["RSI14"]) if not np.isnan(row["RSI14"]) else np.nan
    k, d, j = (float(row["KDJ_K"]), float(row["KDJ_D"]), float(row["KDJ_J"]))
    vr = float(row["VOLUME_RATIO"]) if not np.isnan(row["VOLUME_RATIO"]) else 1.0
    pct = float(row["pct_change"]) if not np.isnan(row["pct_change"]) else 0.0

    reasons, risks = [], []
    score = 0

    # ── 趋势 ──
    if not np.isnan(ma20) and not np.isnan(ma60):
        if close > ma20 > ma60:
            score += 2; reasons.append("均线多头排列，趋势向上")
        elif close < ma20 < ma60:
            score -= 2; reasons.append("均线空头排列，趋势向下")
            risks.append("处于下降通道，反弹需谨慎")
        elif close > ma20:
            score += 1; reasons.append("站上20日线")
        else:
            score -= 1; risks.append("跌破20日线，短线转弱")
    if not np.isnan(dif) and not np.isnan(dea) and not np.isnan(hist):
        if dif > dea and hist > 0:
            score += 1; reasons.append("MACD 金叉红柱，动能转强")
        elif dif < dea and hist < 0:
            score -= 1; risks.append("MACD 死叉绿柱，动能偏弱")

    # ── EMA20 辅助（P0-1.4 用户提案）──
    # EMA20 指数加权对近端价格更敏感，反映"短端动能"。真正的新增信息 = EMA20 相对 MA20 的
    # 位置（近期价格加权强于/弱于等权=动能增强/衰减），而非重复"价相对均线"。
    # 分数影响设小，避免扰动既有追高守卫与区间位置守卫。enabled 默认关；转正前必须用
    # tools/_eval_deep_buypoints.py 做 before/after（同口径 T+1/T+3/T+5 胜率与均值）。
    if _EMA20_AUX["enabled"]:
        ema20 = float(row["EMA20"]) if ("EMA20" in df.columns and not np.isnan(row["EMA20"])) else np.nan
        if not np.isnan(ema20) and ema20 > 0:
            if close > ema20:
                score += _EMA20_AUX.get("score_above", 1); reasons.append("价站上 EMA20，短端动能偏多")
            else:
                score += _EMA20_AUX.get("score_below", -1); risks.append("价跌破 EMA20，短端动能转弱")
            # EMA20 vs MA20 动能差（新增动量信息）
            cw = _EMA20_AUX.get("cross_weight", 0)
            if cw and not np.isnan(ma20) and ma20 > 0:
                rel = (ema20 - ma20) / ma20 * 100
                band = _EMA20_AUX.get("cross_band_pct", 3.0)
                if rel > band:
                    score += cw; reasons.append(f"EMA20 高于 MA20 {rel:.1f}%，短线动能增强")
                elif rel < -band:
                    score -= cw; risks.append(f"EMA20 低于 MA20 {abs(rel):.1f}%，短线动能走弱")

    # ── 追高/扩展度守卫：价远高于 20 日线时不喊"买/加"，防止买在浪尖 ──
    if not np.isnan(ma20) and ma20 > 0:
        ext = (close - ma20) / ma20 * 100
        if ext > 15:
            score -= 2; risks.append(f"价高于 MA20 达 {ext:.0f}%，扩展度大，追高风险高")
        elif ext > 8:
            score -= 1; risks.append(f"价高于 MA20 {ext:.0f}%，接近中高位，追高需谨慎")

    # ── 区间位置守卫（P0-1.3）：把买点从"追涨确认"拉回"中低位低吸" ──
    # 数据结论：近20日区间位置 frac20 是比 MA20 扩展度更干净的"是否已在阶段高位"信号。
    #   买在顶部(frac20≥0.9/创20日新高) T+3 胜率仅 ~56%；买在中低位(frac20<0.5) T+3 胜率 ~70%。
    #   因此在区间顶部强降分、中低位加分，避免买点扎堆在局部高点。
    _rng = _range_pos(df, i)
    frac20 = _rng.get("frac20")
    if frac20 is not None:
        if frac20 >= 0.9 or _rng.get("new_high"):
            score -= 3; risks.append(f"已近20日区间顶部（位置{frac20*100:.0f}%），追高风险大")
        elif frac20 >= 0.75:
            score -= 1; risks.append(f"处于20日区间中上部（位置{frac20*100:.0f}%），介入宜谨慎")
        elif frac20 < 0.5:
            score += 1; reasons.append(f"处于20日区间中低位（位置{frac20*100:.0f}%），回调较充分")

    # ── 量能 ──
    if vr >= 1.3 and pct > 0:
        score += 1; reasons.append(f"放量上涨（量比{vr:.1f}），买盘积极")
    elif vr >= 1.5 and pct < 0:
        score -= 1; risks.append("放量下跌，抛压明显")
    elif vr <= 0.8 and pct < 0:
        reasons.append("缩量回调，抛压有限")
    elif vr >= 1.3 and abs(pct) < 0.3:
        risks.append("放量滞涨，上方有压力")

    # ── 超买超卖（反转提示）──
    if not np.isnan(rsi):
        if rsi > 72:
            score -= 1; risks.append(f"RSI {rsi:.0f} 超买，短线有回踩风险")
        elif rsi < 32:
            score += 1; reasons.append(f"RSI {rsi:.0f} 超卖，具备反弹条件")
    if not np.isnan(k) and not np.isnan(d) and not np.isnan(j):
        if j < 20 and k > d:
            score += 1; reasons.append("KDJ 低位金叉，短线转强")
        elif j > 95 and k < d:
            score -= 1; risks.append("KDJ 高位死叉，短线转弱")

    if not reasons:
        reasons.append("指标信号中性")
    if not risks:
        risks.append("无显著风险信号，按纪律操作")
    return score, reasons, risks


def _classify(score: int):
    """按规则分档：>=5 可建仓 / >=2 可加仓 / >=-2 观望持有 / <-2 谨慎减仓。"""
    if score >= 5:
        return "buy"
    if score >= 2:
        return "add"
    if score >= -2:
        return "hold"
    return "sell"


def _rhythm_at(rhythm: dict, i: int):
    """给定节奏结果，估算第 i 天的当前腿方向与阶段（供逐日信号修正复用）。"""
    pivots = rhythm.get("_pivots")
    cycle = rhythm.get("cycle_avg_days")
    if not pivots or not cycle:
        return None
    cur = None
    for p in pivots:
        if p[0] <= i:
            cur = p
        else:
            break
    if cur is None:
        return None
    direction = "up" if cur[2] == "low" else "down"
    age = i - cur[0]
    pos = (age / cycle * 100) if cycle and cycle > 0 else None
    if direction == "up":
        hint = "上升初期" if pos < 40 else ("上升中段" if pos < 80 else "上升末期")
    else:
        hint = "回调初期" if pos < 40 else ("回调中段" if pos < 80 else "回调末段")
    return {"direction": direction, "age": age, "pos": round(pos, 0) if pos else None, "hint": hint}


def _rhythm_adjust(score: int, ctx, rng=None) -> int:
    """按当前节奏位置修正评分（顺势加分、追高/回调减分），并考虑区间位置。

    上升初期 = 低吸位（+2）；上升中段 = 顺势（+1）；上升末期 = 追高（-1）。
    回调初期 = 回调未完（-2）；回调中段 = 回调未止（-1）；回调末段 = 可能企稳（0）。
    rng 为 _range_pos 结果：若价格已接近区间顶部（frac20≥0.9 或创20日新高），则
    "上升初期/中段"的顺势加分被抵消（不因"早期上升"而在阶段高位继续加仓），
    避免把已在阶段高位的"上升初期"误当作低吸位。
    """
    if not ctx:
        return score
    d, hint = ctx["direction"], ctx["hint"]
    top = bool(rng) and (rng.get("new_high") or (rng.get("frac20") is not None and rng["frac20"] >= 0.9))
    if d == "up":
        if "初期" in hint:
            return score if top else score + 2
        if "中段" in hint:
            return score if top else score + 1
        return score - 1           # 上升末期，追高风险大
    else:
        if "初期" in hint:
            return score - 2
        if "中段" in hint:
            return score - 1
        return score               # 回调末段，关注企稳反转


def _signal_plan_at(df: pd.DataFrame, rhythm: dict, i: int,
                    stop_mult: float = 2.5, rr_target: float = 2.5) -> dict:
    """在第 i 行评估规则信号 + 节奏修正 + 分档，返回该日的完整建议计划。

    供 recent_advice（时间线）与 compute_position_stats（持仓回测）复用，
    保证两处对同一交易日的信号判断与止损/止盈价完全一致。
    """
    score, reasons, risks = _signal_at(df, i)
    score = _rhythm_adjust(score, _rhythm_at(rhythm, i), _range_pos(df, i))
    level = _classify(score)
    label, emoji = _LEVEL_META[level]
    row = df.iloc[i]
    brief = (risks[0] if level == "sell" else reasons[0]) if reasons or risks else ""
    close = float(row["close"])
    atr = float(row["ATR"]) if not np.isnan(row["ATR"]) else np.nan
    entry = round(close, 2)
    stop_l = round(close - stop_mult * atr, 2) if (level in ("buy", "add") and atr and np.isfinite(atr) and atr > 0) else None
    tp_l = None
    if stop_l:
        risk = (close - stop_l) / close
        tp_l = round(close + risk * rr_target * close, 2)
    return {
        "date": df.index[i],
        "close": entry,
        "pct_change": round(float(row["pct_change"]), 2) if not np.isnan(row["pct_change"]) else None,
        "level": level,
        "label": label,
        "emoji": emoji,
        "brief": brief,
        "entry_price": entry,
        "stop_loss": stop_l,
        "take_profit": tp_l,
    }


def recent_advice(df: pd.DataFrame, rhythm: dict, days: int = 30,
                  stop_mult: float = 2.5, rr_target: float = 2.5) -> list:
    """逐日规则信号（含节奏修正），返回最近 days 个交易日的建议时间线。

    对 buy/add 日额外给出当日的参考建仓计划（入场价=当日收盘 / 止损=收盘-2.5×ATR /
    止盈=入场+盈亏比×风险），供前端在 K 线上标注买点并复盘"推荐是否正确"。
    """
    if len(df) < 2:
        return []
    days = max(1, min(days, len(df)))
    start = len(df) - days
    out = []
    for i in range(start, len(df)):
        out.append(_signal_plan_at(df, rhythm, i, stop_mult, rr_target))
    return out


def compute_position_stats(df: pd.DataFrame, advice: list, *,
                           max_hold_days: Optional[int] = None,
                           stop_mult: float = 2.5, rr_target: float = 2.5,
                           include_curve: bool = True) -> Optional[dict]:
    """把 buy/add 建议转成逐笔模拟持仓，并聚合「持仓统计」。

    口径（与 recommend_outcome / 出场跟踪完全一致）：
      - 信号日(signal_date)次日开盘价买入（T+1：买入日不可卖，从第 2 个交易日开始判定）；
      - 卖出判定统一用当日收盘价（不做盘中插针触发）；
      - 出场优先级：收盘破止损 → 移动/固定止盈（此处用建议自带固定止盈价）→ 到期卖出
        （max_hold_days 个交易日，默认短线 10）；
      - 未出场的交易按最后收盘价 mark-to-market，status='holding'。

    advice 来自 recent_advice（或 _signal_plan_at 序列）。每个 buy/add 信号视为一次
    「每日候选」机会，独立成笔（可重叠），据此回答「若每日按建议买入这只票的收益分布」。

    返回 {n_trades, n_closed, n_open, summary, exit_reasons, trades, equity_curve}；
    无任何可回测信号时返回 None。
    """
    if df is None or len(df) < 2:
        return None
    if not advice:
        return None
    if max_hold_days is None:
        max_hold_days = get_max_hold("short") or 10
    date_idx = {d: i for i, d in enumerate(df.index)}
    dates = df.index.tolist()

    trades = []
    for a in advice:
        if a.get("level") not in ("buy", "add"):
            continue
        sig_date = a["date"]
        i = date_idx.get(sig_date)
        if i is None or i + 1 >= len(df):
            continue   # 信号日为最后一天，无次日可回测
        entry_date = df.index[i + 1]
        entry_price = float(df.iloc[i + 1]["open"])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        res = evaluate_exit_by_prices(
            entry_price=entry_price, entry_date=sig_date, df=df,
            stop_loss=a.get("stop_loss"), take_profit=a.get("take_profit"),
            max_hold_days=max_hold_days,
        )
        if res.get("status") == "hold":
            # 数据不足/推荐后无行情 → 不构成可回测笔
            if res.get("detail", {}).get("exit_date") is None and res.get("detail", {}).get("current_price") is None:
                continue
        d = res.get("detail") or {}
        trades.append({
            "signal_date": sig_date,
            "entry_date": d.get("entry_date") or entry_date,
            "entry_price": round(entry_price, 2),
            "exit_date": d.get("exit_date"),
            "exit_price": d.get("exit_price"),
            "exit_reason": d.get("exit_reason"),
            "hold_days": d.get("hold_days"),
            "return_pct": d.get("current_pnl_pct"),
            "status": "clear" if res.get("status") == "clear" else "holding",
        })
    if not trades:
        return None

    closed = [t for t in trades if t["status"] == "clear"]
    open_ = [t for t in trades if t["status"] == "holding"]

    # ── 聚合指标 ──
    def _stats(ts):
        if not ts:
            return None
        rets = np.array([t["return_pct"] for t in ts], dtype=float)
        rets = rets[np.isfinite(rets)]
        if len(rets) == 0:
            return None
        wins = rets[rets > 0]
        losses = rets[rets <= 0]
        win_rate = float(np.mean(rets > 0))
        avg_win = float(np.mean(wins)) if len(wins) else 0.0
        avg_loss = float(np.mean(losses)) if len(losses) else 0.0
        profit_factor = (avg_win / abs(avg_loss)) if avg_loss else (float("inf") if wins.size else 0.0)
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
        hold = np.array([t["hold_days"] for t in ts if t["hold_days"] is not None], dtype=float)
        return {
            "n": int(len(ts)),
            "win_rate": round(win_rate * 100, 1),
            "avg_return_pct": round(float(np.mean(rets)), 2),
            "median_return_pct": round(float(np.median(rets)), 2),
            "best_return_pct": round(float(np.max(rets)), 2),
            "worst_return_pct": round(float(np.min(rets)), 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else None,
            "expectancy_pct": round(expectancy, 2),
            "avg_hold_days": round(float(np.mean(hold)), 1) if hold.size else None,
            "max_hold_days": int(np.max(hold)) if hold.size else None,
        }

    summary = _stats(closed)
    if summary is None:
        # 全部仍持仓：用当前浮盈口径兜底，避免面板空白
        summary = _stats(trades)
        summary["basis_pct"] = "含持仓中未实现浮盈"
    summary["n_trades"] = len(trades)
    summary["n_closed"] = len(closed)
    summary["n_open"] = len(open_)
    summary["max_hold_cap"] = max_hold_days

    # 出场原因分布
    exit_reasons: dict = {}
    for t in closed:
        r = (t["exit_reason"] or "其他").split("：")[0]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # ── 组合净值曲线（用于最大回撤）──
    # 每笔信号占 1 份资金，买入日入场、出场后回收为现金（1.0），其余资金空仓。
    # 逐日 mark-to-market：未了结按当日收盘、已了结按出场价冻结。value 归一化到 1.0 起。
    max_drawdown_pct = None
    curve = []
    if include_curve and trades:
        total_units = len(trades)
        last_val = 1.0
        peak = 1.0
        for d in dates:
            val = 0.0
            deployed = 0
            di = date_idx[d]
            for t in trades:
                if t["entry_date"] not in date_idx or d < t["entry_date"]:
                    continue   # 尚未入场
                deployed += 1
                if t["exit_date"] and d >= t["exit_date"] and t["exit_price"] is not None:
                    mark = t["exit_price"] / t["entry_price"]
                else:
                    mark = float(df.iloc[di]["close"]) / t["entry_price"]
                val += mark
            val += (total_units - deployed)   # 未部署资金 = 1.0/份
            last_val = val / total_units
            curve.append({"date": d, "value": round(last_val, 4)})
            if last_val > peak:
                peak = last_val
            if peak > 0:
                dd = (peak - last_val) / peak
                max_drawdown_pct = max(max_drawdown_pct or 0.0, dd)
        if max_drawdown_pct is not None:
            summary["max_drawdown_pct"] = round(max_drawdown_pct * 100, 2)

    out = {
        "n_trades": len(trades),
        "n_closed": len(closed),
        "n_open": len(open_),
        "max_hold_cap": max_hold_days,
        "summary": summary,
        "exit_reasons": exit_reasons,
        "trades": trades,
    }
    if curve:
        out["equity_curve"] = curve
    return out


# ─────────────────────────────────────────────
# 当前可执行建议：节奏 + 量价 + 趋势 → 入场/止损/止盈
# ─────────────────────────────────────────────
def _current_signal(df: pd.DataFrame, trend: dict, volprice: dict, rhythm: dict,
                    stop_mult: float = 2.5, rr_target: float = 2.5) -> dict:
    close = float(df.iloc[-1]["close"])
    atr = trend.get("atr")
    swing_low = rhythm.get("swing_low") if rhythm else None
    i = len(df) - 1
    score, reasons, risks = _signal_at(df, i)
    # 节奏修正（与近期时间线同口径）
    ctx = _rhythm_at(rhythm, i)
    rng = _range_pos(df, i)
    base_score = score
    score = _rhythm_adjust(score, ctx, rng)
    level = _classify(score)
    label, emoji = _LEVEL_META[level]

    # 节奏说明
    if ctx:
        d, hint = ctx["direction"], ctx["hint"]
        if d == "up":
            if "初期" in hint:
                reasons.append("上升初期，回调空间相对有限")
            elif "末期" in hint:
                risks.append("已接近上升末端，追高风险加大")
        else:
            if "末段" in hint:
                reasons.append("回调接近尾声，关注止跌企稳")
            else:
                risks.append("处于回调波段，等待止跌信号")

    # 止盈止损：ATR 止损（含摆动低点兜底）+ 固定盈亏比止盈
    if atr and np.isfinite(atr) and atr > 0:
        stop = close - stop_mult * atr
    else:
        stop = close * 0.95
    if swing_low and swing_low > 0 and swing_low < close and stop > swing_low:
        stop = max(stop, swing_low)   # 止损不深于近期摆动低点
    stop = round(stop, 2)
    risk = (close - stop) / close if close > 0 else 0
    tp = round(close + risk * rr_target * close, 2)
    reward = (tp - close) / close if close > 0 else 0
    rr = round(reward / risk, 2) if risk > 0 else None

    # 排序特征（供候选排名用，见 deep_tracker.sync_from_scan）
    #   score    = 节奏修正后的最终规则分，是 buy 池内部最重要的区分维度
    #              （buy 门槛 5 分，实际分布 5~10，只按 pct_above_ma20 排会把它丢掉）
    #   frac20   = 近 20 日收盘区间位置，比 MA20 扩展度更干净的"是否已在阶段高位"信号
    #   risk_pct = ATR 止损宽度占价格比，衡量单笔风险敞口与波动率
    return {
        "level": level,
        "label": label,
        "emoji": emoji,
        "score": int(score),
        "base_score": int(base_score),
        "frac20": (round(float(rng["frac20"]), 4)
                   if rng.get("frac20") is not None else None),
        "risk_pct": round(risk * 100, 2) if risk > 0 else None,
        "atr_pct": (round(atr / close * 100, 2)
                    if atr and np.isfinite(atr) and close > 0 else None),
        "reasons": reasons,
        "risks": risks,
        "rhythm_note": (ctx["hint"] if ctx else None),
        "action_plan": {
            "entry_price": round(close, 2),
            "stop_loss": stop,
            "take_profit": tp,
            "risk_pct": round(risk * 100, 1) if risk > 0 else None,
            "reward_pct": round(reward * 100, 1) if reward > 0 else None,
            "risk_reward_ratio": rr,
        },
    }


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────
def analyze_stock(conn, code: str, lookback: int = 260, recent_days: int = 45,
                  light: bool = False, as_of: Optional[str] = None) -> dict:
    """个股深度分析主入口。

    返回 dict：{ code, name, as_of, latest, trend, volume_price, rhythm, signal,
                 recent_advice, position_stats, chart }。
    light=True 时跳过 recent_advice / position_stats / chart（供批量扫描提速）。
    数据不足时返回 { insufficient: True, reason }。

    as_of：站在该交易日收盘后分析（只用它及之前的行情）。历史回补必传，
    不传则按最新行情分析（每日盘后扫描的正常用法）。
    """
    df = load_history(conn, code, lookback=lookback, as_of=as_of)
    if df is None or len(df) < 60:
        return {
            "code": code, "insufficient": True,
            "reason": "历史数据不足（<60 个交易日），请先同步该股行情",
        }

    df = _compute(df)

    # 基本信息（industry/market 列可能缺失，防御式查询）
    name, industry, market = code, None, None
    try:
        info = conn.execute(
            "SELECT name, industry, market FROM stock_info WHERE code = ?", (code,)
        ).fetchone()
        if info:
            name = info["name"] or code
            industry = info["industry"] if "industry" in info.keys() else None
            market = info["market"] if "market" in info.keys() else None
    except Exception:
        try:
            info = conn.execute(
                "SELECT name FROM stock_info WHERE code = ?", (code,)
            ).fetchone()
            name = (info["name"] if info else code)
        except Exception:
            pass

    trend = trend_state(df)
    volprice = analyze_volume_price(df)
    rhythm = detect_rhythm(df)
    signal = _current_signal(df, trend, volprice, rhythm)
    # ⚠ 保留 _pivots 供 recent_advice/position_stats 做节奏修正；仅在对外输出时剥除。

    base = {
        "code": code,
        "name": name,
        "industry": industry,
        "market": market,
        "as_of": df.index[-1],
        "latest": {
            "close": round(float(df.iloc[-1]["close"]), 2),
            "pct_change": round(float(df.iloc[-1]["pct_change"]), 2) if not np.isnan(df.iloc[-1]["pct_change"]) else None,
            "volume": round(float(df.iloc[-1]["volume"]), 0),
            "date": df.index[-1],
        },
        "trend": trend,
        "volume_price": volprice,
        "rhythm": None,
        "signal": signal,
    }
    if light:
        base["rhythm"] = {k: v for k, v in rhythm.items() if k != "_pivots"}
        return base

    advice = recent_advice(df, rhythm, days=recent_days)
    base["recent_advice"] = advice
    # 持仓统计：把最近 recent_days 天内的 buy/add 建议逐笔模拟并聚合收益
    ps = compute_position_stats(df, advice, stop_mult=2.5, rr_target=2.5)
    base["position_stats"] = ps

    # 图表数据：近 120 根 K 线
    n_chart = min(120, len(df))
    chart_df = df.iloc[-n_chart:]
    chart = {
        "dates": list(chart_df.index),
        "close": [round(float(x), 2) for x in chart_df["close"]],
        "ma5": [round(float(x), 2) if not np.isnan(x) else None for x in chart_df["MA5"]],
        "ma20": [round(float(x), 2) if not np.isnan(x) else None for x in chart_df["MA20"]],
        "ma60": [round(float(x), 2) if not np.isnan(x) else None for x in chart_df["MA60"]],
        "ema20": [round(float(x), 2) if not np.isnan(x) else None for x in chart_df["EMA20"]] if "EMA20" in chart_df.columns else [],
        "volume": [round(float(x), 0) for x in chart_df["volume"]],
        "open": [round(float(x), 2) for x in chart_df["open"]],
        "high": [round(float(x), 2) for x in chart_df["high"]],
        "low": [round(float(x), 2) for x in chart_df["low"]],
    }
    base["recent_advice"] = advice
    base["rhythm"] = {k: v for k, v in rhythm.items() if k != "_pivots"}
    base["chart"] = chart
    return base


def scan_market_buy(conn, codes, limit: int = 10) -> list:
    """在候选股票池上跑深析信号，筛选出「明日可推荐买入」的股票并排名。

    只对给定 codes（今日推荐 + 自选 + 持仓等"已关注池"）逐一分析，避免全市场逐只
    计算指标导致请求超时。筛选条件：
      1) 深析信号 level ∈ {buy, add}（追高/扩展度守卫已在 _signal_at 内生效）；
      2) 价相对 MA20 偏离 ≤ 15%（不买过度扩展的高位票）；
      3) 排除 ST/退市。
    排序：仅按信号级别（建议买入 > 可加仓），不做多指标加权求和，避免把个别强值平均掉。
    返回 [{ code, name, industry, level, label, emoji, rhythm_leg, rhythm_hint,
            reasons, pct_above_ma20, action_plan }]。
    """
    results = []
    for code in codes:
        try:
            res = analyze_stock(conn, code, lookback=260, recent_days=0, light=True)
            if res.get("insufficient"):
                continue
            sig = res.get("signal") or {}
            lvl = sig.get("level")
            if lvl not in ("buy", "add"):
                continue
            name = res.get("name") or code
            if "ST" in (name or "") or "退" in (name or ""):
                continue
            rhythm = res.get("rhythm") or {}
            tr = res.get("trend") or {}
            hint = rhythm.get("position_hint") or ""
            leg = rhythm.get("current_leg") or ""
            ext = tr.get("pct_above_ma20")
            if ext is not None and ext > 15:
                continue
            results.append({
                "code": code,
                "name": name,
                "industry": res.get("industry"),
                "level": lvl,
                "label": sig.get("label"),
                "emoji": sig.get("emoji"),
                "rhythm_leg": leg,
                "rhythm_hint": hint,
                "reasons": (sig.get("reasons") or [])[:2],
                "pct_above_ma20": ext,
                "action_plan": sig.get("action_plan"),
            })
        except Exception:
            continue
    # 排序：只按信号级别（建议买入 > 可加仓），不做多指标加权求和，避免把个别强值平均掉
    results.sort(key=lambda x: 0 if x["level"] == "buy" else 1)
    return results[:limit]


# ─────────────────────────────────────────────
# 全市场深析扫描（每日同步+打分后运行，结果落库供「明日候选」读取）
# ─────────────────────────────────────────────
def _ensure_market_signal_table(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS stock_deep_signal (
        scan_date     TEXT NOT NULL,
        code          TEXT NOT NULL,
        name          TEXT,
        industry      TEXT,
        level         TEXT,
        label         TEXT,
        emoji         TEXT,
        rhythm_leg    TEXT,
        rhythm_hint   TEXT,
        pct_above_ma20 REAL,
        entry_price   REAL,
        stop_loss     REAL,
        take_profit   REAL,
        reasons       TEXT,
        created_at    TEXT,
        score         INTEGER,   -- 节奏修正后的最终规则分（buy≥5），候选排名主排序键
        base_score    INTEGER,   -- 节奏修正前的规则分
        frac20        REAL,      -- 近20日收盘区间位置 0~1（越低=回调越充分）
        risk_pct      REAL,      -- ATR 止损宽度占价比 %
        atr_pct       REAL,      -- ATR 占价比 %（波动率）
        PRIMARY KEY (scan_date, code)
    );
    CREATE INDEX IF NOT EXISTS idx_sds_scan ON stock_deep_signal(scan_date);
    """)
    _migrate_signal_table(conn)


# 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列，这里显式检查后 ALTER
_MIGRATE_COLS = {
    "score": "INTEGER",
    "base_score": "INTEGER",
    "frac20": "REAL",
    "risk_pct": "REAL",
    "atr_pct": "REAL",
}


def _migrate_signal_table(conn) -> None:
    try:
        have = {r["name"] for r in conn.execute(
            "PRAGMA table_info(stock_deep_signal)")}
    except Exception:
        return
    for col, typ in _MIGRATE_COLS.items():
        if col not in have:
            try:
                conn.execute(
                    f"ALTER TABLE stock_deep_signal ADD COLUMN {col} {typ}")
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        pass


def candidate_order_by(conn) -> str:
    """stock_deep_signal 候选排名的 ORDER BY 子句（该表的排名口径统一在此定义）。

    deep_tracker.sync_from_scan（挑 top N 建跟踪单）与 get_market_signal_latest
    （面板展示当日候选）共用同一套键，避免"看到的"和"跟踪的"顺序不一致。

    优先级依次：
      1) buy 优先于 add（信号级别；buy 不足时才补 add）
      2) buy 池内：规则分 ≥ top_rank_min_score(6) 的归为「强信号档」优先
      3) 同档内：价相对 MA20 的扩展度升序（贴均线 = 回调到位，不追高）
      4) code 升序：保证同分结果稳定可复现

    为什么是这套键（2026-09-02 定案，依据 tools/eval_topk_pick.py 离线评估：
    60 个扫描日、复刻 deep_tracker 真实入场出场规则，且修正了"末段未走完单被整批剔除"
    的采样偏差后）：
      · 旧逻辑只用 pct_above_ma20 ASC：均值 +0.07%、PF 1.02、胜率 60.2%
      · 新逻辑（score6↓ + ext ASC）： 均值 +1.56%、PF 1.48、胜率 62.9%、止损率 12.6%
      · 稳健性：逐日配对 39/56 天胜出（全 16 个键里最高）；弱市前半段 -0.26% vs 旧 -2.51%；
        topn 取 5/8/10/15/20 新键均值全为正（1.21/1.33/1.56/1.21/1.21），旧键在 5/15 为负
      · 机制（buy 池全样本分层归因）：
          score=5 均值 +0.15%(PF 1.04) / score=6 +0.63%(PF 1.17) / score>=7 -1.18%(PF 0.79)
          → score 必须**封顶**成两档（>=6 与 <6），不能无脑 DESC，否则那 20 只 7 分
            极端票会顶到最前反而更差（未封顶版本 PF 1.41 < 封顶后 1.48）
          ext<1 均值 +1.17%(PF 1.39) / ext 1~3 +0.01% / ext 3~6 -0.21%
          → 同档内挑**贴 MA20** 的，而不是已经拉开距离的
      · frac20（20日区间位置）分层虽然最单调（<0.33 为 +2.27%、>=0.66 为 -0.59%），
        但 buy 池里 frac<0.33 只占 1.4%，做主键在 top10 上区分度不足（实测 PF 1.14），
        仅作为 score 的辅助维度保留在落库字段里。
      · 注：上述 60 日评估是在"as_of 修正后的候选表"上做的，是面向未来的增益估计
        （+1.5pp）。若拿它重放当初在修正前建的 620 只真实单，增益只有 +0.19pp——
        因为那些旧单本身是在未修正的候选数据上挑的，两套数据的 ext 分布不同。
        两个数字测的是不同东西，新键实现与模拟器已在两套口径下交叉验证一致（+1.56%）。

    ⚠ score 列缺失时（旧库 / 测试内存表）自动回退到旧的 ext 单键排序，保证不报错。
    """
    try:
        # ⚠ 用索引 r[1] 而不是 r["name"]：PRAGMA 结果只有设了 row_factory=Row 的连接
        # 才支持按名取值，否则 r["name"] 抛 TypeError → 被 except 吞掉 → 静默回退旧排序。
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(stock_deep_signal)")}
    except Exception:
        cols = set()
    if "score" not in cols:
        return ("CASE level WHEN 'buy' THEN 0 ELSE 1 END, "
                "COALESCE(pct_above_ma20, 999) ASC, code ASC")
    cap = int(DEEP_TRACK.get("top_rank_min_score", 6) or 6)
    return (f"CASE level WHEN 'buy' THEN 0 ELSE 1 END, "
            f"CASE WHEN COALESCE(score, 0) >= {cap} THEN 0 ELSE 1 END, "
            f"COALESCE(pct_above_ma20, 999) ASC, code ASC")


def _insert_market_signals(conn, scan_date: str, rows: list):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        ap = r.get("action_plan") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO stock_deep_signal
            (scan_date, code, name, industry, level, label, emoji,
             rhythm_leg, rhythm_hint, pct_above_ma20,
             entry_price, stop_loss, take_profit, reasons, created_at,
             score, base_score, frac20, risk_pct, atr_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (scan_date, r["code"], r["name"], r.get("industry"), r["level"],
             r.get("label"), r.get("emoji"),
             r.get("rhythm_leg"), r.get("rhythm_hint"), r.get("pct_above_ma20"),
             ap.get("entry_price"), ap.get("stop_loss"), ap.get("take_profit"),
             json.dumps(r.get("reasons") or [], ensure_ascii=False), now,
             r.get("score"), r.get("base_score"), r.get("frac20"),
             r.get("risk_pct"), r.get("atr_pct")),
        )


import threading
_scan_tls = threading.local()


def _scan_conn():
    """取当前工作线程专属的 sqlite 连接（首次调用时创建，之后复用）。

    连接不能跨线程共享，但**每只股票都新建连接**的开销会吃掉全部并行收益
    （实测 50 只从 39ms/只 劣化到 270ms/只）。线程内复用后，连接数 = 线程数。
    """
    c = getattr(_scan_tls, "conn", None)
    if c is None:
        import sqlite3 as _sq
        from core.db import DB_PATH
        c = _sq.connect(DB_PATH, timeout=30)
        c.row_factory = _sq.Row
        c.execute("PRAGMA busy_timeout=10000")
        _scan_tls.conn = c
    return c


def _scan_one(code: str, lookback: int, as_of: Optional[str]) -> Optional[dict]:
    """单股深析 + 候选筛选（在工作线程内跑）。失败一律返回 None，与串行版吞异常的行为一致。"""
    try:
        res = analyze_stock(_scan_conn(), code, lookback=lookback, recent_days=0,
                            light=True, as_of=as_of)
        if res.get("insufficient"):
            return None
        sig = res.get("signal") or {}
        lvl = sig.get("level")
        if lvl not in ("buy", "add"):
            return None
        name = res.get("name") or code
        if "ST" in name or "退" in name:
            return None
        tr = res.get("trend") or {}
        rhythm = res.get("rhythm") or {}
        ext = tr.get("pct_above_ma20")
        if ext is not None and ext > 15:
            return None
        return {
            "code": code,
            "name": name,
            "industry": res.get("industry"),
            "level": lvl,
            "label": sig.get("label"),
            "emoji": sig.get("emoji"),
            "rhythm_leg": rhythm.get("current_leg") or "",
            "rhythm_hint": rhythm.get("position_hint") or "",
            "pct_above_ma20": ext,
            "score": sig.get("score"),
            "base_score": sig.get("base_score"),
            "frac20": sig.get("frac20"),
            "risk_pct": sig.get("risk_pct"),
            "atr_pct": sig.get("atr_pct"),
            "action_plan": sig.get("action_plan"),
            "reasons": (sig.get("reasons") or [])[:2],
        }
    except Exception:
        return None


# ⚠ 并行度说明（实测结论，勿凭直觉改）：
#   analyze_stock 是纯 Python 计算（pandas rolling + KDJ 递归 + zigzag 循环），
#   **不释放 GIL**，所以 ThreadPoolExecutor 不但无效反而有害：
#     实测 300 只：1 线程 26.4s(88ms/只) → 8 线程 111.8s(373ms/只)，慢 4.2 倍。
#   真正有效的是多进程（ProcessPoolExecutor），实测 8 进程提速 3.3x（65ms→20ms/只），
#   12 进程不再提升（已饱和）。
#   但 Windows 的 spawn 会重新导入主模块，在 Flask 请求线程里起进程池会重复启动 app，
#   所以进程并行放在独立脚本 tools/backfill_deep_scan.py 里（由 Flask 用 subprocess 拉起）。
#   这里默认保持串行，保证每日盘后扫描行为不变、不受 GIL 争用拖累。
_DEFAULT_SCAN_WORKERS = int(os.environ.get("DEEP_SCAN_WORKERS", "0") or 0)


def run_full_market_scan(conn, max_stocks: Optional[int] = None,
                         progress_callback=None, batch: int = 50,
                         scan_date: Optional[str] = None, lookback: int = 180,
                         as_of: Optional[str] = None,
                         workers: Optional[int] = None) -> dict:
    """全市场深析扫描：对全部活跃股票跑深析信号，把「买入/加仓且不追高」的候选落库。

    用途：每日数据同步 + 打分重算完成后调用（可后台任务），供「明日候选」读取。
    筛选：level ∈ {buy, add}、价相对 MA20 ≤ 15%、非 ST/退市。
    返回 { scan_date, as_of, scanned, candidates, elapsed_s, workers }。

    ⚠ as_of 语义：不传时默认取 scan_date，即「站在该日收盘后、只用当时可见的行情」
      来分析。这是历史回补正确性的关键 —— 若用最新行情贴历史日期的标签，
      回补出的信号全部预知未来，胜率虚高到不可用（look-ahead bias）。
      每日盘后扫描 scan_date=今天，as_of=今天，二者一致，行为与之前完全相同。
    """
    _ensure_market_signal_table(conn)
    scan_date = scan_date or datetime.now().strftime("%Y-%m-%d")
    if as_of is None:
        as_of = scan_date
    # 该日无行情（非交易日/停摆）时不扫，避免贴着历史日期写入"最新行情"的结果
    has_day = conn.execute(
        "SELECT 1 FROM daily_price WHERE trade_date = ? LIMIT 1", (as_of,)
    ).fetchone()
    if not has_day:
        return {"scan_date": scan_date, "as_of": as_of, "scanned": 0,
                "candidates": 0, "elapsed_s": 0.0,
                "skipped": f"{as_of} 无行情数据"}
    codes = [r["code"] for r in conn.execute(
        """
        SELECT DISTINCT d.code FROM daily_price d
        JOIN stock_info i ON i.code = d.code
        WHERE i.is_active = 1
        ORDER BY d.code
        """
    ).fetchall()]
    if max_stocks:
        codes = codes[:max_stocks]
    # 清掉当日旧结果（重扫覆盖）
    conn.execute("DELETE FROM stock_deep_signal WHERE scan_date = ?", (scan_date,))
    conn.commit()

    total = len(codes)
    cands = []
    t0 = time.time()
    n_workers = _DEFAULT_SCAN_WORKERS if workers is None else int(workers or 0)

    def _progress(done):
        if progress_callback:
            try:
                progress_callback(round(done * 100 / total, 1) if total else 0,
                                  f"全市场深析扫描 {done}/{total}")
            except Exception:
                pass

    if n_workers and n_workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        buf, done = [], 0
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(_scan_one, code, lookback, as_of) for code in codes]
            for fu in as_completed(futs):
                done += 1
                try:
                    r = fu.result()
                except Exception:
                    r = None
                if r:
                    cands.append(r)
                    buf.append(r)
                if len(buf) >= batch:
                    _insert_market_signals(conn, scan_date, buf)
                    conn.commit()
                    buf = []
                if done % 200 == 0 or done == total:
                    _progress(done)
        if buf:
            _insert_market_signals(conn, scan_date, buf)
            conn.commit()
    else:
        buf = []
        for idx, code in enumerate(codes):
            r = _scan_one(code, lookback, as_of)
            if r:
                cands.append(r)
                buf.append(r)
            if len(buf) >= batch:
                _insert_market_signals(conn, scan_date, buf)
                conn.commit()
                buf = []
            if progress_callback and (idx % 200 == 0 or idx == total - 1):
                _progress(idx + 1)
        if buf:
            _insert_market_signals(conn, scan_date, buf)
            conn.commit()

    return {"scan_date": scan_date, "as_of": as_of, "scanned": total,
            "candidates": len(cands), "elapsed_s": round(time.time() - t0, 1),
            "workers": n_workers}


def get_market_signal_latest(conn, limit: int = 20) -> list:
    """读取最近一次全市场深析扫描的结果（按信号级别排序）。"""
    _ensure_market_signal_table(conn)
    row = conn.execute("SELECT MAX(scan_date) AS d FROM stock_deep_signal").fetchone()
    scan_date = row["d"] if row else None
    if not scan_date:
        return []
    rows = conn.execute(
        f"""
        SELECT * FROM stock_deep_signal
        WHERE scan_date = ?
        ORDER BY {candidate_order_by(conn)}
        LIMIT ?
        """,
        (scan_date, limit),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["reasons"] = json.loads(d.get("reasons") or "[]")
        except Exception:
            d["reasons"] = []
        d["action_plan"] = {
            "entry_price": d.get("entry_price"),
            "stop_loss": d.get("stop_loss"),
            "take_profit": d.get("take_profit"),
        }
        items.append(d)
    return items
