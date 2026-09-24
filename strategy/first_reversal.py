"""
strategy/first_reversal.py —— 「反转首日」信号线（观察池型，独立于今日推荐）
================================================================
来源：用户反馈「600641 已上涨 4 天才推荐，MACD/KDJ 在反转第一天没推荐，
      明天买入大概率挂在高位」（2026-09-23）；
      追加需求（2026-09-24）：「加入 KDJ。MACD 反转到 0 值以上、同时 KDJ K 在
      20~40 时，后面 10 天趋势不错。」

═══ 定位：首日位置观察池，**不是买入推荐** ═══════════════════════════
问题本质：现有两条短线线**结构上无法在反转首日响**——
  · 短线融合（pure_bottom_v2）奖励「刚回踩」（相对 10 日低点反弹 0~4%），
    首日大涨必然 >4% ⇒ score_rebound 归零；
  · 隔日动量走龙虎榜净买，反转首日通常未上榜。
故本线为新增独立线，只回答一个问题：**今天哪些票刚完成「首次站上 MA20 +
大阳 + 放量」**——即入场位最低的那一天。

═══ 实测（tools/_diag_reversal_v5.py + _diag_kdj_hypothesis.py，
    2024-07~2026-09 / 544 交易日 / 4500 只主板+创业板 / OC 现实口径，
    市场中性超额 = 当日信号组 OC 均值 − 当日全市场 OC 均值，基线 +0.157%）═══

  A3 = 首次站上 MA20 & 大阳≥5% & 成交额量比≥1.5 & 非涨停：
    n=15841、日均 29.1 只、OC +0.328%、胜率 49.5%、
    **超额 +0.174pp、t=+2.80、分年 +0.23/+0.14/+0.18pp 三年一致 ✅**
    信号日中位偏离 MA20 仅 **+3.8%**（600641 首日 +4.90%，而被推荐日已 +39.93%）
  600641 实测：本线**只在 2026-09-16 命中一次**（+6.80%、量比 1.54、ext +4.90%），
    此后 09-17~09-23 永不命中 ⇒ 结构上锁死「首日入场」，当日 T+1 OC +11.18%。

  ⚠ 但按报告 §6C 预登记门槛（分年 OC>0 **且胜率≥52%**），**胜率 49.5% 未达标**；
    超额 +0.174pp 扣 A 股往返成本（约 0.1%）后仅余 +0.07pp ⇒ **不构成可交易的
    独立 alpha**。故不占 Top-N 名额、不计入推荐复盘/出场跟踪胜率。

═══ 用户 KDJ / MACD 0 轴假设的独立检验（同口径，全市场）═══
  · MACD DIF **上穿 0 轴**当日：n=42942、日均 78.9、胜率 48.2%、
    超额 **+0.013pp、t=+0.33** ⇒ **无 alpha**
  · DIF>0 且 KDJ K∈20~40：n=242844、日均 446、胜率 **51.8%**、
    超额 **-0.003pp、t=-0.11** ⇒ **无 alpha**
  · KDJ K 分档（DIF>0 前提下）**胜率完全单调**：
      K<20 → 52.7% | K20~40 → 51.8% | K40~60 → 50.4% | K60~80 → 48.1% | **K>80 → 44.0%**
    （全体口径同向：51.9/50.8/49.8/48.6/44.7），且**三种市场状态下 K>80 都更差**
    （强市 -0.46pp / 平市 -0.08pp / 弱市 -0.11pp）
  ⇒ **用户直觉在「胜率」维度成立**（低位买胜率高、K>80 胜率仅 44%），
    **但收益维度不成立**：同日配对组间差 +0.003pp（t=+0.08），
    分年 2024 -0.160 / 2025 -0.055 / **2026 +0.200（t=+2.42 反向）**
    ⇒ 铁律 3（分年 + 剔除单年都要成立）不过 ⇒ **KDJ 只能做提示标签，不能做硬过滤**。
  · 叠加到本线：A3+K∈20~40 → 超额 +0.113pp（低于基线 +0.174）、日均 5.4 只；
    A3+K>80 → n=59、OC **-0.871%**、胜率 42.4%（量太小，剔除它几乎不改变整体）
  ⇒ 故本线**默认不做 KDJ 过滤**，但把 K/D 写进 triggers，K>80 时加 ⚠ 标签。

═══ 出场纪律对本线为负贡献（同持有期市场基线校准，_diag_reversal_v5.py）═══
  | 出场 | 均值 | 胜率 | 超额 |
  |---|---|---|---|
  | 裸持有 T+1 收盘 | +0.328% | 49.5% | **+0.174pp** |
  | 止损-6%/止盈+10%/5日 | +0.80% | 47.7% | -0.212pp |
  | ATR2.5(5~15%)/止盈+10%/5日 | +0.81% | 52.4% | -0.251pp |
  止损把胜率换到 52.4% 但期望跑输「随便买一只持有 5 天」（市场 5 日漂移 +1.01%）
  ⇒ 反转头日的票本身在跟涨市场，止损只在噪声回调里被扫出。

═══ ⚠⚠ 口径事故记录（务必先读，docs/data-price-source-defect.md）═══
  1) 曾误判 `daily_price.close` 在行级混价源，并改用它 `cumprod(1+pct_change)`
     重建序列 ⇒ 实际是 `pct_change` 字段 63.3% 的行恒为 0（未写入），
     该「修复」把 63% 交易日当成 0%，毁掉整条序列（v6/v7 结论全部作废）。
  2) 定论（_diag_which_is_truth.py）：**`close` 权威、`pct_change` 不可用**。
     故本模块一律用 `calc_true_ret()`（close/close.shift(1)）自算涨跌幅。

  是否启用由 FIRST_REVERSAL["enabled"] 控制；不占 Top-N 名额、不计入推荐复盘。
"""
import math
from typing import Optional

import pandas as pd

from strategy.indicators import calc_true_ret


def _kdj_last(high, low, close, n: int = 9):
    """KDJ（K/D 末值与前一值）。返回 (K, D, K_prev, D_prev) 或 None。"""
    c, h, l = (pd.Series(x, dtype="float64") for x in (close, high, low))
    low_n = l.rolling(n).min()
    high_n = h.rolling(n).max()
    rsv = (c - low_n) / (high_n - low_n + 1e-12) * 100
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    if len(k) < 2 or pd.isna(k.iloc[-1]) or pd.isna(d.iloc[-1]) \
            or pd.isna(k.iloc[-2]) or pd.isna(d.iloc[-2]):
        return None
    return float(k.iloc[-1]), float(d.iloc[-1]), float(k.iloc[-2]), float(d.iloc[-2])


def scan_first_reversal(df, name: Optional[str] = None,
                        total_shares=None,
                        params: Optional[dict] = None,
                        market_pct: Optional[float] = None,
                        code: Optional[str] = None) -> Optional[dict]:
    """
    反转首日扫描：仅评估 df 最新交易日，命中返回 stock_signal 兼容 dict，否则 None。

    :param df:           单只股票日线 DataFrame（升序，最新日在最后一行）；
                         需含 open/high/low/close(/amount|volume)；`pct_change` 列会被忽略
    :param name:         股票名称（仅展示）
    :param total_shares: 流通股本，用于市值上限过滤；缺失时跳过
    :param params:       FIRST_REVERSAL 配置
    :param market_pct:   信号日全市场平均涨幅（%），供 max_market_pct 门控
    :param code:         股票代码（20cm 板涨停阈值判定）；None 时回退 df["code"]

    命中条件（A3）：
      1) 首次站上 MA20：close > MA20 且前一日 close <= 前一日 MA20
      2) 大阳：自算涨幅（close/close.shift(1)）>= min_pct（默认 5.0）
      3) 放量：成交额（或成交量）量比 >= vol_ratio_min（默认 1.5，对前 5 日均值）
      4) 非涨停（涨停票次日巨幅高开买不到）
    可选（默认关闭；实测均无增益）：KDJ K 上限否决、MA20 拐头、热门日门控、市值上限
    """
    if df is None or len(df) < 21:
        return None

    p = params or {}
    min_pct = float(p.get("min_pct", 5.0))
    vol_ratio_min = float(p.get("vol_ratio_min", 1.5))
    exclude_limit_up = bool(p.get("exclude_limit_up", True))
    require_turn_up = bool(p.get("require_ma20_turn_up", False))
    # ⚠ KDJ 硬否决默认关闭：收益维度不显著（同日配对 t=+0.08，2026 反向），仅胜率维度成立
    kdj_k_max = p.get("kdj_k_max")
    stop_pct = float(p.get("stop_loss_pct", -0.05))
    take_pct = float(p.get("take_profit_pct", 0.10))
    max_market_pct = p.get("max_market_pct")
    max_mktcap = p.get("max_mktcap")

    if max_market_pct is not None and market_pct is not None:
        try:
            if float(market_pct) >= float(max_market_pct):
                return None
        except (TypeError, ValueError):
            pass

    latest = df.iloc[-1]
    try:
        close = float(latest["close"])
        high = float(latest["high"])
        low = float(latest["low"])
    except (TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(x) and x > 0 for x in (close, high, low)):
        return None

    idx = df.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
    code = str(code or (df["code"].iloc[-1] if "code" in df.columns else "") or "")

    closes = pd.to_numeric(df["close"], errors="coerce").astype(float)
    ret = calc_true_ret(df)                     # 权威口径：close/close.shift(1)
    pct = ret.iloc[-1]
    if pct is None or not math.isfinite(float(pct)) or pd.isna(pct):
        return None
    pct = float(pct)

    # 1) 非涨停（20cm 板用 19.8，与 next_day_momentum / surge_breakout 同口径）
    if exclude_limit_up:
        limit_thr = 19.8 if code.startswith(("300", "301", "688", "689")) else 9.8
        if pct >= limit_thr:
            return None

    # 2) 大阳
    if pct < min_pct:
        return None

    # 3) 首次站上 MA20
    ma20 = float(closes.iloc[-20:].mean())
    ma20_prev = float(closes.iloc[-21:-1].mean())
    prev_close = float(closes.iloc[-2])
    if not all(math.isfinite(x) and x > 0 for x in (ma20, ma20_prev, prev_close)):
        return None
    if not (close > ma20 and prev_close <= ma20_prev):
        return None
    if require_turn_up and not (ma20 > ma20_prev):
        return None

    # 4) 放量：优先成交额口径（跨行可比），退化到成交量
    vol_col = "amount" if "amount" in df.columns and df["amount"].notna().any() else "volume"
    vols = pd.to_numeric(df[vol_col], errors="coerce").astype(float)
    prev_mean = float(vols.iloc[-6:-1].mean()) if len(vols) >= 6 else 0.0
    if not math.isfinite(prev_mean) or prev_mean <= 0:
        return None
    vol_ratio = float(vols.iloc[-1]) / prev_mean
    if vol_ratio < vol_ratio_min:
        return None

    # 5) KDJ（提示用；可选硬否决）
    kdj = _kdj_last(pd.to_numeric(df["high"], errors="coerce").astype(float),
                    pd.to_numeric(df["low"], errors="coerce").astype(float),
                    closes)
    if kdj_k_max is not None and kdj is not None and kdj[0] > float(kdj_k_max):
        return None

    # 6) 可选：流通市值上限
    try:
        ts = float(total_shares) if total_shares else None
    except (TypeError, ValueError):
        ts = None
    if max_mktcap and ts and ts > 0 and ts * close > float(max_mktcap):
        return None

    # ── 派生展示量 ────────────────────────────────────────────────
    ext = (close / ma20 - 1) * 100
    prev20 = float(closes.iloc[-21]) if len(closes) >= 21 else None
    ret20_prev = ((prev_close / prev20 - 1) * 100
                  if prev20 and math.isfinite(prev20) and prev20 > 0 else None)

    buy_price = round(close, 2)
    stop_loss = round(close * (1 + stop_pct), 2)
    take_profit = round(close * (1 + take_pct), 2)

    fusion = 30.0 \
        + min(max(vol_ratio - vol_ratio_min, 0.0) / 3.0, 1.0) * 10.0 \
        + min(max(pct - min_pct, 0.0) / 5.0, 1.0) * 10.0
    fusion = min(fusion, 50.0)

    triggers = [
        f"反转首日：首次站上 MA20（偏离 {ext:+.1f}%，入场位低）",
        f"大阳 {pct:+.1f}%（未涨停，次日开盘可买）",
        f"量比 {vol_ratio:.1f}（放量确认）",
    ]
    if kdj is not None:
        # K 越低历史胜率越高（K<20 52.7% → K>80 44.0%，单调）；K>80 加风险标签
        tag = "⚠ 高位" if kdj[0] > 80 else ("低位" if kdj[0] < 20 else "")
        triggers.append(f"KDJ K={kdj[0]:.0f} / D={kdj[1]:.0f}{(' · ' + tag) if tag else ''}")
    if ret20_prev is not None and ret20_prev <= -10:
        triggers.append(f"前 20 日 {ret20_prev:+.1f}%（深调后首次反转）")

    return {
        "horizon": "short",
        "strategy": "反转首日",
        "fusion_score": round(fusion, 2),
        "buy_price": buy_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trade_date": trade_date,
        "triggers": triggers,
        "ext_pct": round(ext, 2),
        "vol_ratio": round(vol_ratio, 2),
        "pct_change": round(pct, 2),
        "kdj_k": round(kdj[0], 2) if kdj else None,
        "kdj_d": round(kdj[1], 2) if kdj else None,
        "ret20_prev": round(ret20_prev, 2) if ret20_prev is not None else None,
    }
