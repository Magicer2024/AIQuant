"""
factor_lib.py —— 因子库
=======================
功能：
- 因子注册表 FACTOR_REGISTRY
- 因子批量计算 compute_all_factors()
- 动态归一化 normalize_factor()
- IC 计算与监控
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Callable
from strategy.indicators import (
    calc_ma, calc_macd, calc_rsi, calc_kdj, calc_cci, calc_dpo,
    calc_obv, calc_atr, calc_bollinger, calc_historical_volatility, calc_dmi,
)


# ── 归一化策略 ────────────────────────────────

def _norm_price_factor(values: pd.Series, factor_name: str) -> pd.Series:
    """价量因子归一化：保留原始语义，线性映射到[0,1]"""
    if "RSI" in factor_name or "KDJ" in factor_name:
        return (values / 100.0).clip(0, 1)
    if "CCI" in factor_name:
        return ((values + 200) / 400).clip(0, 1)
    lo, hi = values.quantile(0.01), values.quantile(0.99)
    clipped = values.clip(lower=lo, upper=hi)
    if hi - lo < 1e-9:
        return pd.Series(0.5, index=values.index)
    return ((clipped - lo) / (hi - lo)).clip(0, 1)


def _norm_fundamental_factor(values: pd.Series) -> pd.Series:
    """基本面因子归一化：中心化后截断到[-1,1]"""
    median = values.median()
    mad = (values - median).abs().median()
    if mad < 1e-9:
        return pd.Series(0.0, index=values.index)
    return ((values - median) / (mad * 1.4826)).clip(-1, 1)


def _norm_sentiment_factor(values: pd.Series) -> pd.Series:
    """情绪/流动性因子归一化：截断到[0,1]"""
    lower, upper = values.quantile(0.01), values.quantile(0.99)
    if upper - lower < 1e-9:
        return pd.Series(0.5, index=values.index)
    return ((values - lower) / (upper - lower)).clip(0, 1)


# ── 因子注册表 ─────────────────────────────────

FACTOR_REGISTRY = {
    # ===== 趋势类 =====
    "MA5_偏离":     {"category": "trend", "norm": "price"},
    "MA10_偏离":    {"category": "trend", "norm": "price"},
    "MA20_偏离":    {"category": "trend", "norm": "price"},
    "MA60_偏离":    {"category": "trend", "norm": "price"},
    "MA_多头强度":  {"category": "trend", "norm": "price"},
    "MACD_DIF":     {"category": "trend", "norm": "price"},
    "MACD_DEA":     {"category": "trend", "norm": "price"},
    "MACD_HIST":    {"category": "trend", "norm": "price"},
    "MACD_金叉距离": {"category": "trend", "norm": "price"},
    "ADX":          {"category": "trend", "norm": "price"},
    "PDI":          {"category": "trend", "norm": "price"},
    "MDI":          {"category": "trend", "norm": "price"},
    "DPO":          {"category": "trend", "norm": "price"},
    "CCI":          {"category": "trend", "norm": "price"},

    # ===== 动量类 =====
    "RSI_6":   {"category": "momentum", "norm": "price"},
    "RSI_9":   {"category": "momentum", "norm": "price"},
    "RSI_14":  {"category": "momentum", "norm": "price"},
    "RSI_21":  {"category": "momentum", "norm": "price"},
    "BIAS_6":  {"category": "momentum", "norm": "price"},
    "BIAS_12": {"category": "momentum", "norm": "price"},
    "BIAS_24": {"category": "momentum", "norm": "price"},
    "MOM_10":  {"category": "momentum", "norm": "price"},
    "MOM_20":  {"category": "momentum", "norm": "price"},
    "KDJ_K":   {"category": "momentum", "norm": "price"},
    "KDJ_D":   {"category": "momentum", "norm": "price"},
    "KDJ_J":   {"category": "momentum", "norm": "price"},
    "RET_3D":  {"category": "momentum", "norm": "price"},
    "RET_5D":  {"category": "momentum", "norm": "price"},
    "RET_10D": {"category": "momentum", "norm": "price"},
    "RET_20D": {"category": "momentum", "norm": "price"},

    # ===== 波动类 =====
    "BB_PCT_B":     {"category": "volatility", "norm": "price"},
    "BB_BANDWIDTH": {"category": "volatility", "norm": "price"},
    "ATR_PCT":      {"category": "volatility", "norm": "price"},
    "HV_10":        {"category": "volatility", "norm": "price"},
    "HV_20":        {"category": "volatility", "norm": "price"},

    # ===== 量能类 =====
    "VOL_RATIO_5":  {"category": "volume", "norm": "price"},
    "VOL_RATIO_10": {"category": "volume", "norm": "price"},
    "VOL_RATIO_20": {"category": "volume", "norm": "price"},
    "OBV_偏离度":   {"category": "volume", "norm": "price"},
    "VOL_波动率":    {"category": "volume", "norm": "price"},
    "VWAP_偏离":    {"category": "volume", "norm": "price"},
    "AMOUNT_RATIO_20": {"category": "volume", "norm": "price"},

    # ===== 形态类 =====
    "CONSEC_UP":      {"category": "pattern", "norm": "price"},
    "CONSEC_DOWN":    {"category": "pattern", "norm": "price"},
    "AMPLITUDE_5":    {"category": "pattern", "norm": "price"},
    "AMPLITUDE_20":   {"category": "pattern", "norm": "price"},
    "GAP_UP":         {"category": "pattern", "norm": "price"},
    "GAP_DOWN":       {"category": "pattern", "norm": "price"},
    "HAMMER":         {"category": "pattern", "norm": "price"},
    "ENGULF_BULL":    {"category": "pattern", "norm": "price"},
    "ENGULF_BEAR":    {"category": "pattern", "norm": "price"},
    "MORNING_STAR":   {"category": "pattern", "norm": "price"},
    "EVENING_STAR":   {"category": "pattern", "norm": "price"},
    "DOUBLE_BOTTOM":  {"category": "pattern", "norm": "price"},
    "DOUBLE_TOP":     {"category": "pattern", "norm": "price"},

    # ===== 流动性 =====
    "TURNOVER":         {"category": "liquidity", "norm": "sentiment"},
    "TURNOVER_5MA":     {"category": "liquidity", "norm": "sentiment"},
    "TURNOVER_PCTL":    {"category": "liquidity", "norm": "sentiment"},
    "AMIHUD_ILLIQ":     {"category": "liquidity", "norm": "sentiment"},
    "PV_CORREL":        {"category": "liquidity", "norm": "sentiment"},

    # ===== 基本面 =====
    "PE_TTM":           {"category": "fundamental", "norm": "fundamental"},
    "PB_LF":            {"category": "fundamental", "norm": "fundamental"},
    "ROE":              {"category": "fundamental", "norm": "fundamental"},
    "ROA":              {"category": "fundamental", "norm": "fundamental"},
    "PROFIT_YOY":       {"category": "fundamental", "norm": "fundamental"},
    "REVENUE_YOY":      {"category": "fundamental", "norm": "fundamental"},
    "GROSS_MARGIN":     {"category": "fundamental", "norm": "fundamental"},
    "NET_MARGIN":       {"category": "fundamental", "norm": "fundamental"},
    "DEBT_RATIO":       {"category": "fundamental", "norm": "fundamental"},
    "CURRENT_RATIO":    {"category": "fundamental", "norm": "fundamental"},
    "CFO_RATIO":        {"category": "fundamental", "norm": "fundamental"},
    "MKT_CAP":          {"category": "fundamental", "norm": "fundamental"},
    "MKT_CAP_PCTL":     {"category": "fundamental", "norm": "sentiment"},

    # ===== 情绪 =====
    "RPS":              {"category": "sentiment", "norm": "sentiment"},
    "SECTOR_RANK":      {"category": "sentiment", "norm": "sentiment"},
    "LHB_FLAG":         {"category": "sentiment", "norm": "sentiment"},
}


def get_factor_category(factor_name: str) -> str:
    return FACTOR_REGISTRY.get(factor_name, {}).get("category", "unknown")


def get_factor_norm(factor_name: str) -> str:
    return FACTOR_REGISTRY.get(factor_name, {}).get("norm", "price")


def normalize_factor(values: pd.Series, factor_name: str) -> pd.Series:
    """按因子类型选择归一化策略"""
    norm_type = get_factor_norm(factor_name)
    if norm_type == "fundamental":
        return _norm_fundamental_factor(values)
    elif norm_type == "sentiment":
        return _norm_sentiment_factor(values)
    else:
        return _norm_price_factor(values, factor_name)


def get_factor_names_by_category(category: str) -> list:
    """按类别获取因子名列表"""
    return [k for k, v in FACTOR_REGISTRY.items() if v["category"] == category]


def get_factor_names_by_categories(categories: list) -> list:
    """按多个类别获取因子名列表"""
    result = []
    for cat in categories:
        result.extend(get_factor_names_by_category(cat))
    return result


# ── 批量计算 ──────────────────────────────────

def compute_all_factors(df: pd.DataFrame, index_close: pd.Series = None,
                        fundamental_data: dict = None) -> pd.DataFrame:
    """
    批量计算所有价量因子。

    Args:
        df: OHLCV DataFrame, 含 open/high/low/close/volume/amount/turnover 列
        index_close: 大盘指数收盘价 Series（用于计算 RPS），可选
        fundamental_data: 基本面数据 dict，{factor_name: value}，可选

    Returns:
        所有因子归一化后的 DataFrame，index 与 df 对齐
    """
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    amount = df.get("amount", volume * close)
    result = pd.DataFrame(index=df.index)

    # ── 趋势类 ──
    mas = calc_ma(close, periods=[5, 10, 20, 60])
    result["MA5_偏离"] = (close - mas["MA5"]) / mas["MA5"].clip(lower=1e-9)
    result["MA10_偏离"] = (close - mas["MA10"]) / mas["MA10"].clip(lower=1e-9)
    result["MA20_偏离"] = (close - mas["MA20"]) / mas["MA20"].clip(lower=1e-9)
    result["MA60_偏离"] = (close - mas["MA60"]) / mas["MA60"].clip(lower=1e-9)
    result["MA_多头强度"] = (
        ((mas["MA5"] - mas["MA10"]) / mas["MA10"].clip(lower=1e-9)).clip(lower=0) * 0.5
        + ((mas["MA10"] - mas["MA20"]) / mas["MA20"].clip(lower=1e-9)).clip(lower=0) * 0.3
        + ((mas["MA20"] - mas["MA60"]) / mas["MA60"].clip(lower=1e-9)).clip(lower=0) * 0.2
    )

    macd = calc_macd(close)
    result["MACD_DIF"] = macd["MACD_DIF"] / close
    result["MACD_DEA"] = macd["MACD_DEA"] / close
    result["MACD_HIST"] = macd["MACD_HIST"] / close
    result["MACD_金叉距离"] = (macd["MACD_DIF"] - macd["MACD_DEA"]).abs()

    result["CCI"] = calc_cci(high, low, close)

    dpo = calc_dpo(close)
    result["DPO"] = dpo["DPO"] / close.clip(lower=1e-9)

    dmi = calc_dmi(high, low, close)
    result["ADX"] = dmi["DMI_ADX"] / 100.0
    result["PDI"] = dmi["DMI_PDI"] / 100.0
    result["MDI"] = dmi["DMI_MDI"] / 100.0

    # ── 动量类 ──
    rsi_all = calc_rsi(close, periods=[6, 9, 14, 21])
    for p in [6, 9, 14, 21]:
        result[f"RSI_{p}"] = rsi_all[f"RSI{p}"]

    for p in [6, 12, 24]:
        ma_p = close.rolling(p).mean()
        result[f"BIAS_{p}"] = (close - ma_p) / ma_p.clip(lower=1e-9)

    for p in [10, 20]:
        result[f"MOM_{p}"] = close - close.shift(p)

    kdj = calc_kdj(high, low, close)
    result["KDJ_K"] = kdj["KDJ_K"]
    result["KDJ_D"] = kdj["KDJ_D"]
    result["KDJ_J"] = kdj["KDJ_J"]

    for p in [3, 5, 10, 20]:
        result[f"RET_{p}D"] = close.pct_change(p)

    # ── 波动类 ──
    bb = calc_bollinger(close)
    result["BB_PCT_B"] = bb["BB_PCT_B"]
    result["BB_BANDWIDTH"] = bb["BB_BANDWIDTH"]

    atr_series = calc_atr(high, low, close)
    result["ATR_PCT"] = atr_series / close.clip(lower=1e-9)

    result["HV_10"] = calc_historical_volatility(close, 10)["HV_10"]
    result["HV_20"] = calc_historical_volatility(close, 20)["HV_20"]

    # ── 量能类 ──
    for p in [5, 10, 20]:
        vol_ma = volume.rolling(p).mean()
        result[f"VOL_RATIO_{p}"] = volume / vol_ma.clip(lower=1e-9)

    obv = calc_obv(close, volume)
    obv_ma = obv.rolling(20).mean()
    obv_range = obv.rolling(20).max() - obv.rolling(20).min()
    result["OBV_偏离度"] = ((obv - obv_ma) / obv_range.clip(lower=1e-9)).clip(-1, 1)

    result["VOL_波动率"] = volume.rolling(20).std() / volume.rolling(20).mean().clip(lower=1e-9)

    vwap = (amount / volume.clip(lower=1e-9)).fillna(close)
    result["VWAP_偏离"] = (close - vwap) / close.clip(lower=1e-9)

    result["AMOUNT_RATIO_20"] = amount / amount.rolling(20).mean().clip(lower=1e-9)

    # ── 形态类 ──
    result["CONSEC_UP"] = _calc_consecutive(close, "up")
    result["CONSEC_DOWN"] = _calc_consecutive(close, "down")
    result["AMPLITUDE_5"] = (high.rolling(5).max() - low.rolling(5).min()) / close.rolling(5).mean()
    result["AMPLITUDE_20"] = (high.rolling(20).max() - low.rolling(20).min()) / close.rolling(20).mean()
    result["GAP_UP"] = ((low - df["high"].shift(1)) / df["high"].shift(1).clip(lower=1e-9) > 0.005).astype(float)
    result["GAP_DOWN"] = ((df["low"].shift(1) - high) / df["low"].shift(1).clip(lower=1e-9) > 0.005).astype(float)
    result["HAMMER"] = _detect_hammer(df["open"], high, low, close).astype(float)
    result["ENGULF_BULL"] = _detect_engulf(df, "bull").astype(float)
    result["ENGULF_BEAR"] = _detect_engulf(df, "bear").astype(float)
    result["MORNING_STAR"] = _detect_morning_star(df).astype(float)
    result["EVENING_STAR"] = _detect_evening_star(df).astype(float)
    result["DOUBLE_BOTTOM"] = _detect_double_pattern(close, "bottom").astype(float)
    result["DOUBLE_TOP"] = _detect_double_pattern(close, "top").astype(float)

    # ── 流动性 ──
    if "turnover" in df.columns:
        result["TURNOVER"] = df["turnover"]
        result["TURNOVER_5MA"] = df["turnover"].rolling(5).mean()
        result["TURNOVER_PCTL"] = df["turnover"].rolling(252).rank(pct=True)
    ret = close.pct_change()
    result["AMIHUD_ILLIQ"] = (ret.abs() / amount.clip(lower=1e-9)) * 1e8
    result["PV_CORREL"] = close.rolling(20).corr(volume)

    # ── 情绪 ──
    if index_close is not None:
        result["RPS"] = close.pct_change(20) - index_close.pct_change(20)

    # ── 基本面插值 ──
    if fundamental_data:
        for fname, fval in fundamental_data.items():
            if fname in FACTOR_REGISTRY:
                result[fname] = fval

    # ── 归一化 ──
    for col in result.columns:
        if col in FACTOR_REGISTRY:
            result[col] = normalize_factor(result[col], col)
    result = result.fillna(0)

    return result


# ── K线形态识别 ───────────────────────────────

def _calc_consecutive(close: pd.Series, direction: str) -> pd.Series:
    diff = close.diff()
    if direction == "up":
        sign = (diff > 0).astype(int)
    else:
        sign = (diff < 0).astype(int)
    streaks = sign.groupby((sign != sign.shift()).cumsum()).cumsum()
    return (streaks / 10.0).clip(0, 1)


def _detect_hammer(open_: pd.Series, high: pd.Series, low: pd.Series,
                   close: pd.Series) -> pd.Series:
    body = (close - open_).abs()
    lower_shadow = (open_.combine(close, min) - low)
    upper_shadow = (high - open_.combine(close, max))
    is_hammer = (lower_shadow > body * 2) & (upper_shadow < body * 0.3)
    return is_hammer.fillna(False)


def _detect_engulf(df: pd.DataFrame, direction: str) -> pd.Series:
    open_, close = df["open"], df["close"]
    prev_open, prev_close = open_.shift(1), close.shift(1)
    if direction == "bull":
        return ((prev_close < prev_open) & (close > open_) &
                (open_ <= prev_close) & (close >= prev_open)).fillna(False)
    else:
        return ((prev_close > prev_open) & (close < open_) &
                (open_ >= prev_close) & (close <= prev_open)).fillna(False)


def _detect_morning_star(df: pd.DataFrame) -> pd.Series:
    open_, close = df["open"], df["close"]
    body1 = close.shift(2) - open_.shift(2)
    body2 = (close.shift(1) - open_.shift(1)).abs()
    body3 = close - open_
    return ((body1 < 0) & (body2 < body1.abs() * 0.3) & (body3 > 0) &
            (close > (open_.shift(2) + close.shift(2)) / 2)).fillna(False)


def _detect_evening_star(df: pd.DataFrame) -> pd.Series:
    open_, close = df["open"], df["close"]
    body1 = close.shift(2) - open_.shift(2)
    body2 = (close.shift(1) - open_.shift(1)).abs()
    body3 = close - open_
    return ((body1 > 0) & (body2 < body1.abs() * 0.3) & (body3 < 0) &
            (close < (open_.shift(2) + close.shift(2)) / 2)).fillna(False)


def _detect_double_pattern(close: pd.Series, pattern: str) -> pd.Series:
    low_20 = close.rolling(20).min()
    high_20 = close.rolling(20).max()
    mid = (high_20 + low_20) / 2
    if pattern == "bottom":
        near_low = (close - low_20).abs() / low_20.clip(lower=1e-9) < 0.03
        prev_near = near_low.shift(10).fillna(False)
        return (near_low & prev_near & (close > mid)).fillna(False)
    else:
        near_high = (close - high_20).abs() / high_20.clip(lower=1e-9) < 0.03
        prev_near = near_high.shift(10).fillna(False)
        return (near_high & prev_near & (close < mid)).fillna(False)


# ── 因子 IC 计算 ──────────────────────────────

def calc_factor_ic(factor_values: pd.Series, forward_returns: pd.Series) -> float:
    """计算单个因子的 IC (Rank IC)"""
    common = factor_values.notna() & forward_returns.notna()
    if common.sum() < 30:
        return 0.0
    return factor_values[common].rank().corr(forward_returns[common].rank())


def calc_all_ic(factor_df: pd.DataFrame, forward_returns: pd.Series) -> dict:
    """计算所有因子的 IC 值"""
    return {col: calc_factor_ic(factor_df[col], forward_returns) for col in factor_df.columns}


def filter_by_ic(factor_df: pd.DataFrame, forward_returns: pd.Series,
                 min_abs_ic: float = 0.02) -> list:
    """按 |IC| >= min_abs_ic 筛选因子，返回通过筛选的因子名列表"""
    ic = calc_all_ic(factor_df, forward_returns)
    return [name for name, v in ic.items() if abs(v) >= min_abs_ic]
