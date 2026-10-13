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
from flask import Blueprint, request

from core.db import get_conn, _safe_add_column
from utils.api import ok, fail


investor_bp = Blueprint("investor", __name__, url_prefix="/api/investor")


@investor_bp.route("/account_config", methods=["GET"])
def account_config():
    from config.personal_config import get_personal_config
    config = get_personal_config()
    return ok({"reference_account": config["position_plan_account"],
               "max_position_pct": config["position_plan_max_pct"],
               "max_position_amount": config["position_plan_account"] * config["position_plan_max_pct"],
               "main_board_only": config["main_board_only"],
               "budget_kind": "reference_budget", "available_cash": None})


# 信号灯判定与今日推荐编排已下沉（T7）：
#   REGIME_LABEL / calc_signal → strategy/recommendation.py
#   大盘冷热「最新交易日 + 10 分钟缓存」→ strategy/market_regime.py
from strategy.recommendation import build_today_payload


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

        -- 持仓卖出流水（减仓/平仓），用于统计历史持仓收益率
        CREATE TABLE IF NOT EXISTS personal_trade (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,             -- 关联 personal_position.id（删除持仓时级联清理）
            code        TEXT NOT NULL,
            name        TEXT,
            action      TEXT NOT NULL,     -- reduce 减仓 | close 平仓
            shares      INTEGER NOT NULL,  -- 卖出股数
            price       REAL NOT NULL,     -- 卖出价
            cost_price  REAL NOT NULL,     -- 该笔成本价
            pnl         REAL NOT NULL,     -- 已实现盈亏（元）
            pnl_pct     REAL NOT NULL,     -- 收益率（%）
            traded_at   TEXT,
            created_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pt_code      ON personal_trade(code);
        CREATE INDEX IF NOT EXISTS idx_pt_traded_at ON personal_trade(traded_at);
        """)
        # stock_info 可能缺少 industry 列（个人投资者会希望按行业筛选）
        _safe_add_column(conn, "stock_info", "industry", "TEXT")
        # 自选股价格预警：目标价 + 触发方向（above=现价↑目标 / below=现价↓目标）
        _safe_add_column(conn, "personal_watchlist", "target_price", "REAL")
        _safe_add_column(conn, "personal_watchlist", "alert_dir", "TEXT")
        # 持仓卖出流水关联 position_id（老库迁移；删除持仓时级联清理其流水）
        _safe_add_column(conn, "personal_trade", "position_id", "INTEGER")


# ─────────────────────────────────────────────
# 1. 今日推荐：直接告诉散户“买什么、什么价买、什么价卖、什么价止损”
# ─────────────────────────────────────────────


# 出场跟踪编排已下沉 core/services/exit_tracking.py（T7）
from core.services.exit_tracking import build_exit_advice_payload


@investor_bp.route("/today", methods=["GET"])
def today_recommendations():
    """今日推荐（三周期 action plan）—— 薄路由：参数解析 + ok/fail。
    取数/门控/regime 天花板/推荐卡片组装见 strategy.recommendation.build_today_payload。
    支持 ?date=YYYY-MM-DD 指定日期、?horizon=short|mid|long 单组过滤、?limit=0..8。
    """
    limit = max(0, min(request.args.get("limit", default=4, type=int), 8))
    horizon_filter = (request.args.get("horizon") or "").strip().lower() or None
    if horizon_filter not in (None, "short", "mid", "long"):
        return fail("horizon 仅支持 short / mid / long")
    return ok(build_today_payload(
        limit=limit,
        horizon_filter=horizon_filter,
        target_date=request.args.get("date"),
    ))




@investor_bp.route("/today/dates", methods=["GET"])
def available_dates():
    """获取有推荐数据的日期列表（供日期选择器使用）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal ORDER BY scan_date DESC LIMIT 60"
        ).fetchall()
        dates = [r["scan_date"] for r in rows]
        return ok({"dates": dates})


# 观察信号线编排（强势突破/反转首日/反转历史/龙虎榜机构席位）已下沉
# core/services/signal_picks.py（T7）
from core.services.signal_picks import (
    build_surge_picks_payload,
    build_reversal_picks_payload,
    build_reversal_history_payload,
    build_lhb_jg_track_payload,
)


@investor_bp.route("/surge_picks", methods=["GET"])
def surge_picks():
    """次日强势观察（强势突破信号线）—— 薄路由；卡片+历史回放见 signal_picks。

    ⚠ 2026-10-12 首页栏目已下线（面板与前端函数移除、SURGE_BREAKOUT["enabled"]=False
      停止每日生成），本接口**保留**：历史信号仍可查成绩单，复活时无需改后端。
    """
    limit = max(0, min(request.args.get("limit", default=6, type=int), 12))
    days = max(1, min(request.args.get("days", default=60, type=int), 180))
    return ok(build_surge_picks_payload(
        limit=limit, days=days, target_date=request.args.get("date")))


@investor_bp.route("/reversal_picks", methods=["GET"])
def reversal_picks():
    """反转首日观察（first_reversal 信号线）—— 薄路由；编排见 signal_picks。"""
    limit = max(0, min(request.args.get("limit", default=12, type=int), 40))
    return ok(build_reversal_picks_payload(
        limit=limit, target_date=request.args.get("date")))


@investor_bp.route("/reversal_history", methods=["GET"])
def reversal_history():
    """反转首日历史明细（读 recommend_outcome）—— 薄路由；编排见 signal_picks。"""
    days = max(5, min(request.args.get("days", default=60, type=int), 180))
    limit = max(20, min(request.args.get("limit", default=600, type=int), 2000))
    try:
        return ok(build_reversal_history_payload(days=days, limit=limit))
    except Exception as e:
        return fail(f"反转首日复盘读取失败: {e}", 500)


@investor_bp.route("/lhb_jg_track", methods=["GET"])
def lhb_jg_track():
    """龙虎榜机构席位建仓跟踪（成本位参考，非收益信号）—— 薄路由；编排见 signal_picks。"""
    days = max(5, min(request.args.get("days", default=20, type=int), 120))
    min_cnt = max(1, min(request.args.get("min_cnt", default=2, type=int), 20))
    limit = max(1, min(request.args.get("limit", default=30, type=int), 100))
    only_priced = request.args.get("only_priced", default=1, type=int) == 1
    return ok(build_lhb_jg_track_payload(
        days=days, min_cnt=min_cnt, limit=limit,
        sort_key=request.args.get("sort", default="cum_ratio"),
        only_priced=only_priced))


@investor_bp.route("/exit_advice", methods=["GET"])
def exit_advice():
    """出场跟踪（short/mid/long 分组）—— 薄路由。
    选股口径/连续段合并/回踩入场/移动止盈/收益统计全部在
    core.services.exit_tracking.build_exit_advice_payload（与今日推荐/复盘同口径）。
    """
    try:
        return ok(build_exit_advice_payload())
    except Exception as e:
        return fail(f"出场建议评估失败: {e}", 500)


# 市场速览编排已下沉 core/services/market_overview.py（T7）
from core.services.market_overview import build_market_overview_payload


@investor_bp.route("/market-overview", methods=["GET"])
def market_overview():
    """市场速览（核心指数 + 宽度 + 大盘冷热 + 自适应权重）—— 薄路由。"""
    return ok(build_market_overview_payload())


# 个人持仓 / 自选股 / 风险告警编排已下沉 core/services/personal.py（T7）
from core.services.personal import (
    PersonalRequestError,
    list_positions_payload, add_position as svc_add_position,
    reduce_position as svc_reduce_position,
    delete_position as svc_delete_position,
    positions_history_payload,
    list_watchlist_payload, add_watchlist as svc_add_watchlist,
    remove_watchlist as svc_remove_watchlist,
    risk_alerts_payload,
)


@investor_bp.route("/positions", methods=["GET"])
def list_positions():
    """当前持仓（最新价 + 浮盈 + 出场诊断）—— 薄路由；编排见 personal 服务。"""
    return ok(list_positions_payload())


@investor_bp.route("/positions", methods=["POST"])
def add_position():
    """新增持仓 —— 薄路由；校验/写库见 personal 服务。"""
    try:
        return ok(svc_add_position(request.get_json(silent=True) or {}))
    except PersonalRequestError as e:
        return fail(e.msg, e.status)


@investor_bp.route("/positions/<int:pid>/reduce", methods=["POST"])
def reduce_position(pid: int):
    """减仓 / 平仓卖出 —— 薄路由；流水与状态更新见 personal 服务。"""
    try:
        return ok(svc_reduce_position(pid, request.get_json(silent=True) or {}))
    except PersonalRequestError as e:
        return fail(e.msg, e.status)


@investor_bp.route("/positions/<int:pid>", methods=["DELETE"])
def delete_position(pid: int):
    """删除持仓记录（级联流水）—— 薄路由。"""
    try:
        return ok(svc_delete_position(pid))
    except PersonalRequestError as e:
        return fail(e.msg, e.status)


@investor_bp.route("/positions/history", methods=["GET"])
def positions_history():
    """历史卖出流水与收益率汇总 —— 薄路由。"""
    return ok(positions_history_payload())


@investor_bp.route("/watchlist", methods=["GET"])
def get_watchlist():
    """自选股列表 —— 薄路由。"""
    return ok(list_watchlist_payload())


@investor_bp.route("/watchlist", methods=["POST"])
def add_watchlist():
    """加入自选（异步补拉历史数据）—— 薄路由。"""
    try:
        return ok(svc_add_watchlist(request.get_json(silent=True) or {}))
    except PersonalRequestError as e:
        return fail(e.msg, e.status)


@investor_bp.route("/watchlist/<code>", methods=["DELETE"])
def remove_watchlist(code: str):
    """移出自选（保留历史行情）—— 薄路由。"""
    try:
        return ok(svc_remove_watchlist(code))
    except PersonalRequestError as e:
        return fail(e.msg, e.status)


@investor_bp.route("/alerts", methods=["GET"])
def risk_alerts():
    """当前风险告警 —— 薄路由；编排见 personal 服务。"""
    return ok(risk_alerts_payload())


# 推荐复盘闭环（outcome / 历史）编排已下沉 core/services/review.py（T7）
from core.services.review import (
    outcome_summary_payload, outcome_list_payload,
    recommendations_history_payload,
)


@investor_bp.route("/outcome_summary", methods=["GET"])
def outcome_summary():
    """推荐胜率闭环汇总 —— 薄路由；读 recommend_outcome 见 review 服务。"""
    days = max(1, min(request.args.get("days", 30, type=int), 90))
    try:
        return ok(outcome_summary_payload(days))
    except Exception as e:
        return fail(f"查询失败: {e}", 500)


@investor_bp.route("/outcome_list", methods=["GET"])
def outcome_list():
    """推荐结果明细列表 —— 薄路由。"""
    days = max(1, min(request.args.get("days", 30, type=int), 90))
    limit = max(1, min(request.args.get("limit", 200, type=int), 500))
    try:
        return ok(outcome_list_payload(days, limit))
    except Exception as e:
        return fail(f"查询失败: {e}", 500)


@investor_bp.route("/recommendations/history", methods=["GET"])
def recommendations_history():
    """历史推荐复盘（短线，合并连续段）—— 薄路由；口径见 review 服务。"""
    # ⚠ 默认值必须是 int：Flask 参数缺失时直接返回 default、不做类型转换，
    # 若写 "60"（字符串）则 min(days, 90) 抛 TypeError → 500（2026-09-10 修）
    days = max(1, min(request.args.get("days", 60, type=int), 90))
    return ok(recommendations_history_payload(days))


# 指标白话解释 / 因子趋势 / 规则命中编排已下沉 core/services/browse.py（T7）
from core.services.browse import (
    EXPLAINERS, factor_trend_payload, rule_signals_payload,
)


@investor_bp.route("/explain/<metric>", methods=["GET"])
def explain(metric: str):
    """指标白话解释 —— 薄路由。"""
    item = EXPLAINERS.get(metric)
    if not item:
        return fail(f"未知指标: {metric}", 404)
    return ok(item)


@investor_bp.route("/explain", methods=["GET"])
def explain_all():
    """列出所有可解释的指标 —— 薄路由。"""
    return ok({"metrics": list(EXPLAINERS.keys()), "items": EXPLAINERS})


@investor_bp.route("/factor_trend", methods=["GET"])
def factor_trend():
    """融合分归因与趋势 —— 薄路由；取数见 browse 服务。"""
    raw = (request.args.get("codes") or "").strip()
    codes = [c.strip() for c in raw.split(",")
             if c.strip().isdigit() and len(c.strip()) == 6][:50]
    if not codes:
        return fail("缺少合法 codes（6 位数字，逗号分隔）", 400)
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(2, min(90, days))
    return ok(factor_trend_payload(codes, days))


@investor_bp.route("/rule_signals", methods=["GET"])
def rule_signals():
    """常驻策略规则命中 —— 薄路由；取数见 browse 服务。"""
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
    return ok(rule_signals_payload(date, limit, max_hits))


# 个股深度编排已下沉 core/services/stock_deep_service.py；
# 推荐跟踪（含后台回补进程）编排已下沉 core/services/deep_tracking.py（T7）
from core.services.stock_deep_service import (
    market_payload as deep_market_payload,
    submit_scan as deep_submit_scan,
    scan_status_payload as deep_scan_status_payload,
    analyze_payload as deep_analyze_payload,
    search_payload as deep_search_payload,
    recent_payload as deep_recent_payload,
)
from core.services.deep_tracking import (
    DeepTrackError,
    list_payload as track_list_payload,
    refresh_payload as track_refresh_payload,
    read_backfill_progress, start_backfill as track_start_backfill,
    close_payload as track_close_payload,
    delete_payload as track_delete_payload,
)


@investor_bp.route("/stock_deep/market", methods=["GET"])
def stock_deep_market():
    """明日买入候选（全市场扫描优先，已关注池兜底）—— 薄路由。"""
    limit = max(1, min(request.args.get("limit", 10, type=int), 30))
    try:
        return ok(deep_market_payload(limit))
    except Exception as e:
        return fail(f"明日候选计算失败: {e}", 500)


@investor_bp.route("/stock_deep/scan", methods=["POST"])
def stock_deep_scan():
    """手动触发全市场深析扫描（异步任务）—— 薄路由。"""
    try:
        return ok(deep_submit_scan())
    except Exception as e:
        return fail(f"触发全市场深析扫描失败: {e}", 500)


@investor_bp.route("/stock_deep/scan_status", methods=["GET"])
def stock_deep_scan_status():
    """全市场深析扫描状态 —— 薄路由。"""
    try:
        return ok(deep_scan_status_payload())
    except Exception as e:
        return fail(f"查询扫描状态失败: {e}", 500)


@investor_bp.route("/stock_deep/<code>", methods=["GET"])
def stock_deep(code: str):
    """单股深度分析（量价 + 节奏 + 买卖建议）—— 薄路由。"""
    try:
        from config.strategy_params import DEEP_LOOKBACK
        lookback = max(120, min(request.args.get("lookback", DEEP_LOOKBACK, type=int), 600))
        recent_days = max(10, min(request.args.get("recent_days", 45, type=int), 90))
        return ok(deep_analyze_payload(code, lookback, recent_days))
    except Exception as e:
        return fail(f"个股深度分析失败: {e}", 500)


@investor_bp.route("/stocks/search", methods=["GET"])
def stock_search():
    """股票搜索/自动补全 —— 薄路由。"""
    q = (request.args.get("q") or "").strip()
    limit = max(1, min(request.args.get("limit", 20, type=int), 50))
    return ok(deep_search_payload(q, limit))


@investor_bp.route("/stock_deep/recent", methods=["GET"])
def stock_deep_recent():
    """近期关注候选（自选+持仓+最新推荐）—— 薄路由。"""
    return ok(deep_recent_payload())


@investor_bp.route("/deep_track", methods=["GET"])
def deep_track_list():
    """深析推荐跟踪列表 + 汇总 —— 薄路由。"""
    try:
        status = request.args.get("status", "").strip() or None
        limit = max(1, min(request.args.get("limit", 100, type=int), 1000))
        days = request.args.get("days", None, type=int)
        return ok(track_list_payload(status, limit, days))
    except Exception as e:
        return fail(f"跟踪列表读取失败: {e}", 500)


@investor_bp.route("/deep_track/refresh", methods=["POST"])
def deep_track_refresh():
    """手动推进跟踪 / 回补最近 N 个扫描日的跟踪单 —— 薄路由。"""
    try:
        body = request.get_json(silent=True) or {}
        days = int(body.get("backfill_days") or 0)
        return ok(track_refresh_payload(days))
    except Exception as e:
        return fail(f"跟踪推进失败: {e}", 500)


@investor_bp.route("/deep_track/backfill_scan", methods=["POST"])
def deep_track_backfill_scan():
    """启动历史扫描日回补（后台独立进程，立即返回）—— 薄路由。"""
    try:
        body = request.get_json(silent=True) or {}
        days = max(1, min(int(body.get("days") or 30), 180))
        procs = max(1, min(int(body.get("procs") or 8), 16))
        return ok(track_start_backfill(days, procs))
    except DeepTrackError as e:
        return fail(e.msg, e.status)
    except Exception as e:
        return fail(f"启动回补失败: {e}", 500)


@investor_bp.route("/deep_track/backfill_scan/status", methods=["GET"])
def deep_track_backfill_scan_status():
    """轮询历史扫描日回补进度 —— 薄路由。"""
    try:
        return ok(read_backfill_progress())
    except Exception as e:
        return fail(f"进度读取失败: {e}", 500)


@investor_bp.route("/deep_track/<int:tid>/close", methods=["POST"])
def deep_track_close(tid: int):
    """手动平仓（body 可带 price）—— 薄路由。"""
    try:
        body = request.get_json(silent=True) or {}
        return ok(track_close_payload(tid, body.get("price")))
    except ValueError as e:
        return fail(str(e), 400)
    except Exception as e:
        return fail(f"平仓失败: {e}", 500)


@investor_bp.route("/deep_track/<int:tid>", methods=["DELETE"])
def deep_track_delete(tid: int):
    """删除一笔跟踪（不进收益统计）—— 薄路由。"""
    try:
        return ok(track_delete_payload(tid))
    except DeepTrackError as e:
        return fail(e.msg, e.status)
    except Exception as e:
        return fail(f"删除失败: {e}", 500)


# ─────────────────────────────────────────────
# 启动时确保表存在（被 app.py 注册时调用）
# ─────────────────────────────────────────────
def init_investor_tables():
    _ensure_personal_tables()
    try:
        from strategy.deep_tracker import ensure_table
        with get_conn() as conn:
            ensure_table(conn)
    except Exception:
        pass  # 建表失败不阻断启动
