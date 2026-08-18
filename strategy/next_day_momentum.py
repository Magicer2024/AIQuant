"""
strategy/next_day_momentum.py —— 隔日动量信号（龙虎榜净买占比）
================================================================
独立于超跌反弹并行的一条短线信号线。仅评估最新交易日（避免历史信号膨胀）。

Phase 1 样本外验证（tools/mine_next_day.py，2026-01~07 验证窗）：
  龙虎榜「净买额占总成交比 >= 10% 且当日非涨停」子集：
    次日 open->close（OC 现实口径）胜率 54.3%、均值 +0.95%。
  关键口径：龙虎榜在收盘后才公布，散户只能次日（T+1）开盘买入，
  故用 OC 口径而非 CC（perf_1d 含一个买不到的隔夜跳空，会高估胜率）。
  剔除当日涨停是必要过滤——涨停票次日巨幅高开根本买不到、日内易回落。
  2026-08 追加：剔除当日大跌/跌停（max_down_pct，默认 -7%）——净买占比>=10%
  但当日大跌/跌停的信号是负期望子集（历史 T+1 -1.0%/胜率47%、T+2 -3.6%、
  T+5 -5.8%/胜率25%，麦迪科技 603990 2026-08-13 跌停日误入今日推荐即此盲区）。
"""
import math
from typing import Optional


def scan_next_day_momentum(df, lhb_row: Optional[dict] = None,
                           name: Optional[str] = None,
                           total_shares=None,
                           params: Optional[dict] = None) -> Optional[dict]:
    """
    龙虎榜隔日动量：仅评估最新交易日，命中返回 stock_signal 兼容 dict，否则 None。

    :param df:       该股票日线 DataFrame（用于取最新收盘价、对齐日期）
    :param lhb_row:  该股票在「最新龙虎榜日期」的记录 dict
                     （需含 trade_date/code/net_buy_ratio/pct_change/reason），无则返回 None
    :param params:   NEXT_DAY_MOMENTUM 配置（min_net_buy_ratio/exclude_limit_up/
                     max_down_pct/stop_loss_pct/take_profit_pct）

    命中条件：净买占比 >= 阈值(默认10) 且（如启用）当日非涨停、且当日非大跌/
             跌停（max_down_pct，默认 -7%；负期望子集，2026-08 修复），
             且龙虎榜日期 == df 最新交易日（否则 LHB 相对行情已过期）。
    返回 dict：horizon="short"、strategy="隔日动量"、fusion_score(0~50)、
             buy_price(次日开盘买入，参考价用当日收盘)、stop_loss、take_profit、
             trade_date、triggers。
    """
    if not lhb_row or df is None or len(df) < 2:
        return None

    p = params or {}
    min_ratio = float(p.get("min_net_buy_ratio", 10.0))
    exclude_limit_up = bool(p.get("exclude_limit_up", True))
    stop_pct = float(p.get("stop_loss_pct", -0.04))
    take_pct = float(p.get("take_profit_pct", 0.065))
    max_down_pct = p.get("max_down_pct")

    # 1) 净买占比门槛
    ratio = lhb_row.get("net_buy_ratio")
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ratio) or ratio < min_ratio:
        return None

    # 2) 对齐：龙虎榜日期必须等于该股票最新交易日
    idx = df.index[-1]
    trade_date = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
    lhb_date = str(lhb_row.get("trade_date") or "")[:10]
    if lhb_date != trade_date:
        return None

    # 3) 当日涨跌极端过滤：涨停剔除（买不到且次日易回落）+ 大跌/跌停剔除
    #    （2026-08 修复：净买占比>=10% 但当日大跌/跌停的信号是明确负期望子集——
    #    历史 T+1 -1.0%/胜率47%、T+2 -3.6%/34%、T+5 -5.8%/25%；麦迪科技 603990
    #    2026-08-13 跌停日（-9.99%）龙虎榜净买 10.6% 即因此误入今日短线推荐）
    code = str(lhb_row.get("code") or "")
    pct = lhb_row.get("pct_change")
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None
    if exclude_limit_up and pct_f is not None and math.isfinite(pct_f):
        # 20cm：创业板/科创板阈值 19.8，其余 9.8
        limit_thr = 19.8 if code.startswith(("300", "301", "688", "689")) else 9.8
        if pct_f >= limit_thr:
            return None
    if max_down_pct is not None and pct_f is not None and math.isfinite(pct_f) \
            and float(max_down_pct) < 0 and pct_f <= float(max_down_pct):
        return None

    latest = df.iloc[-1]
    try:
        close = float(latest["close"])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(close) or close <= 0:
        return None

    buy_price = round(close, 2)
    stop_loss = round(close * (1 + stop_pct), 2)
    take_profit = round(close * (1 + take_pct), 2)

    # fusion_score：净买占比映射 0~50（ratio 10→30，>=30→50），与其他短线量纲对齐
    fusion = 30.0 + min(max(ratio - min_ratio, 0.0) / 20.0, 1.0) * 20.0

    triggers = [
        f"龙虎榜净买占比 {ratio:.1f}%（≥{int(min_ratio)}）",
        "次日开盘买入（龙虎榜盘后公布）",
    ]
    reason = str(lhb_row.get("reason") or "").strip()
    if reason:
        triggers.append(reason[:24])

    return {
        "horizon": "short",
        "strategy": "隔日动量",
        "fusion_score": round(fusion, 2),
        "buy_price": buy_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trade_date": trade_date,
        "triggers": triggers,
    }
