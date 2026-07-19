"""
routes/investor.py —— 个人投资者专属 API

面向个人投资者的"轻量 + 可执行"接口：
- 今日推荐（带买入价/止损/止盈，告诉你该怎么操作）
- 市场速览（指数 + 北向资金 + 大盘冷热）
- 个人持仓与盈亏（含预警）
- 自选股（增删查）
- 量化指标白话解释（教育散户）
- 风险告警（接近止损/单股集中度过高等）
"""
import json
from datetime import date, datetime
from typing import Optional

from flask import Blueprint, request, jsonify

from core.db import get_conn, _safe_add_column, init_db
from utils.api import ok, fail
from utils.serialization import sanitize_numeric as _sanitize


investor_bp = Blueprint("investor", __name__, url_prefix="/api/investor")


# ─────────────────────────────────────────────
# 信号灯判定（散户最关心的"今天能不能动手"）
# ─────────────────────────────────────────────
REGIME_LABEL = {
    "hot": "火热", "warm": "偏暖", "neutral": "震荡",
    "cool": "偏冷", "cold": "冰点", "unknown": "数据不足",
}


def _calc_signal(*, score, risk_reward, risk_pct, market_regime, is_held, trend_up):
    """根据评分 / 盈亏比 / 大盘冷热 / 持仓联动 / 趋势，返回操作信号灯。

    返回 dict: { level, emoji, label, reasons: list, warnings: list }
    """
    reasons: list = []
    warnings: list = []

    # 0. 盈亏比太差（一票否决）
    if risk_reward is not None and risk_reward < 1.5:
        return {
            "level": "avoid",
            "emoji": "⛔",
            "label": "避免",
            "reasons": [f"盈亏比仅 {risk_reward:.2f}，性价比差"],
            "warnings": ["止损距离过近或止盈空间不足，建议放弃"],
        }

    # 1. 评分门槛
    if score < 60:
        return {
            "level": "wait",
            "emoji": "🔴",
            "label": "观望",
            "reasons": [f"综合评分 {score:.0f}，低于 60 分门槛"],
            "warnings": ["等评分回升或换标的"],
        }

    # 2. 大盘冷热权重
    if market_regime in ("cold", "cool"):
        # 大盘冷：要评分 ≥ 80 才"可小仓"
        if score >= 80:
            reasons.append(f"大盘{REGIME_LABEL.get(market_regime, '')}，但个股独立强势")
            warnings.append("建议仓位 ≤ 10%，抢反弹思路")
        else:
            return {
                "level": "wait",
                "emoji": "🔴",
                "label": "观望",
                "reasons": [f"大盘{REGIME_LABEL.get(market_regime, '')}，评分 {score:.0f} 不算高"],
                "warnings": ["大盘弱势时减少开仓"],
            }
    else:
        reasons.append(f"大盘{REGIME_LABEL.get(market_regime, '')}配合")

    # 3. 止损位检查
    if risk_pct is not None and risk_pct < 3:
        warnings.append(f"止损位仅 -{risk_pct:.1f}%，易被洗出，可放宽至 -5%")

    # 4. 持仓联动 + 趋势
    if is_held:
        if trend_up:
            return {
                "level": "add",
                "emoji": "🟡",
                "label": "可加仓",
                "reasons": reasons + ["你已持有，趋势确认向上"],
                "warnings": warnings + ["加仓不破止损位"],
            }
        else:
            return {
                "level": "hold_watch",
                "emoji": "🟡",
                "label": "持仓观察",
                "reasons": reasons + ["你已持有，评分未继续走高"],
                "warnings": warnings + ["不操作，等趋势明朗"],
            }
    else:
        return {
            "level": "buy",
            "emoji": "🟢",
            "label": "可建仓",
            "reasons": reasons + ["未持仓，可建仓"],
            "warnings": warnings,
        }


# ─────────────────────────────────────────────
# 表结构补丁（兼容老库；新表见 db.init_db）
# ─────────────────────────────────────────────
def _ensure_personal_tables():
    """个人持仓 + 自选股表（仅在本模块调用一次）。"""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS personal_position (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT NOT NULL,
            name        TEXT,
            shares      INTEGER NOT NULL,
            cost_price  REAL NOT NULL,
            stop_loss   REAL,
            take_profit REAL,
            note        TEXT,
            opened_at   TEXT,
            closed_at   TEXT,
            status      TEXT DEFAULT 'holding',
            created_at  TEXT,
            updated_at  TEXT,
            UNIQUE(code, opened_at)
        );
        CREATE INDEX IF NOT EXISTS idx_pp_code   ON personal_position(code);
        CREATE INDEX IF NOT EXISTS idx_pp_status ON personal_position(status);

        CREATE TABLE IF NOT EXISTS personal_watchlist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT NOT NULL UNIQUE,
            note        TEXT,
            created_at  TEXT
        );
        """)
        # stock_info 可能缺少 industry 列（个人投资者会希望按行业筛选）
        _safe_add_column(conn, "stock_info", "industry", "TEXT")


# ─────────────────────────────────────────────
# 1. 今日推荐：直接告诉散户"买什么、什么价买、什么价卖、什么价止损"
# ─────────────────────────────────────────────
@investor_bp.route("/today", methods=["GET"])
def today_recommendations():
    """今日推荐（action plan）
    来源：stock_signal 表（融合分高 + 已有明确买入价/止损/止盈）
    排序：fusion_score DESC，取前 N 条
    支持 ?date=2026-06-18 查看指定日期的推荐
    """
    limit = request.args.get("limit", default=10, type=int)
    limit = max(1, min(limit, 30))
    target_date = request.args.get("date")

    with get_conn() as conn:
        # 如果指定了日期，用指定日期；否则用最新日期
        if target_date:
            scan_date = target_date
        else:
            row = conn.execute("SELECT MAX(scan_date) AS d FROM stock_signal").fetchone()
            scan_date = row["d"] if row else None

        if not scan_date:
            return ok({"date": None, "count": 0, "items": []})

        rows = conn.execute(
            """
            SELECT
                s.code, s.name, s.price AS signal_price,
                s.buy_price, s.stop_loss, s.take_profit,
                s.fusion_score, s.vol_score, s.ma_score,
                s.diverge_score, s.bottom_score, s.whale_score,
                s.trigger_list, s.scan_date, s.trade_date,
                dp.close  AS latest_close,
                dp.pct_change AS latest_pct,
                dp.high   AS latest_high,
                dp.low    AS latest_low,
                i.industry
            FROM stock_signal s
            LEFT JOIN daily_price dp
                ON dp.code = s.code AND dp.trade_date = (
                    SELECT MAX(trade_date) FROM daily_price WHERE code = s.code
                )
            LEFT JOIN stock_info i ON i.code = s.code
            WHERE s.scan_date = ?
              AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
            ORDER BY COALESCE(s.fusion_score, 0) DESC
            LIMIT ?
            """,
            (scan_date, limit),
        ).fetchall()

        # ── 联动查询 1：当前大盘冷热（影响信号灯） ──
        regime_row = conn.execute(
            """
            SELECT AVG(pct_change) AS avg_pct
            FROM daily_price
            WHERE trade_date = (SELECT MAX(trade_date) FROM daily_price)
            """
        ).fetchone()
        avg_pct = regime_row["avg_pct"] if regime_row else None
        if avg_pct is None:
            market_regime = "unknown"
        elif avg_pct > 1.0:
            market_regime = "hot"
        elif avg_pct > 0.3:
            market_regime = "warm"
        elif avg_pct > -0.3:
            market_regime = "neutral"
        elif avg_pct > -1.0:
            market_regime = "cool"
        else:
            market_regime = "cold"

        # ── 联动查询 2：当前持仓（影响"可加仓/可减仓"信号） ──
        held_codes = {r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM personal_position WHERE status='holding'"
        ).fetchall()}

        # ── 联动查询 3：每只推荐票近 5 个交易日的打分（趋势判断） ──
        code_list = [r["code"] for r in rows]
        score_trend: dict = {}
        if code_list:
            placeholders = ",".join("?" * len(code_list))
            trend_rows = conn.execute(
                f"""
                SELECT code, trade_date, score FROM stock_score
                WHERE code IN ({placeholders})
                  AND trade_date >= date('now', '-7 days')
                ORDER BY code, trade_date DESC
                """,
                code_list,
            ).fetchall()
            for tr in trend_rows:
                score_trend.setdefault(tr["code"], []).append(tr["score"] or 0)

        items = []
        for r in rows:
            d = dict(r)
            # 散户视角的"建议价位"
            entry = d.get("buy_price") or d.get("signal_price") or d.get("latest_close") or 0
            stop = d.get("stop_loss") or 0
            tp = d.get("take_profit") or 0
            latest = d.get("latest_close") or 0
            risk_pct = round(((entry - stop) / entry * 100), 1) if entry > 0 and stop > 0 else None
            reward_pct = round(((tp - entry) / entry * 100), 1) if entry > 0 and tp > 0 else None
            risk_reward = None
            if risk_pct and reward_pct and risk_pct > 0:
                risk_reward = round(reward_pct / risk_pct, 2)

            # 散户可读的中文评分理由（基于 5 个子分）
            reasons = []
            weaknesses = []
            sub_scores = [
                ("vol_score",     "成交量放大",   "量能不足，未出现放量突破"),
                ("ma_score",      "均线多头",     "均线未形成多头排列"),
                ("diverge_score", "量价背离修复", "量价结构未现背离修复信号"),
                ("bottom_score",  "底部企稳",     "底部形态尚未确认"),
                ("whale_score",   "主力资金流入", "未检测到主力资金明显流入"),
            ]
            for key, good_label, weak_label in sub_scores:
                val = d.get(key) or 0
                if val >= 60:
                    reasons.append(good_label)
                elif val > 0:
                    weaknesses.append(weak_label)
            if not weaknesses:
                weaknesses.append("综合评分达标，暂无明显短板")

            # 触发策略列表（解析 trigger_list 字符串）
            triggers = []
            tl = d.get("trigger_list")
            if tl:
                try:
                    parsed = json.loads(tl)
                    if isinstance(parsed, list):
                        triggers = [str(x) for x in parsed]
                except (json.JSONDecodeError, TypeError):
                    triggers = [s.strip() for s in str(tl).split(",") if s.strip()]

            # ── 新增 1：3 个挂单位置（激进/稳健/保守） ──
            entry_strategy = None
            if entry > 0 and latest > 0:
                # 激进：现价 × 0.99（给 1% 折让，盘中可买到）
                # 稳健：现价 × 0.97（小幅回踩 3%）
                # 保守：max(建议价 × 0.94, 止损位 × 1.01)（深度回踩但不破止损）
                aggressive = round(latest * 0.99, 2)
                stable     = round(latest * 0.97, 2)
                conservative_floor = max(entry * 0.94, stop * 1.01) if stop > 0 else entry * 0.94
                conservative = round(conservative_floor, 2)
                # 推荐挂单：默认稳健（最常见的小回踩买点）
                recommend = "stable"
                recommend_label = "建议挂 稳健价"
                # 如果止损位离现价很近（< 4%），保守价 = 止损位 × 1.01 容易比稳健价高，回退到稳健
                if conservative > stable and stop > 0 and (latest - stop) / latest < 0.05:
                    conservative = round(stop * 1.02, 2)
                # 推荐文案
                pct_to_stable = round((latest - stable) / latest * 100, 1) if latest > 0 else None
                recommend_hint = (
                    f"挂 {stable}（较现价-{pct_to_stable}%）"
                    if pct_to_stable is not None else f"挂 {stable}"
                )
                entry_strategy = {
                    "aggressive": aggressive,
                    "stable": stable,
                    "conservative": conservative,
                    "recommend": recommend,
                    "recommend_label": recommend_label,
                    "recommend_hint": recommend_hint,
                }

            # ── 新增 2：建仓分批建议（按 1.5 万账户 20% 仓位 = 3000 元上限） ──
            position_plan = None
            if entry > 0:
                # 总金额上限：1.5 万 × 20% = 3000 元（A 股最小 1 手 = 100 股，按 100 整手）
                max_amount = 3000
                # 用激进价估算总股数（向下取整到 100 股一手）
                total_shares_100 = int(max_amount / max(aggressive if entry_strategy else entry, 0.01) // 100) * 100
                if total_shares_100 < 100:
                    total_shares_100 = 0
                if total_shares_100 > 0 and entry_strategy:
                    b1 = total_shares_100 // 2  # 50% 试仓
                    b2 = (total_shares_100 - b1) // 2  # 30% 加仓
                    b3 = total_shares_100 - b1 - b2  # 剩余 20%
                    p_a = entry_strategy["aggressive"]
                    p_s = entry_strategy["stable"]
                    p_c = entry_strategy["conservative"]
                    position_plan = {
                        "account_size": 15000,
                        "max_position_pct": 20,
                        "max_amount": max_amount,
                        "total_shares": total_shares_100,
                        "total_cost_est": round(total_shares_100 * p_s, 0),  # 用稳健价估算
                        "batches": [
                            {
                                "ratio": 0.5,
                                "shares": b1,
                                "price": p_a,
                                "amount": round(b1 * p_a, 0),
                                "note": "试仓",
                                "trigger": f"现价附近直接买入 {b1} 股",
                            },
                            {
                                "ratio": 0.3,
                                "shares": b2,
                                "price": p_s,
                                "amount": round(b2 * p_s, 0),
                                "note": "回踩加仓",
                                "trigger": f"回踩 -3% 买入 {b2} 股",
                            },
                            {
                                "ratio": 0.2,
                                "shares": b3,
                                "price": p_c,
                                "amount": round(b3 * p_c, 0),
                                "note": "深度回踩加仓",
                                "trigger": f"深度回踩 -6% 买入 {b3} 股",
                            },
                        ],
                        "warning": "若资金不足一手（100 股），整张卡片跳过",
                    }

            # ── 新增 3：操作信号灯（持仓联动 + 大盘冷热 + 盈亏比） ──
            score = d.get("fusion_score") or 0
            is_held = d["code"] in held_codes
            trend = score_trend.get(d["code"], [])
            trend_up = len(trend) >= 2 and trend[0] > trend[1]  # 今日 > 昨日

            signal = _calc_signal(
                score=score,
                risk_reward=risk_reward,
                risk_pct=risk_pct,
                market_regime=market_regime,
                is_held=is_held,
                trend_up=trend_up,
            )

            items.append({
                "code": d.get("code"),
                "name": d.get("name"),
                "industry": d.get("industry"),
                "fusion_score": round(d.get("fusion_score") or 0, 1),
                "latest_close": d.get("latest_close"),
                "latest_pct": d.get("latest_pct"),
                "action_plan": {
                    "entry_price": round(entry, 2) if entry else None,
                    "stop_loss": round(stop, 2) if stop else None,
                    "take_profit": round(tp, 2) if tp else None,
                    "risk_pct": risk_pct,
                    "reward_pct": reward_pct,
                    "risk_reward_ratio": risk_reward,
                },
                "entry_strategy": entry_strategy,   # 新增
                "position_plan": position_plan,     # 新增
                "signal": signal,                   # 新增
                "reasons": reasons,
                "weaknesses": weaknesses,
                "triggers": triggers,
                "scan_date": d.get("scan_date"),
                "trade_date": d.get("trade_date"),
            })
        return ok({
            "date": items[0]["scan_date"] if items else None,
            "count": len(items),
            "items": _sanitize(items),
            "market_regime": market_regime,  # 新增：让前端知道当前大盘冷热
        })


@investor_bp.route("/today/dates", methods=["GET"])
def available_dates():
    """获取有推荐数据的日期列表（供日期选择器使用）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal ORDER BY scan_date DESC LIMIT 60"
        ).fetchall()
        dates = [r["scan_date"] for r in rows]
        return ok({"dates": dates})


# ─────────────────────────────────────────────
# 2. 市场速览：指数涨跌 + 北向资金 + 大盘冷热
# ─────────────────────────────────────────────
@investor_bp.route("/market-overview", methods=["GET"])
def market_overview():
    """市场速览：核心指数 + 北向资金 + 大盘情绪冷热"""
    indices = {
        "000001": "上证指数",
        "399001": "深证成指",
        "399006": "创业板指",
        "000300": "沪深300",
        "000688": "科创50",
        "000905": "中证500",
    }

    with get_conn() as conn:
        # 指数最新一日
        idx_rows = conn.execute(
            """
            SELECT code, MAX(trade_date) AS d FROM index_daily GROUP BY code
            """
        ).fetchall()
        latest_dates = {r["code"]: r["d"] for r in idx_rows if r["d"]}

        index_cards = []
        for code, name in indices.items():
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

        # 北向资金最近一日
        north = conn.execute(
            """SELECT trade_date, north_net_buy, hgt_net_buy, sgt_net_buy
               FROM stock_hsgt_north
               ORDER BY trade_date DESC LIMIT 1"""
        ).fetchone()
        north_data = dict(north) if north else None

        # 大盘冷热：近 20 日指数平均涨幅 + 成交活跃度（用 daily_price 总成交额近似）
        sentiment_row = conn.execute(
            """
            SELECT
              AVG(pct_change) AS avg_pct,
              SUM(amount)     AS total_amount
            FROM daily_price
            WHERE trade_date = (SELECT MAX(trade_date) FROM daily_price)
            """
        ).fetchone()
        avg_pct = sentiment_row["avg_pct"] if sentiment_row else None
        total_amt = sentiment_row["total_amount"] if sentiment_row else None

        # 简单规则：日均涨幅 > 0.5% 偏热，< -0.5% 偏冷
        if avg_pct is None:
            regime = "unknown"
            regime_label = "数据不足"
        elif avg_pct > 1.0:
            regime, regime_label = "hot", "火热（注意追高风险）"
        elif avg_pct > 0.3:
            regime, regime_label = "warm", "偏暖（可适度参与）"
        elif avg_pct > -0.3:
            regime, regime_label = "neutral", "震荡（精选个股）"
        elif avg_pct > -1.0:
            regime, regime_label = "cool", "偏冷（控制仓位）"
        else:
            regime, regime_label = "cold", "冰点（防守为主）"

        return ok({
            "indices": _sanitize(index_cards),
            "north_bound": _sanitize({
                "trade_date": north_data["trade_date"] if north_data else None,
                "north_net_buy": north_data["north_net_buy"] if north_data else None,
                "hgt_net_buy":   north_data["hgt_net_buy"] if north_data else None,
                "sgt_net_buy":   north_data["sgt_net_buy"] if north_data else None,
                "label": (
                    "大幅净流入，外资积极"
                    if north_data and north_data["north_net_buy"] and north_data["north_net_buy"] > 50e8 else
                    "净流入，外资偏多" if north_data and north_data["north_net_buy"] and north_data["north_net_buy"] > 0 else
                    "净流出，外资偏空" if north_data and north_data["north_net_buy"] and north_data["north_net_buy"] < -50e8 else
                    "小幅净流出"
                    if north_data and north_data["north_net_buy"] else "暂无数据"
                ),
            }),
            "sentiment": {
                "regime": regime,
                "label": regime_label,
                "avg_pct": round(avg_pct, 2) if avg_pct is not None else None,
                "total_amount": round(total_amt / 1e8, 1) if total_amt else None,  # 亿
            },
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


# ─────────────────────────────────────────────
# 3. 个人持仓：CRUD + 实时盈亏
# ─────────────────────────────────────────────
@investor_bp.route("/positions", methods=["GET"])
def list_positions():
    """列出当前持仓（含最新价 + 浮动盈亏）"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   dp.close AS latest_close,
                   dp.pct_change AS latest_pct
            FROM personal_position p
            LEFT JOIN daily_price dp
              ON dp.code = p.code AND dp.trade_date = (
                  SELECT MAX(trade_date) FROM daily_price WHERE code = p.code
              )
            WHERE p.status = 'holding'
            ORDER BY p.opened_at DESC
            """
        ).fetchall()

        items = []
        total_cost = 0.0
        total_value = 0.0
        for r in rows:
            d = dict(r)
            cost = (d.get("cost_price") or 0) * (d.get("shares") or 0)
            value = (d.get("latest_close") or 0) * (d.get("shares") or 0)
            pnl = value - cost if d.get("latest_close") else 0
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0

            # 是否触发止损/止盈预警
            warning = None
            sl = d.get("stop_loss")
            tp = d.get("take_profit")
            if d.get("latest_close") and sl and d["latest_close"] <= sl:
                warning = "已触及止损价，请考虑止损"
            elif d.get("latest_close") and tp and d["latest_close"] >= tp:
                warning = "已触及止盈价，可考虑止盈"

            total_cost += cost
            total_value += value
            items.append({
                **{k: d.get(k) for k in (
                    "id", "code", "name", "shares", "cost_price",
                    "stop_loss", "take_profit", "note", "opened_at"
                )},
                "latest_close": d.get("latest_close"),
                "latest_pct": d.get("latest_pct"),
                "cost": round(cost, 2),
                "value": round(value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "warning": warning,
            })

        # 持仓集中度
        concentration = None
        if total_value > 0:
            max_single = max((it["value"] for it in items), default=0)
            concentration = round(max_single / total_value * 100, 1)

        return ok({
            "positions": _sanitize(items),
            "summary": {
                "total_cost": round(total_cost, 2),
                "total_value": round(total_value, 2),
                "total_pnl": round(total_value - total_cost, 2),
                "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2)
                                  if total_cost > 0 else 0,
                "position_count": len(items),
                "max_concentration_pct": concentration,
            },
        })


@investor_bp.route("/positions", methods=["POST"])
def add_position():
    """新增持仓 {code, shares, cost_price, stop_loss?, take_profit?, note?}"""
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    if not code or not body.get("shares") or not body.get("cost_price"):
        return fail("缺少 code/shares/cost_price", 400)

    try:
        shares = int(body["shares"])
        cost_price = float(body["cost_price"])
    except (ValueError, TypeError):
        return fail("shares/cost_price 必须是数字", 400)

    stop_loss = body.get("stop_loss")
    take_profit = body.get("take_profit")

    with get_conn() as conn:
        # 自动补全股票名
        info = conn.execute(
            "SELECT name FROM stock_info WHERE code = ?", (code,)
        ).fetchone()
        name = info["name"] if info else body.get("name", code)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            INSERT OR REPLACE INTO personal_position
              (code, name, shares, cost_price, stop_loss, take_profit,
               note, opened_at, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'holding', ?, ?)
            """,
            (code, name, shares, cost_price,
             float(stop_loss) if stop_loss else None,
             float(take_profit) if take_profit else None,
             body.get("note"), now, now, now),
        )
        new_id = conn.execute(
            "SELECT id FROM personal_position WHERE code=? AND opened_at=?",
            (code, now),
        ).fetchone()
        return ok({"id": new_id["id"] if new_id else None, "code": code, "name": name})


@investor_bp.route("/positions/<int:pid>", methods=["DELETE"])
def delete_position(pid: int):
    """删除/平仓持仓"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE personal_position SET status='closed', closed_at=?, updated_at=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             pid),
        )
        return ok({"closed": pid})


# ─────────────────────────────────────────────
# 4. 自选股
# ─────────────────────────────────────────────
@investor_bp.route("/watchlist", methods=["GET"])
def get_watchlist():
    """自选股列表（附带最新价 + 当日打分）"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.code, w.note, w.created_at,
                   i.name, i.industry,
                   dp.close  AS latest_close,
                   dp.pct_change AS latest_pct,
                   ss.score AS latest_score
            FROM personal_watchlist w
            LEFT JOIN stock_info i      ON i.code = w.code
            LEFT JOIN daily_price dp    ON dp.code = w.code
                AND dp.trade_date = (SELECT MAX(trade_date) FROM daily_price WHERE code = w.code)
            LEFT JOIN stock_score ss    ON ss.code = w.code
                AND ss.trade_date = (SELECT MAX(trade_date) FROM stock_score WHERE code = w.code)
            ORDER BY w.created_at DESC
            """
        ).fetchall()
        return ok(_sanitize([dict(r) for r in rows]))


@investor_bp.route("/watchlist", methods=["POST"])
def add_watchlist():
    """加入自选
    行为：
      1) 写入 personal_watchlist
      2) 异步触发该股历史数据补拉 + 策略分重算（避免阻塞 HTTP 响应）
      3) 返回 task_id，前端可通过 /api/sync/status/<task_id> 轮询进度
         或 3-5 秒后直接调 GET /api/investor/watchlist 看到 latest_close/latest_score
    body: {"code": "600519", "note": "..."}
    """
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        return fail("缺少 code", 400)
    if not (code.isdigit() and len(code) == 6):
        return fail("code 格式非法（需 6 位数字）", 400)

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO personal_watchlist(code, note, created_at) VALUES (?,?,?)",
            (code, body.get("note"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        inserted = cur.rowcount > 0

    task_id = None
    sync_status = "already_has_data"
    try:
        from core.db import has_watchlist_data
        from core.task_queue import submit_task, is_any_running
        # 已经在跑其它任务时，仍允许加自选（不阻塞）
        if is_any_running() and not has_watchlist_data(code):
            sync_status = "queued"
        elif not has_watchlist_data(code):
            from core.sync import sync_single_stock_to_watchlist
            task_id = submit_task(lambda: sync_single_stock_to_watchlist(code, verbose=False))
            sync_status = "syncing"
        # else: sync_status = "already_has_data" —— 不用再拉
    except Exception as e:
        # 异步触发失败不阻塞 INSERT 已成功的响应
        sync_status = f"sync_dispatch_failed: {e}"

    return ok({"code": code, "task_id": task_id, "sync_status": sync_status,
               "message": "已加入自选，历史数据补拉中" if sync_status == "syncing" else "已加入自选"})


@investor_bp.route("/watchlist/<code>", methods=["DELETE"])
def remove_watchlist(code: str):
    """移出自选
    行为：仅删除 personal_watchlist 记录，**不**清理 daily_price / stock_score 中的历史数据。
    原因：回测 / 历史推荐 / 持仓分析都仍需这些数据。
    """
    if not (code.isdigit() and len(code) == 6):
        return fail("code 格式非法", 400)
    with get_conn() as conn:
        conn.execute("DELETE FROM personal_watchlist WHERE code = ?", (code,))
        return ok({"removed": code, "message": "已移出自选，历史行情数据保留"})


# ─────────────────────────────────────────────
# 5. 风险告警
# ─────────────────────────────────────────────
@investor_bp.route("/alerts", methods=["GET"])
def risk_alerts():
    """当前风险告警（基于持仓）"""
    with get_conn() as conn:
        positions = conn.execute(
            """
            SELECT p.*, dp.close AS latest_close
            FROM personal_position p
            LEFT JOIN daily_price dp ON dp.code=p.code
                AND dp.trade_date = (SELECT MAX(trade_date) FROM daily_price WHERE code=p.code)
            WHERE p.status='holding'
            """
        ).fetchall()

        alerts = []
        for r in positions:
            d = dict(r)
            latest = d.get("latest_close")
            if not latest:
                continue
            # 1) 触及止损
            if d.get("stop_loss") and latest <= d["stop_loss"]:
                alerts.append({
                    "level": "danger",
                    "code": d["code"], "name": d.get("name"),
                    "msg": f"已触及止损价 {d['stop_loss']:.2f}，当前 {latest:.2f}",
                })
            # 2) 接近止损（差 ≤3%）
            elif d.get("stop_loss") and latest <= d["stop_loss"] * 1.03:
                alerts.append({
                    "level": "warning",
                    "code": d["code"], "name": d.get("name"),
                    "msg": f"接近止损价（差 {((latest/d['stop_loss']-1)*100):.1f}%）",
                })
            # 3) 触及止盈
            if d.get("take_profit") and latest >= d["take_profit"]:
                alerts.append({
                    "level": "info",
                    "code": d["code"], "name": d.get("name"),
                    "msg": f"已触及止盈价 {d['take_profit']:.2f}，可考虑止盈",
                })

        # 持仓集中度告警
        rows = conn.execute(
            """
            SELECT p.code, p.shares, dp.close,
                   (p.cost_price * p.shares) AS cost,
                   (dp.close * p.shares)    AS value
            FROM personal_position p
            LEFT JOIN daily_price dp ON dp.code=p.code
                AND dp.trade_date = (SELECT MAX(trade_date) FROM daily_price WHERE code=p.code)
            WHERE p.status='holding'
            """
        ).fetchall()
        total = sum((r["value"] or 0) for r in rows)
        if total > 0:
            for r in rows:
                v = r["value"] or 0
                ratio = v / total * 100
                if ratio >= 20:
                    alerts.append({
                        "level": "warning",
                        "code": r["code"], "name": r["code"],
                        "msg": f"单股仓位占比 {ratio:.1f}%，过于集中（建议 <20%）",
                    })

        # 持仓票打分下降告警
        alerts.extend(_score_drop_alerts(conn))

        return ok({"alerts": alerts, "count": len(alerts)})


# ─────────────────────────────────────────────
# 6. 历史推荐回测：过去 30 天推荐的真实表现
# ─────────────────────────────────────────────
@investor_bp.route("/recommendations/history", methods=["GET"])
def recommendations_history():
    """历史推荐回测：过去 N 天推荐的股票，到今天的实际涨跌
    GET /api/investor/recommendations/history?days=30
    """
    days = request.args.get("days", "30", type=int)
    days = max(1, min(days, 90))

    with get_conn() as conn:
        # 获取最近 N 天的推荐记录（stock_signal 中有 buy_price 的）
        signals = conn.execute(
            """
            SELECT s.code, s.name, s.scan_date, s.buy_price, s.stop_loss, s.take_profit,
                   s.fusion_score
            FROM stock_signal s
            WHERE s.scan_date >= date('now', ?)
              AND s.buy_price IS NOT NULL
            ORDER BY s.scan_date DESC
            """,
            (f"-{days} days",),
        ).fetchall()

        if not signals:
            return ok({"items": [], "summary": {}, "days": days})

        # 按 (code, scan_date) 去重，取每只票最新一次推荐
        seen = {}
        for r in signals:
            key = r["code"]
            if key not in seen:
                seen[key] = dict(r)

        results = []
        for code, sig in seen.items():
            # 推荐日的价格
            entry = sig.get("buy_price") or 0
            if entry <= 0:
                continue

            # 推荐日之后的最新价格
            latest = conn.execute(
                """
                SELECT close, trade_date FROM daily_price
                WHERE code = ? AND trade_date > ?
                ORDER BY trade_date ASC LIMIT 1
                """,
                (code, sig["scan_date"]),
            ).fetchone()

            # 最新价格（不限日期）
            latest_any = conn.execute(
                """
                SELECT close, trade_date FROM daily_price
                WHERE code = ?
                ORDER BY trade_date DESC LIMIT 1
                """,
                (code,),
            ).fetchone()

            # 用推荐日后第一个交易日的价格计算收益
            if latest:
                current = latest["close"]
                current_date = latest["trade_date"]
            elif latest_any:
                current = latest_any["close"]
                current_date = latest_any["trade_date"]
            else:
                continue

            pnl_pct = round((current - entry) / entry * 100, 2) if entry > 0 else 0
            hit_stop = False
            hit_tp = False
            if sig.get("stop_loss") and current <= sig["stop_loss"]:
                hit_stop = True
            if sig.get("take_profit") and current >= sig["take_profit"]:
                hit_tp = True

            # 判断是否仍在持仓中（推荐日后 N 天内）
            from datetime import datetime as _dt
            try:
                scan = _dt.strptime(sig["scan_date"], "%Y-%m-%d")
                now = _dt.now()
                days_held = (now - scan).days
            except (ValueError, TypeError):
                days_held = 0

            results.append({
                "code": code,
                "name": sig.get("name"),
                "scan_date": sig["scan_date"],
                "entry_price": round(entry, 2),
                "current_price": round(current, 2),
                "current_date": current_date,
                "pnl_pct": pnl_pct,
                "hit_stop_loss": hit_stop,
                "hit_take_profit": hit_tp,
                "fusion_score": round(sig.get("fusion_score") or 0, 1),
                "days_held": days_held,
            })

        # 汇总统计
        if results:
            wins = [r for r in results if r["pnl_pct"] > 0]
            losses = [r for r in results if r["pnl_pct"] <= 0]
            avg_return = sum(r["pnl_pct"] for r in results) / len(results)
            win_rate = len(wins) / len(results) * 100 if results else 0
            stop_count = sum(1 for r in results if r["hit_stop_loss"])
            tp_count = sum(1 for r in results if r["hit_take_profit"])
            summary = {
                "total": len(results),
                "win_count": len(wins),
                "loss_count": len(losses),
                "win_rate": round(win_rate, 1),
                "avg_return": round(avg_return, 2),
                "best_return": round(max(r["pnl_pct"] for r in results), 2),
                "worst_return": round(min(r["pnl_pct"] for r in results), 2),
                "stop_loss_count": stop_count,
                "take_profit_count": tp_count,
            }
        else:
            summary = {}

        # 按收益排序
        results.sort(key=lambda x: x["pnl_pct"], reverse=True)

        return ok({
            "items": _sanitize(results),
            "summary": summary,
            "days": days,
        })


# ─────────────────────────────────────────────
# 7. 持仓关联打分告警：持仓票打分下降时自动告警
# ─────────────────────────────────────────────
def _score_drop_alerts(conn):
    """检查持仓票的打分变化，生成打分下降告警"""
    alerts = []
    # 获取所有持仓票
    positions = conn.execute(
        "SELECT code, name FROM personal_position WHERE status='holding'"
    ).fetchall()

    for pos in positions:
        code = pos["code"]
        name = pos["name"] or code
        # 最近两次打分
        scores = conn.execute(
            """
            SELECT trade_date, score FROM stock_score
            WHERE code = ?
            ORDER BY trade_date DESC LIMIT 2
            """,
            (code,),
        ).fetchall()

        if len(scores) >= 2:
            latest_score = scores[0]["score"] or 0
            prev_score = scores[1]["score"] or 0
            drop = prev_score - latest_score
            if drop >= 10:
                alerts.append({
                    "level": "warning",
                    "code": code,
                    "name": name,
                    "msg": f"打分下降 {drop:.0f} 分（{prev_score:.0f}→{latest_score:.0f}），注意风险",
                })
            elif drop >= 5:
                alerts.append({
                    "level": "info",
                    "code": code,
                    "name": name,
                    "msg": f"打分小幅回落 {drop:.0f} 分（{prev_score:.0f}→{latest_score:.0f}）",
                })
        elif len(scores) == 1:
            # 只有一次打分，检查是否低于阈值
            s = scores[0]["score"] or 0
            if s < 30:
                alerts.append({
                    "level": "warning",
                    "code": code,
                    "name": name,
                    "msg": f"当前打分仅 {s:.0f} 分，低于安全线",
                })

    return alerts


# ─────────────────────────────────────────────
# 8. 量化指标白话解释（教育散户）
# ─────────────────────────────────────────────
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


@investor_bp.route("/explain/<metric>", methods=["GET"])
def explain(metric: str):
    """指标白话解释"""
    item = EXPLAINERS.get(metric)
    if not item:
        return fail(f"未知指标: {metric}", 404)
    return ok(item)


@investor_bp.route("/explain", methods=["GET"])
def explain_all():
    """列出所有可解释的指标"""
    return ok({"metrics": list(EXPLAINERS.keys()), "items": EXPLAINERS})


# ─────────────────────────────────────────────
# 启动时确保表存在（被 app.py 注册时调用）
# ─────────────────────────────────────────────
def init_investor_tables():
    _ensure_personal_tables()
