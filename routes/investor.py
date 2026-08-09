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
import time
from datetime import date, datetime
from typing import Optional

from flask import Blueprint, request

from core.db import get_conn, _safe_add_column, init_db
from utils.api import ok, fail
from utils.serialization import sanitize_numeric as _sanitize
from config.personal_config import (
    POSITION_PLAN_ACCOUNT, POSITION_PLAN_MAX_PCT,
    MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES,
)
from config.strategy_params import T1_GAP_GUARD


investor_bp = Blueprint("investor", __name__, url_prefix="/api/investor")


# ─────────────────────────────────────────────
# 信号灯判定（散户最关心的"今天能不能动手"）
# ─────────────────────────────────────────────
REGIME_LABEL = {
    "hot": "火热", "warm": "偏暖", "neutral": "震荡",
    "cool": "偏冷", "cold": "冰点", "unknown": "数据不足",
}


# ─────────────────────────────────────────────
# 大盘冷热：多日宽度 composite（替代单日均值，抗单日噪音）
# ─────────────────────────────────────────────
_regime_cache = {"ts": 0.0, "regime": None}
_REGIME_CACHE_TTL = 600  # 10 分钟（盘中数据低频变化）


def _regime_from_avg_pct(avg_pct):
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


def _compute_market_regime(conn):
    """
    多日宽度 composite 市场冷热（10 分钟缓存）。

    综合三项（替代原单日 AVG(pct_change) 的"一天定生死"，避免冰点↔火热来回跳）：
      1) 近 5 个交易日涨跌家数比（多日均值，抗单日暴涨暴跌）
      2) 全市场站上 MA20 的个股占比（真实宽度，近 5 日均值）
      3) 沪深300 / 中证500 近 5 日斜率均值
    composite = 50 + 三项贡献（各 ±20 / ±20 / ±15），正常区间约 25~75。
    档位：>=60 hot / >=55 warm / >=45 neutral / >=40 cool / else cold。
    任一环节异常时回退单日均值逻辑（原行为）。
    """
    now = time.time()
    if _regime_cache["regime"] and (now - _regime_cache["ts"]) < _REGIME_CACHE_TTL:
        return _regime_cache["regime"]

    try:
        # 1) 涨跌家数比（近 5 个交易日）
        rows = conn.execute(
            """
            SELECT trade_date,
                   SUM(CASE WHEN pct_change > 0 THEN 1 ELSE 0 END) AS up,
                   COUNT(*) AS total
            FROM daily_price
            WHERE trade_date >= date('now', '-12 days')
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 5
            """
        ).fetchall()
        if not rows:
            regime = "unknown"
            _regime_cache.update({"ts": now, "regime": regime})
            return regime
        up_ratio = sum((r["up"] or 0) / r["total"] for r in rows if r["total"]) / len(rows)

        # 2) 宽度：全市场站上 MA20 的个股占比（近 5 个交易日）
        wrows = conn.execute(
            """
            SELECT d.trade_date,
                   AVG(CASE WHEN d.close > d.ma20 THEN 1.0 ELSE 0.0 END) AS width
            FROM (
                SELECT code, trade_date, close,
                       AVG(close) OVER (
                           PARTITION BY code ORDER BY trade_date
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                       ) AS ma20
                FROM daily_price
                WHERE trade_date >= date('now', '-30 days')
            ) d
            GROUP BY d.trade_date
            ORDER BY d.trade_date DESC
            LIMIT 5
            """
        ).fetchall()
        width = sum(float(r["width"] or 0) for r in wrows) / len(wrows) if wrows else 0.5

        # 3) 指数斜率（沪深300 / 中证500，近 5 日）
        slopes = []
        for _icode in ("000300", "000905"):
            irows = conn.execute(
                "SELECT trade_date, close FROM index_daily WHERE code=? "
                "ORDER BY trade_date DESC LIMIT 6", (_icode,)).fetchall()
            if len(irows) >= 2 and irows[-1]["close"]:
                slopes.append((float(irows[0]["close"]) - float(irows[-1]["close"]))
                              / float(irows[-1]["close"]))
        idx_slope = sum(slopes) / len(slopes) if slopes else 0.0

        score = 50.0 + (up_ratio - 0.5) * 100.0 * 0.4 \
                      + (width - 0.5) * 100.0 * 0.4 \
                      + idx_slope * 300.0
        if score >= 60:
            regime = "hot"
        elif score >= 55:
            regime = "warm"
        elif score >= 45:
            regime = "neutral"
        elif score >= 40:
            regime = "cool"
        else:
            regime = "cold"
    except Exception:
        # composite 计算失败（表结构/数据异常）→ 回退原单日均值口径
        avg_pct = conn.execute(
            "SELECT AVG(pct_change) AS avg_pct FROM daily_price "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM daily_price)"
        ).fetchone()["avg_pct"]
        regime = _regime_from_avg_pct(avg_pct)

    _regime_cache.update({"ts": now, "regime": regime})
    return regime


def _calc_signal(*, score, risk_reward, risk_pct, market_regime, is_held, trend_up):
    """根据评分 / 盈亏比 / 大盘冷热 / 持仓联动 / 趋势，返回操作信号灯。

    注意：fusion_score 量纲为 0~50（strategies.py 中 clip(upper=50)），
    门槛按此量纲设定：30 分≈百分制 60，40 分≈百分制 80。

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

    # 1. 评分门槛（30/50 ≈ 百分制 60）
    if score < 30:
        return {
            "level": "wait",
            "emoji": "🔴",
            "label": "观望",
            "reasons": [f"综合评分 {score:.0f}（满分 50），低于 30 分门槛"],
            "warnings": ["等评分回升或换标的"],
        }

    # 2. 大盘冷热权重
    if market_regime in ("cold", "cool"):
        # 大盘冷：要评分 ≥ 40（≈百分制 80）才“可小仓”
        if score >= 40:
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
        # 自选股价格预警：目标价 + 触发方向（above=现价↑目标 / below=现价↓目标）
        _safe_add_column(conn, "personal_watchlist", "target_price", "REAL")
        _safe_add_column(conn, "personal_watchlist", "alert_dir", "TEXT")


# ─────────────────────────────────────────────
# 1. 今日推荐：直接告诉散户“买什么、什么价买、什么价卖、什么价止损”
# ─────────────────────────────────────────────

# 出场跟踪起始日：该日期（含）起的推荐纳入跟踪，此前的推荐放弃；无结束时间限制
EXIT_TRACK_START_DATE = "2026-07-20"


def _split_continuous_segments(seg: list, df, horizon: str) -> list:
    """将一段连续推荐按卖出断点拆分。

    段内持仓若已在某交易日卖出（触发止损/止盈/到期），卖出日（含）之后的新推荐
    属于新一轮交易，不能与卖出前的推荐合并展示（如：07-31 推荐、08-03 更新止损、
    08-04 触发止损卖出，则 08-04 的新推荐应独立成段）。递归拆分直至无断点。
    """
    if len(seg) <= 1 or df is None or df.empty:
        return [seg]
    import pandas as pd
    from strategy.exit_advisor import evaluate_exit_by_prices, HORIZON_MAX_HOLD

    first, last = seg[0], seg[-1]
    if hasattr(df.index, 'strftime'):
        after = df[df.index > pd.Timestamp(first["scan_date"])]
    else:
        after = df[df.index > first["scan_date"]]
    if after.empty:
        return [seg]
    _f = after.iloc[0]
    entry_price = float(_f["open"]) if _f["open"] else (
        float(_f["close"]) or first["buy_price"])

    # 用段内最新止损/止盈评估整段持仓
    adv = evaluate_exit_by_prices(
        entry_price=entry_price,
        entry_date=first["scan_date"],
        df=df,
        stop_loss=last["stop_loss"],
        take_profit=last["take_profit"],
        max_hold_days=HORIZON_MAX_HOLD.get(horizon),
    )
    exit_date = (adv.get("detail") or {}).get("exit_date")
    if not exit_date:
        return [seg]   # 未卖出，整段保留

    # 卖出日（含）当天及之后的新推荐 → 新一轮交易，从该处拆开
    split_at = None
    for i, s in enumerate(seg):
        if s["scan_date"] >= exit_date:
            split_at = i
            break
    if split_at is None or split_at == 0:
        return [seg]
    # 注意：返回的是「段列表」——前半段包成单元素列表再与递归结果拼接
    return [seg[:split_at]] + _split_continuous_segments(seg[split_at:], df, horizon)


def _get_exit_advice(d: dict) -> Optional[dict]:
    """P0: 对推荐日后 14 天内的票评估出场状态（与出场跟踪同口径）。

    买入价 = 推荐日后首个交易日开盘价；卖出条件 = 推荐自带止损/止盈价 + 周期持仓上限。
    返回 None（新推荐，无需出场评估）或 {status, reason, detail}。
    """
    scan_date = d.get("scan_date")
    code = d.get("code")
    if not scan_date or not code:
        return None

    # 只对推荐日后 14 天内的票评估
    try:
        from datetime import datetime as _dt
        scan_dt = _dt.strptime(str(scan_date)[:10], "%Y-%m-%d")
        days_since = (_dt.now() - scan_dt).days
        if days_since > 14 or days_since < 0:  # 超过 14 天不再评估
            return None
    except (ValueError, TypeError):
        return None

    try:
        from strategy.exit_advisor import evaluate_exit_by_prices, HORIZON_MAX_HOLD
        import pandas as pd
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT trade_date, open, close, high, low
                FROM daily_price WHERE code = ?
                ORDER BY trade_date ASC
            """, (code,)).fetchall()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
        if hasattr(df.index, 'strftime'):
            after = df[df.index > pd.Timestamp(str(scan_date)[:10])]
        else:
            after = df[df.index > str(scan_date)[:10]]
        if after.empty:
            entry_price = d.get("buy_price") or d.get("signal_price") or 0
        else:
            first = after.iloc[0]
            entry_price = float(first["open"]) if first["open"] else (
                float(first["close"]) or d.get("buy_price") or 0)
        if not entry_price or entry_price <= 0:
            return None
        advice = evaluate_exit_by_prices(
            entry_price=entry_price,
            entry_date=str(scan_date)[:10],
            df=df,
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            max_hold_days=HORIZON_MAX_HOLD.get(d.get("horizon") or "short"),
        )
        return advice
    except Exception:
        return None


@investor_bp.route("/today", methods=["GET"])
def today_recommendations():
    """今日推荐（action plan）—— 按 short/mid/long 三周期分组

    来源：stock_signal 表（融合分高 + 已有明确买入价/止损/止盈）
    分组：每组按 fusion_score DESC 各取 limit 条（默认每组 8）
    支持 ?date=2026-06-18 查看指定日期；?horizon=short 只看单组
    返回 {date, market_regime, count, groups:{short,mid,long}, items(=short 组别名，兼容旧前端)}
    """
    limit = request.args.get("limit", default=8, type=int)
    limit = max(1, min(limit, 30))
    horizon_filter = (request.args.get("horizon") or "").strip().lower() or None
    if horizon_filter not in (None, "short", "mid", "long"):
        return fail("horizon 仅支持 short / mid / long")
    target_date = request.args.get("date")

    with get_conn() as conn:
        # 如果指定了日期，用指定日期；否则用最新日期
        if target_date:
            scan_date = target_date
        else:
            row = conn.execute("SELECT MAX(scan_date) AS d FROM stock_signal").fetchone()
            scan_date = row["d"] if row else None

        if not scan_date:
            return ok({
                "date": None, "count": 0, "items": [],
                "groups": {"short": [], "mid": [], "long": []},
                "market_regime": "unknown",
            })

        # stock_info.pe_ttm 可能不存在（老库）—— 降级：有才启用长线估值过滤
        has_pe_ttm = False
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_info)").fetchall()}
            has_pe_ttm = "pe_ttm" in cols
        except Exception:
            has_pe_ttm = False

        horizons = [horizon_filter] if horizon_filter else ["short", "mid", "long"]
        # 板块限制：小资金未开通科创/创业板权限，仅推主板（前缀来自配置常量，非用户输入）
        board_filter = ""
        if MAIN_BOARD_ONLY:
            board_filter = "".join(
                f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
        rows_by_horizon: dict = {}
        for hz in horizons:
            pe_filter = ""
            if hz == "long" and has_pe_ttm:
                # 长线降级财务过滤：pe_ttm 有值时剔除亏损股（<=0）与明显高估股（>100）
                pe_filter = " AND (i.pe_ttm IS NULL OR (i.pe_ttm > 0 AND i.pe_ttm <= 100))"
            rows_by_horizon[hz] = conn.execute(
                f"""
                SELECT
                    s.code, s.name, s.price AS signal_price,
                    s.buy_price, s.stop_loss, s.take_profit,
                    s.fusion_score, s.vol_score, s.ma_score,
                    s.diverge_score, s.bottom_score, s.whale_score,
                    s.trigger_list, s.scan_date, s.trade_date,
                    COALESCE(s.horizon, 'short') AS horizon,
                    s.strategy,
                    lp.close  AS latest_close,
                    lp.pct_change AS latest_pct,
                    lp.high   AS latest_high,
                    lp.low    AS latest_low,
                    i.industry
                FROM stock_signal s
                LEFT JOIN latest_price lp ON lp.code = s.code
                LEFT JOIN stock_info i ON i.code = s.code
                WHERE s.scan_date = ?
                  AND COALESCE(s.horizon, 'short') = ?
                  AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
                  AND s.name NOT LIKE '%ST%'
                  AND s.name NOT LIKE '%退%'
                  {board_filter}
                  {pe_filter}
                ORDER BY COALESCE(s.fusion_score, 0) DESC
                LIMIT ?
                """,
                # 多取 3 倍候选：盈亏比不达标的「避免」级会被剔除，由替补顶上
                (scan_date, hz, limit * 3),
            ).fetchall()
        all_rows = [r for hz in horizons for r in rows_by_horizon[hz]]

        # ── 联动查询 1：当前大盘冷热（影响信号灯；多日宽度 composite，10 分钟缓存） ──
        market_regime = _compute_market_regime(conn)
        # 最新交易日（供前端判断推荐是否已是最新，避免盘前/非交易日反复触发重算）
        _latest_td = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_price"
        ).fetchone()["d"]

        # ── 联动查询 2：当前持仓（影响"可加仓/可减仓"信号） ──
        held_codes = {r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM personal_position WHERE status='holding'"
        ).fetchall()}

        # ── 联动查询 3：每只推荐票近 5 个交易日的打分（趋势判断） ──
        code_list = [r["code"] for r in all_rows]
        score_trend: dict = {}
        if code_list:
            placeholders = ",".join("?" * len(code_list))
            trend_rows = conn.execute(
                f"""
                SELECT code, trade_date, fusion_score AS score FROM daily_price
                WHERE code IN ({placeholders})
                  AND trade_date >= date('now', '-7 days')
                ORDER BY code, trade_date DESC
                """,
                code_list,
            ).fetchall()
            for tr in trend_rows:
                score_trend.setdefault(tr["code"], []).append(tr["score"] or 0)

        def _build_item(d: dict) -> dict:
            """把一行 stock_signal 记录加工成散户可执行的推荐卡片"""
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

            # 散户可读的中文评分理由（基于 5 个子分，量纲 0~10，见 strategies.py 的 0-3→0-10 映射）
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
                if val >= 6:      # 6/10 ≈ 百分制 60，认定为亮点
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

            # ── 建仓分批建议（账户规模/占比见 config.personal_config） ──
            position_plan = None
            if entry > 0:
                # 总金额上限：账户 × 仓位占比（A 股最小 1 手 = 100 股，按 100 整手）
                max_amount = int(POSITION_PLAN_ACCOUNT * POSITION_PLAN_MAX_PCT)
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
                        "account_size": POSITION_PLAN_ACCOUNT,
                        "max_position_pct": int(POSITION_PLAN_MAX_PCT * 100),
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

            # ── 突破确认买点（回放验证：推荐后等收盘突破推荐日以来最高价再入场，
            #    隔日胜率 45.2%→46.5%、均值 0.00%→+0.24%，是唯一正向的时机确认信号）──
            confirm_trigger = None
            # 隔日动量本身就是突破日盘后信号，次日开盘即入场，不适用突破确认
            if d.get("strategy") != "隔日动量":
                try:
                    hi_row = conn.execute(
                        "SELECT MAX(high) AS hi FROM daily_price WHERE code=? AND trade_date>=?",
                        (d.get("code"), d.get("scan_date")),
                    ).fetchone()
                    if hi_row and hi_row["hi"]:
                        confirm_trigger = round(hi_row["hi"] * 1.002, 2)  # 高点上方 0.2%，避免假突破
                except Exception:
                    confirm_trigger = None

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
            # ── 追高守卫（P2-3.2 重做，2026-08）：跳空过大才放弃，平开按原计划买 ──
            # 旧语义：T+1 相对信号日收盘涨 >2.5% → 降级 wait。但隔夜跳空恰是唯一正
            # edge（diag：T+1 OC +0.08% vs CC -0.82%，差 0.9pct 全在跳空上），好日子里
            # 它砍掉该赚的钱。新语义：相对信号日收盘涨幅 > 止盈价（≈+8%）→ 已错过，
            # level=sell 不追；平开/小涨保持原 buy（挂信号价/回踩支撑，不再 wait）。
            gap_pct = None
            if entry > 0 and latest > 0 and T1_GAP_GUARD.get("enabled", True):
                gap_pct = (latest - entry) / entry * 100
                # 跳空上限：优先用推荐自带止盈价（tp>entry 时 ≈ +8%）；止盈缺失回退旧阈值
                gap_limit = ((tp / entry - 1) * 100
                             if tp and entry > 0 and tp > entry
                             else float(T1_GAP_GUARD.get("max_gap_pct", 2.5)))
                if gap_pct > gap_limit:
                    signal["level"] = "sell"
                    signal["emoji"] = "🔴"
                    signal["label"] = "止盈离场"
                    signal["warnings"] = signal["warnings"] + [
                        f"相对信号日收盘已涨 +{gap_pct:.1f}%（{entry:.2f} → {latest:.2f}），"
                        f"已越过止盈位 +{gap_limit:.1f}%，错过买点不追，等回踩"
                    ]
            # 可建仓但现价还在确认线下方 → 提示等突破确认再入场
            if signal["level"] == "buy" and confirm_trigger and latest > 0 and latest < confirm_trigger:
                signal["warnings"] = signal["warnings"] + [
                    f"建议等收盘站上 {confirm_trigger}（推荐后高点）再入场，突破确认后隔日胜率更高"
                ]

            return {
                "code": d.get("code"),
                "name": d.get("name"),
                "industry": d.get("industry"),
                "horizon": d.get("horizon") or "short",
                "strategy": d.get("strategy"),
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
                "confirm_trigger": confirm_trigger,  # 突破确认买点（推荐日以来最高价上方）
                "exit_advice": _get_exit_advice(d),  # P0: 出场建议
                "reasons": reasons,
                "weaknesses": weaknesses,
                "triggers": triggers,
                "scan_date": d.get("scan_date"),
                "trade_date": d.get("trade_date"),
            }

        # 构建候选卡片后剔除「避免」级（盈亏比 <1.5 一票否决），再截取前 limit 条
        # 稳健 regime 门控（2026-08 P1-2.3）：cold/cool 时短线推荐数量减半，
        # 避免最差 cohort 满仓推荐（diag 最差 cohort T+1 胜率仅 ~40%）；cold 时
        # short 组已过 _calc_signal 的票也整组强制降级 wait（保守，可等回暖再上）。
        groups = {}
        for hz in horizons:
            items = [_build_item(dict(r)) for r in rows_by_horizon[hz]]
            items = [it for it in items if it["signal"]["level"] != "avoid"]
            if hz == "short" and market_regime in ("cold", "cool"):
                items = items[:max(1, limit // 2)]
                if market_regime == "cold":
                    for it in items:
                        if it["signal"]["level"] == "buy":
                            it["signal"]["level"] = "wait"
                            it["signal"]["emoji"] = "🔴"
                            it["signal"]["label"] = "大盘冷，观望"
                            it["signal"]["reasons"] = it["signal"].get("reasons", []) + [
                                "大盘 cold，短线整组降级观望"]
            groups[hz] = items[:limit]
        for hz in ("short", "mid", "long"):
            groups.setdefault(hz, [])
        short_items = groups.get("short", [])
        return ok({
            "date": scan_date,
            "latest_trade_date": _latest_td,  # 推荐是否为最新交易日由前端据此判断
            "count": sum(len(v) for v in groups.values()),
            "groups": _sanitize(groups),
            "items": _sanitize(short_items),   # 兼容旧前端：items = short 组
            "market_regime": market_regime,  # 让前端知道当前大盘冷热
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


@investor_bp.route("/exit_advice", methods=["GET"])
def exit_advice():
    """出场跟踪：对 2026-07-20 起推荐的出场状态（按 short/mid/long 三周期分组）

    GET /api/investor/exit_advice
    口径：与「今日推荐」对齐（每组每天 fusion_score 前 8）；起始日为 EXIT_TRACK_START_DATE，
         此前的推荐放弃，无结束时间限制——未卖出的持续跟踪直到卖出；
         同一股票同一周期在交易日上连续推荐时合并为一条：推荐日取最新一次、
         持仓天数从最早一次累计（买入价=最早推荐日次日开盘价）、止损/止盈按最新一次判定；
         开始持有 = 推荐日，买入价 = 推荐日之后首个交易日的开盘价；
         卖出条件 = 推荐自带止损/止盈价 + 按周期持仓上限（短线10/中线60/长线不限）。
    返回 {items(平铺), groups:{short,mid,long}, summary, start_date}，状态 hold/clear。
    """
    try:
        from strategy.exit_advisor import evaluate_exit_by_prices, HORIZON_MAX_HOLD
        from core.db import get_conn as _get_conn
        import pandas as pd

        # 板块限制：与「今日推荐」口径一致
        board_filter = ""
        if MAIN_BOARD_ONLY:
            board_filter = "".join(
                f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)

        with _get_conn() as conn:
            # 近 N 个交易日的推荐记录（与「今日推荐」对齐：每天每周期 fusion_score 前 8）
            signals = conn.execute(f"""
                SELECT code, name, scan_date, buy_price,
                       stop_loss, take_profit, fusion_score, horizon, strategy
                FROM (
                    SELECT s.code, s.name, s.scan_date, s.buy_price,
                           s.stop_loss, s.take_profit, s.fusion_score,
                           COALESCE(s.horizon, 'short') AS horizon,
                           s.strategy,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.scan_date, COALESCE(s.horizon, 'short')
                               ORDER BY COALESCE(s.fusion_score, 0) DESC
                           ) AS rn
                    FROM stock_signal s
                    WHERE s.scan_date >= ?
                      AND s.buy_price IS NOT NULL
                      AND s.buy_price > 0
                      AND s.name NOT LIKE '%ST%'
                      AND s.name NOT LIKE '%退%'
                      {board_filter}
                )
                WHERE rn <= 8
                ORDER BY scan_date DESC
            """, (EXIT_TRACK_START_DATE,)).fetchall()

            if not signals:
                return ok({"items": [], "groups": {"short": [], "mid": [], "long": []},
                           "count": 0, "start_date": EXIT_TRACK_START_DATE,
                           "summary": {"hold": 0, "clear": 0}})

            # 按 (code, scan_date, horizon) 去重
            seen = set()
            unique_signals = []
            for s in signals:
                key = (s["code"], s["scan_date"], s["horizon"])
                if key not in seen:
                    seen.add(key)
                    unique_signals.append(s)

            # 按 code 缓存日线（连续分段拆分与评估复用），避免重复查询
            _df_cache: dict = {}

            # ── 连续推荐合并：同 (code, horizon) 且推荐日按交易日连续 → 合并为一段 ──
            # 段内：推荐日展示最新一次，持仓天数从最早一次累计，止损/止盈按最新一次判定；
            # 但段内持仓若中途已卖出，卖出日（含）之后的新推荐拆分为新一轮交易
            trade_dates = [r["trade_date"] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date ASC"
            ).fetchall()]
            td_index = {d: i for i, d in enumerate(trade_dates)}

            by_key: dict = {}
            for s in unique_signals:
                by_key.setdefault((s["code"], s["horizon"]), []).append(s)

            raw_segments = []   # 每段 = [最早推荐 ... 最新推荐]（scan_date 升序）
            for sigs in by_key.values():
                sigs.sort(key=lambda x: x["scan_date"])
                seg = [sigs[0]]
                for s in sigs[1:]:
                    prev = seg[-1]
                    # 连续 = 两个推荐日在交易日历中相邻（中间没有其他交易日）
                    consecutive = (
                        prev["scan_date"] in td_index
                        and s["scan_date"] in td_index
                        and td_index[s["scan_date"]] == td_index[prev["scan_date"]] + 1
                    )
                    if consecutive:
                        seg.append(s)
                    else:
                        raw_segments.append(seg)
                        seg = [s]
                raw_segments.append(seg)

            # 按卖出断点拆分后展开（卖出后的新推荐独立成段）
            segments = []
            for seg in raw_segments:
                code = seg[0]["code"]
                if code not in _df_cache:
                    rows = conn.execute("""
                        SELECT trade_date, open, close, high, low
                        FROM daily_price
                        WHERE code = ?
                        ORDER BY trade_date ASC
                    """, (code,)).fetchall()
                    _df_cache[code] = (
                        pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
                        if rows else None
                    )
                segments.extend(_split_continuous_segments(seg, _df_cache.get(code), seg[0]["horizon"]))

            results = []
            for seg in segments:
                first = seg[0]      # 最早推荐：持仓起算日 + 真实建仓买入价
                last = seg[-1]      # 最新推荐：展示推荐日 + 最新止损/止盈判定
                code = last["code"]
                horizon = last["horizon"]

                df = _df_cache.get(code)
                if df is None or df.empty:
                    continue

                # 买入价 = 最早推荐日的下一个交易日开盘价（真实建仓成本）
                if hasattr(df.index, 'strftime'):
                    after = df[df.index > pd.Timestamp(first["scan_date"])]
                else:
                    after = df[df.index > first["scan_date"]]
                if after.empty:
                    # 尚未到买入日（最早推荐日即最新交易日）：还未买入，不显示
                    continue
                _f = after.iloc[0]
                entry_price = float(_f["open"]) if _f["open"] else (
                    float(_f["close"]) or first["buy_price"])
                entry_date = str(after.index[0])[:10]

                # 持仓从最早推荐日起算；止损/止盈按最新推荐判定
                advice = evaluate_exit_by_prices(
                    entry_price=entry_price,
                    entry_date=first["scan_date"],
                    df=df,
                    stop_loss=last["stop_loss"],
                    take_profit=last["take_profit"],
                    max_hold_days=HORIZON_MAX_HOLD.get(horizon),
                )
                detail = dict(advice.get("detail") or {})
                if entry_date is not None:
                    detail["entry_date"] = entry_date
                if len(seg) > 1:
                    detail["first_scan_date"] = first["scan_date"]  # 连续推荐起点

                results.append({
                    "code": code,
                    "name": last["name"],
                    "scan_date": last["scan_date"],   # 推荐日 = 最新一次推荐
                    "horizon": horizon,
                    "strategy": last["strategy"],
                    "entry_price": round(entry_price, 2),
                    "fusion_score": round(last["fusion_score"] or 0, 1),
                    "status": advice["status"],
                    "reason": advice["reason"],
                    "detail": detail,
                })

        # 排序：推荐日降序（最新在前），同一天内 clear > hold
        status_order = {"clear": 0, "hold": 1}
        results.sort(key=lambda x: (x["scan_date"], -status_order.get(x["status"], 2)), reverse=True)

        # 按周期分组
        groups = {"short": [], "mid": [], "long": []}
        for r in results:
            groups.setdefault(r["horizon"], []).append(r)

        return ok(_sanitize({
            "items": results,
            "groups": groups,
            "count": len(results),
            "start_date": EXIT_TRACK_START_DATE,
            "summary": {
                "hold": sum(1 for r in results if r["status"] == "hold"),
                "clear": sum(1 for r in results if r["status"] == "clear"),
            },
        }))
    except Exception as e:
        return fail(f"出场建议评估失败: {e}", 500)


# ─────────────────────────────────────────────
# 2. 市场速览：指数涨跌 + 北向资金 + 大盘冷热
# ─────────────────────────────────────────────

def _get_adaptive_weights_info() -> dict:
    """P3: 获取自适应权重信息（供 market-overview API 返回）。"""
    try:
        from strategy.adaptive_weights import get_market_summary
        return get_market_summary()
    except Exception:
        return {"regime": "unknown", "regime_label": "未知", "adaptive_enabled": False}

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

        # 简单规则 → 多日宽度 composite（10 分钟缓存，抗单日噪音）
        _REGIME_HINT = {
            "hot": "火热（注意追高风险）", "warm": "偏暖（可适度参与）",
            "neutral": "震荡（精选个股）", "cool": "偏冷（控制仓位）",
            "cold": "冰点（防守为主）",
        }
        regime = _compute_market_regime(conn)
        regime_label = _REGIME_HINT.get(regime, "数据不足")

        return ok({
            "indices": _sanitize(index_cards),
            "breadth": _sanitize({
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
            "adaptive_weights": _get_adaptive_weights_info(),  # P3: 自适应权重
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


# ─────────────────────────────────────────────
# 3. 个人持仓：CRUD + 实时盈亏
# ─────────────────────────────────────────────
def _diagnose_position(conn, code: str, cost_price, opened_at) -> Optional[dict]:
    """对单条持仓运行 exit_advisor 出场诊断，返回 {status, reason, detail} 或 None。

    entry_price 取持仓成本价，entry_date 取建仓日；异常时降级为 None，不影响持仓主列表。
    """
    if not code or not cost_price or cost_price <= 0 or not opened_at:
        return None
    try:
        from strategy.exit_advisor import evaluate_exit
        import pandas as pd
        rows = conn.execute(
            """
            SELECT trade_date, close, high, low
            FROM daily_price WHERE code = ?
            ORDER BY trade_date ASC
            """,
            (code,),
        ).fetchall()
        if not rows:
            return None
        df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
        return evaluate_exit(
            entry_price=float(cost_price),
            entry_date=str(opened_at)[:10],
            df=df,
        )
    except Exception:
        return None


@investor_bp.route("/positions", methods=["GET"])
def list_positions():
    """列出当前持仓（含最新价 + 浮动盈亏 + 出场诊断）"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   lp.close AS latest_close,
                   lp.pct_change AS latest_pct
            FROM personal_position p
            LEFT JOIN latest_price lp ON lp.code = p.code
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

            # 出场诊断（清仓/减仓/持有）——复用推荐票出场纪律
            advice = _diagnose_position(conn, d.get("code"),
                                        d.get("cost_price"), d.get("opened_at"))

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
                "advice": advice,
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
    """自选股列表（附带最新价 + 当日打分 + 目标价）"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.code, w.note, w.created_at,
                   w.target_price, w.alert_dir,
                   i.name, i.industry,
                   lp.close  AS latest_close,
                   lp.pct_change AS latest_pct,
                   lp.fusion_score AS latest_score
            FROM personal_watchlist w
            LEFT JOIN stock_info i   ON i.code = w.code
            LEFT JOIN latest_price lp ON lp.code = w.code
            ORDER BY w.created_at DESC
            """
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            tp = d.get("target_price")
            latest = d.get("latest_close")
            # 距目标价百分比（正=还需上涨，负=已超过）
            if tp and tp > 0 and latest:
                d["target_distance_pct"] = round((tp - latest) / latest * 100, 2)
            else:
                d["target_distance_pct"] = None
            items.append(d)
        return ok(_sanitize(items))


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

    # 可选目标价（价格预警）：target_price + alert_dir(above/below)
    target_price = body.get("target_price")
    try:
        target_price = float(target_price) if target_price not in (None, "") else None
    except (TypeError, ValueError):
        target_price = None
    alert_dir = (body.get("alert_dir") or "above").strip().lower()
    if alert_dir not in ("above", "below"):
        alert_dir = "above"

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO personal_watchlist(code, note, created_at) VALUES (?,?,?)",
            (code, body.get("note"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        inserted = cur.rowcount > 0
        # 无论新增还是已存在，均允许更新目标价
        if target_price is not None:
            conn.execute(
                "UPDATE personal_watchlist SET target_price=?, alert_dir=? WHERE code=?",
                (target_price, alert_dir, code),
            )

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
    行为：仅删除 personal_watchlist 记录，**不**清理 daily_price 中的历史数据。
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
            SELECT p.*, lp.close AS latest_close
            FROM personal_position p
            LEFT JOIN latest_price lp ON lp.code = p.code
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
            SELECT p.code, p.shares, lp.close,
                   (p.cost_price * p.shares) AS cost,
                   (lp.close * p.shares)    AS value
            FROM personal_position p
            LEFT JOIN latest_price lp ON lp.code = p.code
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

        # 自选股价格预警：目标价触达/接近
        watch_rows = conn.execute(
            """
            SELECT w.code, w.target_price, w.alert_dir,
                   i.name, lp.close AS latest_close
            FROM personal_watchlist w
            LEFT JOIN stock_info i ON i.code = w.code
            LEFT JOIN latest_price lp ON lp.code = w.code
            WHERE w.target_price IS NOT NULL AND w.target_price > 0
            """
        ).fetchall()
        for r in watch_rows:
            d = dict(r)
            latest = d.get("latest_close")
            tp = d.get("target_price")
            if not latest or not tp:
                continue
            direction = (d.get("alert_dir") or "above").lower()
            name = d.get("name") or d["code"]
            gap_pct = (latest - tp) / tp * 100
            if direction == "above":
                if latest >= tp:
                    alerts.append({
                        "level": "info", "code": d["code"], "name": name,
                        "msg": f"自选已达目标价 {tp:.2f}（现价 {latest:.2f}）",
                    })
                elif gap_pct >= -2:
                    alerts.append({
                        "level": "warning", "code": d["code"], "name": name,
                        "msg": f"自选接近目标价 {tp:.2f}（差 {abs(gap_pct):.1f}%）",
                    })
            else:  # below
                if latest <= tp:
                    alerts.append({
                        "level": "info", "code": d["code"], "name": name,
                        "msg": f"自选已跌至目标价 {tp:.2f}（现价 {latest:.2f}）",
                    })
                elif gap_pct <= 2:
                    alerts.append({
                        "level": "warning", "code": d["code"], "name": name,
                        "msg": f"自选接近目标价 {tp:.2f}（差 {abs(gap_pct):.1f}%）",
                    })

        return ok({"alerts": alerts, "count": len(alerts)})


# ─────────────────────────────────────────────
# 6. 历史推荐回测：过去 30 天推荐的真实表现
# ─────────────────────────────────────────────
@investor_bp.route("/outcome_summary", methods=["GET"])
def outcome_summary():
    """推荐胜率闭环汇总（从 recommend_outcome 表读取）
    GET /api/investor/outcome_summary?days=30
    """
    days = request.args.get("days", 30, type=int)
    days = max(1, min(days, 90))
    try:
        from core.outcome_tracker import get_summary
        summary = get_summary(days)
        return ok({"summary": summary, "days": days})
    except Exception as e:
        return fail(f"查询失败: {e}", 500)


@investor_bp.route("/outcome_list", methods=["GET"])
def outcome_list():
    """推荐结果明细列表
    GET /api/investor/outcome_list?days=30&limit=200
    """
    days = request.args.get("days", 30, type=int)
    days = max(1, min(days, 90))
    limit = request.args.get("limit", 200, type=int)
    limit = max(1, min(limit, 500))
    try:
        from core.outcome_tracker import get_outcome_list
        items = get_outcome_list(days, limit)
        return ok(_sanitize({"items": items, "days": days, "total": len(items)}))
    except Exception as e:
        return fail(f"查询失败: {e}", 500)

@investor_bp.route("/recommendations/history", methods=["GET"])
def recommendations_history():
    """历史推荐回测：过去 N 天短线推荐的股票，到今天的实际涨跌
    GET /api/investor/recommendations/history?days=30
    优先从 recommend_outcome 表读取持久化数据，fallback 到实时计算。
    口径：仅统计短线（horizon=short）推荐；同一股票连续交易日不间断
    被推荐时合并为一段，只显示首次推荐日并给出连续推荐天数。
    """
    days = request.args.get("days", "30", type=int)
    days = max(1, min(days, 90))

    # 优先尝试从 recommend_outcome 读取
    try:
        from core.outcome_tracker import get_merged_outcome_list, get_merged_summary
        items = get_merged_outcome_list(days, "short")
        if items:
            summary = get_merged_summary(days, "short")
            # 转换为前端兼容格式
            results = []
            for it in items:
                ret = it.get("exit_return") or it.get("t5_return") or it.get("t3_return") or it.get("t1_return") or 0
                results.append({
                    "code": it["code"],
                    "name": it.get("name"),  # get_merged_outcome_list 已 JOIN stock_info 补股名
                    "scan_date": it["scan_date"],          # 兼容字段 = 首次推荐日
                    "first_scan_date": it["first_scan_date"],
                    "last_scan_date": it["last_scan_date"],
                    "streak_days": it["streak_days"],       # 连续推荐天数
                    "entry_price": round(it["entry_price"], 2) if it.get("entry_price") else None,
                    "current_price": None,
                    "pnl_pct": ret,
                    "hit_stop_loss": bool(it.get("hit_stop")),
                    "hit_take_profit": bool(it.get("hit_tp")),
                    "fusion_score": round(it.get("fusion_score") or 0, 1),
                    "t1_return": it.get("t1_return"),
                    "t2_return": it.get("t2_return"),
                    "t3_return": it.get("t3_return"),
                    "t5_return": it.get("t5_return"),
                    "exit_reason": it.get("exit_reason"),
                    "days_held": None,
                })
            # 按首次推荐日降序（同日按收益降序），复盘列表以时间线为主
            results.sort(key=lambda x: (x["first_scan_date"], x["pnl_pct"]), reverse=True)
            return ok({
                "items": _sanitize(results),
                "summary": summary,
                "days": days,
                "source": "recommend_outcome",
                "merged": True,
            })
    except Exception:
        pass  # fallback 到实时计算

    # Fallback: 实时计算（口径与 recommend_outcome 一致：短线 + 连续推荐合并 + 主板过滤）
    from core.outcome_tracker import _merge_continuous_segments

    # 板块限制：与今日推荐同口径（小资金仅推主板）
    board_filter = ""
    if MAIN_BOARD_ONLY:
        board_filter = "".join(
            f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)

    with get_conn() as conn:
        # 获取最近 N 天的短线推荐记录（stock_signal 中有 buy_price 的）
        signals = conn.execute(
            f"""
            SELECT s.code, s.name, s.scan_date, s.buy_price, s.stop_loss, s.take_profit,
                   s.fusion_score
            FROM stock_signal s
            WHERE s.scan_date >= date('now', ?)
              AND s.buy_price IS NOT NULL
              AND COALESCE(s.horizon, 'short') = 'short'
              {board_filter}
            ORDER BY s.code ASC, s.scan_date ASC
            """,
            (f"-{days} days",),
        ).fetchall()

        if not signals:
            return ok({"items": [], "summary": {}, "days": days})

        # 连续交易日不间断的推荐合并为一段（每段取首次推荐日 + 连续天数）
        segments = _merge_continuous_segments(signals, conn)

        results = []
        for seg in segments:
            code = seg["code"]
            entry = seg.get("buy_price") or 0
            if entry <= 0:
                continue

            # 段首推荐日之后最多 5 个交易日收盘价，计算 T1/T2/T3/T5
            prices = conn.execute(
                """
                SELECT trade_date, close FROM daily_price
                WHERE code = ? AND trade_date > ?
                ORDER BY trade_date ASC LIMIT 5
                """,
                (code, seg["scan_date"]),
            ).fetchall()

            def _ret(n):
                if len(prices) >= n and prices[n - 1]["close"]:
                    return round((prices[n - 1]["close"] - entry) / entry * 100, 2)
                return None

            t1, t2, t3, t5 = _ret(1), _ret(2), _ret(3), _ret(5)
            pnl_pct = t5 if t5 is not None else (t1 if t1 is not None else 0)
            hit_stop = False
            hit_tp = False
            if prices:
                current = prices[0]["close"]
                current_date = prices[0]["trade_date"]
                if seg.get("stop_loss") and current <= seg["stop_loss"]:
                    hit_stop = True
                if seg.get("take_profit") and current >= seg["take_profit"]:
                    hit_tp = True
            else:
                current_date = None

            # 判断是否仍在持仓中（首次推荐日后 N 天内）
            from datetime import datetime as _dt
            try:
                scan = _dt.strptime(seg["scan_date"], "%Y-%m-%d")
                now = _dt.now()
                days_held = (now - scan).days
            except (ValueError, TypeError):
                days_held = 0

            results.append({
                "code": code,
                "name": seg.get("name"),
                "scan_date": seg["scan_date"],
                "first_scan_date": seg["first_scan_date"],
                "last_scan_date": seg["last_scan_date"],
                "streak_days": seg["streak_days"],
                "entry_price": round(entry, 2),
                "current_price": round(prices[0]["close"], 2) if prices else None,
                "current_date": current_date,
                "pnl_pct": pnl_pct,
                "hit_stop_loss": hit_stop,
                "hit_take_profit": hit_tp,
                "fusion_score": round(seg.get("fusion_score") or 0, 1),
                "t1_return": t1,
                "t2_return": t2,
                "t3_return": t3,
                "t5_return": t5,
                "days_held": days_held,
            })

        # 汇总统计（合并口径）
        if results:
            def _win_stat(key):
                vals = [r[key] for r in results if r.get(key) is not None]
                if not vals:
                    return {"n": 0, "win": 0, "win_rate": 0, "avg_return": 0}
                win = sum(1 for v in vals if v > 0)
                avg = sum(vals) / len(vals)
                return {"n": len(vals), "win": win,
                        "win_rate": round(win / len(vals) * 100, 1),
                        "avg_return": round(avg, 2)}

            wins = [r for r in results if r["pnl_pct"] > 0]
            losses = [r for r in results if r["pnl_pct"] <= 0]
            avg_return = sum(r["pnl_pct"] for r in results) / len(results)
            win_rate = len(wins) / len(results) * 100 if results else 0
            stop_count = sum(1 for r in results if r["hit_stop_loss"])
            tp_count = sum(1 for r in results if r["hit_take_profit"])
            summary = {
                "total": len(results),
                "t1": _win_stat("t1_return"),
                "t2": _win_stat("t2_return"),
                "t3": _win_stat("t3_return"),
                "t5": _win_stat("t5_return"),
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

        # 按首次推荐日降序（同日按收益降序），复盘列表以时间线为主
        results.sort(key=lambda x: (x["first_scan_date"], x["pnl_pct"]), reverse=True)

        return ok({
            "items": _sanitize(results),
            "summary": summary,
            "days": days,
            "merged": True,
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
            SELECT trade_date, fusion_score AS score FROM daily_price
            WHERE code = ?
            ORDER BY trade_date DESC LIMIT 2
            """,
            (code,),
        ).fetchall()

        # 注：融合分 fusion_score 为 0~50 量纲，阈值较原 0~100 打分折半
        if len(scores) >= 2:
            latest_score = scores[0]["score"] or 0
            prev_score = scores[1]["score"] or 0
            drop = prev_score - latest_score
            if drop >= 5:
                alerts.append({
                    "level": "warning",
                    "code": code,
                    "name": name,
                    "msg": f"融合分下降 {drop:.0f} 分（{prev_score:.0f}→{latest_score:.0f}），注意风险",
                })
            elif drop >= 3:
                alerts.append({
                    "level": "info",
                    "code": code,
                    "name": name,
                    "msg": f"融合分小幅回落 {drop:.0f} 分（{prev_score:.0f}→{latest_score:.0f}）",
                })
        elif len(scores) == 1:
            # 只有一次评分，检查是否低于阈值（0~50 量纲）
            s = scores[0]["score"] or 0
            if s < 15:
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


@investor_bp.route("/factor_trend", methods=["GET"])
def factor_trend():
    """融合分归因 + 趋势（纯读 daily_price）

    GET /api/investor/factor_trend?codes=600519,000001&days=30
    - codes: 逗号分隔 6 位代码，最多 50 个
    - days:  取最近 N 个交易日，默认 30，clamp 到 [2, 90]
    返回 { code: [ {trade_date, fusion_score, vol_score, ma_score,
                    diverge_score, bottom_score, whale_score}, ... ] }（按日期升序）
    同时服务：sparkline（days=7 取 fusion_score 序列）与详情弹窗（days=30 含 5 分项）。
    """
    raw = (request.args.get("codes") or "").strip()
    codes = [c.strip() for c in raw.split(",") if c.strip().isdigit() and len(c.strip()) == 6]
    codes = codes[:50]
    if not codes:
        return fail("缺少合法 codes（6 位数字，逗号分隔）", 400)

    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(2, min(90, days))

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
    return ok(_sanitize(result))


@investor_bp.route("/rule_signals", methods=["GET"])
def rule_signals():
    """常驻策略规则命中（选股→回测→推荐闭环的推荐端）

    GET /api/investor/rule_signals?date=YYYY-MM-DD&limit=100&max_hits=200
    - date 缺省取 strategy_signals 最新 trade_date
    - limit 缺省 100，上限 500（命中数可能很大，保护前端）
    - max_hits 缺省 200：当日命中超过该数的规则视为过泛（无筛选价值），整条规则排除
    返回该日命中列表，JOIN 股名与最新收盘价，按 confidence 降序。
    """
    date = (request.args.get("date") or "").strip()
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    try:
        max_hits = int(request.args.get("max_hits", 200))
    except (TypeError, ValueError):
        max_hits = 200
    max_hits = max(1, min(max_hits, 100000))
    with get_conn() as conn:
        if not date:
            row = conn.execute("SELECT MAX(trade_date) AS d FROM strategy_signals").fetchone()
            date = row["d"] if row and row["d"] else None
        if not date:
            return ok(_sanitize({"date": None, "total": 0, "items": []}))
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
    return ok(_sanitize({
        "date": date, "total": total, "items": items,
        "excluded_rules": excluded_rules, "max_hits": max_hits,
    }))


# ─────────────────────────────────────────────
# 启动时确保表存在（被 app.py 注册时调用）
# ─────────────────────────────────────────────
def init_investor_tables():
    _ensure_personal_tables()
