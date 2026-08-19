"""
strategy/mid_long.py —— 中线 / 长线选股信号扫描
================================================

补齐推荐体系的三周期（horizon）维度：

- 短期（short, 1~10 交易日）：core/sync.py 现有 5 信号融合（strategies.py）
- 中期（mid,   10~60 交易日）：本模块 scan_mid_term —— 基于 strategy/strategy.py
  composite 评分的三过滤确认入场（锚点上穿 + 突破确认 + 不追高 + 弱市闸门，
  2026-08-19 风格替换；止损止盈仍为 ATR 口径）
- 长期（long,  60+ 交易日）：本模块 scan_long_term —— MA60/MA120 长趋势
  + 低波动 + 回撤过滤（财务降级过滤在推荐 API 层做）

与短线扫描的差异：
  短线扫描在 recalc_all_scores 中对"每个历史交易日"逐日写 stock_signal；
  中/长线信号持续期长，逐日写库会造成行数膨胀且价值低，
  因此本模块只对"最新一个交易日"评估，命中写一条信号。
  /api/investor/today 取 MAX(scan_date) 分组展示，不受影响。

输入 df 约定：OHLCV 日线，含 open/high/low/close/volume 列，日期索引升序
（与 core.sync.recalc_all_scores 中 get_daily_price(code) 的返回一致）。
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd


# ─────────────────────────────────────────────
# 中线：三过滤确认入场（2026-08-19 风格替换，替代"上穿当日"触发）
# ─────────────────────────────────────────────

def scan_mid_term(df: pd.DataFrame, min_rows: int = 80,
                  score_threshold: float = 65.0,
                  weak_market_ok: bool = True) -> Optional[dict]:
    """
    中线三过滤确认信号（锚点 + 突破确认 + 不追高 + 弱市闸门）。

    命中条件（缺一不可）：
      1) 锚点：近 mid_anchor_lookback 日（默认 5）内 composite 分最近一次
         上穿 score_threshold（前一日 <阈值、当日 >=阈值）；
      2) 确认：当日收盘 > 锚点日高点 × mid_confirm_mult（默认 1.002），
         且当日为该锚点的首个确认日（锚点与今日之间的任何一日均未突破）；
      3) 位置：当日偏离 MA20 ≤ mid_dev_ma20_max（默认 5%，不追高）；
      4) 弱市闸门：weak_market_ok=False（调用方按全市场当日平均涨幅判定）不出信号。

    回测依据（tools/verify_mid_three_filter.py：全历史 21.2 万笔，
    test=2020+ 9.7 万笔，出场同线上 launch=0.06/trail=0.10/cap=0.12）：
      旧"上穿当日"入场: test 胜率 40.1% 均值 +0.930% PF 1.79
      本三过滤组合:     test 胜率 43.2% 均值 +1.385% PF 1.74（日均 61 信号）

    Returns:
        None 或 dict:
          horizon/strategy/fusion_score(0~50)/buy_price/stop_loss/take_profit/
          trade_date/triggers(中文命中描述)
    """
    if df is None or len(df) < min_rows:
        return None
    if not weak_market_ok:
        return None

    # 延迟导入，避免与 strategy/strategy.py 形成循环依赖
    from strategy.strategy import run_strategy

    try:
        result = run_strategy(df, "composite")
    except Exception:
        return None
    if result is None or len(result) < 2:
        return None

    from config.strategy_params import get_param
    lookback = int(get_param("mid_anchor_lookback"))
    confirm_mult = float(get_param("mid_confirm_mult"))
    dev_max = float(get_param("mid_dev_ma20_max"))

    score = result["COMPOSITE_SCORE"].astype(float)
    cross = (score >= score_threshold) & (score.shift(1) < score_threshold)
    close_s = result["close"].astype(float)
    high_s = result["high"].astype(float)
    ma20_s = result["MA20"].astype(float)

    last = len(result) - 1
    latest = result.iloc[-1]
    close = float(latest["close"])
    ma20 = float(ma20_s.iloc[last])
    if not math.isfinite(ma20) or ma20 <= 0:
        return None

    # 过滤 3：偏离 MA20 上限（不追高）
    dev = close / ma20 - 1.0
    if not math.isfinite(dev) or dev > dev_max:
        return None

    # 过滤 1：最近 lookback 日内的上穿锚点
    anchor = None
    for age in range(1, lookback + 1):
        i = last - age
        if i < 0:
            break
        if bool(cross.iloc[i]):
            anchor = i
            break
    if anchor is None:
        return None

    # 过滤 2：当日首次收盘突破锚点日高点 × confirm_mult
    target = float(high_s.iloc[anchor]) * confirm_mult
    if not math.isfinite(target) or close <= target:
        return None
    between = close_s.iloc[anchor + 1: last]
    if len(between) > 0 and bool((between > target).any()):
        return None  # 锚点后已有确认日，今日不是首个确认日

    atr = float(latest.get("ATR", 0) or 0)
    if math.isfinite(atr) and atr > 0:
        # 止损 = close - k×ATR，止盈 = close + 2.5×k×ATR（盈亏比固定 2.5）。
        # k 默认 2.5（2026-08 回测：2.0 止损率 63% 偏紧被震荡误扫，2.5 均值/胜率↑、PF 略降）。
        k = float(get_param("mid_atr_stop_mult"))
        stop = round(close - k * atr, 4)
        take = round(close + 2.5 * k * atr, 4)
    else:
        # ATR 列缺失/NaN 时给保守兜底：-8% / +20%
        stop = round(close * 0.92, 4)
        take = round(close * 1.20, 4)

    anchor_score = float(score.iloc[anchor])
    triggers = [
        "锚点日综合评分 " + str(round(anchor_score, 1)) + " 分（上穿" + str(int(score_threshold)) + "）",
        "收盘突破锚点日高点（确认入场）",
        "偏离MA20 " + str(round(dev * 100, 1)) + "%（≤" + str(round(dev_max * 100, 1)) + "% 不追高）",
    ]
    if bool(latest.get("STRONG_BUY", False)):
        triggers.append("强烈买入区间（≥80 分）")
    ma_bull = (latest.get("MA5", 0) > latest.get("MA10", 0)) and \
              (latest.get("MA10", 0) > latest.get("MA20", 0))
    if ma_bull:
        triggers.append("均线多头排列")

    idx = result.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]

    return {
        "horizon": "mid",
        "strategy": "中线综合",
        "fusion_score": round(anchor_score / 2.0, 2),  # 0~100 → 0~50 量纲对齐
        "buy_price": round(close, 2),
        "stop_loss": round(stop, 2),
        "take_profit": round(take, 2),
        "trade_date": trade_date,
        "triggers": triggers,
    }


# ─────────────────────────────────────────────
# 长线：MA60/MA120 长趋势 + 低波动 + 回撤过滤（3 个月+）
# ─────────────────────────────────────────────

def scan_long_term(df: pd.DataFrame, min_rows: int = 250,
                   score_threshold: float = 2.0,
                   vol_max: float = 0.35,
                   max_drawdown_from_high: float = 0.40) -> Optional[dict]:
    """
    长线趋势信号（持仓 3 个月以上）。

    连续打分（满分 3，>= score_threshold 命中）：
      1) MA60 > MA120 且收盘价站上 MA120（长趋势向上）        +1.0
      2) MA120 斜率向上（高于 21 个交易日前）                  +1.0
      3) 60 日年化波动率 < vol_max（默认 35%，低波）           +0.5
      4) 距 250 日最高点回撤 < max_drawdown_from_high（默认 40%）+0.5

    止损：MA120 × long_ma_stop_mult（默认 0.95，跌破长期趋势线离场）
    止盈：入场价 × 1.5（长线目标位，实战中建议移动止盈）
    """
    if df is None or len(df) < min_rows:
        return None

    d = df[["close"]].copy()
    close = d["close"].astype(float)

    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    if math.isnan(ma60.iloc[-1]) or math.isnan(ma120.iloc[-1]):
        return None

    last_close = float(close.iloc[-1])
    last_ma60 = float(ma60.iloc[-1])
    last_ma120 = float(ma120.iloc[-1])

    score = 0.0
    triggers: list[str] = []

    # 1) 长趋势向上
    if last_ma60 > last_ma120 and last_close > last_ma120:
        score += 1.0
        triggers.append("MA60>MA120 且股价站上 MA120")

    # 2) MA120 斜率向上
    if len(ma120) > 21 and not math.isnan(ma120.iloc[-22]):
        if last_ma120 > float(ma120.iloc[-22]):
            score += 1.0
            triggers.append("MA120 斜率向上")

    # 3) 低波动
    ret = close.pct_change().dropna()
    if len(ret) >= 60:
        vol_annual = float(ret.tail(60).std() * math.sqrt(252))
        if math.isfinite(vol_annual) and vol_annual < vol_max:
            score += 0.5
            triggers.append(f"60 日年化波动率 {vol_annual * 100:.0f}%（低波）")

    # 4) 距高点回撤受控
    high_250 = float(close.tail(250).max())
    if high_250 > 0:
        dd = 1.0 - last_close / high_250
        if dd < max_drawdown_from_high:
            score += 0.5
            triggers.append(f"距 250 日高点回撤 {dd * 100:.0f}%（受控）")

    if score < score_threshold or not triggers:
        return None

    idx = close.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]

    from config.strategy_params import get_param
    m = float(get_param("long_ma_stop_mult"))

    return {
        "horizon": "long",
        "strategy": "长线趋势",
        "fusion_score": round(score / 3.0 * 50.0, 2),  # 0~3 → 0~50 量纲对齐
        "buy_price": round(last_close, 2),
        "stop_loss": round(last_ma120 * m, 2),
        "take_profit": round(last_close * 1.5, 2),
        "trade_date": trade_date,
        "triggers": triggers,
    }
