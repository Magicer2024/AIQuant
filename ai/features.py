"""
ai/features.py —— 特征工程

为AI模型构建技术指标特征：
  - 趋势特征：MA斜率、价格位置
  - 动量特征：RSI、MACD、CCI
  - 波动特征：ATR、布林带宽度
  - 量价特征：量比、OBV、资金流入
  - 形态特征：阳线占比、振幅
"""

import numpy as np
import pandas as pd


def calc_ma_slope(series: pd.Series, period: int = 5) -> float:
    """计算均线斜率（角度）"""
    if len(series) < period:
        return 0.0
    ma = series.rolling(period).mean()
    x = np.arange(period)
    y = ma.dropna().values[-period:]
    if len(y) < 2:
        return 0.0
    slope = np.polyfit(x, y, 1)[0]
    return slope


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI指标"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """MACD指标，返回 DIF, DEA, MACD柱"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_hist = (dif - dea) * 2
    return dif, dea, macd_hist


def calc_bollinger(close: pd.Series, period: int = 20) -> tuple:
    """布林带，返回 中轨、上轨、下轨、宽度"""
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    width = (upper - lower) / (ma + 1e-10)
    return ma, upper, lower, width


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR真实波幅"""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """OBV能量潮"""
    obv = [0]
    for i in range(1, len(close)):
        if close.iloc[i] > close.iloc[i-1]:
            obv.append(obv[-1] + volume.iloc[i])
        elif close.iloc[i] < close.iloc[i-1]:
            obv.append(obv[-1] - volume.iloc[i])
        else:
            obv.append(obv[-1])
    return pd.Series(obv, index=close.index)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    从原始K线数据构建特征矩阵
    :param df: DataFrame，包含 open/high/low/close/volume
    :return: 特征DataFrame
    """
    df = df.copy()
    df = df.sort_values("date").reset_index(drop=True)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # ── 趋势特征 ──────────────────────────────
    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    # 价格在各均线之上/之下的标志
    df["above_ma5"] = (close > df["ma5"]).astype(int)
    df["above_ma20"] = (close > df["ma20"]).astype(int)
    df["above_ma60"] = (close > df["ma60"]).astype(int)

    # 均线斜率
    df["ma5_slope"] = df["ma5"].rolling(5).apply(lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True)
    df["ma20_slope"] = df["ma20"].rolling(10).apply(lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True)

    # ── 动量特征 ──────────────────────────────
    df["rsi_14"] = calc_rsi(close, 14)
    df["rsi_6"] = calc_rsi(close, 6)

    dif, dea, macd_hist = calc_macd(close)
    df["macd_dif"] = dif
    df["macd_dea"] = dea
    df["macd_hist"] = macd_hist

    # ── 波动特征 ──────────────────────────────
    df["atr_14"] = calc_atr(high, low, close, 14)
    _, upper, lower, width = calc_bollinger(close)
    df["boll_width"] = width
    df["boll_pos"] = (close - lower) / (upper - lower + 1e-10)  # 价格在布林带中的位置

    # ── 量价特征 ──────────────────────────────
    df["vol_ratio_5"] = volume / volume.rolling(5).mean()
    df["vol_ratio_20"] = volume / volume.rolling(20).mean()
    df["obv"] = calc_obv(close, volume)
    df["obv_slope"] = df["obv"].diff(5)

    # 价量相关性（5日）
    df["price_vol_corr"] = close.rolling(5).corr(volume)

    # ── 形态特征 ──────────────────────────────
    df["body_pct"] = (close - df["open"]).abs() / (high - low + 1e-10)  # 实体占比
    df["upper_shadow"] = (high - close.where(close > df["open"], df["open"])) / (high - low + 1e-10)
    df["lower_shadow"] = (close.where(close > df["open"], df["open"]) - low) / (high - low + 1e-10)

    df["green_ratio_5"] = (close > df["open"]).rolling(5).mean()  # 近5日阳线比例
    df["green_ratio_10"] = (close > df["open"]).rolling(10).mean()

    df["amplitude_5"] = ((high.rolling(5).max() - low.rolling(5).min()) / low.rolling(5).min()) * 100

    # ── 收益率特征 ──────────────────────────────
    df["ret_1"] = close.pct_change(1) * 100
    df["ret_5"] = close.pct_change(5) * 100
    df["ret_10"] = close.pct_change(10) * 100
    df["ret_20"] = close.pct_change(20) * 100

    # 涨跌幅绝对值
    df["abs_ret_5"] = df["ret_5"].abs()

    # ── 滞后特征 ──────────────────────────────
    for lag in [1, 2, 3]:
        df[f"close_lag_{lag}"] = close.shift(lag) / close - 1
        df[f"volume_lag_{lag}"] = volume.shift(lag) / volume - 1

    return df


def build_label(df: pd.DataFrame, forward_days: int = 5, threshold: float = 0.03) -> pd.Series:
    """
    构建标签：未来N日收益率是否超过阈值
    :param forward_days: 前瞻天数
    :param threshold: 涨幅阈值（如0.03=3%）
    :return: 1=上涨超过阈值, 0=震荡, -1=下跌超过阈值
    """
    future_ret = df["close"].shift(-forward_days) / df["close"] - 1
    label = pd.Series(0, index=df.index)
    label[future_ret > threshold] = 1
    label[future_ret < -threshold] = -1
    return label


FEATURE_COLUMNS = [
    "above_ma5", "above_ma20", "above_ma60",
    "ma5_slope", "ma20_slope",
    "rsi_14", "rsi_6",
    "macd_dif", "macd_dea", "macd_hist",
    "atr_14", "boll_width", "boll_pos",
    "vol_ratio_5", "vol_ratio_20", "obv_slope", "price_vol_corr",
    "body_pct", "upper_shadow", "lower_shadow",
    "green_ratio_5", "green_ratio_10", "amplitude_5",
    "ret_1", "ret_5", "ret_10", "ret_20", "abs_ret_5",
    "close_lag_1", "close_lag_2", "close_lag_3",
    "volume_lag_1", "volume_lag_2", "volume_lag_3",
]
