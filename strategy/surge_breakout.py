"""
strategy/surge_breakout.py —— 强势突破信号（首页「次日强势观察」栏目专用）
================================================================
独立于今日推荐的观察型信号线：寻找次日（T+1）可能涨停或大涨的高弹性票。
仅评估最新交易日（与隔日动量同，避免历史信号膨胀）。

Phase 1 样本外验证（tools/mine_surge_next_day.py，2026-01~07 验证窗，主板 OC 口径）：
  当日 +5%~9.8% 未涨停 + 收盘创 20 日新高 + 量比>=1.5 + 收盘位于振幅上段，
  叠加流通市值 <150 亿：
    OC 胜率 49.9%、均值 +0.43%、次日大涨率(OC>=5%) 12.9%、盘中触涨停 6.1%、日均 4.4 只。
  关键口径：
    - 当日未涨停才入选——涨停票次日巨幅高开买不到（隔夜跳空吃掉溢价，
      首板/连板族 OC 均值 <=0 已实测验证）；
    - OC（次日 open->close）为现实口径：信号在 T 日收盘后算出，只能 T+1 开盘买；
    - 火热日门控：全市场当日平均涨幅 >= +1% 时追突破是明确负期望（OC -1.38%），
      由调用方预计算 market_ok 传入。

龙虎榜硬过滤（tools/eval_lhb_surge_boost.py 两窗验证，2026-08 用户确认宁缺毋滥）：
  大阳突破当日上榜且净买>0 的子集在两窗均一致增强：
    训练窗 OC 均值 +1.61%/大涨率 23.1%/触涨停 19.2%，
    验证窗 OC 胜率 53.6%/均值 +1.04%/大涨率 25.0%/触涨停 21.4%
    （未上榜对照仅 11.8%/4.8%）。
  更严阈值（净买≥3000万等）验证窗虽更亮，但训练窗无一致改进且绝对金额
  与小盘偏好冲突（小票大额净买稀少），故采用基线「同日上榜且净买>0」硬过滤。
  代价：日均候选 1.0~1.3 只、部分交易日为空——用户明确接受。
"""
import math
from typing import Optional


def scan_surge_breakout(df, name: Optional[str] = None,
                        total_shares=None,
                        params: Optional[dict] = None,
                        market_ok: bool = True,
                        lhb_row: Optional[dict] = None) -> Optional[dict]:
    """
    强势突破扫描：仅评估 df 最新交易日，命中返回 stock_signal 兼容 dict，否则 None。

    :param df:           该股票日线 DataFrame（需含 open/high/low/close/volume/pct_change）
    :param total_shares: 流通股本（股），用于市值上限过滤；缺失时跳过市值项
    :param params:       SURGE_BREAKOUT 配置
    :param market_ok:    大盘门控（当日全市场平均涨幅未过热），调用方预计算
    :param lhb_row:      该股票在「最新龙虎榜日期」的记录 dict（需含 trade_date/
                         net_buy/net_buy_ratio）；lhb_required 开启时，必须同日上榜
                         且净买>0 才产生信号（硬过滤，两窗验证大涨率翻倍）
    """
    if df is None or len(df) < 30 or not market_ok:
        return None

    p = params or {}
    min_pct = float(p.get("min_pct", 5.0))
    max_pct = float(p.get("max_pct", 9.8))
    vol_ratio_min = float(p.get("vol_ratio_min", 1.5))
    close_pos_min = float(p.get("close_pos_min", 0.7))
    window = int(p.get("new_high_window", 20))
    max_mktcap = p.get("max_mktcap")
    stop_pct = float(p.get("stop_loss_pct", -0.04))
    take_pct = float(p.get("take_profit_pct", 0.08))

    latest = df.iloc[-1]
    try:
        close = float(latest["close"])
        pct = float(latest.get("pct_change") if hasattr(latest, "get")
                    else latest["pct_change"])
        volume = float(latest["volume"])
        high = float(latest["high"])
        low = float(latest["low"])
    except (TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(x) and x > 0 for x in (close, volume, high)) \
            or not math.isfinite(pct):
        return None

    # 1) 大阳但未涨停：次日开盘才买得到，且保留隔日冲高空间
    if not (min_pct <= pct < max_pct):
        return None

    idx = df.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]

    # 1.5) 龙虎榜硬过滤（用户确认宁缺毋滥）：必须信号日同日上榜且净买>0。
    # 两窗验证：大涨率 25% / 触涨停 21.4%，远优于未上榜的 11.8% / 4.8%；
    # 代价是日均候选仅 1~1.3 只、部分交易日为空。lhb_required=False 可回退纯 K 线模式。
    lhb_ratio = None
    if p.get("lhb_required", True):
        lhb_date = str((lhb_row or {}).get("trade_date") or "")[:10]
        if not lhb_row or lhb_date != trade_date:
            return None
        try:
            net_buy = float(lhb_row.get("net_buy"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(net_buy) or net_buy <= 0:
            return None
        try:
            lhb_ratio = float(lhb_row.get("net_buy_ratio"))
        except (TypeError, ValueError):
            lhb_ratio = None

    # 2) 放量：对前 5 日均量（不含当日，与挖掘脚本 vol_ma5_prev 同口径）
    prev_vols = df["volume"].iloc[-6:-1].astype(float)
    prev_mean = float(prev_vols.mean()) if len(prev_vols) else 0.0
    if not math.isfinite(prev_mean) or prev_mean <= 0:
        return None
    vol_ratio = volume / prev_mean
    if vol_ratio < vol_ratio_min:
        return None

    # 3) 突破前高：收盘 > 前 N 日最高价
    prev_highs = df["high"].iloc[-(window + 1):-1].astype(float)
    if len(prev_highs) < window:
        return None
    prev_high_max = float(prev_highs.max())
    if not math.isfinite(prev_high_max) or close <= prev_high_max:
        return None

    # 4) 收盘位置：位于当日振幅上段（强势不回落）
    rng = max(high - low, 1e-9)
    close_pos = (close - low) / rng
    if close_pos < close_pos_min:
        return None

    # 5) 市值上限（小盘弹性；缺股本时跳过，与质量过滤口径一致）
    try:
        ts = float(total_shares) if total_shares else None
    except (TypeError, ValueError):
        ts = None
    if max_mktcap and ts and ts > 0 and ts * close > float(max_mktcap):
        return None

    buy_price = round(close, 2)
    stop_loss = round(close * (1 + stop_pct), 2)
    take_profit = round(close * (1 + take_pct), 2)

    # fusion_score：强度映射 0~50（量比与涨幅各贡献 10 分，与短线量纲对齐）
    fusion = 30.0 \
        + min(max(vol_ratio - vol_ratio_min, 0.0) / 3.0, 1.0) * 10.0 \
        + min(max(pct - min_pct, 0.0) / (max_pct - min_pct), 1.0) * 10.0

    triggers = [
        f"大阳 {pct:+.1f}%（未涨停，次日开盘可买）",
        f"收盘创 {window} 日新高（突破前平台）",
        f"量比 {vol_ratio:.1f}（放量确认）",
    ]
    # 硬过滤模式下命中的票必然同日上榜净买，直接展示净买占比
    if lhb_ratio is not None and math.isfinite(lhb_ratio):
        triggers.append(f"🐯 龙虎榜净买占比 {lhb_ratio:.1f}%（主力资金加持）")
    elif p.get("lhb_required", True):
        triggers.append("🐯 龙虎榜净买入（主力资金加持）")
    fusion = min(fusion, 50.0)

    return {
        "horizon": "short",
        "strategy": "强势突破",
        "fusion_score": round(fusion, 2),
        "buy_price": buy_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trade_date": trade_date,
        "triggers": triggers,
        "vol_ratio": round(vol_ratio, 2),
        "pct_change": round(pct, 2),
        "close_pos": round(close_pos, 2),
        "lhb_net_buy_ratio": (round(lhb_ratio, 2)
                              if lhb_ratio is not None else None),
    }
