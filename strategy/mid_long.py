"""
strategy/mid_long.py —— 中线 / 长线选股信号扫描
================================================

补齐推荐体系的三周期（horizon）维度：

- 短期（short, 1~10 交易日）：core/sync.py 现有 5 信号融合（strategies.py）
- 中期（mid,   10~60 交易日）：本模块 scan_mid_term —— 复活 strategy/strategy.py
  的中线综合评分（0~100 分 + ATR 止损止盈）
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
# 中线：复活 strategy/strategy.py 综合评分（1~8 周持仓）
# ─────────────────────────────────────────────

def scan_mid_term(df: pd.DataFrame, min_rows: int = 80,
                  score_threshold: float = 65.0) -> Optional[dict]:
    """
    中线综合评分信号（趋势/动量/量能/布林多因子 0~100 分）。

    命中条件：最新交易日 COMPOSITE_SCORE >= score_threshold 且 BUY_SIGNAL
    （综合分上穿阈值当日，避免同一趋势重复出信号）。

    Returns:
        None 或 dict:
          horizon/strategy/fusion_score(0~50)/buy_price/stop_loss/take_profit/
          trade_date/triggers(中文命中描述)
    """
    if df is None or len(df) < min_rows:
        return None

    # 延迟导入，避免与 strategy/strategy.py 形成循环依赖
    from strategy.strategy import run_strategy

    try:
        result = run_strategy(df, "composite")
    except Exception:
        return None
    if result is None or len(result) < 2:
        return None

    latest = result.iloc[-1]
    score = float(latest.get("COMPOSITE_SCORE", 0) or 0)
    if not math.isfinite(score) or score < score_threshold:
        return None
    if not bool(latest.get("BUY_SIGNAL", False)):
        return None

    close = float(latest["close"])
    stop = float(latest.get("STOP_LOSS", 0) or 0)
    take = float(latest.get("TAKE_PROFIT", 0) or 0)
    if not math.isfinite(stop) or not math.isfinite(take) or stop <= 0 or take <= close:
        # ATR 列缺失/NaN 时给保守兜底：-8% / +20%
        stop = round(close * 0.92, 4)
        take = round(close * 1.20, 4)

    # 命中的中文理由（基于最新指标状态）
    triggers = ["中线综合评分 " + str(round(score, 1)) + " 分（≥" + str(int(score_threshold)) + "）"]
    if bool(latest.get("STRONG_BUY", False)):
        triggers.append("强烈买入区间（≥80 分）")
    ma_bull = (latest.get("MA5", 0) > latest.get("MA10", 0)) and \
              (latest.get("MA10", 0) > latest.get("MA20", 0))
    if ma_bull:
        triggers.append("均线多头排列")
    if latest.get("DMI_ADX", 0) and float(latest.get("DMI_ADX") or 0) > 25:
        triggers.append("ADX 趋势强劲")

    idx = result.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]

    return {
        "horizon": "mid",
        "strategy": "中线综合",
        "fusion_score": round(score / 2.0, 2),  # 0~100 → 0~50 量纲对齐
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

    止损：MA120 × 0.99（跌破长期趋势线离场）
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

    return {
        "horizon": "long",
        "strategy": "长线趋势",
        "fusion_score": round(score / 3.0 * 50.0, 2),  # 0~3 → 0~50 量纲对齐
        "buy_price": round(last_close, 2),
        "stop_loss": round(last_ma120 * 0.99, 2),
        "take_profit": round(last_close * 1.5, 2),
        "trade_date": trade_date,
        "triggers": triggers,
    }
