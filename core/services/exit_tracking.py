"""
core/services/exit_tracking.py —— 出场跟踪编排逻辑（T7 从 routes/investor.py 下沉）
=================================================================================

承接两类共享逻辑（今日推荐卡片 + /exit_advice 端点同源）：
  - 三周期移动止盈参数（TUNABLE_PARAMS，DB 可覆盖、60s TTL）
  - 抄底类短线回踩成交模拟（与 strategy/deep_tracker 同口径）
  - 连续推荐段按卖出断点拆分
  - 对单条信号评估「推荐日后 14 天内」出场状态

纯出场计算仍在 strategy/exit_advisor.py，本模块只负责取数与参数装配。
"""
from __future__ import annotations

from typing import Optional

from config.strategy_params import get_param
from core.db import get_conn


# ─────────────────────────────────────────────
# 移动止盈参数
# ─────────────────────────────────────────────
def exit_trailing_params(horizon: str) -> tuple:
    """按周期取移动止盈参数 (trailing_pct, partial_tp, stop_cap_pct)。

    2026-08 落地：short/mid/long 三周期全部启用移动止盈（让利润奔跑，不轻易止盈）。
    partial_tp（启动线）覆盖 stock_signal.take_profit 作启动线——long 自带的
    take_profit=现价×1.5（+50% 目标价）不宜直接当启动线；short 用 short_take_profit。
    stop_cap_pct：止损宽度上限（2026-08-19 落地）——仅中线叠加，信号自带 k×ATR
    止损可达 -20%，收窄到 -12% 消除深浮亏尾部；short/long 传 None 不叠加。
    """
    if horizon == "short":
        return get_param("short_trailing_pct"), get_param("short_take_profit"), None
    if horizon == "mid":
        return (get_param("mid_trailing_pct"), get_param("mid_partial_tp"),
                get_param("mid_stop_max_width"))
    return get_param("long_trailing_pct"), get_param("long_partial_tp"), None


# ─────────────────────────────────────────────
# 抄底类短线：回踩确认入场
# ─────────────────────────────────────────────
def short_pullback_enabled() -> bool:
    """抄底类短线「回踩确认入场」开关（2026-09-05 落地，deep_track 实证口径）。"""
    try:
        return bool(int(get_param("short_pullback_entry")))
    except Exception:
        return False


def is_pullback_line(horizon: str, strategy) -> bool:
    """该信号是否走回踩确认入场：仅抄底类短线（strategy != '隔日动量'）。

    隔日动量豁免——动量 alpha 依赖次日开盘建仓，等回踩会直接错过行情；
    参数 short_pullback_entry=0 时整体回退次日开盘口径。
    """
    return (horizon == "short" and short_pullback_enabled()
            and (strategy or "") != "隔日动量")


def short_pullback_fill(df, scan_date, buy_price, window: int):
    """回踩成交模拟：信号日后 window 个交易日内 low 触及买点才成交。

    买点 = 信号日收盘价（stock_signal.buy_price）；成交价 = min(买点, 当日开盘)。
    口径与 strategy/deep_tracker._try_fill 一致（个股深度跟踪已实证
    +1.32%/胜率61.5% 的入场纪律）。
    返回 (fill_ts, fill_price, pending)：
      - 成交           → (ts, price, False)
      - 未成交+窗口走完 → (None, None, False)  → 放弃（expired，不计入收益统计）
      - 未成交+窗口未走完 → (None, None, True) → 等待（watching）
    """
    import pandas as pd
    if df is None or df.empty or not buy_price or float(buy_price) <= 0:
        return None, None, False
    if hasattr(df.index, "strftime"):
        after = df[df.index > pd.Timestamp(str(scan_date)[:10])]
    else:
        after = df[df.index > str(scan_date)[:10]]
    if after.empty:
        return None, None, True
    for ts, row in after.head(window).iterrows():
        low = row.get("low")
        if low is None or pd.isna(low):
            continue
        if float(low) <= float(buy_price):
            open_ = row.get("open")
            if open_ is None or pd.isna(open_):
                open_ = buy_price
            return ts, round(min(float(buy_price), float(open_)), 2), False
    return None, None, len(after) < window


def df_prev_day(df, ts):
    """df 中 ts 的前一个交易日（str）。用于 evaluate_exit_by_prices 的
    entry_date 传参：传 fill 日的前一交易日 → 其 after 首行恰为 fill 日，
    与 deep_tracker「j=1 = 建仓日，T+1 不判出场」语义完全对齐。"""
    try:
        pos = df.index.get_loc(ts)
    except Exception:
        return None
    if isinstance(pos, int) and pos > 0:
        return str(df.index[pos - 1])[:10]
    return None


def split_continuous_segments(seg: list, df, horizon: str, entry_mode: str = "open") -> list:
    """将一段连续推荐按卖出断点拆分。

    段内持仓若已在某交易日卖出（触发止损/止盈/到期），卖出日（含）之后的新推荐
    属于新一轮交易，不能与卖出前的推荐合并展示（如：07-31 推荐、08-03 更新止损、
    08-04 触发止损卖出，则 08-04 的新推荐应独立成段）。递归拆分直至无断点。

    entry_mode="pullback"（抄底类短线回踩确认入场，2026-09-05）：
      - 段首推荐起 window 个交易日内未回踩买点 → 整段未成交（expired/watching），
        标记记在 seg[0]["_pullback_state"]，不做卖出拆分；
      - 成交 → 以回踩成交价为建仓价、建仓日为持仓起点评估出场（entry_date 传
        建仓日前一交易日，使 evaluate_exit_by_prices 的 j=1 恰为建仓日），
        卖出日（含）后的新推荐拆为新一轮（新一轮重新等回踩）。
    """
    if len(seg) <= 1 or df is None or df.empty:
        return [seg]
    import pandas as pd
    from strategy.exit_advisor import evaluate_exit_by_prices, get_max_hold

    # 复制为 dict（元素可能是只读 sqlite3.Row；回踩状态标记需可写）
    seg = [dict(s) for s in seg]
    first, last = seg[0], seg[-1]

    entry_date_arg = first["scan_date"]
    if entry_mode == "pullback":
        window = max(1, int(get_param("short_entry_window_days")))
        fill_ts, fill_price, pending = short_pullback_fill(
            df, first["scan_date"], first["buy_price"], window)
        if fill_ts is None:
            seg[0]["_pullback_state"] = "watching" if pending else "expired"
            return [seg]
        prev_day = df_prev_day(df, fill_ts)
        if prev_day is None:
            entry_mode = "open"   # 极端情形（df 首行即成交日）回退开盘口径
        else:
            entry_price = fill_price
            entry_date_arg = prev_day

    if entry_mode == "open":
        if hasattr(df.index, 'strftime'):
            after = df[df.index > pd.Timestamp(first["scan_date"])]
        else:
            after = df[df.index > first["scan_date"]]
        if after.empty:
            return [seg]
        _f = after.iloc[0]
        entry_price = float(_f["open"]) if _f["open"] else (
            float(_f["close"]) or first["buy_price"])

    # 用段内最新止损/止盈评估整段持仓（三周期均开启移动止盈，与出场跟踪同口径）
    _trail, _partial, _stop_cap = exit_trailing_params(horizon)
    adv = evaluate_exit_by_prices(
        entry_price=entry_price,
        entry_date=entry_date_arg,
        df=df,
        stop_loss=last["stop_loss"],
        take_profit=last["take_profit"],
        max_hold_days=get_max_hold(horizon),
        trailing_pct=_trail,
        partial_tp=_partial,
        stop_cap_pct=_stop_cap,
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
    return [seg[:split_at]] + split_continuous_segments(
        seg[split_at:], df, horizon, entry_mode)


def get_exit_advice(d: dict) -> Optional[dict]:
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
        from strategy.exit_advisor import evaluate_exit_by_prices, get_max_hold
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
        hz = d.get("horizon") or "short"
        # 抄底类短线：回踩确认入场（2026-09-05，与出场跟踪/复盘同口径）
        if is_pullback_line(hz, d.get("strategy")):
            window = max(1, int(get_param("short_entry_window_days")))
            fill_ts, fill_price, pending = short_pullback_fill(
                df, scan_date, d.get("buy_price"), window)
            if fill_ts is None:
                if pending:
                    return {"status": "watching",
                            "reason": (f"等待回踩买点 {d.get('buy_price')}"
                                       f"（{window} 个交易日内触及才建仓）"),
                            "detail": {}}
                return {"status": "expired",
                        "reason": "回踩窗口内未触及买点，未成交（放弃）",
                        "detail": {}}
            prev_day = df_prev_day(df, fill_ts)
            if prev_day is not None:
                _trail, _partial, _stop_cap = exit_trailing_params(hz)
                return evaluate_exit_by_prices(
                    entry_price=fill_price,
                    entry_date=prev_day,
                    df=df,
                    stop_loss=d.get("stop_loss"),
                    take_profit=d.get("take_profit"),
                    max_hold_days=get_max_hold(hz),
                    trailing_pct=_trail,
                    partial_tp=_partial,
                    stop_cap_pct=_stop_cap,
                )
            # 极端情形（df 首行即成交日）→ 落到下方开盘口径
        _trail, _partial, _stop_cap = exit_trailing_params(hz)
        advice = evaluate_exit_by_prices(
            entry_price=entry_price,
            entry_date=str(scan_date)[:10],
            df=df,
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            max_hold_days=get_max_hold(hz),
            # 三周期均启用移动止盈：浮盈达启动线后不再达价即卖，回撤才清仓
            trailing_pct=_trail,
            partial_tp=_partial,
            stop_cap_pct=_stop_cap,
        )
        return advice
    except Exception:
        return None


def build_exit_advice_payload() -> dict:
    """出场跟踪全量视图：对 EXIT_TRACK_START_DATE 起推荐的出场状态（short/mid/long 分组）。

    口径：与「推荐复盘 / 今日推荐」对齐（short 组 = 隔日动量优先 + 低扩展度补足 +
         fusion≥short_conf_gate 门控 + 止盈离场剔除；mid/long = fusion_score 前 4）；
         起始日为 EXIT_TRACK_START_DATE，此前的推荐放弃，无结束时间限制——未卖出的持续跟踪直到卖出；
         同一股票同一周期在交易日上连续推荐时合并为一条：推荐日取最新一次、
         持仓天数从最早一次累计（买入价=最早推荐日次日开盘价）、止损/止盈按最新一次判定；
         开始持有 = 推荐日，买入价 = 推荐日之后首个交易日的开盘价；
         卖出条件 = 推荐自带止损/止盈价 + 按周期持仓上限（短线10/中线60/长线不限）。
         抄底类短线（strategy != '隔日动量'，short_pullback_entry=1）为回踩确认入场：
         T+1 起 short_entry_window_days 个交易日内 low 触及买点（信号日收盘价）才成交，
         成交价 = min(买点, 当日开盘)；未回踩 → watching/expired，不计入收益统计
         （2026-09-05 落地，根因：次日开盘追反弹高点为负期望，见推荐复盘诊断）。
    返回 {items(平铺), groups:{short,mid,long}, summary, start_date}，状态 hold/clear。
    """
    import pandas as pd
    from strategy.exit_advisor import evaluate_exit_by_prices, get_max_hold
    from config.personal_config import (main_board_filter as _main_board_filter,
                                        EXIT_TRACK_START_DATE,
                                        POSITION_PLAN_MAX_PCT)
    from config.strategy_params import T1_GAP_GUARD as _T1_GAP_GUARD
    from core.outcome_tracker import (short_gate_sql, short_order_clause,
                                      short_observe_bottom_sql,
                                      long_order_clause,
                                      long_second_order_clause,
                                      long_pool_filter_sql,
                                      long_sort_pool_mult)
    from core.repository.price_repo import get_price_frames
    from utils.serialization import sanitize_numeric

    # 板块限制：与「今日推荐」口径一致
    board_filter = _main_board_filter("s.code")

    # 选股口径与「推荐复盘（recommend_outcome）/ 今日推荐」完全一致（2026-08 短线 8→4 改造）：
    #   short = 隔日动量信号优先占名额 + 低扩展度（pct_above_ma20 升序）补足 +
    #           fusion≥short_conf_gate 门控 + 止盈离场（gap guard）剔除；
    #   mid/long = fusion_score 前 4。
    # 此前 exit_advice 仍用旧 fusion 排序（无门控/无剔除），与推荐复盘不一致——旧算法残余，已对齐。
    gate = float(get_param("short_conf_gate"))
    # gap guard 止盈离场。
    # ⚠ 2026-09-19：历史链路默认禁用（history_gap_guard 默认 0）——原逻辑用
    # latest_price.close（当前最新价）回溯历史信号 = 用未来价格剔除历史样本，
    # 使「信号发出后涨过止盈价」的赢家被系统性踢出跟踪（实测名额内 short 剔 8.9%，
    # 被剔票 T+5 +8.49%/胜率 90% vs 保留票 -0.26%/43.9%）；且实时语义下恒不触发。
    # 详见 config/strategy_params.py::history_gap_guard 注释。
    gap_enabled = (bool(_T1_GAP_GUARD.get("enabled", True))
                   and bool(int(get_param("history_gap_guard") or 0)))
    # gap guard 止盈离场（与 core/outcome_tracker.insert_new_outcomes 同口径：现价相对
    # 信号价涨幅 > 止盈涨幅 = 已错过买点不追，不纳入出场跟踪）；T1_GAP_GUARD 关闭时恒 0
    if gap_enabled:
        gap_sql = ("CASE WHEN lp.close IS NOT NULL AND lp.close > 0 "
                   "AND s.buy_price > 0 "
                   "AND s.take_profit IS NOT NULL AND s.take_profit > s.buy_price "
                   "AND (lp.close / s.buy_price - 1) > (s.take_profit / s.buy_price - 1) "
                   "THEN 1 ELSE 0 END")
    else:
        gap_sql = "0"

    with get_conn() as conn:
        # T1 辅助过滤（恐慌日闸门 + MA5 偏离，按信号日 regime 自动切换，
        # 隔日动量豁免；参数 >=99 禁用）+ 大盘走弱闸门（2026-08-26 弱市日
        # 只保留隔日动量）——各信号日按当天历史 regime 判定，两闸门共用
        # short_gate_sql（同一窗口 regime 只算一次，T4），与今日推荐/复盘同口径
        t1_cond, t1_params, mk_cond, mk_params = short_gate_sql(
            conn, EXIT_TRACK_START_DATE)
        # 抄底融合线降观察（2026-09-06）：不占短线名额（三处出口同口径）
        ob_cond, ob_params = short_observe_bottom_sql()
        horizon_specs = {
            # short：龙虎榜动量信号优先占名额，抄底票按扩展度排序补足
            # （方向由 short_ext_sort_desc 控制，见 short_order_clause；
            # 与今日推荐/推荐复盘同口径）
            "short": (short_order_clause(),
                      f" AND s.fusion_score >= ? AND {t1_cond} AND {mk_cond} AND {ob_cond}",
                      [gate, *t1_params, *mk_params, *ob_params]),
            "mid":   ("COALESCE(s.fusion_score, 0) DESC", "", []),
            # long 2026-09-20 起：「低波票池过滤 + 连续排序键」（见 long_pool_filter_sql /
            # long_order_clause 注释；原 score>=2.0 票池长期超额 −0.92%/t=−7.30）
            "long":  (long_order_clause(), long_pool_filter_sql(), []),
        }
        # 每日名额上限：short 取 short_top_n（2026-08-20 由 4 → 3），mid/long 保持前 4
        caps = {"short": max(1, int(get_param("short_top_n"))), "mid": 4, "long": 4}
        # 2026-07-20 起的推荐记录（与「推荐复盘」同口径：每日名额 short=3 / mid/long=4）
        signals = []
        for hz, (order_clause, gate_sql_cond, gate_params) in horizon_specs.items():
            _base_where = f"""
                    FROM stock_signal s
                    LEFT JOIN latest_price lp ON lp.code = s.code
                    WHERE s.scan_date >= ?
                      AND COALESCE(s.horizon, 'short') = ?
                      AND s.buy_price IS NOT NULL
                      AND s.buy_price > 0
                      AND s.name NOT LIKE '%ST%'
                      AND s.name NOT LIKE '%退%'
                      -- 强势突破是首页独立栏目信号线，不纳入出场跟踪
                      AND COALESCE(s.strategy, '') != '强势突破'
                      -- 反转首日（2026-09-24）同为独立观察栏目线，不纳入出场跟踪
                      AND COALESCE(s.strategy, '') != '反转首日'
                      -- 缩量回踩 2026-08-26 停用（实证负期望），不纳入出场跟踪
                      AND COALESCE(s.strategy, '') != '缩量回踩'
                      AND ({gap_sql}) = 0
                      {board_filter}
                      {gate_sql_cond}"""
            # 第二层作用于第一层的派生表，无表别名 ⇒ 必须传空串（见函数注释）
            _long_second = (long_second_order_clause("")
                            if hz == "long" else None)
            if _long_second:
                # long 两层排序（与复盘入库 / 今日推荐同口径，见 long_order_clause）
                _pool = caps[hz] * long_sort_pool_mult()
                signals += conn.execute(f"""
                    SELECT code, name, scan_date, buy_price,
                           stop_loss, take_profit, fusion_score, horizon, strategy
                    FROM (
                        SELECT code, name, scan_date, buy_price, stop_loss,
                               take_profit, fusion_score, horizon, strategy,
                               pct_above_ma20,
                               ROW_NUMBER() OVER (
                                   PARTITION BY scan_date, horizon
                                   ORDER BY {_long_second}
                               ) AS rn
                        FROM (
                            SELECT s.code, s.name, s.scan_date, s.buy_price,
                                   s.stop_loss, s.take_profit, s.fusion_score,
                                   COALESCE(s.horizon, 'short') AS horizon,
                                   s.strategy, s.pct_above_ma20,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY s.scan_date, COALESCE(s.horizon, 'short')
                                       ORDER BY {order_clause}
                                   ) AS rn_pool
                            {_base_where}
                        )
                        WHERE rn_pool <= ?
                    )
                    WHERE rn <= ?
                """, (EXIT_TRACK_START_DATE, hz, *gate_params,
                      _pool, caps[hz])).fetchall()
            else:
                signals += conn.execute(f"""
                    SELECT code, name, scan_date, buy_price,
                           stop_loss, take_profit, fusion_score, horizon, strategy
                    FROM (
                        SELECT s.code, s.name, s.scan_date, s.buy_price,
                               s.stop_loss, s.take_profit, s.fusion_score,
                               COALESCE(s.horizon, 'short') AS horizon,
                               s.strategy,
                               ROW_NUMBER() OVER (
                                   PARTITION BY s.scan_date, COALESCE(s.horizon, 'short')
                                   ORDER BY {order_clause}
                               ) AS rn
                        {_base_where}
                    )
                    WHERE rn <= ?
                """, (EXIT_TRACK_START_DATE, hz, *gate_params, caps[hz])).fetchall()

        if not signals:
            return {"items": [], "groups": {"short": [], "mid": [], "long": []},
                    "count": 0, "start_date": EXIT_TRACK_START_DATE,
                    "summary": {"hold": 0, "clear": 0}}

        # 按 (code, scan_date, horizon) 去重
        seen = set()
        unique_signals = []
        for s in signals:
            key = (s["code"], s["scan_date"], s["horizon"])
            if key not in seen:
                seen.add(key)
                unique_signals.append(s)

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

        # 按 code 缓存日线（连续分段拆分与评估复用）：单条 SQL 批量取回
        # （原逐票查询 = N 只 N 条 SQL，T4 消 N+1），结构与逐票一致
        _df_cache = get_price_frames(
            [seg[0]["code"] for seg in raw_segments], conn=conn)

        # 按卖出断点拆分后展开（卖出后的新推荐独立成段）
        segments = []
        for seg in raw_segments:
            code = seg[0]["code"]
            hz0 = seg[0]["horizon"]
            st0 = seg[0]["strategy"]
            mode = "pullback" if is_pullback_line(hz0, st0) else "open"
            segments.extend(split_continuous_segments(
                seg, _df_cache.get(code), hz0, mode))

        results = []
        for seg in segments:
            first = seg[0]      # 最早推荐：持仓起算日 + 真实建仓买入价
            last = seg[-1]      # 最新推荐：展示推荐日 + 最新止损/止盈判定
            code = last["code"]
            horizon = last["horizon"]

            df = _df_cache.get(code)
            if df is None or df.empty:
                continue

            # ── 抄底类短线：回踩确认入场（2026-09-05，与复盘/今日推荐同口径）──
            if is_pullback_line(horizon, last["strategy"]):
                window = max(1, int(get_param("short_entry_window_days")))
                fill_ts, fill_price, pending = short_pullback_fill(
                    df, first["scan_date"], first["buy_price"], window)
                if fill_ts is None:
                    st = "watching" if pending else "expired"
                    # 未成交（待回踩 / 窗口过期）也要回传最新价 + 所属行情日：
                    # 用于判断「现价离回踩买点还差多少」（2026-09-11 新增，供前端"现价"列）
                    _lc = df["close"].iloc[-1] if len(df) else None
                    _lc = round(float(_lc), 2) if _lc is not None and pd.notna(_lc) else None
                    results.append({
                        "code": code,
                        "name": last["name"],
                        "scan_date": last["scan_date"],
                        "horizon": horizon,
                        "strategy": last["strategy"],
                        "entry_price": round(float(first["buy_price"] or 0), 2),
                        "fusion_score": round(last["fusion_score"] or 0, 1),
                        "status": st,
                        "reason": (
                            f"等待回踩：{window} 个交易日内触及买点 "
                            f"{first['buy_price']} 才建仓"
                            if pending else
                            f"{window} 个交易日内未回踩买点，未成交（放弃）"),
                        "detail": {"entry_date": None, "hold_days": 0,
                                   "current_pnl_pct": None,
                                   "current_price": _lc,
                                   "current_date": str(df.index[-1]) if len(df) else None},
                    })
                    continue
                prev_day = df_prev_day(df, fill_ts)
                if prev_day is not None:
                    _trail, _partial, _stop_cap = exit_trailing_params(horizon)
                    advice = evaluate_exit_by_prices(
                        entry_price=fill_price,
                        entry_date=prev_day,
                        df=df,
                        stop_loss=last["stop_loss"],
                        take_profit=last["take_profit"],
                        max_hold_days=get_max_hold(horizon),
                        trailing_pct=_trail,
                        partial_tp=_partial,
                        stop_cap_pct=_stop_cap,
                    )
                    detail = dict(advice.get("detail") or {})
                    if len(seg) > 1:
                        detail["first_scan_date"] = first["scan_date"]
                    results.append({
                        "code": code,
                        "name": last["name"],
                        "scan_date": last["scan_date"],
                        "horizon": horizon,
                        "strategy": last["strategy"],
                        "entry_price": round(fill_price, 2),
                        "fusion_score": round(last["fusion_score"] or 0, 1),
                        "status": advice["status"],
                        "reason": advice["reason"],
                        "detail": detail,
                    })
                    continue
                # 极端情形（df 首行即成交日）→ 落到下方开盘口径

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

            # 持仓从最早推荐日起算；止损/止盈按最新推荐判定（三周期均开启移动止盈）
            _trail, _partial, _stop_cap = exit_trailing_params(horizon)
            advice = evaluate_exit_by_prices(
                entry_price=entry_price,
                entry_date=first["scan_date"],
                df=df,
                stop_loss=last["stop_loss"],
                take_profit=last["take_profit"],
                max_hold_days=get_max_hold(horizon),
                trailing_pct=_trail,
                partial_tp=_partial,
                stop_cap_pct=_stop_cap,
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

    # ── 按周期统计持有收益（clear=已实现出场收益，hold=当前浮盈）──
    def _seg_pnl(r):
        v = (r.get("detail") or {}).get("current_pnl_pct")
        return v if isinstance(v, (int, float)) else None

    perf = {}
    for hz in ("short", "mid", "long"):
        segs = [r for r in results if r["horizon"] == hz]
        clears = [r for r in segs if r["status"] == "clear"]
        holds = [r for r in segs if r["status"] == "hold"]
        watches = [r for r in segs if r["status"] == "watching"]
        expireds = [r for r in segs if r["status"] == "expired"]
        cp = [v for v in (_seg_pnl(r) for r in clears) if v is not None]
        hp = [v for v in (_seg_pnl(r) for r in holds) if v is not None]
        allp = cp + hp
        # ⚠ 口径（2026-09-11 修正）：单笔收益率**不能简单相加**——把 37 笔的 -0.8%~-7.6%
        # 直接求和会得到 "-111.7%"，读起来像"本金亏了 111%"，实际只是算术和，无资金含义。
        # 正确展示两个口径：
        #   avg_pct  = 平均每笔收益（等权，无仓位假设）—— 唯一不依赖仓位假设的可比指标
        #   comp_pct = 等权资金曲线（按每笔满仓复利连乘）—— 标准策略业绩口径
        # cum_pnl 保留为算术和（仅调试用，前端不再作为主口径展示）
        #
        # ⚠ 口径（2026-09-19 再修正）：comp_pct（逐笔**顺序**复利）本身也是误导的——
        # 它把**并行**持仓当成**串联**：短线 40+ 笔分布在 40 个交易日里、每日最多 3 只
        # 同时持有，按顺序复利等价于假设"一份钱做完一笔再做下一笔"，于是 -2% 的单笔
        # 均摊被放大成 -65% 的"累计"，视觉上像本金亏光。前端已弃用该口径，改用：
        #   pnl_at_pos = 按仓位累计 = Σ(单笔收益%) × POSITION_PLAN_MAX_PCT
        # 每笔按 20% 仓位下单时，对总资金的影响可**线性累加**（忽略复利，量级一致），
        # 有明确资金含义，直接回答"我的账户大概亏/赚了多少"。comp_pct 保留字段仅供调试。
        _avg = round(sum(allp) / len(allp), 2) if allp else None
        _comp = None
        if allp:
            _c = 1.0
            for _v in allp:
                _c *= (1 + _v / 100)
            _comp = round((_c - 1) * 100, 1)
        perf[hz] = {
            "segments": len(segs),
            "hold": len(holds),
            "clear": len(clears),
            # 持仓上限（单一事实来源 = get_max_hold，DB 覆盖后前端不会显示旧值）：
            # 供「持仓 5/10」这类展示，避免把 10/60 硬编码进前端
            "max_hold": get_max_hold(hz),
            # 回踩确认入场（2026-09-05）：待回踩/未成交未建仓，
            # 不计入收益与胜率统计，仅计数展示
            "watching": len(watches),
            "expired": len(expireds),
            # 已清仓：平均出场收益与胜率（相对建仓价，含止损/止盈/到期出场）
            "clear_mean_pct": round(sum(cp) / len(cp), 2) if cp else None,
            "clear_win_rate": round(sum(1 for v in cp if v > 0) / len(cp) * 100, 1) if cp else None,
            # 持仓中：当前平均浮盈
            "hold_mean_pct": round(sum(hp) / len(hp), 2) if hp else None,
            # 综合（清仓+持仓）：全部段平均持有收益
            "all_mean_pct": round(sum(allp) / len(allp), 2) if allp else None,
            # 展示口径（见上方注释）
            "avg_pct": _avg,
            "comp_pct": _comp,     # 逐笔顺序复利（已弃用，仅调试/向后兼容）
            "pnl_at_pos": (round(sum(allp) * POSITION_PLAN_MAX_PCT, 1)
                           if allp else None),
            "pos_pct": round(POSITION_PLAN_MAX_PCT * 100),
            "cum_pnl": round(sum(allp), 2) if allp else None,
            "win_rate": round(sum(1 for v in allp if v > 0) / len(allp) * 100, 1) if allp else None,
        }

    return sanitize_numeric({
        "items": results,
        "groups": groups,
        "count": len(results),
        "start_date": EXIT_TRACK_START_DATE,
        "summary": {
            "hold": sum(1 for r in results if r["status"] == "hold"),
            "clear": sum(1 for r in results if r["status"] == "clear"),
            "perf": perf,
        },
    })
