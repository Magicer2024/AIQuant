"""
中线交易策略模块
适合持仓周期：1~8周（5~40个交易日）

策略体系：
1. 均线多头排列策略（趋势跟踪）
2. MACD底背离+金叉策略（反转确认）
3. 布林带突破策略（波动率突破）
4. 综合评分策略（多因子打分）
"""
import pandas as pd
import numpy as np
from strategy.indicators import calc_all_indicators


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────

def _cross_up(series_a: pd.Series, series_b: pd.Series) -> pd.Series:
    """上穿：series_a 从下方穿越 series_b"""
    return (series_a.shift(1) < series_b.shift(1)) & (series_a >= series_b)


def _cross_down(series_a: pd.Series, series_b: pd.Series) -> pd.Series:
    """下穿：series_a 从上方穿越 series_b"""
    return (series_a.shift(1) > series_b.shift(1)) & (series_a <= series_b)


# ─────────────────────────────────────────────
# 策略1：均线多头排列策略
# ─────────────────────────────────────────────

def strategy_ma_trend(df: pd.DataFrame) -> pd.DataFrame:
    """
    均线多头排列策略
    买入条件：
      - MA5 > MA10 > MA20 > MA60（多头排列）
      - 价格站上MA20
      - 成交量放大（量比 > 1.5）
      - MA10 上穿 MA20（金叉触发）
    卖出条件：
      - MA5 下穿 MA20（死叉）
      - 价格跌破MA20 且连续2日收在MA20下方
    """
    d = df.copy()

    # 多头排列判断
    bullish_array = (d["MA5"] > d["MA10"]) & (d["MA10"] > d["MA20"]) & (d["MA20"] > d["MA60"])

    # 金叉信号
    ma_golden_cross = _cross_up(d["MA10"], d["MA20"])

    # 量能放大
    volume_amplify = d["VOLUME_RATIO"] > 1.5

    # 价格站上MA20
    above_ma20 = d["close"] > d["MA20"]

    # 买入信号
    d["BUY_SIGNAL"] = ma_golden_cross & bullish_array & volume_amplify
    d["BUY_SCORE"] = (
        bullish_array.astype(int) * 30 +
        ma_golden_cross.astype(int) * 25 +
        volume_amplify.astype(int) * 20 +
        above_ma20.astype(int) * 25
    )

    # 死叉卖出信号
    ma_death_cross = _cross_down(d["MA5"], d["MA20"])
    below_ma20_2days = (d["close"] < d["MA20"]) & (d["close"].shift(1) < d["MA20"].shift(1))

    d["SELL_SIGNAL"] = ma_death_cross | below_ma20_2days
    d["STRATEGY"] = "MA趋势"
    return d


# ─────────────────────────────────────────────
# 策略2：MACD底背离+金叉策略
# ─────────────────────────────────────────────

def strategy_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    MACD中线策略
    买入条件：
      - DIF 上穿 DEA（MACD金叉）
      - 金叉发生在零轴附近或零轴下方（更可靠）
      - RSI14 在 40~65 区间（不超买）
    卖出条件：
      - DIF 下穿 DEA（MACD死叉）
      - MACD柱状图连续收缩（顶背离迹象）
    """
    d = df.copy()

    # MACD金叉
    macd_golden = _cross_up(d["MACD_DIF"], d["MACD_DEA"])
    # 金叉位置：零轴下方更可靠
    below_zero = d["MACD_DIF"] < 0
    near_zero = d["MACD_DIF"].abs() < d["close"] * 0.005  # DIF绝对值小于股价的0.5%

    # RSI过滤
    rsi_healthy = (d["RSI14"] > 35) & (d["RSI14"] < 68)

    # MACD柱状图由负转正（动能转换）
    hist_turn_positive = (d["MACD_HIST"] > 0) & (d["MACD_HIST"].shift(1) <= 0)

    d["BUY_SIGNAL"] = macd_golden & rsi_healthy
    d["BUY_SCORE"] = (
        macd_golden.astype(int) * 35 +
        below_zero.astype(int) * 20 +
        near_zero.astype(int) * 15 +
        rsi_healthy.astype(int) * 20 +
        hist_turn_positive.astype(int) * 10
    )

    # 死叉卖出
    macd_death = _cross_down(d["MACD_DIF"], d["MACD_DEA"])
    # 顶背离：价格创新高但MACD没有创新高
    price_new_high = d["close"] > d["close"].rolling(20).max().shift(1)
    macd_no_new_high = d["MACD_DIF"] < d["MACD_DIF"].rolling(20).max().shift(1)
    top_divergence = price_new_high & macd_no_new_high

    d["SELL_SIGNAL"] = macd_death | top_divergence
    d["STRATEGY"] = "MACD金叉"
    return d


# ─────────────────────────────────────────────
# 策略3：布林带突破策略
# ─────────────────────────────────────────────

def strategy_boll_breakout(df: pd.DataFrame) -> pd.DataFrame:
    """
    布林带突破策略
    买入条件：
      - 带宽经历收窄后突然放大（布林带挤压后突破）
      - 价格向上突破上轨或中轨
      - 成交量明显放大
    卖出条件：
      - 价格跌破中轨
      - 价格触及上轨后回落（超买回归）
    """
    d = df.copy()

    # 布林带收窄（带宽低于过去20日平均带宽的70%）
    boll_squeeze = d["BOLL_WIDTH"] < d["BOLL_WIDTH"].rolling(20).mean() * 0.7
    # 价格突破上轨
    break_upper = (d["close"] > d["BOLL_UPPER"]) & (d["close"].shift(1) <= d["BOLL_UPPER"].shift(1))
    # 价格突破中轨（更保守）
    break_mid = _cross_up(d["close"], d["BOLL_MID"])
    # 成交量放大
    vol_amplify = d["VOLUME_RATIO"] > 1.8

    # 前期收窄
    prev_squeeze = boll_squeeze.shift(1).fillna(False) | boll_squeeze.shift(2).fillna(False) | boll_squeeze.shift(3).fillna(False)

    d["BUY_SIGNAL"] = (break_upper | break_mid) & vol_amplify & prev_squeeze
    d["BUY_SCORE"] = (
        break_upper.astype(int) * 30 +
        break_mid.astype(int) * 15 +
        vol_amplify.astype(int) * 25 +
        prev_squeeze.astype(int) * 30
    )

    # 跌破中轨卖出
    break_mid_down = _cross_down(d["close"], d["BOLL_MID"])
    # 触及上轨后连续回落
    touch_upper = d["close"] >= d["BOLL_UPPER"] * 0.98
    fall_from_upper = touch_upper.shift(1) & (d["close"] < d["close"].shift(1))

    d["SELL_SIGNAL"] = break_mid_down | fall_from_upper
    d["STRATEGY"] = "布林带突破"
    return d


# ─────────────────────────────────────────────
# 综合评分策略（核心策略）
# ─────────────────────────────────────────────

def strategy_composite(df_with_indicators: pd.DataFrame) -> pd.DataFrame:
    """
    综合多因子评分策略（中线核心策略）
    评分系统 0~100 分，60分以上可考虑买入

    因子权重：
    - 趋势因子（均线）: 30%
    - 动量因子（MACD/RSI）: 30%
    - 成交量因子: 20%
    - 波动率因子（布林带）: 20%
    """
    d = df_with_indicators.copy()

    scores = pd.Series(0.0, index=d.index)
    signals = []

    # ── 趋势因子（30分）──
    # MA多头排列 (15分)
    ma_bull = (d["MA5"] > d["MA10"]) & (d["MA10"] > d["MA20"]) & (d["MA20"] > d["MA60"])
    scores += ma_bull.astype(float) * 15
    # 价格站上MA20 (10分)
    scores += (d["close"] > d["MA20"]).astype(float) * 10
    # MA5 上穿 MA10 (5分)
    scores += _cross_up(d["MA5"], d["MA10"]).astype(float) * 5

    # ── 动量因子（30分）──
    # MACD金叉 (15分)
    macd_golden = _cross_up(d["MACD_DIF"], d["MACD_DEA"])
    scores += macd_golden.astype(float) * 15
    # RSI健康区间 35~65 (10分)
    rsi_ok = (d["RSI14"] > 35) & (d["RSI14"] < 65)
    scores += rsi_ok.astype(float) * 10
    # KDJ金叉 (5分)
    kdj_golden = _cross_up(d["KDJ_K"], d["KDJ_D"])
    scores += kdj_golden.astype(float) * 5

    # ── 成交量因子（20分）──
    # 量比 > 1.5 (10分)
    scores += (d["VOLUME_RATIO"] > 1.5).astype(float) * 10
    # OBV上涨趋势 (10分)
    obv_rising = d["OBV"] > d["OBV"].rolling(10).mean()
    scores += obv_rising.astype(float) * 10

    # ── 布林带因子（20分）──
    # 价格在布林带中轨以上 (10分)
    scores += (d["close"] > d["BOLL_MID"]).astype(float) * 10
    # 布林带不超买（未触及上轨） (10分)
    not_overbought = d["close"] < d["BOLL_UPPER"] * 0.97
    scores += not_overbought.astype(float) * 10

    # ── ADX趋势强度加分 ──
    adx_strong = d["DMI_ADX"] > 25
    scores += adx_strong.astype(float) * 5  # 额外5分

    d["COMPOSITE_SCORE"] = scores.clip(0, 100)

    # 信号生成
    d["BUY_SIGNAL"] = (d["COMPOSITE_SCORE"] >= 65) & (d["COMPOSITE_SCORE"].shift(1) < 65)
    d["HOLD_SIGNAL"] = d["COMPOSITE_SCORE"] >= 55
    d["SELL_SIGNAL"] = (d["COMPOSITE_SCORE"] < 45) & (d["COMPOSITE_SCORE"].shift(1) >= 45)

    # 强烈买入信号（分数≥80）
    d["STRONG_BUY"] = d["COMPOSITE_SCORE"] >= 80

    d["STRATEGY"] = "综合评分"
    return d


# ─────────────────────────────────────────────
# 止损止盈管理
# ─────────────────────────────────────────────

def calc_stop_loss_take_profit(df: pd.DataFrame, atr_multiplier_stop: float = 2.0, risk_reward: float = 2.5) -> pd.DataFrame:
    """
    基于ATR计算动态止损止盈位
    :param atr_multiplier_stop: 止损 = 入场价 - N * ATR
    :param risk_reward: 盈亏比，止盈 = 入场价 + risk_reward * 风险
    """
    d = df.copy()
    d["STOP_LOSS"] = d["close"] - atr_multiplier_stop * d["ATR"]
    risk = atr_multiplier_stop * d["ATR"]
    d["TAKE_PROFIT"] = d["close"] + risk_reward * risk
    d["STOP_PCT"] = (atr_multiplier_stop * d["ATR"] / d["close"] * 100).round(2)
    d["TARGET_PCT"] = (risk_reward * atr_multiplier_stop * d["ATR"] / d["close"] * 100).round(2)
    return d


# ─────────────────────────────────────────────
# 完整策略分析入口
# ─────────────────────────────────────────────

def run_strategy(df: pd.DataFrame, strategy_name: str = "composite") -> pd.DataFrame:
    """
    运行指定策略
    :param df: 原始OHLCV数据
    :param strategy_name: 策略名称 composite/ma_trend/macd/boll
    :return: 含指标和信号的完整DataFrame
    """
    df_ind = calc_all_indicators(df)

    if strategy_name == "ma_trend":
        result = strategy_ma_trend(df_ind)
    elif strategy_name == "macd":
        result = strategy_macd(df_ind)
    elif strategy_name == "boll":
        result = strategy_boll_breakout(df_ind)
    else:  # composite
        result = strategy_composite(df_ind)

    result = calc_stop_loss_take_profit(result)
    return result


def get_latest_signal(df_strategy: pd.DataFrame) -> dict:
    """获取最新的交易信号"""
    latest = df_strategy.iloc[-1]
    prev = df_strategy.iloc[-2] if len(df_strategy) > 1 else latest

    strategy = df_strategy.get("STRATEGY", pd.Series(["综合评分"])).iloc[-1]
    score = float(latest.get("COMPOSITE_SCORE", 0))

    if score >= 80:
        action = "强烈买入"
        color = "#ff4d4d"
    elif score >= 65:
        action = "建议买入"
        color = "#ff7875"
    elif score >= 55:
        action = "持有观望"
        color = "#ffa940"
    elif score >= 45:
        action = "谨慎持有"
        color = "#fadb14"
    elif score >= 30:
        action = "建议卖出"
        color = "#73d13d"
    else:
        action = "立即减仓"
        color = "#40a9ff"

    return {
        "date": str(df_strategy.index[-1].date()),
        "close": float(latest["close"]),
        "action": action,
        "color": color,
        "score": round(score, 1),
        "buy_signal": bool(latest.get("BUY_SIGNAL", False)),
        "sell_signal": bool(latest.get("SELL_SIGNAL", False)),
        "strong_buy": bool(latest.get("STRONG_BUY", False)),
        "stop_loss": round(float(latest.get("STOP_LOSS", 0)), 2),
        "take_profit": round(float(latest.get("TAKE_PROFIT", 0)), 2),
        "stop_pct": round(float(latest.get("STOP_PCT", 0)), 2),
        "target_pct": round(float(latest.get("TARGET_PCT", 0)), 2),
        "rsi14": round(float(latest.get("RSI14", 50)), 1),
        "macd_dif": round(float(latest.get("MACD_DIF", 0)), 4),
        "kdj_k": round(float(latest.get("KDJ_K", 50)), 1),
        "kdj_d": round(float(latest.get("KDJ_D", 50)), 1),
    }


if __name__ == "__main__":
    from data_fetcher import get_stock_history
    df = get_stock_history("000001", start_date="20230101")
    result = run_strategy(df, "composite")
    signal = get_latest_signal(result)
    print("最新信号:", signal)
    buy_dates = result[result["BUY_SIGNAL"] == True]
    print(f"\n买入信号出现 {len(buy_dates)} 次：")
    print(buy_dates[["close", "COMPOSITE_SCORE", "STOP_LOSS", "TAKE_PROFIT"]].tail(5))
