"""
backtest/indicator_engine.py
=============================
批量指标计算引擎

策略回测时一次性计算所有常用技术指标，避免在每个策略函数中重复计算。
"""

import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _safe_div(a, b):
    """安全除法，避免除零"""
    return a / b.clip(lower=1e-12)


def _rolling_min(series, n, min_periods=None):
    return series.rolling(n, min_periods=min_periods or n).min()


def _rolling_max(series, n, min_periods=None):
    return series.rolling(n, min_periods=min_periods or n).max()


def _rolling_mean(series, n, min_periods=None):
    return series.rolling(n, min_periods=min_periods or n).mean()


def _rolling_std(series, n, min_periods=None):
    return series.rolling(n, min_periods=min_periods or n).std()


def _shift(series, n):
    return series.shift(n)


# ─────────────────────────────────────────────────────────────────────────────
# 指标计算函数（纯函数，无状态）
# ─────────────────────────────────────────────────────────────────────────────

def calc_ma(close, n):
    return close.rolling(n).mean()


def calc_ema(close, n):
    return close.ewm(span=n, adjust=False).mean()


def calc_rsi(close, period=14):
    """RSI 相对强弱指数"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.clip(lower=1e-12)
    return 100 - (100 / (1 + rs))


def calc_kdj(high, low, close, n=9, m1=3, m2=3):
    """KDJ 随机指标"""
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = _safe_div(close - low_n, high_n - low_n) * 100
    k = rsv.ewm(com=(m1 - 1) / 2, adjust=False).mean()
    d = k.ewm(com=(m1 - 1) / 2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def calc_macd(close, fast=12, slow=26, signal=9):
    """MACD 指数平滑异同移动平均线"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_bar = (dif - dea) * 2
    return dif, dea, macd_bar


def calc_boll(close, period=20, k=2):
    """布林带"""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def calc_wr(high, low, close, period=14):
    """威廉指标 %R"""
    high_n = high.rolling(period).max()
    low_n = low.rolling(period).min()
    wr = _safe_div(high_n - close, high_n - low_n) * -100 + 100
    return wr


def calc_cci(high, low, close, period=14):
    """CCI 商品通道指标"""
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean())
    cci = _safe_div(tp - sma_tp, 0.015 * mad)
    return cci


def calc_atr(high, low, close, period=14):
    """ATR 平均真实波幅"""
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_obv(close, volume):
    """OBV 能量潮"""
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


def calc_vol_ratio(volume, period=5):
    """量比"""
    return _safe_div(volume, volume.rolling(period).mean())


def calc_amp(high, low):
    """当日振幅 (%)"""
    return _safe_div(high - low, low) * 100


# ─────────────────────────────────────────────────────────────────────────────
# 指标注册表
# 定义所有可用的指标：key → (显示名, 计算函数, 需要参数)
# ─────────────────────────────────────────────────────────────────────────────

class IndicatorRegistry:
    """指标注册表，提供指标定义和计算接口"""

    def __init__(self):
        self._indicators = {}
        self._register_defaults()

    def _register_defaults(self):
        c = self._reg  # 简化引用

        # ── 价格/均线 ──────────────────────────────────
        c("close",        "收盘价",       lambda df: df["close"],                    {})
        c("open",         "开盘价",       lambda df: df["open"],                     {})
        c("high",         "最高价",       lambda df: df["high"],                     {})
        c("low",          "最低价",       lambda df: df["low"],                      {})
        c("MA5",          "MA5",          lambda df: df["close"].rolling(5).mean(),    {})
        c("MA10",         "MA10",         lambda df: df["close"].rolling(10).mean(),  {})
        c("MA20",         "MA20",         lambda df: df["close"].rolling(20).mean(),  {})
        c("MA30",         "MA30",         lambda df: df["close"].rolling(30).mean(),  {})
        c("MA60",         "MA60",         lambda df: df["close"].rolling(60).mean(),  {})
        c("MA120",        "MA120",        lambda df: df["close"].rolling(120).mean(), {})
        c("EMA5",         "EMA5",         lambda df: df["close"].ewm(span=5, adjust=False).mean(),  {})
        c("EMA10",        "EMA10",        lambda df: df["close"].ewm(span=10, adjust=False).mean(), {})

        # ── 动量指标 ────────────────────────────────────
        c("RSI6",         "RSI(6)",      lambda df: calc_rsi(df["close"], 6),       {})
        c("RSI12",        "RSI(12)",      lambda df: calc_rsi(df["close"], 12),      {})
        c("RSI14",        "RSI(14)",      lambda df: calc_rsi(df["close"], 14),      {})
        c("KDJ_K",        "KDJ_K",        lambda df: calc_kdj(df["high"], df["low"], df["close"])[0], {})
        c("KDJ_D",        "KDJ_D",        lambda df: calc_kdj(df["high"], df["low"], df["close"])[1], {})
        c("KDJ_J",        "KDJ_J",        lambda df: calc_kdj(df["high"], df["low"], df["close"])[2], {})
        c("MACD",         "MACD",         lambda df: calc_macd(df["close"])[2],       {})
        c("MACD_DIF",     "DIF",          lambda df: calc_macd(df["close"])[0],       {})
        c("MACD_DEA",     "DEA",          lambda df: calc_macd(df["close"])[1],       {})
        c("威廉R",          "威廉%R(14)",   lambda df: calc_wr(df["high"], df["low"], df["close"]), {})
        c("CCI",          "CCI(14)",      lambda df: calc_cci(df["high"], df["low"], df["close"]), {})

        # ── 量能指标 ────────────────────────────────────
        c("volume",       "成交量",       lambda df: df["volume"],                   {})
        c("amount",       "成交额(万)",  lambda df: df["amount"] / 1e4,             {})
        c("vol_ma5",      "5日均量",      lambda df: df["volume"].rolling(5).mean(),{}),
        c("vol_ma10",     "10日均量",     lambda df: df["volume"].rolling(10).mean(),{}),
        c("vol_ma20",     "20日均量",     lambda df: df["volume"].rolling(20).mean(),{}),
        c("量比",          "量比(5日)",    lambda df: calc_vol_ratio(df["volume"], 5), {}),
        c("换手率",        "换手率(%)",   lambda df: df["turnover"],                {}),
        c("OBV",          "OBV",          lambda df: calc_obv(df["close"], df["volume"]), {}),

        # ── 波动/幅度指标 ────────────────────────────────
        c("振幅",          "当日振幅(%)",  lambda df: calc_amp(df["high"], df["low"]),{}),
        c("ATR",          "ATR(14)",      lambda df: calc_atr(df["high"], df["low"], df["close"]), {}),

        # ── 收益率类 ────────────────────────────────────
        c("ret_3d",       "3日收益率(%)", lambda df: df["close"].pct_change(3) * 100,          {}),
        c("ret_5d",       "5日收益率(%)", lambda df: df["close"].pct_change(5) * 100,          {}),
        c("ret_10d",      "10日收益率(%)",lambda df: df["close"].pct_change(10) * 100,         {}),
        c("ret_20d",      "20日收益率(%)",lambda df: df["close"].pct_change(20) * 100,         {}),
        c("ret_60d",      "60日收益率(%)",lambda df: df["close"].pct_change(60) * 100,         {}),

        # ── 布林带 ─────────────────────────────────────
        c("BOLL_UPPER",   "布林上轨",     lambda df: calc_boll(df["close"])[0],     {}),
        c("BOLL_MID",     "布林中轨",     lambda df: calc_boll(df["close"])[1],     {}),
        c("BOLL_LOWER",   "布林下轨",     lambda df: calc_boll(df["close"])[2],     {}),

        # ── 策略评分 ────────────────────────────────────
        c("fusion_score", "融合分",       lambda df: df["fusion_score"],             {}),
        c("vol_score",    "放量分",       lambda df: df["vol_score"],               {}),
        c("ma_score",     "均线分",       lambda df: df["ma_score"],                {}),
        c("diverge_score","背离分",       lambda df: df["diverge_score"],           {}),
        c("bottom_score", "抄底分",       lambda df: df["bottom_score"],            {}),
        c("whale_score",  "主力分",       lambda df: df["whale_score"],              {}),

        # ── 排名/分位类（须由 compute_indicators 补充）──
        c("total_mv",     "总市值(亿)",   lambda df: df["total_mv"],                {}),   # 由回测引擎计算
        c("circ_mv",      "流通市值(亿)", lambda df: df["circ_mv"],                 {}),   # 由回测引擎计算

        # ── 排名/分位类（须由 compute_indicators 补充）──
        c("vol_rank_pct",     "成交量分位(%)",  None, {}),   # 由 engine 填充
        c("amount_rank_pct",  "成交额分位(%)",  None, {}),   # 由 engine 填充

    def _reg(self, key, label, calc_fn, meta):
        """注册一个指标"""
        self._indicators[key] = {
            "key":       key,
            "label":     label,
            "calc_fn":   calc_fn,
            "category":  self._infer_category(key),
            "meta":      meta,
        }

    def _infer_category(self, key: str) -> str:
        """根据 key 推断分类"""
        if key in ("close", "open", "high", "low"):
            return "价格"
        # 动量判断须在均线之前（因为 MACD_DIF 含 "MA"）
        if key.startswith("RSI") or key.startswith("KDJ") or key.startswith("MACD") or key in ("威廉R", "CCI"):
            return "动量"
        if ("MA" in key and not key.startswith("MACD")) or "EMA" in key or "BOLL" in key:
            return "均线"
        if "vol" in key or key in ("volume", "amount", "量比", "换手率", "OBV"):
            return "量能"
        if "mv" in key or "市值" in key:
            return "市值"
        if "ret" in key or key in ("振幅", "ATR"):
            return "波动"
        if "score" in key:
            return "综合"
        if "rank" in key:
            return "排名"
        return "其他"

    def keys(self):
        return list(self._indicators.keys())

    def by_category(self) -> dict:
        """按分类返回指标列表"""
        cats = {}
        for k, v in self._indicators.items():
            cat = v["category"]
            cats.setdefault(cat, []).append(v)
        return cats

    def get(self, key) -> dict:
        return self._indicators.get(key)

    def calc(self, key: str, df: pd.DataFrame) -> pd.Series:
        """计算指定指标"""
        ind = self._indicators.get(key)
        if ind is None:
            raise KeyError(f"未知指标: {key}")
        fn = ind["calc_fn"]
        if fn is None:
            raise ValueError(f"指标 {key} 无预计算函数，需要在 engine 中填充")
        return fn(df)


# ─────────────────────────────────────────────────────────────────────────────
# 批量指标引擎
# ─────────────────────────────────────────────────────────────────────────────

# 全局注册表（单例）
_REGISTRY = IndicatorRegistry()


def get_registry() -> IndicatorRegistry:
    return _REGISTRY


def compute_indicators(df: pd.DataFrame, extra_ranks: bool = True) -> pd.DataFrame:
    """
    批量计算所有注册指标，添加到 df 中返回。

    计算过程：
    1. 调用所有有 calc_fn 的指标
    2. 填充预定义列（score 类）
    3. 计算截面排名/分位（可选）

    Args:
        df: 原始 OHLCV 数据
        extra_ranks: 是否计算截面排名分位

    Returns:
        添加了所有指标列的 DataFrame（copy）
    """
    out = df.copy().reset_index(drop=True)   # 统一为 RangeIndex，消除 index 对齐隐患

    # ── Step1: 计算所有可计算的指标 ──────────────────────
    # 记录原始列名，确保 trade_date 等基础列在最后仍然存在
    orig_cols = set(out.columns)
    for key, ind in _REGISTRY._indicators.items():
        fn = ind["calc_fn"]
        if fn is None:
            continue  # 排名类等需要后处理
        try:
            col = fn(out)
            if isinstance(col, pd.DataFrame):
                # 安全兜底：万一有指标返回 DataFrame，逐列取 .values 赋值
                for sub_k, sub_v in zip(["MACD", "MACD_DIF", "MACD_DEA"],
                                         [col.iloc[:, i] if i < len(col.columns) else col.iloc[:, 0]
                                          for i in range(3)]):
                    out[sub_k] = sub_v.values
            elif isinstance(col, tuple):
                # calc_macd / calc_boll / calc_kdj 返回 tuple of Series
                # 用 .values 赋值，避免 pandas 按 index 对齐时覆盖其他列
                assign_map = {
                    "MACD": 0, "MACD_DIF": 1, "MACD_DEA": 2,
                    "KDJ_K": 0, "KDJ_D": 1, "KDJ_J": 2,
                    "BOLL_UPPER": 0, "BOLL_MID": 1, "BOLL_LOWER": 2,
                }
                for sub_k, idx in assign_map.items():
                    if idx < len(col):
                        out[sub_k] = col[idx].values
            else:
                # 普通 Series：用 .values 赋值，防止 index 对齐污染其他列
                out[key] = col.values if hasattr(col, 'values') else col
        except Exception:
            pass  # 单个指标失败不影响整体

    # ── Step1b: 确保 trade_date 列存在（指标计算过程中可能被覆盖）──
    if 'trade_date' not in out.columns and 'trade_date' in orig_cols:
        raise RuntimeError("compute_indicators 意外丢失 trade_date 列，请检查指标计算逻辑")

    # ── Step2: 预定义列兜底（数据库已有 fusion_score 等）──
    score_cols = ["fusion_score", "vol_score", "ma_score",
                  "diverge_score", "bottom_score", "whale_score"]
    for col in score_cols:
        if col not in out.columns:
            out[col] = 0.0

    # ── Step3: 截面排名分位 ──────────────────────────────
    if extra_ranks:
        close_col = out["close"]
        for vol_key in ["volume", "amount"]:
            if vol_key in out.columns:
                rank_key = f"{vol_key}_rank_pct"
                out[rank_key] = (
                    out[vol_key].rank(ascending=False, pct=True) * 100
                )

    return out


def eval_condition(series: pd.Series,
                   cond_type: str,
                   comparator: str,
                   threshold,
                   period: int = None,
                   stat_method: str = None) -> pd.Series:
    """
    在一个指标序列上计算条件，返回 bool Series。

    Args:
        series:       指标序列（每日一个值）
        cond_type:    "point" | "all_n" | "any_n" | "stat_n"
        comparator:   "gt" | "gte" | "lt" | "lte" | "eq" | "cross_above" | "cross_below" | "between"
        threshold:    阈值（标量或同长度 Series）
        period:       时间窗口（用于 all_n/any_n/stat_n）
        stat_method:  统计方式（用于 stat_n）: "mean" | "max" | "min" | "sum" | "std"

    Returns:
        bool Series，每个位置 True=满足条件
    """
    import numbers

    def _compare(a, op, b):
        """逐元素比较，返回 bool Series"""
        if op == "gt":      return a > b
        if op == "gte":     return a >= b
        if op == "lt":      return a < b
        if op == "lte":     return a <= b
        if op == "eq":      return a == b
        if op == "between":
            low, high = b if isinstance(b, (list, tuple)) else (b, b)
            return (a >= low) & (a <= high)
        if op == "cross_above":
            prev = a.shift(1)
            return (a > b) & (prev <= b)
        if op == "cross_below":
            prev = a.shift(1)
            return (a < b) & (prev >= b)
        raise ValueError(f"未知比较符: {op}")

    if cond_type == "point":
        return _compare(series, comparator, threshold)

    elif cond_type in ("all_n", "any_n"):
        if period is None or period < 1:
            raise ValueError("all_n/any_n 需要指定 period")
        cond = _compare(series, comparator, threshold)
        if cond_type == "all_n":
            # 窗口内全部为 True
            return cond.rolling(period, min_periods=period).min() == 1
        else:  # any_n
            return cond.rolling(period, min_periods=1).max() == 1

    elif cond_type == "stat_n":
        if period is None or stat_method is None:
            raise ValueError("stat_n 需要指定 period 和 stat_method")
        if stat_method == "mean":
            stat = series.rolling(period).mean()
        elif stat_method == "max":
            stat = series.rolling(period).max()
        elif stat_method == "min":
            stat = series.rolling(period).min()
        elif stat_method == "sum":
            stat = series.rolling(period).sum()
        elif stat_method == "std":
            stat = series.rolling(period).std()
        else:
            raise ValueError(f"未知统计方法: {stat_method}")
        return _compare(stat, comparator, threshold)

    else:
        raise ValueError(f"未知条件类型: {cond_type}")
