"""
core/services/browse.py —— 指标解释 / 因子趋势 / 规则命中只读编排（T7 下沉）
=========================================================================

- EXPLAINERS：量化指标白话解释（纯静态教育内容）
- factor_trend_payload：daily_price 融合分及 5 分项时序（sparkline/详情共用）
- rule_signals_payload：strategy_signals 当日规则命中（排除命中过泛的规则）
"""
from __future__ import annotations

from core.db import get_conn
from utils.serialization import sanitize_numeric

# 量化指标白话解释（教育散户；纯静态）
EXPLAINERS = {
    "fusion_score": {
        "title": "综合评分",
        "desc": "融合 5 类策略（放量、均线、量价背离、底部、主力）得出的 0~100 分。",
        "for_dummies": "分数越高代表越多个策略同时看好这只票。但分数高 ≠ 一定赚钱，只是历史上这种条件出现时赚钱概率较大。",
        "tip": "建议作为初筛，80+ 分再人工看 K 线确认。",
    },
    "stop_loss": {
        "title": "止损价",
        "desc": "股价跌到这个价位时坚决卖出，限制亏损。",
        "for_dummies": "相当于'认错线'。设好之后一定要执行，否则和没设一样。",
        "tip": "常见做法：买入价下方 5~8%。短线严、中线宽。",
    },
    "take_profit": {
        "title": "止盈价",
        "desc": "股价涨到这个价位时分批卖出，落袋为安。",
        "for_dummies": "相当于'满意线'。很多人赚过又亏回去，就是因为不设止盈。",
        "tip": "常见做法：买入价上方 15~25%。",
    },
    "sharpe_ratio": {
        "title": "夏普比率",
        "desc": "每承担 1 单位风险，能获得多少超额收益。",
        "for_dummies": "数字越大越好。>1 算不错，>2 算优秀，<0 就是亏钱。",
        "tip": "新手直接看这个数就行，比年化收益更靠谱。",
    },
    "max_drawdown": {
        "title": "最大回撤",
        "desc": "历史上从最高点跌到最低点的幅度。",
        "for_dummies": "代表你可能遇到的最难受的亏损。-30% 意味着满仓可能亏掉 3 成。",
        "tip": "回测时务必关注；个人投资者建议控制在 -20% 以内的策略。",
    },
    "win_rate": {
        "title": "胜率",
        "desc": "赚钱的交易占总交易次数的比例。",
        "for_dummies": "胜率高不一定赚钱（赚少亏多也不行）。要看配合的平均盈亏比。",
        "tip": "胜率 40% + 盈亏比 3:1 也是好策略。",
    },
    "annual_return": {
        "title": "年化收益",
        "desc": "按复利折算到每年的收益率。",
        "for_dummies": "看起来很高，但要看是否经历过牛熊周期。",
        "tip": "回测时间最好覆盖一轮完整牛熊（≥2 年）。",
    },
    "north_net_buy": {
        "title": "北向资金净买入",
        "desc": "境外资金通过沪深港通买入 A 股的净额。",
        "for_dummies": "俗称'外资动向'。持续大幅净买入常被视为积极信号。",
        "tip": "看 5 日或 20 日累计趋势，比看单日更靠谱。",
    },
    "market_cap": {
        "title": "市值",
        "desc": "股价 × 总股本，单位通常是亿元。",
        "for_dummies": "盘子大 = 稳但弹性小；盘子小 = 弹性大但容易暴涨暴跌。",
        "tip": "新手建议 100~1000 亿之间，避开'小票'风险。",
    },
}


def factor_trend_payload(codes: list[str], days: int) -> dict:
    """融合分归因 + 趋势（纯读 daily_price）。

    返回 { code: [ {trade_date, fusion_score, vol_score, ma_score,
                    diverge_score, bottom_score, whale_score}, ... ] }（日期升序）。
    同时服务 sparkline（days=7）与详情弹窗（days=30 含 5 分项）。
    """
    result = {}
    with get_conn() as conn:
        for code in codes:
            rows = conn.execute(
                """
                SELECT trade_date, fusion_score, vol_score, ma_score,
                       diverge_score, bottom_score, whale_score
                FROM daily_price WHERE code = ?
                ORDER BY trade_date DESC LIMIT ?
                """,
                (code, days),
            ).fetchall()
            result[code] = [dict(r) for r in reversed(rows)]
    return sanitize_numeric(result)


def rule_signals_payload(date: str, limit: int, max_hits: int) -> dict:
    """常驻策略规则当日命中（选股→回测→推荐闭环的推荐端）。

    date 为空时取 strategy_signals 最新 trade_date；
    当日命中数超过 max_hits 的规则视为过泛（无筛选价值），整条排除。
    """
    with get_conn() as conn:
        if not date:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM strategy_signals"
            ).fetchone()
            date = row["d"] if row and row["d"] else None
        if not date:
            return sanitize_numeric({"date": None, "total": 0, "items": []})

        # 按规则统计当日命中数，排除命中过泛的规则
        hit_rows = conn.execute(
            """SELECT rule_id, COUNT(*) AS hits FROM strategy_signals
               WHERE trade_date = ? GROUP BY rule_id""",
            (date,),
        ).fetchall()
        rule_hits = {r["rule_id"]: r["hits"] for r in hit_rows}
        excluded_rules = sum(1 for h in rule_hits.values() if h > max_hits)
        total = sum(h for h in rule_hits.values() if h <= max_hits)
        rows = conn.execute(
            """
            SELECT s.code, si.name AS name, s.rule_id, s.rule_name, s.confidence,
                   dp.close AS latest_close,
                   r.win_rate AS rule_win_rate, r.annual_return AS rule_annual_return,
                   r.total_trades AS rule_total_trades
            FROM strategy_signals s
            LEFT JOIN stock_info si ON si.code = s.code
            LEFT JOIN daily_price dp ON dp.code = s.code AND dp.trade_date = s.trade_date
            LEFT JOIN strategy_rules r ON r.id = s.rule_id
            WHERE s.trade_date = ?
              AND s.rule_id IN (
                  SELECT rule_id FROM strategy_signals
                  WHERE trade_date = ? GROUP BY rule_id HAVING COUNT(*) <= ?
              )
            ORDER BY s.confidence DESC
            LIMIT ?
            """,
            (date, date, max_hits, limit),
        ).fetchall()

    items = [dict(r) for r in rows]
    for it in items:
        it["rule_hits"] = rule_hits.get(it["rule_id"])
    return sanitize_numeric({
        "date": date, "total": total, "items": items,
        "excluded_rules": excluded_rules, "max_hits": max_hits,
    })
