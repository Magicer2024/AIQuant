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

from config.strategy_params import SHORT_TREND_GATE, QUALITY_FILTER

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
