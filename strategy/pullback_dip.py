"""
strategy/pullback_dip.py —— 缩量回踩低吸信号线（short horizon，优先占 Top3 名额）
================================================================
2026-08-22 落地（用户外部方案主推①「缩量回踩低吸」）。独立短线信号线，
模式同隔日动量：short horizon、优先占名额、不受 T1 恐慌日过滤约束。

Phase 1 挖掘（tools/mine_pullback_quality.py，真实复盘口径：次日开盘买 +
信号日收盘定止损 + 方案A出场，主板 2024-01~2026-08，train/test 双窗验证）：
  V3 = 回踩 MA10 企稳 + 量缩至 5日均量(含当日) 60% 内 + 趋势闸门
       (MA20 向上且收盘站上) + 前 20 日涨幅≥10%（题材强度近似替代）
  train +1.82%/胜率56.8%(n=12168)，test +0.40%/胜率46.0%(n=1795)，
  train/test 双正方向一致；对比现网短线 Top3 链 test -0.97%。
  近 6 月 -0.28%（同期所有测试族最优；近期大盘贪婪上沿整体难做）。

关键结论（Part C 分桶）：V3 条件在 fusion>=22 抄底池内日均仅命中 0.18 条——
低吸票抄底融合分天然偏低、进不了候选池（「动量/抄底分低被 top-N 误杀」
判断成立），故以独立信号线接入而非链内过滤。

与挖掘脚本的口径对应（逐条核对过，禁止漂移）：
  - vol5 = rolling(5).mean() 含当日（mine 脚本 vols.rolling(5) 同窗）
  - 回踩企稳：low <= MA10×(1+band) 且 close >= MA10×(1-band)，band=0.01
  - 趋势闸门：MA20 当日值 > 5 日前值 且 close > MA20
  - 前期涨幅：前 20 根（不含信号日）最高收盘 / 窗口首日收盘 - 1 >= rally_min
"""
import math
from typing import Optional

import pandas as pd


def pullback_dip_series(df: pd.DataFrame,
                        params: Optional[dict] = None) -> pd.DataFrame:
    """向量化计算缩量回踩 V3 命中序列（供生产扫描与历史回填共用）。

    返回与 df 同索引 DataFrame，含：
      PB_HIT   bool  —— V3 四条件全部命中
      PB_SHRINK float —— 量缩比（当日量/5日均量，越小越缩）
    """
    p = params or {}
    shrink_max = float(p.get("vol_shrink_max", 0.6))
    band = float(p.get("ma_touch_band", 0.01))
    rally_win = int(p.get("rally_window", 20))
    rally_min = float(p.get("rally_min", 0.10))

    out = pd.DataFrame(index=df.index)
    close = df["close"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    vol5 = vol.rolling(5).mean()          # 含当日，与挖掘口径一致
    shrink = vol / vol5
    # 前期涨幅：窗口 [i-20, i-1] 最高收盘 / 窗口首日收盘 - 1（不含信号日）
    win_max = close.rolling(rally_win).max().shift(1)
    win_first = close.shift(rally_win)
    rally = win_max / win_first - 1
    out["PB_SHRINK"] = shrink
    out["PB_HIT"] = (
        (low <= ma10 * (1 + band)) & (close >= ma10 * (1 - band))   # 回踩企稳
        & (shrink <= shrink_max)                                     # 量缩
        & (ma20 > ma20.shift(5)) & (close > ma20)                    # 趋势闸门
        & (rally >= rally_min)                                       # 前期涨幅
    )
    # 比较运算对 NaN 天然返回 False，PB_HIT 无需 fillna
    return out


def scan_pullback_dip(df, name: Optional[str] = None,
                      total_shares=None,
                      params: Optional[dict] = None) -> Optional[dict]:
    """
    缩量回踩扫描：仅评估 df 最新交易日，命中返回 stock_signal 兼容 dict。

    :param df:           该股票日线 DataFrame（需含 open/high/low/close/volume）
    :param name/total_shares: 仅用于签名对齐（质量过滤由调用方做，同隔日动量）
    :param params:       PULLBACK_DIP 配置
    """
    if df is None or len(df) < 30:
        return None
    p = params or {}
    s = pullback_dip_series(df, p)
    if not bool(s["PB_HIT"].iloc[-1]):
        return None

    latest = df.iloc[-1]
    try:
        close = float(latest["close"])
        low = float(latest["low"])
    except (TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(x) and x > 0 for x in (close, low)):
        return None

    shrink = float(s["PB_SHRINK"].iloc[-1])
    idx = df.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]

    close_s = df["close"].astype(float)
    ma10 = float(close_s.rolling(10).mean().iloc[-1])
    ma20 = float(close_s.rolling(20).mean().iloc[-1])

    stop_pct = float(p.get("stop_loss_pct", -0.05))
    take_pct = float(p.get("take_profit_pct", 0.08))
    buy_price = round(close, 2)
    stop_loss = round(close * (1 + stop_pct), 2)
    take_profit = round(close * (1 + take_pct), 2)

    # fusion_score：量纲 0~50，基础 30（天然过 short_conf_gate=22 门控，
    # 同隔日动量「fusion≥30 天然过门控」模式）；量缩程度与贴线精度各贡献至多 10 分
    shrink_max = float(p.get("vol_shrink_max", 0.6))
    shrink_bonus = min(max(shrink_max - shrink, 0.0) / shrink_max, 1.0) * 10.0
    band = float(p.get("ma_touch_band", 0.01))
    # 收盘越贴近 MA10（支撑位）分越高：偏离 0 满分，+1% 以上 0 分
    touch_dev = max(close / ma10 - 1.0, 0.0) if ma10 > 0 else 1.0
    touch_bonus = min(max(1.0 - touch_dev / band, 0.0), 1.0) * 10.0
    fusion = min(30.0 + shrink_bonus + touch_bonus, 50.0)

    triggers = [
        f"缩量回踩 MA10 企稳（量缩至 5日均量 {shrink * 100:.0f}%）",
        f"MA20 向上且站上（趋势确认）",
        f"前 20 日涨幅 ≥{float(p.get('rally_min', 0.10)) * 100:.0f}%（题材强度）",
    ]

    return {
        "horizon": "short",
        "strategy": "缩量回踩",
        "fusion_score": round(fusion, 2),
        "buy_price": buy_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trade_date": trade_date,
        "triggers": triggers,
        "vol_shrink": round(shrink, 3),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
    }
