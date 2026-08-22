"""
core/outcome_tracker.py —— 推荐结果闭环追踪
=============================================

将 stock_signal 中的每条推荐持久化到 recommend_outcome 表，
并在后续交易日逐步填充 T+1/3/5/10 收益、最大浮盈/浮亏、
是否触发止损/止盈、出场原因等，形成完整的胜率闭环。

调用时机：
  - recalc_all_scores 末尾（best-effort）
  - 可独立调用：python -c "from core.outcome_tracker import run; run()"
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from core.db import get_conn
from config.personal_config import (
    MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES, EXIT_TRACK_START_DATE,
)


# ─────────────────────────────────────────────
# 0. 短线 T1 辅助过滤（恐慌日闸门 + MA5 偏离，按大盘冷热自动切换）
# ─────────────────────────────────────────────

def short_t1_filter_sql(conn, start_date: str, end_date: str | None = None,
                        alias: str = "s") -> tuple[str, list]:
    """短线抄底推荐 T1 辅助过滤 SQL 片段（组合条件 C + regime 自动切换）。

    2026-08-20 挖掘落地（tools/eval_short_t1_filter.py 特征分桶 +
    eval_short_t1_filter2.py 实装语义复核）：抄底票应在市场下跌日推荐
    （恐慌日反弹概率高），上涨日推荐等于追高。组合条件：
      1) 恐慌日闸门：信号日全市场平均涨跌幅 < short_down_market_gate（默认 0）
      2) MA5 偏离：信号日收盘价距 MA5 偏离 <= short_dev_ma5_max（默认 -2%）
    test 窗 T1 胜率 42.1%→49.5%（+7.4pp），但强市段均值反向——闸门本质是
    弱市减亏工具。故按信号日当天的大盘 regime 自动切换（与前端信号灯同口径，
    core/market_regime.py，历史可复现）：
      cold/cool（冰点/偏冷）→ 启用过滤；neutral/warm/hot/unknown → 不过滤。

    隔日动量信号豁免（独立正 alpha 信号线，不受抄底过滤器约束）；
    任一参数设 >=99 即禁用对应条件（两者都禁则恒真，等同回退）。
    [start_date, end_date] 为信号日窗口（end_date 缺省到最新数据日）。
    返回 (sql_cond, params)：sql_cond 为完整布尔表达式，AND 到 short 组 WHERE。
    """
    from config.strategy_params import get_param
    mkt_gate = float(get_param("short_down_market_gate"))
    dev = float(get_param("short_dev_ma5_max"))
    if mkt_gate >= 99 and dev >= 99:
        return "1", []
    # regime 自动切换：仅信号日为冰点/偏冷的日期启用过滤
    from core.market_regime import compute_regime_series
    series = compute_regime_series(conn, start_date, end_date)
    active = sorted(d for d, r in series.items() if r in ("cold", "cool"))
    if not active:
        return "1", []
    parts: list = []
    params: list = []
    if mkt_gate < 99:
        parts.append(f"(SELECT AVG(pct_change) FROM daily_price "
                     f"WHERE trade_date = {alias}.scan_date) < ?")
        params.append(mkt_gate)
    if dev < 99:
        # dev<=-2 → close/MA5-1 <= -2% → MA5 >= close/(1+dev/100)
        # 抄底信号 s.price = 信号日收盘价（sync.py 写入），round(2) 误差可忽略
        parts.append(f"(SELECT AVG(close) FROM (SELECT close FROM daily_price d "
                     f"WHERE d.code = {alias}.code AND d.trade_date <= {alias}.scan_date "
                     f"ORDER BY d.trade_date DESC LIMIT 5)) >= {alias}.price / ?")
        params.append(1 + dev / 100.0)
    if not parts:
        return "1", []
    ph = ",".join("?" * len(active))
    # 非启用日（scan_date NOT IN）直接放行；启用日豁免独立正期望信号线
    # （隔日动量/缩量回踩，不受抄底恐慌日过滤约束，与挖掘口径一致）
    return (f"(COALESCE({alias}.strategy, '') IN ('隔日动量', '缩量回踩') "
            f"OR {alias}.scan_date NOT IN ({ph}) "
            f"OR ({' AND '.join(parts)}))"), [*active, *params]


def short_order_clause(alias: str = "s") -> str:
    """短线候选排序 SQL 子句：独立正期望信号线优先占名额，其余按扩展度排序。

    名额优先级：隔日动量(0) > 缩量回踩(1, 2026-08-22 落地，train/test 双正，
    见 strategy/pullback_dip.py) > 其余抄底票(2)。抄底票之间按扩展度排序，
    方向由 short_ext_sort_desc 控制（1=降序 / 0=升序，当前默认升序）。
    2026-08-22 曾依据 tools/eval_short_fusion_cap.py（候选缓存评估）短暂切换
    降序，同日被真实复盘口径重放（tools/eval_short_sort_replay.py，recon 窗
    desc -2.27% vs asc -0.29%）推翻并回退，详见参数注释。三处出口
    （今日推荐/出场跟踪/复盘入库）共用本函数保证口径一致。
    """
    from config.strategy_params import get_param
    direction = "DESC" if int(get_param("short_ext_sort_desc")) else "ASC"
    return (f"CASE WHEN {alias}.strategy = '隔日动量' THEN 0 "
            f"WHEN {alias}.strategy = '缩量回踩' THEN 1 ELSE 2 END, "
            f"COALESCE({alias}.pct_above_ma20, 0) {direction}, "
            f"COALESCE({alias}.fusion_score, 0) DESC")


# ─────────────────────────────────────────────
# 1. 从 stock_signal 导入新推荐到 recommend_outcome
# ─────────────────────────────────────────────

def insert_new_outcomes(days_back: int = 60):
    """将 stock_signal 中窗口内、尚未录入 recommend_outcome 的推荐写入。

    口径：与「今日推荐」面板一致（config/personal_config.py 的主板过滤 +
    每日名额上限才是真正的「推荐」：short 取 short_top_n=3（2026-08-20 由 4 收缩），
    mid/long 取前 4；short 组额外与今日推荐同口径：
    fusion≥short_conf_gate 门控 + T1 辅助过滤（恐慌日闸门 + MA5 偏离，
    隔日动量豁免）+ 扩展度排序（方向由 short_ext_sort_desc 控制，当前升序）
    + 止盈离场(gap guard sell)剔除——2026-08 短线 8→4 改造）。

    窗口：近 N 天 与 EXIT_TRACK_START_DATE（2026-07-20，出场跟踪/推荐复盘起始日）
    取较晚者——该日期之前的推荐视为旧算法数据，不写入也不保留（清理后不回填）。

    2026-08-09 修复：原 INSERT OR IGNORE 按 (code, scan_date, horizon) 去重追加，
    多次重算（v1/v2 引擎、不同过滤链）的 Top8 并集在表内累积，short 组每天
    19~28 条。改为窗口内「先清空再重写」：recommend_outcome 恒等于当前
    stock_signal 的每日每组名额上限截取（收益字段清空后由 evaluate_outcomes 重新评估）。
    """
    from datetime import date, timedelta
    start_date = max(
        (date.today() - timedelta(days=days_back)).isoformat(),
        EXIT_TRACK_START_DATE,
    )
    # 板块限制：与 routes/investor.py 的今日推荐同口径（小资金仅推主板）
    board_filter = ""
    if MAIN_BOARD_ONLY:
        board_filter = "".join(
            f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)
    # short 组与今日推荐同口径：fusion 门控 + 低扩展度排序 + 止盈离场剔除
    from config.strategy_params import get_param, T1_GAP_GUARD
    gate = float(get_param("short_conf_gate"))
    gap_enabled = bool(T1_GAP_GUARD.get("enabled", True))
    # gap guard 止盈离场（与 routes/investor.py 同口径：现价相对信号价涨幅
    # > 止盈涨幅 = 已错过买点不追，不导入复盘追踪）；T1_GAP_GUARD 关闭时恒 0
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
        # 隔日动量豁免；参数 >=99 禁用）——复盘窗内各信号日按当天历史 regime 判定
        t1_cond, t1_params = short_t1_filter_sql(conn, start_date)
        horizon_specs = {
            # short：龙虎榜动量信号优先占名额，抄底票按扩展度排序补足
            # （方向由 short_ext_sort_desc 控制，见 short_order_clause；与今日推荐同口径）
            "short": (short_order_clause(),
                      f" AND s.fusion_score >= ? AND {t1_cond}", [gate, *t1_params]),
            "mid":   ("COALESCE(s.fusion_score, 0) DESC", "", []),
            "long":  ("COALESCE(s.fusion_score, 0) DESC", "", []),
        }
        # 每日名额上限：short 取 short_top_n（2026-08-20 由 4 → 3），mid/long 保持前 4
        top_n = max(1, int(get_param("short_top_n")))
        caps = {"short": top_n, "mid": 4, "long": 4}
        # 先清窗口内旧记录（随引擎/过滤链/推荐口径变化而更新，旧记录一并移除）
        conn.execute(
            "DELETE FROM recommend_outcome WHERE scan_date >= ?",
            (start_date,))
        for hz, (order_clause, gate_sql_cond, gate_params) in horizon_specs.items():
            # 注意：gap_sell 过滤必须发生在 ROW_NUMBER 之前（先剔除止盈离场、
            # 再按扩展度取前 caps[hz]），与今日推荐「先剔 sell 再截取」语义一致，
            # 被剔除的票不占排名、由后续候选替补。
            conn.execute(f"""
                INSERT OR IGNORE INTO recommend_outcome
                    (code, scan_date, horizon, strategy, entry_price,
                     stop_loss, take_profit, fusion_score)
                SELECT code, scan_date, horizon, strategy, buy_price,
                       stop_loss, take_profit, fusion_score
                FROM (
                    SELECT
                        s.code,
                        s.scan_date,
                        COALESCE(s.horizon, 'short') AS horizon,
                        s.strategy,
                        s.buy_price,
                        s.stop_loss,
                        s.take_profit,
                        s.fusion_score,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.scan_date, COALESCE(s.horizon, 'short')
                            ORDER BY {order_clause}
                        ) AS rn
                    FROM stock_signal s
                    LEFT JOIN latest_price lp ON lp.code = s.code
                    WHERE s.scan_date >= ?
                      AND COALESCE(s.horizon, 'short') = ?
                      AND s.buy_price IS NOT NULL
                      AND s.buy_price > 0
                      AND s.name NOT LIKE '%ST%'
                      AND s.name NOT LIKE '%退%'
                      -- 强势突破是首页独立栏目信号线，不计入推荐复盘胜率
                      AND COALESCE(s.strategy, '') != '强势突破'
                      AND ({gap_sql}) = 0
                      {board_filter}
                      {gate_sql_cond}
                )
                WHERE rn <= ?
            """, (start_date, hz, *gate_params, caps[hz]))


# ─────────────────────────────────────────────
# 2. 评估未完成的推荐结果
# ─────────────────────────────────────────────

def evaluate_outcomes():
    """遍历 recommend_outcome 中尚未完全评估的记录，用 daily_price 填充收益。

    评估逻辑（horizon 感知）：
      - short：T+1/2/3/5/10 收益 + 移动止盈出场（2026-08-20 方案A：止损收盘判定、
        启动线激活后高点回撤清仓、持满 short_max_hold_days 了结，与出场跟踪同口径）。
      - mid/long：长周期策略——用 evaluate_exit_by_prices 按真实出场纪律
        （移动止盈 + 周期持仓上限）逐日模拟出场。持仓期远超短线的 10 天：
        中线窗口 ~70 交易日（max_hold=60）、长线窗口 ~130 交易日（max_hold=None
        不设强制出场上限）。未出场时 exit_return 保持 NULL，由后续交易日继续评估，
        避免「60+ 天策略被 10 天强制了结」的误判。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        # short：沿用原触发条件（t10 未评估完）；mid/long：只要还没出场就持续评估
        rows = conn.execute("""
            SELECT id, code, scan_date, horizon, entry_price, stop_loss, take_profit
            FROM recommend_outcome
            WHERE entry_price > 0
              AND (
                (COALESCE(horizon, 'short') = 'short'
                 AND (t10_return IS NULL OR evaluated_at IS NULL OR t2_return IS NULL))
                OR
                (COALESCE(horizon, 'short') IN ('mid', 'long')
                 AND (exit_return IS NULL OR evaluated_at IS NULL))
              )
            ORDER BY scan_date ASC
        """).fetchall()

        if not rows:
            return 0

        updated = 0
        for row in rows:
            horizon = row["horizon"] or "short"
            if horizon in ("mid", "long"):
                if _evaluate_mid_long(conn, row, horizon, now_str):
                    updated += 1
            else:
                if _evaluate_short(conn, row, now_str):
                    updated += 1

    return updated


def _evaluate_short(conn, row, now_str: str) -> bool:
    """短线评估：T+N 收益 + 移动止盈出场（2026-08-20 方案A 口径统一）。

    出场与 routes/investor.py 出场跟踪（evaluate_exit_by_prices）及退出网格
    回测（tools/eval_short_return_boost.py simulate）同口径：
      - 出场模拟 entry = 推荐日后首个交易日开盘价（2026-08-22 对齐出场跟踪/
        实盘执行；开盘缺失回退当日收盘/信号价，同 _get_exit_advice 回退链）。
        此前用信号日收盘价作 entry，启动线偏低导致出场判定与出场跟踪矛盾
        （如 002379 复盘 +4.12% vs 出场跟踪 -11.11%）。
      - A股 T+1：买入日只累计持仓/状态，不检查出场（与出场跟踪一致）
      - 止损：收盘价跌破止损价（推荐自带 stop_loss，缺失回退 exec_entry×(1+short_stop_loss)）
      - 移动止盈：持仓期最高价（盘中 high）首次达到启动线（short_take_profit 比例×
        exec_entry，回退推荐自带 take_profit 价）后启用；移动止盈线 = 最高价×
        (1-short_trailing_pct)，只上移不下移；收盘跌破 → trailing_stop 离场
      - 到期：持满 short_max_hold_days（方案A=10）以收盘了结
    T+N 收益/max_return/min_return 口径不变（entry=信号日收盘价）。
    未持满且未触发时 exit 字段保持 NULL，由后续交易日继续评估。"""
    rid = row["id"]
    code = row["code"]
    scan_date = row["scan_date"]
    entry = row["entry_price"]
    stop = row["stop_loss"]
    tp = row["take_profit"]

    from config.strategy_params import get_param
    from strategy.exit_advisor import get_max_hold
    trail_pct = get_param("short_trailing_pct")
    max_hold = get_max_hold("short") or 10

    prices = conn.execute("""
        SELECT trade_date, open, close, high, low
        FROM daily_price
        WHERE code = ? AND trade_date > ?
        ORDER BY trade_date ASC
        LIMIT 15
    """, (code, scan_date)).fetchall()

    if not prices:
        return False

    # 出场模拟 entry：次日开盘（与出场跟踪同口径），T+N 收益仍用信号日收盘 entry
    first = prices[0]
    exec_entry = float(first["open"]) if first["open"] else (
        float(first["close"]) or entry)

    # 启动线：参数比例优先（新口径，基于 exec_entry），回退推荐自带止盈价（旧信号兼容）
    launch_price = None
    launch_ratio = get_param("short_take_profit")
    if launch_ratio and 0 < launch_ratio < 1.0 and exec_entry > 0:
        launch_price = exec_entry * (1 + launch_ratio)
    elif tp and tp > 0:
        launch_price = tp
    # 止损价：推荐自带优先，缺失回退参数比例（基于 exec_entry）
    if (stop is None or stop <= 0) and exec_entry > 0:
        stop = exec_entry * (1 + float(get_param("short_stop_loss")))

    def _ret(n):
        if len(prices) >= n:
            c = prices[n - 1]["close"]
            if c and entry > 0:
                return round((c - entry) / entry * 100, 2)
        return None

    t1 = _ret(1)
    t2 = _ret(2)
    t3 = _ret(3)
    t5 = _ret(5)
    t10 = _ret(10)

    max_ret = None
    min_ret = None
    for p in prices:
        if p["high"] and entry > 0:
            r = (p["high"] - entry) / entry * 100
            max_ret = r if max_ret is None else max(max_ret, r)
        if p["low"] and entry > 0:
            r = (p["low"] - entry) / entry * 100
            min_ret = r if min_ret is None else min(min_ret, r)

    if max_ret is not None:
        max_ret = round(max_ret, 2)
    if min_ret is not None:
        min_ret = round(min_ret, 2)

    hit_stop = 0
    launched = 0
    exit_reason = None
    exit_date = None
    exit_return = None

    # 移动止盈状态机（收盘判定口径）：启动线用盘中 high 触发，卖出用收盘价判定；
    # 止损优先于移动止盈线判定（与回测 simulate/出场跟踪一致）
    highest = None
    tline = None
    trail_ok = trail_pct is not None and 0.01 <= trail_pct < 1.0
    for j, p in enumerate(prices[:max_hold], start=1):
        close = p["close"]
        if not close or close <= 0:
            break
        high = p["high"] or close
        if highest is None or high > highest:
            highest = high
        if not launched and launch_price and highest >= launch_price:
            launched = 1
        if launched and trail_ok:
            line = highest * (1 - trail_pct)
            if tline is None or line > tline:
                tline = line
        # T+1：买入日（j=1）只累计持仓/状态，不检查出场（与出场跟踪一致）
        if j == 1:
            continue
        if stop and close <= stop:
            hit_stop = 1
            exit_reason = "stop_loss"
            exit_date = p["trade_date"]
            exit_return = round((close - exec_entry) / exec_entry * 100, 2)
            break
        if launched and tline is not None and close <= tline:
            exit_reason = "trailing_stop"
            exit_date = p["trade_date"]
            exit_return = round((close - exec_entry) / exec_entry * 100, 2)
            break
        if j == max_hold:
            exit_reason = "max_hold_days"
            exit_date = p["trade_date"]
            exit_return = round((close - exec_entry) / exec_entry * 100, 2)

    conn.execute("""
        UPDATE recommend_outcome
        SET t1_return = ?, t2_return = ?, t3_return = ?, t5_return = ?, t10_return = ?,
            max_return = ?, min_return = ?,
            hit_stop = ?, hit_tp = ?,
            exit_reason = ?, exit_date = ?, exit_return = ?,
            evaluated_at = ?
        WHERE id = ?
    """, (t1, t2, t3, t5, t10, max_ret, min_ret,
          hit_stop, launched, exit_reason, exit_date, exit_return,
          now_str, rid))
    return True


def _evaluate_mid_long(conn, row, horizon: str, now_str: str) -> bool:
    """中/长线评估：用真实出场纪律（移动止盈 + 周期持仓上限）逐日模拟出场。

    与 routes/investor.py 的出场跟踪同口径（evaluate_exit_by_prices + 周期
    trailing/partial 参数 + get_max_hold）。窗口按周期拉长；未出场时
    exit_reason/exit_date/exit_return 保持 NULL，供后续交易日继续评估。
    """
    import pandas as pd
    from strategy.exit_advisor import evaluate_exit_by_prices, get_max_hold
    from config.strategy_params import get_param

    rid = row["id"]
    code = row["code"]
    scan_date = row["scan_date"]
    entry = row["entry_price"]
    stop = row["stop_loss"]
    tp = row["take_profit"]

    # 长线 60+ 天持仓 → ~130 交易日窗口；中线 10~60 天 → ~70 交易日窗口
    limit = 130 if horizon == "long" else 70
    prices = conn.execute("""
        SELECT trade_date, open, close, high, low
        FROM daily_price
        WHERE code = ? AND trade_date > ?
        ORDER BY trade_date ASC
        LIMIT ?
    """, (code, scan_date, limit)).fetchall()

    if not prices:
        return False

    def _ret(n):
        if len(prices) >= n:
            c = prices[n - 1]["close"]
            if c and entry > 0:
                return round((c - entry) / entry * 100, 2)
        return None

    t1 = _ret(1)
    t2 = _ret(2)
    t3 = _ret(3)
    t5 = _ret(5)
    t10 = _ret(10)

    max_ret = None
    min_ret = None
    for p in prices:
        if p["high"] and entry > 0:
            r = (p["high"] - entry) / entry * 100
            max_ret = r if max_ret is None else max(max_ret, r)
        if p["low"] and entry > 0:
            r = (p["low"] - entry) / entry * 100
            min_ret = r if min_ret is None else min(min_ret, r)
    if max_ret is not None:
        max_ret = round(max_ret, 2)
    if min_ret is not None:
        min_ret = round(min_ret, 2)

    # 逐日模拟出场（与 routes/investor.py::_get_exit_advice 同口径）
    df = pd.DataFrame([dict(p) for p in prices]).set_index("trade_date")
    trailing_pct = get_param("long_trailing_pct" if horizon == "long" else "mid_trailing_pct")
    partial_tp = get_param("long_partial_tp" if horizon == "long" else "mid_partial_tp")
    # 止损宽度上限：仅中线叠加（与出场跟踪同口径），short/long 传 None
    stop_cap_pct = get_param("mid_stop_max_width") if horizon == "mid" else None
    adv = evaluate_exit_by_prices(
        entry_price=entry,
        entry_date=scan_date,
        df=df,
        stop_loss=stop,
        take_profit=tp,
        max_hold_days=get_max_hold(horizon),
        trailing_pct=trailing_pct,
        partial_tp=partial_tp,
        stop_cap_pct=stop_cap_pct,
    )
    detail = adv.get("detail") or {}
    exit_reason = detail.get("exit_reason")
    exit_date = detail.get("exit_date")
    exit_return = None
    hit_stop = 0
    hit_tp = 0
    if adv.get("status") == "clear" and exit_date:
        exit_price = detail.get("exit_price")
        if exit_price and entry > 0:
            exit_return = round((exit_price - entry) / entry * 100, 2)
        if exit_reason and "止损" in str(exit_reason):
            hit_stop = 1
        elif exit_reason and "止盈" in str(exit_reason):
            hit_tp = 1
    # 未出场：exit_return/exit_date/exit_reason 保持 NULL，等待后续交易日继续评估

    conn.execute("""
        UPDATE recommend_outcome
        SET t1_return = ?, t2_return = ?, t3_return = ?, t5_return = ?, t10_return = ?,
            max_return = ?, min_return = ?,
            hit_stop = ?, hit_tp = ?,
            exit_reason = ?, exit_date = ?, exit_return = ?,
            evaluated_at = ?
        WHERE id = ?
    """, (t1, t2, t3, t5, t10, max_ret, min_ret,
          hit_stop, hit_tp, exit_reason, exit_date, exit_return,
          now_str, rid))
    return True


# ─────────────────────────────────────────────
# 3. 汇总统计
# ─────────────────────────────────────────────

def get_summary(days: int = 30) -> dict:
    """返回近 N 日推荐的胜率/平均收益/盈亏比等汇总。

    以 t5_return 作为主要评判标准（短线 5 个交易日）。
    若 t5_return 为空则用 t3 或 t1 代替。
    """
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT code, scan_date, horizon, strategy, entry_price,
                   fusion_score, t1_return, t3_return, t5_return, t10_return,
                   max_return, min_return, hit_stop, hit_tp,
                   exit_reason, exit_return
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?)
              AND entry_price > 0
            ORDER BY scan_date DESC
        """, (cutoff,)).fetchall()

    if not rows:
        return {"total": 0, "win_rate": 0, "avg_return": 0,
                "profit_factor": 0, "best_return": 0, "worst_return": 0,
                "stop_loss_count": 0, "take_profit_count": 0}

    # 用 exit_return 作为最终收益（若有），否则用 t5 > t3 > t1
    returns = []
    for r in rows:
        ret = r["exit_return"]
        if ret is None:
            ret = r["t5_return"]
        if ret is None:
            ret = r["t3_return"]
        if ret is None:
            ret = r["t1_return"]
        if ret is not None:
            returns.append(ret)

    if not returns:
        return {"total": len(rows), "win_rate": 0, "avg_return": 0,
                "profit_factor": 0, "best_return": 0, "worst_return": 0,
                "stop_loss_count": 0, "take_profit_count": 0}

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_return = sum(returns) / len(returns)
    win_rate = len(wins) / len(returns) * 100

    # 盈亏比 = 平均盈利 / 平均亏损绝对值
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else 99.0

    stop_count = sum(1 for r in rows if r["hit_stop"])
    tp_count = sum(1 for r in rows if r["hit_tp"])

    return {
        "total": len(returns),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 2),
        "profit_factor": profit_factor,
        "best_return": round(max(returns), 2),
        "worst_return": round(min(returns), 2),
        "stop_loss_count": stop_count,
        "take_profit_count": tp_count,
    }


# ─────────────────────────────────────────────
# 4. 获取明细列表（供 API 使用）
# ─────────────────────────────────────────────

def get_outcome_list(days: int = 30, limit: int = 200) -> list:
    """返回近 N 日推荐结果明细列表（JOIN stock_info 补股名）。"""
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT o.code, si.name AS name, o.scan_date, o.horizon, o.strategy, o.entry_price,
                   o.stop_loss, o.take_profit, o.fusion_score,
                   o.t1_return, o.t2_return, o.t3_return, o.t5_return, o.t10_return,
                   o.max_return, o.min_return, o.hit_stop, o.hit_tp,
                   o.exit_reason, o.exit_date, o.exit_return, o.evaluated_at
            FROM recommend_outcome o
            LEFT JOIN stock_info si ON si.code = o.code
            WHERE o.scan_date >= date('now', ?)
              AND o.entry_price > 0
            ORDER BY o.scan_date DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 5. 一键运行入口
# ─────────────────────────────────────────────

def run():
    """完整执行：导入新推荐 + 评估结果。"""
    t0 = time.time()
    insert_new_outcomes()
    updated = evaluate_outcomes()
    elapsed = time.time() - t0
    print(f"[outcome_tracker] 评估完成: {updated} 条更新，耗时 {elapsed:.1f}s")
    return updated


# ─────────────────────────────────────────────
# 6. 连续推荐合并（推荐复盘口径）
# ─────────────────────────────────────────────

def _is_consecutive_trade_days(conn, code: str, prev_date: str, cur_date: str) -> bool:
    """判断 cur_date 是否恰好是 prev_date 的下一个交易日（中间无跳空）。"""
    nxt = conn.execute(
        "SELECT trade_date FROM daily_price WHERE code = ? AND trade_date > ? "
        "ORDER BY trade_date ASC LIMIT 1",
        (code, prev_date),
    ).fetchone()
    return nxt is not None and nxt["trade_date"] == cur_date


def _merge_continuous_segments(rows: list, conn) -> list:
    """把按 (code, scan_date) 升序的推荐记录合并为「连续推荐段」。

    - 同一股票同一天多条记录只保留一条；
    - 推荐日之间若跳过了交易日（中间有交易日但未被推荐），视为中断，
      中断后再次推荐则另起一段；
    - 每段只保留一条记录，字段以首次推荐日为准，附加连续推荐天数。
    """
    by_code = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)

    merged = []
    for code, recs in by_code.items():
        # 同日去重（保留首条）
        seen = {}
        for r in recs:
            seen.setdefault(r["scan_date"], r)
        recs = [seen[d] for d in sorted(seen)]

        seg = [recs[0]]
        for r in recs[1:]:
            if _is_consecutive_trade_days(conn, code, seg[-1]["scan_date"], r["scan_date"]):
                seg.append(r)
            else:
                merged.append(_segment_to_row(seg))
                seg = [r]
        merged.append(_segment_to_row(seg))

    return merged


def _segment_to_row(seg: list) -> dict:
    """把一段连续推荐压缩为一行：字段取首次推荐日，附加段信息。"""
    first = seg[0]
    row = dict(first)
    row["first_scan_date"] = first["scan_date"]
    row["last_scan_date"] = seg[-1]["scan_date"]
    row["streak_days"] = len(seg)
    return row


def get_merged_outcome_list(days: int = 30, horizon: str = "short",
                            limit: int = 500) -> list:
    """近 N 日连续推荐合并后的明细列表（短线口径，按首次推荐日降序）。"""
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT o.code, si.name AS name, o.scan_date, o.horizon, o.strategy, o.entry_price,
                   o.stop_loss, o.take_profit, o.fusion_score,
                   o.t1_return, o.t2_return, o.t3_return, o.t5_return, o.t10_return,
                   o.max_return, o.min_return, o.hit_stop, o.hit_tp,
                   o.exit_reason, o.exit_date, o.exit_return, o.evaluated_at
            FROM recommend_outcome o
            LEFT JOIN stock_info si ON si.code = o.code
            WHERE o.scan_date >= date('now', ?)
              AND o.entry_price > 0
              AND o.horizon = ?
            ORDER BY o.code ASC, o.scan_date ASC
            LIMIT ?
        """, (cutoff, horizon, limit)).fetchall()
        merged = _merge_continuous_segments(rows, conn)

    # 按首次推荐日降序（同日按最终收益降序）
    merged.sort(
        key=lambda x: (
            x["first_scan_date"],
            x["exit_return"] if x["exit_return"] is not None else -999,
        ),
        reverse=True,
    )
    return merged


def get_merged_summary(days: int = 30, horizon: str = "short") -> dict:
    """近 N 日连续推荐合并后的汇总：T1/T2/T3/T5 总胜率 + 原有收益统计。"""
    items = get_merged_outcome_list(days, horizon)

    def _win_stat(key):
        vals = [it[key] for it in items if it.get(key) is not None]
        if not vals:
            return {"n": 0, "win": 0, "win_rate": 0, "avg_return": 0}
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        return {"n": len(vals), "win": win,
                "win_rate": round(win / len(vals) * 100, 1),
                "avg_return": round(avg, 2)}

    # 最终收益口径：exit_return > t5 > t3 > t2 > t1
    returns = []
    for it in items:
        ret = it.get("exit_return")
        if ret is None:
            ret = it.get("t5_return")
        if ret is None:
            ret = it.get("t3_return")
        if ret is None:
            ret = it.get("t2_return")
        if ret is None:
            ret = it.get("t1_return")
        if ret is not None:
            returns.append(ret)

    base = {
        "total": len(items),
        "t1": _win_stat("t1_return"),
        "t2": _win_stat("t2_return"),
        "t3": _win_stat("t3_return"),
        "t5": _win_stat("t5_return"),
    }
    if not returns:
        return {**base, "win_rate": 0, "avg_return": 0, "profit_factor": 0,
                "best_return": 0, "worst_return": 0,
                "stop_loss_count": 0, "take_profit_count": 0}

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_return = sum(returns) / len(returns)
    win_rate = len(wins) / len(returns) * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else 99.0

    return {
        **base,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 2),
        "profit_factor": profit_factor,
        "best_return": round(max(returns), 2),
        "worst_return": round(min(returns), 2),
        "stop_loss_count": sum(1 for it in items if it.get("hit_stop")),
        "take_profit_count": sum(1 for it in items if it.get("hit_tp")),
    }
