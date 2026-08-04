"""
strategies.py - 多策略选股框架
================================
包含 5 种不同的选股策略，每种都可独立运行，也可融合使用
返回格式：pd.DataFrame（含 BUY_SIGNAL / SELL_SIGNAL 列，供 Backtester 使用）

策略1: 放量突破型
策略2: 均线粘合型
策略3: 量价背离型
策略4: 抄底型
策略5: 主力建仓型

信号融合：多策略综合打分
"""


def _strategy_return_cols(d):
    """策略函数统一返回列（保留 open 供 T+1 回测引擎使用）"""
    cols = ["close", "volume", "BUY_SIGNAL", "SELL_SIGNAL", "BUY_SCORE", "STRATEGY"]
    if "open" in d.columns:
        cols.insert(1, "open")
    if "high" in d.columns:
        cols.append("high")
    if "low" in d.columns:
        cols.append("low")
    return d[[c for c in cols if c in d.columns]]

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple

# ===================================================================
# 策略1: 放量突破型
# ===================================================================

def strategy_volume_breakout(df: pd.DataFrame,
                            vol_factor: float = 1.5,
                            rise_3d: float = 0.02,
                            score_min: float = 1.0) -> pd.DataFrame:
    """
    放量 + 多头排列 + 3日涨幅
    BUY_SIGNAL = score >= score_min（默认2分触发）
    SELL_SIGNAL = 死叉或跌破MA5

    Args:
        df: OHLCV 数据（含 close, volume 列）
        vol_factor: 放量倍数
        rise_3d: 3日涨幅最小值
        score_min: 触发所需最少条件数

    Returns:
        含 BUY_SIGNAL, SELL_SIGNAL, BUY_SCORE 列的 DataFrame
    """
    d = df.copy()
    n = len(d)

    # ── 基础指标 ──────────────────────────────
    ma5  = d["close"].rolling(5).mean()
    ma10 = d["close"].rolling(10).mean()
    ma20 = d["close"].rolling(20).mean()
    vol5 = d["volume"].rolling(5).mean()

    # ── 连续打分（每项 0~1，满分 3）──────────
    # 1) 量比打分：1.5倍得0.5分，3倍得1.0分（上限）
    vr = d["volume"] / vol5.clip(lower=1e-9)          # 量比
    score_vol = vr.clip(upper=3.0) / 3.0              # 0~1

    # 2) 多头排列打分：强度连续化
    #    MA多头强度 = 价格在MA均线上方程度 + MA向上倾斜程度
    ma_score = (
        ((d["close"] - ma5) / ma5.clip(lower=1e-9)).clip(lower=0) * 0.5 +
        ((ma5 - ma10) / ma10.clip(lower=1e-9)).clip(lower=0) * 0.25 +
        ((ma10 - ma20) / ma20.clip(lower=1e-9)).clip(lower=0) * 0.25
    ).clip(upper=1.0)                                  # 0~1

    # 3) 3日涨幅打分：2%得0.5分，4%得1.0分（上限）
    ret3d = (d["close"] / d["close"].shift(3).clip(lower=1e-9) - 1).fillna(0)
    score_rise = (ret3d / 0.04).clip(lower=0, upper=1.0)   # 0~1

    # 综合 0~3
    buy_score = (score_vol + ma_score + score_rise).fillna(0)

    d["BUY_SCORE"]  = buy_score
    d["BUY_SIGNAL"] = (buy_score >= score_min) & (d.index >= d.index[19])  # score_min=2 在连续打分下对应约2/3条件满足

    # SELL: 跌破MA5 或 死叉
    sell_death = (ma5.shift(1) > ma10.shift(1)) & (ma5 <= ma10)
    sell_break = d["close"] < ma5
    d["SELL_SIGNAL"] = sell_death | sell_break
    d["STRATEGY"] = "放量突破"
    return _strategy_return_cols(d)


# ===================================================================
# 策略2: 均线粘合型
# ===================================================================

def strategy_ma_convergence(df: pd.DataFrame,
                            ma_narrow: float = 0.02) -> pd.DataFrame:
    """
    均线粘合后的向上突破
    BUY_SIGNAL: MA5/MA10/MA20 收窄后向上发散
    """
    d = df.copy()

    ma5  = d["close"].rolling(5).mean()
    ma10 = d["close"].rolling(10).mean()
    ma20 = d["close"].rolling(20).mean()
    vol5 = d["volume"].rolling(5).mean()

    # ── 连续打分（每项 0~1，满分 3）──────────
    # 1) 均线粘合度：聚合幅度/均线均值，越小越粘合
    ma_mid = (ma5 + ma10 + ma20) / 3
    ma_spread = pd.concat([ma5, ma10, ma20], axis=1).max(axis=1) - \
                pd.concat([ma5, ma10, ma20], axis=1).min(axis=1)
    score_conv = (1 - (ma_spread / ma_mid.clip(lower=1e-9)).clip(lower=0, upper=1)).fillna(0)  # 0~1

    # 2) 向上发散强度：价格和均线的关系
    score_up = (
        ((d["close"] - ma5) / ma5.clip(lower=1e-9)).clip(lower=0) * 0.5 +
        ((ma5 - ma20) / ma20.clip(lower=1e-9)).clip(lower=0) * 0.5
    ).clip(upper=1.0)                                     # 0~1

    # 3) 量能稳定度：接近均量最好
    vol_ratio = d["volume"] / vol5.clip(lower=1e-9)
    score_vol = (1 - (vol_ratio - 1).abs() / 0.5).clip(lower=0, upper=1.0).fillna(0)  # 0~1

    buy_score = (score_conv + score_up + score_vol).fillna(0)

    d["BUY_SCORE"]  = buy_score
    d["BUY_SIGNAL"]  = (buy_score >= 1.0) & (d.index >= d.index[19])
    d["SELL_SIGNAL"] = (ma5.shift(1) > ma10.shift(1)) & (ma5 <= ma10) | (d["close"] < ma20)
    d["STRATEGY"] = "均线粘合"
    return _strategy_return_cols(d)


# ===================================================================
# 策略3: 量价背离型
# ===================================================================

def strategy_price_volume_divergence(df: pd.DataFrame) -> pd.DataFrame:
    """
    量价背离检测（反转信号）
    BUY_SIGNAL: 价格新低但成交量未创新高（底背离）
    """
    d = df.copy()

    ma5  = d["close"].rolling(5).mean()
    ma10 = d["close"].rolling(10).mean()
    vol5  = d["volume"].rolling(5).mean()
    vol10 = d["volume"].rolling(10).mean()
    close = d["close"]

    # ── 连续打分（每项 0~1，满分 3）──────────
    # 1) 底背离强度：价格越低分越高，量能越缩分越高
    price_rank = (close - close.rolling(20).min().shift(1)) / \
                 (close.rolling(20).max().shift(1) - close.rolling(20).min().shift(1)).clip(lower=1e-9)
    vol_rank = (d["volume"] - d["volume"].rolling(20).min().shift(1)) / \
               (d["volume"].rolling(20).max().shift(1) - d["volume"].rolling(20).min().shift(1)).clip(lower=1e-9)
    score_div = ((1 - price_rank.clip(lower=0, upper=1)) + vol_rank.clip(lower=0, upper=1)) / 2  # 0~1

    # 2) 反弹强度：前3日跌幅越大分越高，当日涨幅越大分越高
    decline = (-close.diff(3) / close.shift(3).clip(lower=1e-9)).clip(lower=0)
    rebound = ((close - close.shift(1)) / close.shift(1).clip(lower=1e-9)).clip(lower=0)
    vol_surge = (d["volume"] / vol5.clip(lower=1e-9)).clip(lower=0)
    score_rebound = (decline * 0.4 + rebound * 0.3 + (vol_surge / 2) * 0.3).clip(lower=0, upper=1.0).fillna(0)  # 0~1

    # 3) 趋势逆转确认度：MA5上穿MA10
    ma_cross = ((ma5 - ma10) > 0) & ((ma5.shift(1) - ma10.shift(1)) <= 0)
    score_cross = ma_cross.astype(float).clip(upper=1.0)  # 0~1

    buy_score = (score_div + score_rebound + score_cross).fillna(0)

    d["BUY_SCORE"]  = buy_score
    d["BUY_SIGNAL"]  = (buy_score >= 1.0) & (d.index >= d.index[19])
    d["SELL_SIGNAL"] = close < d["close"].rolling(5).mean() * 0.95
    d["STRATEGY"] = "量价背离"
    return _strategy_return_cols(d)


# ===================================================================
# 策略4: 抄底型
# ===================================================================

def strategy_bottom_fishing(df: pd.DataFrame,
                            drop_threshold: float = -0.05) -> pd.DataFrame:
    """
    连续下跌后的放量反弹
    BUY_SIGNAL: 前期大幅下跌 → 底部放量反弹
    """
    d = df.copy()

    ma5  = d["close"].rolling(5).mean()
    vol5 = d["volume"].rolling(5).mean()

    # ── 连续打分（每项 0~1，满分 3）──────────
    # 1) 下跌深度：越深分越高
    low_10d = d["close"].rolling(10).min()
    high_10d_before = d["close"].shift(10).rolling(10).max()
    drop_depth = ((high_10d_before - low_10d) / high_10d_before.clip(lower=1e-9)).clip(lower=0)  # 0~1
    score_drop = drop_depth.clip(upper=1.0)                                     # 0~1

    # 2) 反弹强度：当前价格相对底部越高，反弹越多
    rebound_ratio = ((d["close"] - low_10d) / low_10d.clip(lower=1e-9)).clip(lower=0)  # 0~∞
    score_rebound = (rebound_ratio / 0.05).clip(lower=0, upper=1.0)                # 0~1，5%反弹满分

    # 3) 放量强度：量比越高分越高
    vol_ratio = d["volume"] / vol5.clip(lower=1e-9)
    score_vol = (vol_ratio / 2.0).clip(lower=0, upper=1.0)                        # 0~1，2倍量满分

    buy_score = (score_drop + score_rebound + score_vol).fillna(0)

    d["BUY_SCORE"]  = buy_score
    d["BUY_SIGNAL"]  = (buy_score >= 1.0) & (d.index >= d.index[19])
    d["SELL_SIGNAL"] = (d["close"] < ma5) | (d["volume"] < vol5 * 0.5)
    d["STRATEGY"] = "抄底型"
    return _strategy_return_cols(d)


# ===================================================================
# 策略5: 主力建仓型
# ===================================================================

def strategy_whale_accumulation(df: pd.DataFrame,
                               vol_ratio: float = 3.0) -> pd.DataFrame:
    """
    主力吸筹特征检测
    BUY_SIGNAL: 股价低迷区出现异常放量（主力悄悄建仓）
    """
    d = df.copy()

    ma20  = d["close"].rolling(20).mean()
    vol20 = d["volume"].rolling(20).mean()
    vol5  = d["volume"].rolling(5).mean()

    # ── 连续打分（每项 0~1，满分 3）──────────
    # 1) 低位程度：价格相对 MA20 越低分越高
    price_below = ((ma20 - d["close"]) / ma20.clip(lower=1e-9)).clip(lower=0)  # 0~∞
    score_low = price_below.clip(upper=1.0)                                     # 0~1

    # 2) 放量程度：相对20日均量，3倍量满分
    vr = d["volume"] / vol20.clip(lower=1e-9)
    score_vol = (vr / vol_ratio).clip(lower=0, upper=1.0)                      # 0~1

    # 3) 涨幅受控：3日涨幅越小（0%最好），分越高
    ret3d = (d["close"] / d["close"].shift(3).clip(lower=1e-9) - 1).fillna(0)
    score_ctrl = (1 - (ret3d / 0.05)).clip(lower=0, upper=1.0)                # 0~1，5%涨幅满分

    buy_score = (score_low + score_vol + score_ctrl).fillna(0)

    d["BUY_SCORE"]  = buy_score
    d["BUY_SIGNAL"]  = (buy_score >= 1.0) & (d.index >= d.index[19])
    d["SELL_SIGNAL"] = d["close"] > ma20 * 1.1
    d["STRATEGY"] = "主力建仓"
    return _strategy_return_cols(d)


# ===================================================================
# 策略融合权重常量统一由 config/strategy_params.py 维护。
# 本模块此前另有一份同名 DEFAULT_WEIGHTS = [0,0,0,1,0]（纯抄底），与
# config.strategy_params.DEFAULT_WEIGHTS = [0.30,0.15,0.20,0.20,0.15] 同名不同值，
# 曾在 core/sync.py 中互相遮蔽（顶层导入本模块的、函数内又局部导入 config 的），
# 导致同一个 fusion_score 字段在两条写入路径下口径不一致。故此处不再重复定义。
# 2026-04-03 1年回测（2025-04~2026-04）：
#   纯主力建仓(Whale) -46.54% 最大回撤-52%（176笔，均盈/均亏≈1:1）
#   纯抄底(Bottom)    -7.58%  最大回撤-32.8%（181笔，均盈9.87%/均亏5.86%，盈亏比1.74）← 最优
#   融合(40/0/0/30/30) -31.16%（224笔，均盈11.2% 但胜率仅37.5%）
# ⚠ 5%/周（年化1214%）不可能实现；当前目标：控制回撤在-30%以内
from config.strategy_params import PURE_BOTTOM_WEIGHTS  # noqa: E402  纯抄底 [0,0,0,1,0]

# ===================================================================
# 策略6: 超跌反弹策略（用户新策略 v2）
# 条件：20日跌幅>=12% + 股价站上5日线 + 温和放量(3日均>5日均) + RSI(14) 35~50
#       + 近20日最低价未破（底部平台）
# 交易规则：持仓2~10天 | 止盈+8%~+12%减半仓 | 剩余破5日线清仓 | 止损-6%
#         | 连续2日缩量阴跌破底部清仓
# ===================================================================

def strategy_oversold_rebound(df: pd.DataFrame,
                              drop_threshold: float = -0.12,
                              vol_20d_min: float = 80_000_000,
                              rsi_low: float = 35.0,
                              rsi_high: float = 50.0,
                              new_low_window: int = 5,
                              score_threshold: float = 1.8) -> pd.DataFrame:
    """
    超跌反弹策略 v3（中小盘 + 基本面过滤版）
    买入条件（全满足才触发）：
      1. 近20日跌幅 >= 12%
      2. 收盘价 > MA5（站上5日线）
      3. 近20日日均成交额 >= 8000万元（amount）
      4. 3日均量 > 5日均量（温和放量）
      5. RSI(14) 在 rsi_low~rsi_high 区间（默认35~50）
      6. 股价不创 new_low_window 日内新低（底部平台确认）

    基本面过滤（在 backtest_v3.py 的预筛选阶段完成）：
      - 非ST/*ST/退市
      - 总市值 60~260亿
      - 净利润>0，扣非净利润>0

    SELL_SIGNAL：分批止盈+跟踪止损，详见 backtest_v3.py
    """
    d = df.copy()

    close = d["close"]
    amount = d.get("amount", d["volume"] * d["close"])  # 成交额（优先用amount）
    volume = d["volume"]

    # ── 基础指标 ──────────────────────────────
    ma5   = close.rolling(5).mean()
    vol3  = volume.rolling(3).mean()
    vol5  = volume.rolling(5).mean()
    amt20 = amount.rolling(20).mean()   # 20日均成交额

    # RSI(14)
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.clip(lower=1e-9)
    rsi   = 100 - (100 / (1 + rs))

    # ── 打分（每项 0~1，满分 4 分；需 >= 1.8 才触发）────────
    # ① 下跌深度
    ret_20d = close / close.shift(20).clip(lower=1e-9) - 1
    score_drop = ((-ret_20d) / abs(drop_threshold)).clip(lower=0, upper=1.0)

    # ② 站上MA5强度（2%涨幅满分）
    ma5_strength = ((close - ma5) / ma5.clip(lower=1e-9)).clip(lower=0)
    score_ma5    = (ma5_strength / 0.02).clip(lower=0, upper=1.0)

    # ③ 温和放量（1.5倍量满分）
    vol_ratio = vol3 / vol5.clip(lower=1e-9)
    score_vol = ((vol_ratio - 1) / 0.5).clip(lower=0, upper=1.0)

    # ④ RSI在区间内（低到高映射）
    score_rsi = ((rsi - rsi_low) / (rsi_high - rsi_low)).clip(lower=0, upper=1.0)

    # 综合打分（0~4，需要 >= 1.8）
    buy_score = (score_drop + score_ma5 + score_vol + score_rsi).fillna(0)

    # ── 额外确认 ──────────────────────────────
    # 条件6：股价不创N日新低（底部平台）
    low_n = close.rolling(new_low_window).min()
    not_new_low = close > low_n * 1.002  # 小幅高于N日最低

    # 条件3：20日均成交额门槛
    vol_ok = amt20 >= vol_20d_min

    buy_signal = (buy_score >= score_threshold) & not_new_low & vol_ok & (d.index >= d.index[19])

    # SELL: 收盘价跌破MA5（止盈止损在回测引擎中处理）
    sell_signal = close < ma5

    d["BUY_SCORE"]   = buy_score
    d["BUY_SIGNAL"]  = buy_signal
    d["SELL_SIGNAL"] = sell_signal
    d["STRATEGY"]    = "超跌反弹v3"
    return _strategy_return_cols(d)


# ===================================================================
# 信号融合
# ===================================================================


def fuse_signals(dfs: List[pd.DataFrame],
                 weights: List[float] = None,
                 mode: str = None) -> pd.DataFrame:
    """
    多策略融合
    :param dfs: 多个策略返回的 DataFrame 列表（各含 BUY_SCORE 列，0-3分）
    :param weights: 各策略权重，默认等权 [0.2, 0.2, 0.2, 0.2, 0.2]
    :param mode: 融合方式，None 表示取 config.FUSION_MODE
        - "weighted_avg"（归一化加权平均，历史行为）
              融合分 = Σ(策略分_i × 权重_i) / Σ权重 × 策略数
          缺陷：单一策略再强也会被其他策略的低分稀释。例如熊市权重下
          「抄底」打满 10/10 也只有 15 分，低于高波动阈值 18，数学上永不成信号；
          最终入选的都是"样样中不溜"的票，与纯抄底回测占优的结论相悖。
        - "max"（取最大值，专才不被稀释）
              融合分 = max(策略分_i × 权重_i / max(权重)) × 5
          权重退化为置信度折扣：权重最高的策略满分可达 50，权重减半的策略满分 25。
          任何单一策略足够强即可独立成信号，同时保留市场状态倾向。
    :return: 含 FUSION_SCORE, BUY_SIGNAL 及各策略独立评分列的 DataFrame
             各策略评分列：VOL_SCORE, MA_SCORE, DIVERGE_SCORE, BOTTOM_SCORE, WHALE_SCORE
    """
    if not dfs:
        raise ValueError("策略列表为空")

    if mode is None:
        try:
            from config.strategy_params import FUSION_MODE
            mode = FUSION_MODE
        except Exception:
            mode = "weighted_avg"
    if mode not in ("weighted_avg", "max"):
        mode = "weighted_avg"

    result = dfs[0][["close", "volume"]].copy()
    # 保留 open 列供 T+1 回测引擎使用
    if "open" in dfs[0].columns:
        result["open"] = dfs[0]["open"]

    n_strategies = len(dfs)
    if weights is None:
        weights = [1.0 / n_strategies] * n_strategies
    else:
        weights = list(weights)

    # 各策略评分列名映射（顺序与 dfs 一一对应）
    score_col_names = [
        "VOL_SCORE",      # 放量突破
        "MA_SCORE",       # 均线粘合
        "DIVERGE_SCORE",  # 量价背离
        "BOTTOM_SCORE",   # 抄底
        "WHALE_SCORE",    # 主力建仓
        "OVERSOLD_SCORE", # 超跌反弹
    ]

    # 各策略原始分(0-3) -> 映射到(0-10)
    max_w = max([w for w in weights if w is not None] or [0.0]) or 0.0
    fusion = None
    total_w = 0.0
    for idx, (df_strategy, w) in enumerate(zip(dfs, weights)):
        col = df_strategy["BUY_SCORE"] if "BUY_SCORE" in df_strategy.columns else None
        if col is None:
            continue
        mapped_score = col.fillna(0) * (10.0 / 3.0)
        if idx < len(score_col_names):
            result[score_col_names[idx]] = mapped_score
        if mode == "max":
            # 权重归一到 [0,1] 作为置信度折扣，再逐列取最大（np.maximum 向量化）
            scaled = mapped_score * (w / max_w if max_w > 0 else 0.0) * 5.0
            fusion = scaled if fusion is None else pd.Series(
                np.maximum(fusion.values, scaled.values), index=fusion.index)
        else:
            scaled = mapped_score * w
            fusion = scaled if fusion is None else fusion + scaled
        total_w += w

    if fusion is None:
        raw_score = pd.Series(0.0, index=result.index)
    elif mode == "max":
        raw_score = fusion.clip(upper=50.0)
    elif total_w > 0:
        raw_score = (fusion / total_w * n_strategies).clip(upper=50.0)
    else:
        raw_score = pd.Series(0.0, index=result.index)

    result["FUSION_SCORE"] = raw_score

    # BUY_SIGNAL：融合分 >= 阈值 20 分时触发
    result["BUY_SIGNAL"]   = result["FUSION_SCORE"] >= 20.0
    result["SELL_SIGNAL"]   = False
    return result


def fuse_with_phase34(dfs: List[pd.DataFrame],
                       weights: List[float] = None,
                       phase34_signals: dict = None) -> pd.DataFrame:
    """
    Fuse strategy outputs: prefer Phase 3/4 dynamic scoring, fall back to manual scoring.

    phase34_signals: {"code": "000001", "fusion_score": 35.2, "signals": [...]}
    """
    result = fuse_signals(dfs, weights)
    if phase34_signals and phase34_signals.get("fusion_score", 0) > 0:
        new_score = phase34_signals["fusion_score"]
        new_score = max(0.0, min(50.0, new_score))
        result.loc[result.index[-1], "FUSION_SCORE"] = new_score
    return result

