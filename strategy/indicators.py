"""
技术指标计算模块
包含适合中线交易的主要技术指标：
- 趋势类：MA、EMA、MACD、DMI
- 震荡类：RSI、KDJ、CCI
- 波动类：布林带、ATR
- 成交量：OBV、VWAP、资金流向
"""
import pandas as pd
import numpy as np


def calc_ma(close: pd.Series, periods: list = [5, 10, 20, 60]) -> pd.DataFrame:
    """简单移动平均线"""
    result = pd.DataFrame(index=close.index)
    for p in periods:
        result[f"MA{p}"] = close.rolling(window=p).mean()
    return result


def calc_ema(close: pd.Series, periods: list = [12, 26]) -> pd.DataFrame:
    """指数移动平均线"""
    result = pd.DataFrame(index=close.index)
    for p in periods:
        result[f"EMA{p}"] = close.ewm(span=p, adjust=False).mean()
    return result


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """
    MACD指标
    中线策略重点：MACD金叉/死叉，柱状图颜色变化
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_hist = (dif - dea) * 2
    return pd.DataFrame({
        "MACD_DIF": dif,
        "MACD_DEA": dea,
        "MACD_HIST": macd_hist
    }, index=close.index)


def calc_rsi(close: pd.Series, periods: list = [6, 14, 24]) -> pd.DataFrame:
    """
    RSI相对强弱指标
    中线策略重点：RSI > 70 超买，RSI < 30 超卖
    """
    result = pd.DataFrame(index=close.index)
    for p in periods:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=p - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=p - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        result[f"RSI{p}"] = 100 - (100 / (1 + rs))
    return result


def calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """
    KDJ随机指标
    中线策略重点：K、D、J三线金叉/死叉，超买超卖区域
    """
    low_min = low.rolling(window=n).min()
    high_max = high.rolling(window=n).max()
    rsv = (close - low_min) / (high_max - low_min + 1e-10) * 100

    k = pd.Series(index=close.index, dtype=float)
    d = pd.Series(index=close.index, dtype=float)

    k.iloc[0] = 50.0
    d.iloc[0] = 50.0
    for i in range(1, len(close)):
        k.iloc[i] = (m1 - 1) / m1 * k.iloc[i - 1] + 1 / m1 * rsv.iloc[i]
        d.iloc[i] = (m2 - 1) / m2 * d.iloc[i - 1] + 1 / m2 * k.iloc[i]

    j = 3 * k - 2 * d
    return pd.DataFrame({"KDJ_K": k, "KDJ_D": d, "KDJ_J": j}, index=close.index)


def calc_boll(close: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """
    布林带
    中线策略重点：价格突破上下轨，带宽收窄后突破
    """
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    bandwidth = (upper - lower) / mid * 100
    return pd.DataFrame({
        "BOLL_UPPER": upper,
        "BOLL_MID": mid,
        "BOLL_LOWER": lower,
        "BOLL_WIDTH": bandwidth
    }, index=close.index)


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    ATR平均真实范围（波动率）
    用于止损设置和仓位管理
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean().rename("ATR")


def calc_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    OBV能量潮（On-Balance Volume）
    中线策略重点：OBV与价格背离信号
    """
    direction = np.sign(close.diff().fillna(0))
    obv = (direction * volume).cumsum()
    return obv.rename("OBV")


def calc_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    """成交量加权平均价格（滚动）"""
    typical_price = (high + low + close) / 3
    vwap = (typical_price * volume).rolling(window=period).sum() / volume.rolling(window=period).sum()
    return vwap.rename("VWAP")


def calc_cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    """
    CCI顺势指标
    中线策略重点：CCI超过±100时有趋势信号
    """
    typical_price = (high + low + close) / 3
    ma = typical_price.rolling(window=period).mean()
    md = typical_price.rolling(window=period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    cci = (typical_price - ma) / (0.015 * md)
    return cci.rename("CCI")


def calc_dmi(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14, signal: int = 6) -> pd.DataFrame:
    """
    DMI趋向指标（ADX/+DI/-DI）
    中线策略重点：+DI上穿-DI为买入信号，ADX > 25 趋势明显
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = np.where((high - prev_high) > (prev_low - low),
                       np.maximum(high - prev_high, 0), 0)
    minus_dm = np.where((prev_low - low) > (high - prev_high),
                        np.maximum(prev_low - low, 0), 0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    plus_dm_s = pd.Series(plus_dm, index=close.index).rolling(window=period).mean()
    minus_dm_s = pd.Series(minus_dm, index=close.index).rolling(window=period).mean()

    plus_di = 100 * plus_dm_s / atr
    minus_di = 100 * minus_dm_s / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.rolling(window=signal).mean()

    return pd.DataFrame({
        "DMI_PDI": plus_di,
        "DMI_MDI": minus_di,
        "DMI_ADX": adx
    }, index=close.index)


def calc_dpo(close: pd.Series, n: int = 20) -> pd.DataFrame:
    """DPO 去价格趋势震荡"""
    ma = close.rolling(window=n).mean()
    dpo = close - ma.shift(int(n / 2) + 1)
    return pd.DataFrame({"DPO": dpo}, index=close.index)


def calc_bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """布林带"""
    ma = close.rolling(window=n).mean()
    std = close.rolling(window=n).std()
    upper = ma + k * std
    lower = ma - k * std
    pct_b = (close - lower) / (upper - lower).clip(lower=1e-9)
    bandwidth = (upper - lower) / ma.clip(lower=1e-9)
    return pd.DataFrame({
        "BB_UPPER": upper, "BB_LOWER": lower, "BB_MA": ma,
        "BB_PCT_B": pct_b, "BB_BANDWIDTH": bandwidth,
    }, index=close.index)


def calc_historical_volatility(close: pd.Series, n: int = 20) -> pd.DataFrame:
    """历史波动率（年化）"""
    log_ret = np.log((close / close.shift(1)).clip(lower=1e-12))
    hv = log_ret.rolling(window=n).std() * np.sqrt(252)
    return pd.DataFrame({f"HV_{n}": hv}, index=close.index)


def calc_volume_ratio(volume: pd.Series, period: int = 5) -> pd.Series:
    """
    量比：当日成交量 / 过去N日平均量
    量比 > 2 为放量，量比 < 0.5 为缩量
    """
    avg_volume = volume.shift(1).rolling(window=period).mean()
    vr = volume / avg_volume
    return vr.rename("VOLUME_RATIO")


def calc_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标，返回含完整指标的 DataFrame
    :param df: 必须包含 open, high, low, close, volume 列
    """
    result = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 趋势指标
    ma = calc_ma(close, [5, 10, 20, 60])
    result = pd.concat([result, ma], axis=1)

    ema = calc_ema(close, [12, 26])
    result = pd.concat([result, ema], axis=1)

    macd = calc_macd(close)
    result = pd.concat([result, macd], axis=1)

    dmi = calc_dmi(high, low, close)
    result = pd.concat([result, dmi], axis=1)

    # 震荡指标
    rsi = calc_rsi(close)
    result = pd.concat([result, rsi], axis=1)

    kdj = calc_kdj(high, low, close)
    result = pd.concat([result, kdj], axis=1)

    result["CCI"] = calc_cci(high, low, close)

    # 波动指标
    boll = calc_boll(close)
    result = pd.concat([result, boll], axis=1)

    result["ATR"] = calc_atr(high, low, close)

    # 成交量指标
    result["OBV"] = calc_obv(close, volume)
    result["VWAP"] = calc_vwap(high, low, close, volume)
    result["VOLUME_RATIO"] = calc_volume_ratio(volume)

    return result


if __name__ == "__main__":
    from data_fetcher import get_stock_history
    df = get_stock_history("000001", start_date="20230101")
    df_ind = calc_all_indicators(df)
    print(df_ind[["close", "MA20", "MACD_DIF", "RSI14", "KDJ_K", "BOLL_MID"]].tail(10))
