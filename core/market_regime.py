"""
core/market_regime.py —— 大盘冷热 regime（日期参数化，可对任意历史日复现）
========================================================================

与前端信号灯（routes/investor.py _compute_market_regime）同一口径：
多日宽度 composite = 50 + 三项贡献：
  1) 近 5 个交易日涨跌家数比（±20）
  2) 全市场站上 MA20 占比（近 5 日均值，±20）
  3) 沪深300/中证500 近 5 日斜率均值（±15）
档位：>=60 hot / >=55 warm / >=45 neutral / >=40 cool / else cold。
任一环节异常时回退单日均值口径。

用途：短线恐慌日闸门按 regime 自动切换（冷/凉市启用、其余禁用）——
复盘窗重建必须用信号日当天的历史 regime，故计算必须日期参数化。
"""
from __future__ import annotations

import pandas as pd

# composite 分数 → 档位（降序判定）
_SCORE_LEVELS = (("hot", 60), ("warm", 55), ("neutral", 45), ("cool", 40))


def regime_from_score(score: float) -> str:
    for name, th in _SCORE_LEVELS:
        if score >= th:
            return name
    return "cold"


def regime_from_avg_pct(avg_pct) -> str:
    """单日全市场平均涨幅 → 5 档 regime（composite 数据不足时的回退口径）"""
    if avg_pct is None:
        return "unknown"
    if avg_pct > 1.0:
        return "hot"
    if avg_pct > 0.3:
        return "warm"
    if avg_pct > -0.3:
        return "neutral"
    if avg_pct > -1.0:
        return "cool"
    return "cold"


def compute_regime_series(conn, start_date: str,
                          end_date: str | None = None) -> dict:
    """批量计算 [start_date, end_date] 内每个交易日的 regime。

    返回 {trade_date: regime}；lookback（涨跌比 12 天 / 宽度 30 天 /
    指数 20 天）在函数内部自动前推，调用方无需处理。
    口径与 routes/investor._compute_market_regime 完全一致（含 rolling 5
    窗口与 0.5 兜底），异常时整体回退单日均值口径。
    """
    end_filter = " AND trade_date <= ?" if end_date else ""
    try:
        # 1) 涨跌家数比（近 5 个交易日滑动均值，含当日）
        rows = conn.execute(f"""
            SELECT trade_date,
                   SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up,
                   COUNT(*) AS total
            FROM daily_price
            WHERE trade_date >= date(?, '-12 days'){end_filter}
            GROUP BY trade_date ORDER BY trade_date
        """, (start_date, end_date) if end_date else (start_date,)).fetchall()
        if not rows:
            return {}
        up = pd.DataFrame([{"d": r["trade_date"],
                            "ratio": (r["up"] or 0) / r["total"] if r["total"] else None}
                           for r in rows]).set_index("d")
        up_ratio = up["ratio"].rolling(5, min_periods=1).mean()

        # 2) 宽度：站上 MA20 占比（近 5 个交易日滑动均值）
        wrows = conn.execute(f"""
            SELECT d.trade_date,
                   AVG(CASE WHEN d.close > d.ma20 THEN 1.0 ELSE 0.0 END) AS width
            FROM (
                SELECT code, trade_date, close,
                       AVG(close) OVER (
                           PARTITION BY code ORDER BY trade_date
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                       ) AS ma20
                FROM daily_price
                WHERE trade_date >= date(?, '-30 days'){end_filter}
            ) d
            GROUP BY d.trade_date ORDER BY d.trade_date
        """, (start_date, end_date) if end_date else (start_date,)).fetchall()
        w = pd.DataFrame([{"d": r["trade_date"], "w": float(r["width"] or 0)}
                          for r in wrows]).set_index("d")
        width = w["w"].rolling(5, min_periods=1).mean().fillna(0.5)

        # 3) 指数斜率（沪深300/中证500 近 5 日，两指数均值）
        slopes = []
        for icode in ("000300", "000905"):
            irows = conn.execute(f"""
                SELECT trade_date, close FROM index_daily
                WHERE code = ? AND trade_date >= date(?, '-20 days'){end_filter}
                ORDER BY trade_date
            """, (icode, start_date, end_date) if end_date
                 else (icode, start_date)).fetchall()
            closes = pd.Series(
                {r["trade_date"]: float(r["close"]) for r in irows
                 if r["close"]})
            if len(closes) >= 6:
                slopes.append(closes / closes.shift(5) - 1)
        idx_slope = (sum(slopes) / len(slopes)) if slopes else pd.Series(dtype=float)

        # composite 合成 → 档位
        dates = sorted(set(up_ratio.index) | set(width.index)
                       | set(idx_slope.index))
        out = {}
        for d in dates:
            if d < start_date or (end_date and d > end_date):
                continue
            ur = up_ratio.get(d)
            wd = width.get(d)
            sl = idx_slope.get(d) if d in idx_slope.index else None
            if pd.isna(ur) or pd.isna(wd):
                continue
            score = 50.0 + (ur - 0.5) * 100.0 * 0.4 \
                         + (wd - 0.5) * 100.0 * 0.4 \
                         + (float(sl) * 300.0 if sl is not None and not pd.isna(sl) else 0.0)
            out[d] = regime_from_score(score)
        return out
    except Exception:
        # composite 计算失败（表结构/数据异常）→ 回退单日均值口径
        rows = conn.execute(f"""
            SELECT trade_date, AVG(pct_change) AS avg_pct
            FROM daily_price
            WHERE trade_date >= ?{end_filter}
            GROUP BY trade_date
        """, (start_date, end_date) if end_date else (start_date,)).fetchall()
        return {r["trade_date"]: regime_from_avg_pct(r["avg_pct"]) for r in rows}


def compute_regime_on(conn, as_of_date: str) -> str:
    """某一历史日期 as_of_date 的 regime（用截至该日的数据，可复现）。"""
    series = compute_regime_series(conn, as_of_date, as_of_date)
    return series.get(as_of_date, "unknown")
