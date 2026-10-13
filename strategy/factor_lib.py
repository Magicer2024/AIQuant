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

pd.set_option('future.no_silent_downcasting', True)

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
    if hi - lo < 1e-9:
        return pd.Series(0.5, index=values.index)
    result = (values - lo) / (hi - lo)
    return result.clip(lower=-0.5, upper=1.5)


def _norm_fundamental_factor(values: pd.Series) -> pd.Series:
    """基本面因子归一化：中心化后截断到[-1,1]"""
    median = values.median()
    mad = (values - median).abs().median()
    if mad < 1e-9:
        return pd.Series(0.0, index=values.index)
    return ((values - median) / (mad * 1.4826)).clip(-1, 1)


def _norm_sentiment_factor(values: pd.Series) -> pd.Series:
    """情绪/流动性因子归一化：保留极端值区分度"""
    lower, upper = values.quantile(0.01), values.quantile(0.99)
    if upper - lower < 1e-9:
        return pd.Series(0.5, index=values.index)
    result = (values - lower) / (upper - lower)
    return result.clip(lower=-0.5, upper=1.5)


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

def _want(wanted: Optional[List[str]], *names: str) -> bool:
    """wanted=None 表示全量；否则该组任一因子被请求才算。"""
    return wanted is None or any(n in wanted for n in names)


# ── 分组 builder：每个只算 wanted 命中的列，表达式与原全量路径逐字一致 ──
# （归一化是按列独立的分位数变换，故单列按需计算与全量结果必然相同）

def _add_trend_factors(result: pd.DataFrame, df: pd.DataFrame,
                       wanted: Optional[List[str]]) -> None:
    close = df["close"]
    if _want(wanted, "MA5_偏离", "MA10_偏离", "MA20_偏离",
             "MA60_偏离", "MA_多头强度"):
        # 5 个均线因子共享一次 calc_ma（多头强度依赖全部 4 条均线）
        mas = calc_ma(close, periods=[5, 10, 20, 60])
        if _want(wanted, "MA5_偏离"):
            result["MA5_偏离"] = (close - mas["MA5"]) / mas["MA5"].clip(lower=1e-9)
        if _want(wanted, "MA10_偏离"):
            result["MA10_偏离"] = (close - mas["MA10"]) / mas["MA10"].clip(lower=1e-9)
        if _want(wanted, "MA20_偏离"):
            result["MA20_偏离"] = (close - mas["MA20"]) / mas["MA20"].clip(lower=1e-9)
        if _want(wanted, "MA60_偏离"):
            result["MA60_偏离"] = (close - mas["MA60"]) / mas["MA60"].clip(lower=1e-9)
        if _want(wanted, "MA_多头强度"):
            result["MA_多头强度"] = (
                ((mas["MA5"] - mas["MA10"]) / mas["MA10"].clip(lower=1e-9)).clip(lower=0) * 0.5
                + ((mas["MA10"] - mas["MA20"]) / mas["MA20"].clip(lower=1e-9)).clip(lower=0) * 0.3
                + ((mas["MA20"] - mas["MA60"]) / mas["MA60"].clip(lower=1e-9)).clip(lower=0) * 0.2
            )

    if _want(wanted, "MACD_DIF", "MACD_DEA", "MACD_HIST", "MACD_金叉距离"):
        macd = calc_macd(close)
        if _want(wanted, "MACD_DIF"):
            result["MACD_DIF"] = macd["MACD_DIF"] / close
        if _want(wanted, "MACD_DEA"):
            result["MACD_DEA"] = macd["MACD_DEA"] / close
        if _want(wanted, "MACD_HIST"):
            result["MACD_HIST"] = macd["MACD_HIST"] / close
        if _want(wanted, "MACD_金叉距离"):
            result["MACD_金叉距离"] = (macd["MACD_DIF"] - macd["MACD_DEA"]).abs()

    high, low = df["high"], df["low"]
    if _want(wanted, "CCI"):
        result["CCI"] = calc_cci(high, low, close)
    if _want(wanted, "DPO"):
        dpo = calc_dpo(close)
        result["DPO"] = dpo["DPO"] / close.clip(lower=1e-9)
    if _want(wanted, "ADX", "PDI", "MDI"):
        dmi = calc_dmi(high, low, close)
        if _want(wanted, "ADX"):
            result["ADX"] = dmi["DMI_ADX"] / 100.0
        if _want(wanted, "PDI"):
            result["PDI"] = dmi["DMI_PDI"] / 100.0
        if _want(wanted, "MDI"):
            result["MDI"] = dmi["DMI_MDI"] / 100.0


def _add_momentum_factors(result: pd.DataFrame, df: pd.DataFrame,
                          wanted: Optional[List[str]]) -> None:
    close, high, low = df["close"], df["high"], df["low"]
    rsi_names = ["RSI_6", "RSI_9", "RSI_14", "RSI_21"]
    if _want(wanted, *rsi_names):
        rsi_all = calc_rsi(close, periods=[6, 9, 14, 21])
        for p, name in zip([6, 9, 14, 21], rsi_names):
            if _want(wanted, name):
                result[name] = rsi_all[f"RSI{p}"]

    for p in [6, 12, 24]:
        name = f"BIAS_{p}"
        if _want(wanted, name):
            ma_p = close.rolling(p).mean()
            result[name] = (close - ma_p) / ma_p.clip(lower=1e-9)

    for p in [10, 20]:
        name = f"MOM_{p}"
        if _want(wanted, name):
            result[name] = close - close.shift(p)

    if _want(wanted, "KDJ_K", "KDJ_D", "KDJ_J"):
        kdj = calc_kdj(high, low, close)
        if _want(wanted, "KDJ_K"):
            result["KDJ_K"] = kdj["KDJ_K"]
        if _want(wanted, "KDJ_D"):
            result["KDJ_D"] = kdj["KDJ_D"]
        if _want(wanted, "KDJ_J"):
            result["KDJ_J"] = kdj["KDJ_J"]

    for p in [3, 5, 10, 20]:
        name = f"RET_{p}D"
        if _want(wanted, name):
            result[name] = close.pct_change(p)


def _add_volatility_factors(result: pd.DataFrame, df: pd.DataFrame,
                            wanted: Optional[List[str]]) -> None:
    close, high, low = df["close"], df["high"], df["low"]
    if _want(wanted, "BB_PCT_B", "BB_BANDWIDTH"):
        bb = calc_bollinger(close)
        if _want(wanted, "BB_PCT_B"):
            result["BB_PCT_B"] = bb["BB_PCT_B"]
        if _want(wanted, "BB_BANDWIDTH"):
            result["BB_BANDWIDTH"] = bb["BB_BANDWIDTH"]
    if _want(wanted, "ATR_PCT"):
        atr_series = calc_atr(high, low, close)
        result["ATR_PCT"] = atr_series / close.clip(lower=1e-9)
    if _want(wanted, "HV_10"):
        result["HV_10"] = calc_historical_volatility(close, 10)["HV_10"]
    if _want(wanted, "HV_20"):
        result["HV_20"] = calc_historical_volatility(close, 20)["HV_20"]


def _add_volume_factors(result: pd.DataFrame, df: pd.DataFrame,
                        wanted: Optional[List[str]]) -> None:
    close, volume = df["close"], df["volume"]
    amount = df.get("amount", volume * close)
    for p in [5, 10, 20]:
        name = f"VOL_RATIO_{p}"
        if _want(wanted, name):
            vol_ma = volume.rolling(p).mean()
            result[name] = volume / vol_ma.clip(lower=1e-9)
    if _want(wanted, "OBV_偏离度"):
        obv = calc_obv(close, volume)
        obv_ma = obv.rolling(20).mean()
        obv_range = obv.rolling(20).max() - obv.rolling(20).min()
        result["OBV_偏离度"] = ((obv - obv_ma) / obv_range.clip(lower=1e-9)).clip(-1, 1)
    if _want(wanted, "VOL_波动率"):
        result["VOL_波动率"] = (
            volume.rolling(20).std() / volume.rolling(20).mean().clip(lower=1e-9))
    if _want(wanted, "VWAP_偏离"):
        vwap = (amount / volume.clip(lower=1e-9)).fillna(close)
        result["VWAP_偏离"] = (close - vwap) / close.clip(lower=1e-9)
    if _want(wanted, "AMOUNT_RATIO_20"):
        result["AMOUNT_RATIO_20"] = (
            amount / amount.rolling(20).mean().clip(lower=1e-9))


def _add_pattern_factors(result: pd.DataFrame, df: pd.DataFrame,
                         wanted: Optional[List[str]]) -> None:
    close, high, low = df["close"], df["high"], df["low"]
    if _want(wanted, "CONSEC_UP"):
        result["CONSEC_UP"] = _calc_consecutive(close, "up")
    if _want(wanted, "CONSEC_DOWN"):
        result["CONSEC_DOWN"] = _calc_consecutive(close, "down")
    if _want(wanted, "AMPLITUDE_5"):
        result["AMPLITUDE_5"] = (
            high.rolling(5).max() - low.rolling(5).min()) / close.rolling(5).mean()
    if _want(wanted, "AMPLITUDE_20"):
        result["AMPLITUDE_20"] = (
            high.rolling(20).max() - low.rolling(20).min()) / close.rolling(20).mean()
    if _want(wanted, "GAP_UP"):
        result["GAP_UP"] = ((low - df["high"].shift(1))
                            / df["high"].shift(1).clip(lower=1e-9) > 0.005).astype(float)
    if _want(wanted, "GAP_DOWN"):
        result["GAP_DOWN"] = ((df["low"].shift(1) - high)
                              / df["low"].shift(1).clip(lower=1e-9) > 0.005).astype(float)
    if _want(wanted, "HAMMER"):
        result["HAMMER"] = _detect_hammer(df["open"], high, low, close).astype(float)
    if _want(wanted, "ENGULF_BULL"):
        result["ENGULF_BULL"] = _detect_engulf(df, "bull").astype(float)
    if _want(wanted, "ENGULF_BEAR"):
        result["ENGULF_BEAR"] = _detect_engulf(df, "bear").astype(float)
    if _want(wanted, "MORNING_STAR"):
        result["MORNING_STAR"] = _detect_morning_star(df).astype(float)
    if _want(wanted, "EVENING_STAR"):
        result["EVENING_STAR"] = _detect_evening_star(df).astype(float)
    if _want(wanted, "DOUBLE_BOTTOM"):
        result["DOUBLE_BOTTOM"] = _detect_double_pattern(close, "bottom").astype(float)
    if _want(wanted, "DOUBLE_TOP"):
        result["DOUBLE_TOP"] = _detect_double_pattern(close, "top").astype(float)


def _add_liquidity_factors(result: pd.DataFrame, df: pd.DataFrame,
                          wanted: Optional[List[str]]) -> None:
    close, volume = df["close"], df["volume"]
    amount = df.get("amount", volume * close)
    if "turnover" in df.columns and _want(
            wanted, "TURNOVER", "TURNOVER_5MA", "TURNOVER_PCTL"):
        if _want(wanted, "TURNOVER"):
            result["TURNOVER"] = df["turnover"]
        if _want(wanted, "TURNOVER_5MA"):
            result["TURNOVER_5MA"] = df["turnover"].rolling(5).mean()
        if _want(wanted, "TURNOVER_PCTL"):
            result["TURNOVER_PCTL"] = df["turnover"].rolling(252).rank(pct=True)
    if _want(wanted, "AMIHUD_ILLIQ"):
        ret = close.pct_change()
        result["AMIHUD_ILLIQ"] = (ret.abs() / amount.clip(lower=1e-9)) * 1e8
    if _want(wanted, "PV_CORREL"):
        result["PV_CORREL"] = close.rolling(20).corr(volume)


def compute_all_factors(df: pd.DataFrame, index_close: pd.Series = None,
                        fundamental_data: dict = None,
                        only: Optional[List[str]] = None) -> pd.DataFrame:
    """
    批量计算价量因子。

    Args:
        df: OHLCV DataFrame, 含 open/high/low/close/volume/amount/turnover 列
        index_close: 大盘指数收盘价 Series（用于计算 RPS），可选
        fundamental_data: 基本面数据 dict，{factor_name: value}，可选
        only: 只计算给定因子名列表（T8 按需）：注册表外名称忽略，
              产出列按 only 顺序排列；None = 全量（旧签名行为与列顺序不变）

    Returns:
        归一化后的因子 DataFrame，index 与 df 对齐
    """
    # 去重保序 + 过滤注册表外名称（契约：未知因子忽略而非报错）
    wanted = None
    if only is not None:
        wanted = list(dict.fromkeys(n for n in only if n in FACTOR_REGISTRY))

    result = pd.DataFrame(index=df.index)
    _add_trend_factors(result, df, wanted)
    _add_momentum_factors(result, df, wanted)
    _add_volatility_factors(result, df, wanted)
    _add_volume_factors(result, df, wanted)
    _add_pattern_factors(result, df, wanted)
    _add_liquidity_factors(result, df, wanted)

    # ── 情绪（需指数数据）──
    if index_close is not None and _want(wanted, "RPS"):
        result["RPS"] = df["close"].pct_change(20) - index_close.pct_change(20)

    # ── 基本面插值 ──
    if fundamental_data:
        for fname, fval in fundamental_data.items():
            if fname in FACTOR_REGISTRY and _want(wanted, fname):
                result[fname] = fval

    # ── 归一化（按列独立：按需单列与全量结果逐值相同）──
    for col in result.columns:
        if col in FACTOR_REGISTRY:
            result[col] = normalize_factor(result[col], col)
    result = result.fillna(0)

    # 按需路径：按调用方给定顺序重排（数据源缺失的列自然不产出）
    if wanted is not None:
        result = result[[c for c in wanted if c in result.columns]]
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
    return is_hammer.infer_objects(copy=False).fillna(False)


def _detect_engulf(df: pd.DataFrame, direction: str) -> pd.Series:
    open_, close = df["open"], df["close"]
    prev_open, prev_close = open_.shift(1), close.shift(1)
    if direction == "bull":
        return ((prev_close < prev_open) & (close > open_) &
                (open_ <= prev_close) & (close >= prev_open)).infer_objects(copy=False).fillna(False)
    else:
        return ((prev_close > prev_open) & (close < open_) &
                (open_ >= prev_close) & (close <= prev_open)).infer_objects(copy=False).fillna(False)


def _detect_morning_star(df: pd.DataFrame) -> pd.Series:
    open_, close = df["open"], df["close"]
    body1 = close.shift(2) - open_.shift(2)
    body2 = (close.shift(1) - open_.shift(1)).abs()
    body3 = close - open_
    return ((body1 < 0) & (body2 < body1.abs() * 0.3) & (body3 > 0) &
            (close > (open_.shift(2) + close.shift(2)) / 2)).infer_objects(copy=False).fillna(False)


def _detect_evening_star(df: pd.DataFrame) -> pd.Series:
    open_, close = df["open"], df["close"]
    body1 = close.shift(2) - open_.shift(2)
    body2 = (close.shift(1) - open_.shift(1)).abs()
    body3 = close - open_
    return ((body1 > 0) & (body2 < body1.abs() * 0.3) & (body3 < 0) &
            (close < (open_.shift(2) + close.shift(2)) / 2)).infer_objects(copy=False).fillna(False)


def _detect_double_pattern(close: pd.Series, pattern: str) -> pd.Series:
    low_20 = close.rolling(20).min()
    high_20 = close.rolling(20).max()
    mid = (high_20 + low_20) / 2
    if pattern == "bottom":
        near_low = (close - low_20).abs() / low_20.clip(lower=1e-9) < 0.03
        prev_near = near_low.shift(10).infer_objects(copy=False).fillna(False)
        return (near_low & prev_near & (close > mid)).infer_objects(copy=False).fillna(False)
    else:
        near_high = (close - high_20).abs() / high_20.clip(lower=1e-9) < 0.03
        prev_near = near_high.shift(10).infer_objects(copy=False).fillna(False)
        return (near_high & prev_near & (close < mid)).infer_objects(copy=False).fillna(False)


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
