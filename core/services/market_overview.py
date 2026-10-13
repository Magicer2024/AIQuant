"""
core/services/market_overview.py —— 市场速览编排（T7 从 routes/investor.py 下沉）
==============================================================================

核心指数卡片（index_daily 最新一日 + 近 5 日趋势）、市场宽度（涨跌家数/成交额）、
大盘冷热（strategy.market_regime 多日宽度 composite，10 分钟缓存）、自适应权重摘要。
"""
from __future__ import annotations

from datetime import datetime

from core.db import get_conn
from strategy.market_regime import compute_market_regime
from utils.serialization import sanitize_numeric

# 市场速览展示的核心指数
_INDICES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000688": "科创50",
    "000905": "中证500",
}

# 大盘冷热 → 散户可读操作提示
_REGIME_HINT = {
    "hot": "火热（注意追高风险）", "warm": "偏暖（可适度参与）",
    "neutral": "震荡（精选个股）", "cool": "偏冷（控制仓位）",
    "cold": "冰点（防守为主）",
}


def get_adaptive_weights_info() -> dict:
    """P3: 获取自适应权重信息（供 market-overview API 返回）。"""
    try:
        from strategy.adaptive_weights import get_market_summary
        return get_market_summary()
    except Exception:
        return {"regime": "unknown", "regime_label": "未知", "adaptive_enabled": False}


def build_market_overview_payload() -> dict:
    """市场速览：核心指数 + 市场宽度 + 大盘情绪冷热 + 自适应权重。"""
    with get_conn() as conn:
        # 指数最新一日
        idx_rows = conn.execute(
            """
            SELECT code, MAX(trade_date) AS d FROM index_daily GROUP BY code
            """
        ).fetchall()
        latest_dates = {r["code"]: r["d"] for r in idx_rows if r["d"]}

        index_cards = []
        for code, name in _INDICES.items():
            d = latest_dates.get(code)
            if not d:
                continue
            row = conn.execute(
                """SELECT open, high, low, close, pct_change
                   FROM index_daily WHERE code=? AND trade_date=?""",
                (code, d),
            ).fetchone()
            if not row:
                continue
            # 计算近 5 日涨跌幅（趋势辅助）
            prev = conn.execute(
                """SELECT close FROM index_daily
                   WHERE code=? AND trade_date < ?
                   ORDER BY trade_date DESC LIMIT 5""",
                (code, d),
            ).fetchall()
            trend_5d = None
            if prev and len(prev) >= 1:
                base = prev[-1]["close"]
                trend_5d = round((row["close"] - base) / base * 100, 2) if base else None

            index_cards.append({
                "code": code,
                "name": name,
                "close": round(row["close"], 2),
                "pct_change": round(row["pct_change"] or 0, 2),
                "high": row["high"],
                "low": row["low"],
                "trend_5d_pct": trend_5d,
                "trade_date": d,
            })

        # 市场宽度 + 大盘冷热：涨跌家数、总成交额、平均涨幅（基于 daily_price 最新一日）
        sentiment_row = conn.execute(
            """
            SELECT
              MAX(trade_date)  AS trade_date,
              AVG(pct_change)  AS avg_pct,
              SUM(amount)      AS total_amount,
              SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up_count,
              SUM(CASE WHEN pct_change < 0 THEN 1 ELSE 0 END) AS down_count,
              SUM(CASE WHEN pct_change = 0 THEN 1 ELSE 0 END) AS flat_count
            FROM daily_price
            WHERE trade_date = (SELECT MAX(trade_date) FROM daily_price)
            """
        ).fetchone()
        avg_pct = sentiment_row["avg_pct"] if sentiment_row else None
        total_amt = sentiment_row["total_amount"] if sentiment_row else None
        up_count = (sentiment_row["up_count"] or 0) if sentiment_row else 0
        down_count = (sentiment_row["down_count"] or 0) if sentiment_row else 0
        flat_count = (sentiment_row["flat_count"] or 0) if sentiment_row else 0

        # 多日宽度 composite（10 分钟缓存，抗单日噪音）
        regime = compute_market_regime(conn)
        regime_label = _REGIME_HINT.get(regime, "数据不足")

        return {
            "indices": sanitize_numeric(index_cards),
            "breadth": sanitize_numeric({
                "trade_date": sentiment_row["trade_date"] if sentiment_row else None,
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "total_amount": round(total_amt / 1e8, 0) if total_amt else None,  # 亿
                "label": (
                    "普涨，情绪亢奋" if up_count and up_count > (down_count or 0) * 3 else
                    "涨多跌少，偏强" if up_count > down_count else
                    "普跌，情绪低迷" if down_count and down_count > (up_count or 1) * 3 else
                    "跌多涨少，偏弱" if down_count > up_count else "涨跌相当"
                ) if (up_count or down_count) else "暂无数据",
            }),
            "sentiment": {
                "regime": regime,
                "label": regime_label,
                "avg_pct": round(avg_pct, 2) if avg_pct is not None else None,
                "total_amount": round(total_amt / 1e8, 1) if total_amt else None,  # 亿
            },
            "adaptive_weights": get_adaptive_weights_info(),  # P3: 自适应权重
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
