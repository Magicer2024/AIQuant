"""
strategy/market_regime.py —— 大盘冷热「最新交易日」读取层（带进程内缓存）
=====================================================================

纯计算口径在 core/market_regime.py（日期参数化，历史可复现）；
本模块只承接原 routes/investor._compute_market_regime 的两件事：
  1. 取 daily_price 最新交易日；
  2. 10 分钟进程内缓存（今日推荐 / 市场速览共享同一份，避免重复算宽度）。

T7（investor.py 业务下沉）：路由层不再持有该状态。
"""
from __future__ import annotations

import time

# 10 分钟缓存（盘中该数据低频变化；缓存为进程级，今日推荐与市场速览共享）
_REGIME_CACHE_TTL = 600
_regime_cache: dict = {"ts": 0.0, "regime": None}


def compute_market_regime(conn) -> str:
    """
    多日宽度 composite 市场冷热（10 分钟缓存）。

    综合三项（替代原单日 AVG(pct_change) 的"一天定生死"，避免冰点↔火热来回跳）：
      1) 近 5 个交易日涨跌家数比（多日均值，抗单日暴涨暴跌）
      2) 全市场站上 MA20 的个股占比（真实宽度，近 5 日均值）
      3) 沪深300 / 中证500 近 5 日斜率均值
    composite = 50 + 三项贡献（各 ±20 / ±20 / ±15），正常区间约 25~75。
    档位：>=60 hot / >=55 warm / >=45 neutral / >=40 cool / else cold。
    任一环节异常时回退单日均值逻辑（core.market_regime 内部处理）。
    """
    from core.market_regime import compute_regime_on

    now = time.time()
    if _regime_cache["regime"] and (now - _regime_cache["ts"]) < _REGIME_CACHE_TTL:
        return _regime_cache["regime"]

    latest_td = conn.execute(
        "SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
    regime = compute_regime_on(conn, latest_td) if latest_td else "unknown"
    _regime_cache.update({"ts": now, "regime": regime})
    return regime
