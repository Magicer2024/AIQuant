"""
strategy/rec_filters.py —— 推荐过滤器（趋势闸门 + 质量过滤）

供 core/sync.py::recalc_all_scores 与 tools/eval_short_engine.py 共用。
提供两种粒度：
  - *_series(...)  返回逐日布尔序列（向量化，适合全历史/回测掩码）
  - trend_gate/passes_quality  标量包装（取最新交易日，适合最新日评估）

设计原则：数据缺失时优雅降级——趋势闸门样本不足返回 False（宁缺毋滥），
质量过滤缺 total_shares 时跳过市值项（不因缺数据误杀）。
"""
from __future__ import annotations

import math
import pandas as pd

from config.strategy_params import (SHORT_TREND_GATE, QUALITY_FILTER, CHASE_FILTER,
                                    EXTENSION_FILTER, RSI_SWEET_SPOT, get_param)

# ST/退市名称关键词（大写归一后匹配，"退" 为中文不受 upper 影响）
_ST_KEYWORDS = ("ST", "退")


def _amount_series(df: pd.DataFrame) -> pd.Series:
    """成交额序列：优先用 amount 列，缺失时用 volume×close 估算"""
    amount = df.get("amount")
    if amount is None:
        amount = df["volume"].astype(float) * df["close"].astype(float)
    return amount.astype(float)


def _is_st_name(name) -> bool:
    if not name:
        return False
    nm = str(name).upper()
    return any(kw in nm for kw in _ST_KEYWORDS)


# ─────────────────────────────────────────────
# 趋势闸门：站上均线且均线向上
# ─────────────────────────────────────────────

def trend_gate_series(df: pd.DataFrame, cfg: dict = None) -> pd.Series:
    """
    逐日趋势闸门布尔序列：收盘价 > MA(ma) 且 MA(ma)_今日 >= MA(ma)_{slope_lookback}日前。
    禁用时全 True；样本不足或数据异常处为 False。
    """
    cfg = cfg or SHORT_TREND_GATE
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)

    if cfg is SHORT_TREND_GATE:
        # 默认配置走参数覆盖层（优化器采纳建议后即时生效）
        ma_n = int(get_param("trend_gate_ma"))
        slope_lb = int(get_param("trend_gate_slope_lookback"))
    else:
        ma_n = int(cfg.get("ma", 20))
        slope_lb = int(cfg.get("slope_lookback", 5))
    close = df["close"].astype(float)
    ma = close.rolling(ma_n).mean()
    gate = (close > ma) & (ma >= ma.shift(slope_lb))
    return gate.reindex(df.index).fillna(False).astype(bool)


def trend_gate(df: pd.DataFrame, cfg: dict = None) -> bool:
    """最新交易日是否通过趋势闸门"""
    s = trend_gate_series(df, cfg)
    return bool(s.iloc[-1]) if len(s) else False


# ─────────────────────────────────────────────
# 质量过滤：ST 剔除 + 流动性 + 市值区间
# ─────────────────────────────────────────────

def quality_series(name, df: pd.DataFrame, total_shares: float = None,
                   cfg: dict = None) -> pd.Series:
    """
    逐日质量过滤布尔序列：
      - 名称含 ST/*ST/退 → 整只全 False
      - 近20日日均成交额 >= min_amt20
      - 有 total_shares 时，市值(total_shares×当日收盘) 落在 [min_mktcap, max_mktcap]；
        缺 total_shares 时跳过市值项（优雅降级）
    禁用时全 True。
    """
    cfg = cfg or QUALITY_FILTER
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)

    # ST/退市：整只否决
    if cfg.get("exclude_st", True) and _is_st_name(name):
        return pd.Series(False, index=df.index)

    ok = pd.Series(True, index=df.index)

    # 流动性：近20日均成交额
    min_amt20 = cfg.get("min_amt20") or 0
    if min_amt20 > 0:
        amt20 = _amount_series(df).rolling(20).mean()
        ok &= (amt20 >= min_amt20)

    # 市值区间（缺 total_shares 跳过）
    if total_shares and math.isfinite(float(total_shares)) and float(total_shares) > 0:
        mktcap = float(total_shares) * df["close"].astype(float)
        min_mc = cfg.get("min_mktcap")
        max_mc = cfg.get("max_mktcap")
        if min_mc:
            ok &= (mktcap >= min_mc)
        if max_mc:
            ok &= (mktcap <= max_mc)

    return ok.reindex(df.index).fillna(False).astype(bool)


def passes_quality(name, df: pd.DataFrame, total_shares: float = None,
                   latest_close: float = None, cfg: dict = None) -> bool:
    """
    最新交易日是否通过质量过滤。
    latest_close 仅为兼容旧签名保留；市值按当日收盘序列计算，无需外部传入。
    """
    s = quality_series(name, df, total_shares, cfg)
    return bool(s.iloc[-1]) if len(s) else False


# ─────────────────────────────────────────────
# 追高否决：连板天数 / 涨停打开 / 近3日急涨
# ─────────────────────────────────────────────

def chase_filter_series(df: pd.DataFrame, cfg: dict = None) -> pd.Series:
    """
    逐日追高否决布尔序列（True=可推荐）。任一项命中即 False：
      1. 当日连续涨停 >= max_consecutive_limit（默认 3 连板当日否决）
      2. 近 cooldown_days 个交易日内出现过 >= cooldown_consecutive 连板
         （连板后遗症冷却期：三连板后的第4天起数日仍是高位，000815 8-05 即此情形）
      3. 当日盘中触板（high >= prev_close*(1+9.5%)）但收盘未封住（涨停打开，分歧/出货形态）
      4. 近3日涨幅 >= max_ret_3d（默认 25%）
    数据缺失时按"不否决"处理（保留信号），与质量过滤的降级方向相反：
    追高否决宁可漏杀不可误杀——它防的是高位接盘，缺数据时不臆断。
    禁用时全 True。仅支持单股票 df（与 trend_gate_series 同约定）。
    """
    cfg = cfg or CHASE_FILTER
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)

    close = df["close"].astype(float)
    if "pct_change" in df.columns:
        pct = df["pct_change"].astype(float)
    else:
        pct = close.pct_change() * 100.0
    high = df["high"].astype(float) if "high" in df.columns else close

    ok = pd.Series(True, index=df.index)

    # 连续涨停天数（涨停段内计数）
    limit_pct = float(cfg.get("limit_pct", 9.8))
    is_limit = (pct >= limit_pct).astype(int)
    seg = (is_limit != is_limit.shift(1)).cumsum()
    streak = is_limit.groupby(seg).cumsum()

    # 1) 当日连板否决
    max_consec = int(cfg.get("max_consecutive_limit", 3))
    if max_consec >= 1:
        ok &= streak < max_consec

    # 2) 连板后遗症冷却期：近 cooldown_days 个交易日内出现过 >= cooldown_consecutive 连板
    cooldown_consec = int(cfg.get("cooldown_consecutive", 3))
    cooldown_days = int(cfg.get("cooldown_days", 0))
    if cooldown_consec >= 1 and cooldown_days >= 1:
        recent_max = streak.rolling(cooldown_days, min_periods=1).max()
        ok &= recent_max < cooldown_consec

    # 3) 涨停打开：盘中触板但收盘未封住
    if cfg.get("reject_limit_open", True):
        prev_close = close.shift(1)
        touched = high >= prev_close * (1 + (limit_pct - 0.3) / 100.0)  # 盘中 >= +9.5%
        sealed = pct >= limit_pct                                        # 收盘封住
        limit_open = touched & (~sealed) & prev_close.notna()
        ok &= ~limit_open.fillna(False)

    # 4) 近3日涨幅
    max_ret3 = float(cfg.get("max_ret_3d", 0.25))
    if max_ret3 > 0:
        ret3 = close / close.shift(3) - 1.0
        ok &= ret3.fillna(0.0) < max_ret3

    return ok.reindex(df.index).fillna(False).astype(bool)


def chase_filter(df: pd.DataFrame, cfg: dict = None) -> bool:
    """最新交易日是否通过追高否决（True=可推荐）"""
    s = chase_filter_series(df, cfg)
    return bool(s.iloc[-1]) if len(s) else False


# ─────────────────────────────────────────────
# 扩展度否决：价相对 MA20 偏离过大（挂高位、易均值回归）
# ─────────────────────────────────────────────

def extension_filter_series(df: pd.DataFrame, cfg: dict = None) -> pd.Series:
    """
    逐日扩展度硬否决布尔序列（True=可推荐）。

    当日 close 相对 MA20 偏离 > max_pct_above_ma20（默认 12%）→ 否决。
    背景（tools/diag_short_reco.py）：候选均值偏离 MA20 +6.3%、>8% 占 26.9%，
    高位组（>MA20 8%）T+1 OC 48.3%/+0.04% 劣于低位组（<MA20 2%）52.2%/+0.28%。

    与 chase_filter_series 同约定：仅支持单股票 df；禁用时全 True；
    MA20 样本不足（<20 行）或 close/MA20 缺失时按"不否决"处理（缺数据不臆断，
    与追高否决的降级方向一致——宁可漏杀不可误杀）。
    """
    cfg = cfg or EXTENSION_FILTER
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)

    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    max_above = float(cfg.get("max_pct_above_ma20", 0.12))
    above = (close / ma20 - 1.0).fillna(0.0)   # MA20 缺失 → 偏离 0 → 不否决
    ok = above <= max_above
    return ok.reindex(df.index).fillna(False).astype(bool)


def extension_filter(df: pd.DataFrame, cfg: dict = None) -> bool:
    """最新交易日是否通过扩展度否决（True=可推荐）"""
    s = extension_filter_series(df, cfg)
    return bool(s.iloc[-1]) if len(s) else False


# ─────────────────────────────────────────────
# RSI 甜区过滤器：RSI(14) ∈ [lo, hi]（剔除弱势/超买）
# ─────────────────────────────────────────────

def rsi_sweet_spot_series(df: pd.DataFrame, cfg: dict = None) -> pd.Series:
    """
    逐日 RSI 甜区布尔序列（True=可推荐）。

    仅当当日 RSI(14) 落在 [lo, hi]（默认 [40,65]）内才可推荐：
      - RSI < lo（弱势）：阴跌途中假回踩，容易接飞刀；
      - RSI > hi（超买）：接反弹高位，均值回归尾部。
    背景（tools/backtest_enhance.py E3）：v2 候选叠加该过滤后 T+1 胜率 48.6%→49.3%、
    均值 +0.100%→+0.123%，是唯一稳赚的入场过滤器（E1 宽度跳过证伪、E2 缩量回踩仅边际）。

    与 chase/extension 同约定：仅支持单股票 df；禁用时全 True。
    RSI 样本不足（前 13 日 NaN）→ False（宁可漏杀不可误杀，与回测掩码口径一致：
    backtest_enhance/backtest_exit 中 NaN 参与比较同为 False）。

    ⚠ 算法与 tools/backtest_enhance.py::rsi14 / tools/backtest_exit.py::rsi14 完全一致
    （简单 rolling(14) 均值，非 Wilder ewm），勿改用 strategy/indicators.calc_rsi，
    否则回测/推荐口径分裂会污染 diag 复验。
    """
    cfg = cfg or RSI_SWEET_SPOT
    if df is None or len(df) == 0:
        return pd.Series(dtype=bool)
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)

    lo = float(cfg.get("lo", 40.0))
    hi = float(cfg.get("hi", 65.0))
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.clip(lower=1e-9)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    ok = (rsi >= lo) & (rsi <= hi)
    return ok.reindex(df.index).fillna(False).astype(bool)


def rsi_sweet_spot(df: pd.DataFrame, cfg: dict = None) -> bool:
    """最新交易日是否通过 RSI 甜区（True=可推荐）"""
    s = rsi_sweet_spot_series(df, cfg)
    return bool(s.iloc[-1]) if len(s) else False
