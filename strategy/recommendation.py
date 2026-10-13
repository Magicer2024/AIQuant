"""
strategy/recommendation.py —— 「今日推荐」三周期分组编排（T7 从 routes/investor.py 下沉）
=======================================================================================

路由只做参数解析与 ok/fail 包装；本模块承接：
  - 操作信号灯判定 calc_signal（评分/盈亏比/大盘冷热/持仓联动/趋势）
  - build_today_payload：stock_signal 取数 → 置信门控/弱市闸门/票池过滤 →
    散户可执行卡片（挂单价位、分批建仓计划、突破确认、gap guard）→
    regime 天花板分组（short/mid/long）。

SQL 闸门/排序口径仍在 core.outcome_tracker（三处出口同源）；
出场建议在 core/services/exit_tracking.py；regime 缓存在 strategy/market_regime.py。
"""
from __future__ import annotations

import json

from config.personal_config import (
    POSITION_PLAN_ACCOUNT, POSITION_PLAN_MAX_PCT, main_board_filter,
)
from config.strategy_params import T1_GAP_GUARD, RECO_REGIME_CAP, get_param
from core.db import get_conn
from core.services.exit_tracking import get_exit_advice
from strategy.market_regime import compute_market_regime
from utils.api import identity_meta
from utils.serialization import sanitize_numeric

# 大盘冷热中文标签（信号灯文案）
REGIME_LABEL = {
    "hot": "火热", "warm": "偏暖", "neutral": "震荡",
    "cool": "偏冷", "cold": "冰点", "unknown": "数据不足",
}


def calc_signal(*, score, risk_reward, risk_pct, market_regime, is_held, trend_up,
                horizon=None):
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
    #    2026-08 回测验证：fs≥30 + 低扩展度组合 +0.468%/笔（正期望，累计+146%），
    #    22~30 区间 -0.149%/笔（负期望）→ 可建仓门槛保持 30，不随门控(22)下调，
    #    否则把历史负期望的票标成「可建仓」误导用户。
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
        _extra = ["未持仓，可建仓"]
        if horizon == "short":
            # P0-1.1 改动 C：短线快进快出提示（持仓上限 1 天，买入次日收盘了结）
            _extra.append("短线快进快出：买入次日收盘了结，不恋战")
        return {
            "level": "buy",
            "emoji": "🟢",
            "label": "可建仓",
            "reasons": reasons + _extra,
            "warnings": warnings,
        }


def build_today_payload(*, limit: int, horizon_filter, target_date) -> dict:
    """今日推荐（action plan）—— 按 short/mid/long 三周期分组。

    来源：stock_signal 表（融合分高 + 已有明确买入价/止损/止盈）
    分组：mid/long 按 fusion_score DESC；short 先 fusion≥门控、再按低扩展度排序，
          各取 limit 条（默认 4）；short 组另受 short_top_n=3 约束（2026-08-20 收缩）；
          regime 天花板（RECO_REGIME_CAP 单一来源）：short cold→0/cool→2；
          mid/long cool→2/1 收缩、cold 不归零（2026-09-15，实测 cold 是 mid/long
          唯一正期望档）；其余 regime 走各周期默认上限
    target_date：指定日期；None 用 stock_signal 最新扫描日
    返回 {date, market_regime, count, groups:{short,mid,long}, items(=short 组别名，兼容旧前端)}
    """
    # short 组上限：short_top_n（2026-08-20 由 4 → 3，精选；mid/long 仍用原 limit 逻辑）
    SHORT_CAP = min(limit, max(1, int(get_param("short_top_n"))))

    with get_conn() as conn:
        # 如果指定了日期，用指定日期；否则用最新日期
        if target_date:
            scan_date = target_date
        else:
            row = conn.execute("SELECT MAX(scan_date) AS d FROM stock_signal").fetchone()
            scan_date = row["d"] if row else None

        if not scan_date:
            return {
                "date": None, "count": 0, "items": [],
                "groups": {"short": [], "mid": [], "long": []},
                "market_regime": "unknown",
                "meta": identity_meta(cohort="production", data_as_of=None, conn=conn),
            }

        # stock_info.pe_ttm 可能不存在（老库）—— 降级：有才启用长线估值过滤
        has_pe_ttm = False
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_info)").fetchall()}
            has_pe_ttm = "pe_ttm" in cols
        except Exception:
            has_pe_ttm = False

        horizons = [horizon_filter] if horizon_filter else ["short", "mid", "long"]
        # 板块限制：仅推主板（白名单沪 60x / 深 00x，与 is_main_board 同口径，方案 E2）
        board_filter = main_board_filter("s.code")
        rows_by_horizon: dict = {}
        # 短线置信门控（S4 口径，默认 22，可 DB 覆盖）：低于门控不推荐，弱日自然出 0
        gate = float(get_param("short_conf_gate"))
        # T1 辅助过滤（恐慌日闸门 + MA5 偏离，按当日 regime 自动切换，
        # 隔日动量豁免；参数 >=99 禁用）+ 大盘走弱闸门（2026-08-26）：
        # 弱市日只保留隔日动量（正期望 alpha 线），抄底/回踩类一律不推；
        # 两闸门共用 short_gate_sql（同一窗口 regime 只算一次，T4），
        # 与出场跟踪/复盘入库同口径。
        from core.outcome_tracker import (short_gate_sql, short_order_clause,
                                          short_observe_bottom_sql,
                                          long_order_clause,
                                          long_second_order_clause,
                                          long_pool_filter_sql,
                                          long_sort_pool_mult)
        t1_cond, t1_params, mk_cond, mk_params = short_gate_sql(conn, scan_date, scan_date)
        # 抄底融合线降观察（2026-09-06）：不占短线名额（三处出口同口径）
        ob_cond, ob_params = short_observe_bottom_sql()
        for hz in horizons:
            pe_filter = ""
            if hz == "long" and has_pe_ttm:
                # 长线降级财务过滤：pe_ttm 有值时剔除亏损股（<=0）与明显高估股（>100）
                pe_filter = " AND (i.pe_ttm IS NULL OR (i.pe_ttm > 0 AND i.pe_ttm <= 100))"
            gate_sql = ""
            gate_params: list = []
            if hz == "short":
                gate_sql = f" AND s.fusion_score >= ? AND {t1_cond} AND {mk_cond} AND {ob_cond}"
                gate_params = [gate, *t1_params, *mk_params, *ob_params]
            # short 组选择标准（S4 口径 + 龙虎榜动量优先）：
            #   1) 隔日动量信号（独立正期望 alpha 线，fusion≥30 天然过门控）优先占名额；
            #   2) 抄底票按扩展度排序补足剩余名额（方向由 short_ext_sort_desc 控制，
            #      当前升序=低扩展优先；2026-08-22 降序实验被真实复盘口径推翻回退，
            #      见 short_order_clause 注释）。
            # mid 保持融合分排序；long 2026-09-19 起用两层排序（波动收敛池 → 不追高），
            # 见 long_order_clause 注释（原 fusion DESC 只有 3 个离散值，top4 等价随机）
            if hz == "short":
                order_clause = short_order_clause()
            elif hz == "long":
                order_clause = long_order_clause()
            else:
                order_clause = "COALESCE(s.fusion_score, 0) DESC"
            # long 两层排序：先按 vol_ratio 取池，再在池内按扩展度升序取名额
            # 第二层作用于第一层的派生表，无表别名 ⇒ 必须传空串（见函数注释）
            _long_second = long_second_order_clause("") if hz == "long" else None
            # long 票池过滤（趋势+斜率+低波三项必需），与复盘入库/出场跟踪同口径。
            # 2026-09-20：票池本身长期负 alpha（−0.92%/t=−7.30），必须过滤而非只换排序键
            _long_pool = long_pool_filter_sql() if hz == "long" else ""
            # 今日推荐多取 3 倍候选（盈亏比不达标的「避免」级会被剔除，由替补顶上）
            _want = limit * 3
            _inner = f"""
                SELECT
                    s.code, s.name, s.price AS signal_price,
                    s.buy_price, s.stop_loss, s.take_profit,
                    s.fusion_score, s.vol_score, s.ma_score,
                    s.diverge_score, s.bottom_score, s.whale_score,
                    s.trigger_list, s.scan_date, s.trade_date,
                    COALESCE(s.horizon, 'short') AS horizon,
                    s.strategy,
                    COALESCE(s.pct_above_ma20, 0) AS pct_above_ma20,
                    -- 优先取 daily_price 最新交易日（与 confirm_trigger 同源）；
                    -- latest_price 物化表可能滞后（增量路径曾不刷新），兜底防过期价误判突破确认
                    COALESCE((SELECT d.close FROM daily_price d
                              WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1),
                             lp.close) AS latest_close,
                    COALESCE((SELECT d.pct_change FROM daily_price d
                              WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1),
                             lp.pct_change) AS latest_pct,
                    COALESCE((SELECT d.high FROM daily_price d
                              WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1),
                             lp.high) AS latest_high,
                    COALESCE((SELECT d.low FROM daily_price d
                              WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1),
                             lp.low) AS latest_low,
                    -- 最新价所属的行情日：前端据此提示"最新价是哪天的"，
                    -- 停牌 / 数据滞后的票一眼可辨（2026-09-11 新增，配合最新价列展示）
                    (SELECT d.trade_date FROM daily_price d
                     WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1) AS latest_date,
                    i.industry
                FROM stock_signal s
                LEFT JOIN latest_price lp ON lp.code = s.code
                LEFT JOIN stock_info i ON i.code = s.code
                WHERE s.scan_date = ?
                  AND COALESCE(s.horizon, 'short') = ?
                  AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
                  AND s.name NOT LIKE '%ST%'
                  AND s.name NOT LIKE '%退%'
                  -- 强势突破是首页独立栏目信号线，不占今日推荐名额
                  AND COALESCE(s.strategy, '') != '强势突破'
                  -- 反转首日（2026-09-24）同为独立观察栏目线，不占今日推荐名额
                  AND COALESCE(s.strategy, '') != '反转首日'
                  -- 缩量回踩 2026-08-26 停用（出场跟踪实证负期望，挤占正期望动量名额），
                  -- 与退出跟踪/复盘入库同口径不占名额
                  AND COALESCE(s.strategy, '') != '缩量回踩'
                  {board_filter}
                  {pe_filter}
                  {gate_sql}
                  {_long_pool}
                ORDER BY {order_clause}
                LIMIT ?
                """
            if _long_second:
                # 两层：内层按波动收敛取池，外层在池内按扩展度升序取名额。
                # ⚠ 不能写成单层字典序 ORDER BY vol_ratio ASC, pct_above_ma20 ASC
                # （两者皆连续值 ⇒ 退化成 vol_ratio 单键，2026 年 Δ=−5.62%）。
                _sql = (f"SELECT * FROM ({_inner}) "
                        f"ORDER BY {_long_second} LIMIT ?")
                _params = (scan_date, hz, *gate_params,
                           _want * long_sort_pool_mult(), _want)
            else:
                _sql, _params = _inner, (scan_date, hz, *gate_params, _want)
            rows_by_horizon[hz] = conn.execute(_sql, _params).fetchall()
        all_rows = [r for hz in horizons for r in rows_by_horizon[hz]]

        # ── 联动查询 1：当前大盘冷热（影响信号灯；多日宽度 composite，10 分钟缓存） ──
        market_regime = compute_market_regime(conn)
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
            is_momo = (d.get("strategy") == "隔日动量")
            if is_momo:
                # 龙虎榜动量：子分（vol/ma/bottom 等）恒 0，形态类理由会误导（"量能不足/
                # 均线未多头"与动量信号无关）。改用龙虎榜净买理由 + T+1 快进快出提示。
                reasons.append("龙虎榜主力净买，隔日动量信号（正期望在次日）")
                weaknesses.append(
                    "T+1 快进快出：次日开盘买入、次日收盘了结，不恋战（持仓超 1 天易回吐）")
            else:
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

            # ── 建仓分批建议（账户规模/占比见 config.personal_config；约束口径见 utils.trade_constraints）──
            # 静态参考预算，非账户可用现金：budget_kind=reference_budget、available_cash=None。
            position_plan = None
            if entry > 0:
                from utils import trade_constraints as _tc
                from config.personal_config import PLAN_BATCH_WEIGHTS as _BW
                # 总金额上限：账户 × 仓位占比
                max_amount = int(POSITION_PLAN_ACCOUNT * POSITION_PLAN_MAX_PCT)
                # 用激进价（最保守可成交价）估算最大可买，计入 100 股整手与最低佣金
                est_price = aggressive if entry_strategy else entry
                afford = _tc.max_affordable_shares(max_amount, est_price)
                _notes = ("试仓", "回踩加仓", "深度回踩加仓")
                _triggers = ("现价附近直接买入 {n} 股", "回踩 -3% 买入 {n} 股",
                             "深度回踩 -6% 买入 {n} 股")
                _warning = ("静态参考预算，非账户可用现金；"
                            "分批为 50/30/20 最大余数法整手分配")
                if not afford["executable"] or not entry_strategy:
                    # 1 手也买不起（或缺挂单价）：明确标记不可执行 + 原因，不生成虚构仓位
                    position_plan = {
                        "account_size": POSITION_PLAN_ACCOUNT,
                        "max_position_pct": int(POSITION_PLAN_MAX_PCT * 100),
                        "max_amount": max_amount,
                        "executable": False,
                        "reason": afford["reason"] or "资金不足 1 手（100 股）",
                        "budget_kind": "reference_budget",
                        "available_cash": None,
                        "total_shares": 0,
                        "total_cost_est": 0,
                        "batches": [],
                        "warning": _warning,
                    }
                else:
                    total_shares = afford["shares"]
                    # 50/30/20 最大余数法分配整手；keep_zeros 保证价格/备注按原始权重索引对齐
                    per_batch = _tc.allocate_batches(total_shares, _BW, keep_zeros=True)
                    prices = (entry_strategy["aggressive"], entry_strategy["stable"],
                              entry_strategy["conservative"])
                    batches = []
                    total_cost = 0.0
                    total_commission = 0.0
                    for i, sh in enumerate(per_batch):
                        if sh <= 0:
                            continue  # 删除 0 股批次
                        price = prices[i] if i < len(prices) else prices[-1]
                        amount = sh * price
                        commission = _tc.buy_commission(amount)
                        total_cost += amount
                        total_commission += commission
                        batches.append({
                            "ratio": _BW[i] if i < len(_BW) else 0,
                            "shares": sh,
                            "price": price,
                            "amount": round(amount, 0),
                            "commission": round(commission, 2),
                            "total": round(amount + commission, 2),
                            "note": _notes[i] if i < len(_notes) else "",
                            "trigger": (_triggers[i] if i < len(_triggers)
                                        else "买入 {n} 股").format(n=sh),
                        })
                    position_plan = {
                        "account_size": POSITION_PLAN_ACCOUNT,
                        "max_position_pct": int(POSITION_PLAN_MAX_PCT * 100),
                        "max_amount": max_amount,
                        "executable": True,
                        "budget_kind": "reference_budget",
                        "available_cash": None,
                        "total_shares": total_shares,
                        # 重新检查各批最低佣金后的总金额
                        "total_cost_est": round(total_cost + total_commission, 0),
                        "total_commission_est": round(total_commission, 2),
                        "batches": batches,
                        "warning": _warning,
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

            signal = calc_signal(
                score=score,
                risk_reward=risk_reward,
                risk_pct=risk_pct,
                market_regime=market_regime,
                is_held=is_held,
                trend_up=trend_up,
                horizon=d.get("horizon"),
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
                "pct_above_ma20": d.get("pct_above_ma20"),
                "latest_close": d.get("latest_close"),
                "latest_pct": d.get("latest_pct"),
                "latest_date": d.get("latest_date"),
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
                "exit_advice": get_exit_advice(d),  # P0: 出场建议
                "reasons": reasons,
                "weaknesses": weaknesses,
                "triggers": triggers,
                "scan_date": d.get("scan_date"),
                "trade_date": d.get("trade_date"),
            }

        # 展示层 regime 天花板（config.RECO_REGIME_CAP 单一来源）：
        #   short: cold→0 / cool→2 / 其余→short_top_n；
        #   mid/long: cool 收缩（2/1），cold 不再归零 —— 2026-09-14 实测 cold 是
        #   mid/long 唯一正期望档（long T+60 +2.96% n=2328），归零前提与数据相反，
        #   且当日曾致三档全空（mid 11 条 / long 345 条达标被丢）；其余 → limit。
        groups = {}
        for hz in horizons:
            items = [_build_item(dict(r)) for r in rows_by_horizon[hz]]
            # 剔除「避免」（盈亏比<1.5 一票否决）与「止盈离场」（gap guard：
            # 现价相对信号价已超止盈价，错过买点不追）——sell 级不占推荐名额，
            # 由后续候选顶上或数量减少（宁缺毋滥）。
            items = [it for it in items
                     if it["signal"]["level"] not in ("avoid", "sell")]
            default_cap = SHORT_CAP if hz == "short" else limit
            cap = RECO_REGIME_CAP.get(hz, {}).get(market_regime, default_cap)
            items = items[:cap] if cap > 0 else []
            groups[hz] = items
        for hz in ("short", "mid", "long"):
            groups.setdefault(hz, [])
        short_items = groups.get("short", [])
        return {
            "date": scan_date,
            "latest_trade_date": _latest_td,  # 推荐是否为最新交易日由前端据此判断
            "count": sum(len(v) for v in groups.values()),
            "groups": sanitize_numeric(groups),
            "items": sanitize_numeric(short_items),   # 兼容旧前端：items = short 组
            "market_regime": market_regime,  # 让前端知道当前大盘冷热
            # 身份链元数据（方案 F1）：正式主榜、数据日期、参数版本、信号模型模式
            "meta": identity_meta(cohort="production", data_as_of=scan_date, conn=conn),
        }
