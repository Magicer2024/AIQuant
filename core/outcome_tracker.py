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
    main_board_filter, EXIT_TRACK_START_DATE,
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
        market_table = "market_breadth" if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='market_breadth' AND type='table'"
        ).fetchone() else "daily_price"
        parts.append(f"(SELECT AVG(pct_change) FROM {market_table} "
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


def _short_order_keys(alias: str, chip_first: bool, direction: str) -> str:
    """短线排序键本体（供线上排序与影子组共用，避免两处口径漂移）。

    chip_first=True 时在**策略名额 CASE 之后、扩展度之前**插入筹码集中度键
    `COALESCE(chip_conc, 9.9) ASC` —— 位置是关键：conc 放 ext 之后就完全无效。
    NULL（历史行/计算失败）COALESCE 到 9.9 排最后，等价于该键不生效。
    """
    chip_key = f"COALESCE({alias}.chip_conc, 9.9) ASC, " if chip_first else ""
    return (f"CASE WHEN {alias}.strategy = '隔日动量' THEN 0 "
            f"WHEN {alias}.strategy = '缩量回踩' THEN 1 ELSE 2 END, "
            f"{chip_key}"
            f"COALESCE({alias}.pct_above_ma20, 0) {direction}, "
            f"COALESCE({alias}.fusion_score, 0) DESC")


def short_order_clause(alias: str = "s") -> str:
    """短线候选排序 SQL 子句：独立正期望信号线优先占名额，其余按扩展度排序。

    名额优先级：隔日动量(0) > 缩量回踩(1, 2026-08-22 落地，train/test 双正，
    见 strategy/pullback_dip.py) > 其余抄底票(2)。抄底票之间按扩展度排序，
    方向由 short_ext_sort_desc 控制（1=降序 / 0=升序，当前默认升序）。
    2026-08-22 曾依据 tools/eval_short_fusion_cap.py（候选缓存评估）短暂切换
    降序，同日被真实复盘口径重放（tools/eval_short_sort_replay.py，recon 窗
    desc -2.27% vs asc -0.29%）推翻并回退，详见参数注释。三处出口
    （今日推荐/出场跟踪/复盘入库）共用本函数保证口径一致。

    2026-09-16 追加：`short_chip_sort_prioritize=1` 时插入筹码集中度键
    （见 _short_order_keys）。依据与上线前置条件见
    config/strategy_params.py::short_chip_sort_prioritize 注释。
    """
    from config.strategy_params import get_param
    direction = "DESC" if int(get_param("short_ext_sort_desc")) else "ASC"
    chip_first = bool(int(get_param("short_chip_sort_prioritize") or 0))
    return _short_order_keys(alias, chip_first, direction)


def _short_weak_dates(conn, start_date: str, end_date: str | None = None) -> set:
    """计算 [start_date, end_date] 内的「市场走弱日」集合（宽松口径）。

    2026-08-26 v2 放宽（初版过紧：ANY 指数略破 MA20 即判弱，2 年窗口 73% 交易日
    被标为走弱，回测 2024-07~2026-08 显示其用 ~25% 收益换 2.6pp 回撤，代价过大）。
    宽松判定（任一命中即算弱，只抓「真走弱」而非震荡/恢复期的假弱）：
      1) 信号日 regime == cold（composite 多日宽度冰点，广度+斜率双差，历史可复现）；
      2) 上证指数或沪深300 当日收盘 < 其 MA20 × 0.98（显著跌破中期均线 >2%，趋势破坏）；
      3) 上证指数或沪深300 近 5 个交易日动量 < -1.5%（短线动能显著转负）。
    cool 单独不再触发（需叠加指数破位/动量转负），避免把「偏冷但对指数无碍」的日子
    也一刀切屏蔽；任一指数缺数据时跳过对应项（仅按 regime 判定，不误判为强）。
    """
    import pandas as pd
    from core.market_regime import compute_regime_series
    series = compute_regime_series(conn, start_date, end_date)
    weak = {d for d, r in series.items() if r == "cold"}
    end_filter = " AND trade_date <= ?" if end_date else ""
    for icode in ("000001", "000300"):
        rows = conn.execute(f"""
            SELECT trade_date, close FROM index_daily
            WHERE code = ? AND trade_date >= date(?, '-40 days'){end_filter}
            ORDER BY trade_date
        """, (icode, start_date, end_date) if end_date else (icode, start_date)).fetchall()
        if not rows:
            continue
        df = pd.DataFrame(
            [{"d": r["trade_date"], "c": float(r["close"])} for r in rows]).set_index("d")
        c = df["c"]
        ma20 = c.rolling(20).mean()
        mom5 = c / c.shift(5) - 1
        for d in sorted(series):
            if d not in c.index:
                continue
            cl = c.get(d)
            if cl is None:
                continue
            m = ma20.get(d)
            mo = mom5.get(d)
            if (m is not None and not pd.isna(m) and cl < m * 0.98) or \
               (mo is not None and not pd.isna(mo) and mo < -0.015):
                weak.add(d)
    return weak


def short_market_gate_sql(conn, start_date: str, end_date: str | None = None,
                          alias: str = "s") -> tuple[str, list]:
    """短线「大盘走弱闸门」SQL 片段（组合进 short 组 WHERE，三处出口共用）。

    2026-08-26 用户要求：市场下跌/走弱时不要盲推短线建仓。
    弱市日只保留独立正期望信号线「隔日动量」可占短线名额（抄底/回踩类一律不推），
    即 SQL = (strategy='隔日动量' OR scan_date NOT IN (弱市日))；强市日不过滤。
    与 short_t1_filter_sql 同构（返回 (sql_cond, params)），供「今日推荐 / 出场跟踪 /
    推荐复盘入库」三处共用，保证口径一致。参数里没有开关——弱市判定本身是硬规则，
    若要整体放量可用 all_dates 逻辑之外，此处默认仅在明确弱市日收缩。
    """
    weak = _short_weak_dates(conn, start_date, end_date)
    if not weak:
        return "1", []
    ph = ",".join("?" * len(weak))
    return (f"(COALESCE({alias}.strategy, '') = '隔日动量' "
            f"OR {alias}.scan_date NOT IN ({ph}))"), list(weak)


def short_observe_bottom_sql(alias: str = "s") -> tuple[str, list]:
    """抄底线「短线融合」的短线名额占用条件（2026-09-06 落地，三处出口共用）。

    short_observe_bottom=1 时不占短线名额：今日推荐 / 出场跟踪 / 复盘入库
    三处 short 组 WHERE 统一排除该策略（与「缩量回踩」停用同模式，但保留
    开关便于随时恢复观察）。返回 (sql_cond, params)。

    2026-09-10 收窄：原实现是「排除全部短线融合」，而短线信号 100% 是
    strategy='短线融合'，直接导致短线链路零产出（/api/investor/today short=0）。
    现按 `short_observe_bottom_min_ext`（pct_above_ma20 下限）只排除
    「扩展度不足的抄底票」，保留已站上 MA20 的启动票：
      - min_ext = 0  → 等价旧行为（排除全部），向后兼容
      - min_ext > 0  → (非短线融合) OR (扩展度达标)
    参数依据见 config/strategy_params.py 中该参数注释（3 年同口径回测）。
    """
    from config.strategy_params import get_param
    if not int(get_param("short_observe_bottom") or 0):
        return "1", []
    thr = float(get_param("short_observe_bottom_min_ext") or 0)
    if thr <= 0:
        return (f"COALESCE({alias}.strategy, '') != '短线融合'", [])
    return (f"(COALESCE({alias}.strategy, '') != '短线融合' "
            f"OR COALESCE({alias}.pct_above_ma20, 0) >= ?)", [thr])


# ─────────────────────────────────────────────
# 0b. 长线排序键（2026-09-19 落地，三处出口共用）
# ─────────────────────────────────────────────

def long_vol_sort_enabled() -> bool:
    """长线「波动收敛」排序是否启用（开关 long_vol_sort_enabled，默认 0）。"""
    from config.strategy_params import get_param
    return bool(int(get_param("long_vol_sort_enabled") or 0))


def long_sort_pool_mult() -> int:
    """两层排序第一层的池子倍数（参数 long_vol_sort_pool_mult，默认 5）。"""
    from config.strategy_params import get_param
    return max(1, int(get_param("long_vol_sort_pool_mult") or 5))


def long_lowvol_sort_enabled() -> bool:
    """长线**选股重设计**总开关（long_lowvol_sort_enabled，默认 1）。

    与 long_vol_sort_enabled 的区别见 config/strategy_params.py 注释：
    那个只换排序键（已证劣化、默认关）；本开关同时换**票池 + 排序键**，
    针对的是「票池负 alpha −0.92%/t=−7.30」这个更根本的问题。
    """
    from config.strategy_params import get_param
    return bool(int(get_param("long_lowvol_sort_enabled") or 0))


def long_pool_filter_sql(alias: str = "s") -> str:
    """长线票池过滤：**趋势 + 斜率 + 低波**三项必需，即 `(long_mask & 7) = 7`。

    返回一段可直接拼进 WHERE 的 SQL（开头带 AND）；开关关闭时返回空串。

    为什么必须同时收票池（而不只是换排序键）：原票池 score>=2.0 里最大的一块是
    mask=11（趋势+斜率+浅回撤但**高波动**，日均 165 只），它超额 −0.90%、t=−6.54，
    是负 alpha 的主体；只换排序键救不了。详见 reference/long-selection.md §7。
    """
    if not long_lowvol_sort_enabled():
        return ""
    return f" AND (COALESCE({alias}.long_mask, 0) & 7) = 7"


def long_order_clause(alias: str = "s") -> str:
    """长线候选排序**第一层**。

    优先级（2026-09-20 起）：
      1. long_lowvol_sort_enabled=1（默认）→ `long_rank_key ASC`
         long_rank_key = 0.75×pctile(vol60) + 0.25×pctile(dd250)，是**当日票池内**的
         横截面百分位加权，由 core/sync.py::recompute_long_rank_key 预计算写入。
         ⚠ 必须用预计算列：vol60/dd250 都是连续值，SQL 字典序会退化成单键；
         且百分位分母依赖票池，逐只扫描时算不出来。
      2. long_vol_sort_enabled=1 → `vol_ratio ASC`（旧两层方案，已证劣化、默认关）
      3. 否则回退 `fusion_score DESC`

    2026-09-19 落地依据（tools/_diag_long_rescore.py 重放 scan_long_term，
    2015-2026 共 62.3 万信号 / 2488 采样日 + tools/_diag_long_key_eval.py 压力测试）：

    **原排序键是结构性失效的**：scan_long_term 的 fusion_score = score/3*50，而 score
    只能取 2.0/2.5/3.0 ⇒ 排序键只有 3 个离散值。实测每日 long 票池中位 372 只、
    **与第 4 名同分的票平均 28.2 只** ⇒ `ORDER BY fusion_score DESC LIMIT 4` 等价于
    从 28 只满分票里按 rowid 随机抽 4 只，排序键零信息量。

    替换为两层结构（**必须两层，不能写成字典序**）：
      第一层 `vol_ratio ASC` 取 topN×pool_mult 的候选池
      第二层 `pct_above_ma20 ASC` 在池内取 topN（见 long_second_order_clause）
    ⚠ vol_ratio 与 pct_above_ma20 都是连续值，写成
      `ORDER BY vol_ratio ASC, pct_above_ma20 ASC` 会因几乎无并列而**退化成纯
      vol_ratio 单键** —— 而单键在 2018/2025/2026 分年为负（2026 −5.62%），
      是过拟合到 2016-2021 强市段。两层结构把 2026 修正为 +0.46%。
    """
    if long_lowvol_sort_enabled():
        # long_rank_key 为 NULL 表示不在票池内（或历史未回填）→ 排最后
        return (f"COALESCE({alias}.long_rank_key, 9.9) ASC, "
                f"COALESCE({alias}.fusion_score, 0) DESC")
    if long_vol_sort_enabled():
        return f"COALESCE({alias}.vol_ratio, 9.9) ASC"
    return f"COALESCE({alias}.fusion_score, 0) DESC"


def long_second_order_clause(alias: str = "s") -> str:
    """长线排序**第二层**：在波动收敛池内取「最不追高」的（扩展度升序）。

    仅当 long_vol_sort_enabled() 为真时使用；返回 None 表示无需第二层。

    ⚠ alias 传空串 ""：第二层作用在**内层子查询的结果**上，那一层没有表别名
    （`SELECT * FROM (...)`），再加 `s.` 前缀会报 "no such column: s.xxx"。
    第一层 long_order_clause 用的是真实表别名 s，两者别搞混。
    """
    # 2026-09-20：新方案（long_lowvol_sort_enabled）的排序已由 long_rank_key
    # 一个连续键完成，**不需要**第二层；且实测两层硬切池在 2026 会掉胜率
    # （48.9% vs 软加权 52.2%）。只有旧两层方案才需要第二层。
    if long_lowvol_sort_enabled() or not long_vol_sort_enabled():
        return None
    p = f"{alias}." if alias else ""
    return (f"COALESCE({p}pct_above_ma20, 0) ASC, "
            f"COALESCE({p}fusion_score, 0) DESC")


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
    # 板块限制：与 routes/investor.py 的今日推荐同口径（白名单主板沪 60x / 深 00x）
    board_filter = main_board_filter("s.code")
    # 同一约束的别名版：反转首日 INSERT 的历史回填分支别名为 h。
    # ⚠ 2026-09-24 修复：此前该分支漏了板块过滤 ⇒ 跟踪组混入创业板（未开通权限、
    #   不可交易），且把「创业板贡献绝大部分收益」的失真读数写进了复盘表头
    #   （实测：创业板 n=466 胜率 58.9%/均 +2.38% vs 主板 n=485 胜率 ~49%/均 ~+0.93%）。
    board_filter_hist = main_board_filter("h.code")
    # short 组与今日推荐同口径：fusion 门控 + 低扩展度排序 + 止盈离场剔除
    from config.strategy_params import get_param, T1_GAP_GUARD, FIRST_REVERSAL
    gate = float(get_param("short_conf_gate"))
    # gap guard 止盈离场。
    # ⚠ 2026-09-19：历史链路默认禁用（参数 history_gap_guard 默认 0）。
    # 原逻辑用 latest_price.close（当前最新价）判定「现价相对信号价涨幅 > 止盈涨幅」，
    # 在回看历史信号时等于**用未来价格剔除历史样本**：任何"信号发出后涨过止盈价"
    # 的票都会在每次重算时被踢出，而它们正是赢家（实测名额内 short 被剔 8.9%，
    # 被剔票 T+5 +8.49%/胜率 90%，保留票 -0.26%/43.9%）。且该判定在实时语义下
    # 恒不触发（最新 scan_date 的 608 条信号中越过止盈价的 0 条）⇒ 净效果只有
    # 副作用。详见 config/strategy_params.py::history_gap_guard 注释。
    gap_enabled = (bool(T1_GAP_GUARD.get("enabled", True))
                   and bool(int(get_param("history_gap_guard") or 0)))
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
        # 大盘走弱闸门（2026-08-26）：弱市日只保留隔日动量，与今日推荐/出场跟踪同口径
        mk_cond, mk_params = short_market_gate_sql(conn, start_date)
        # 抄底融合线降观察（2026-09-06）：不占短线名额（与今日推荐/出场跟踪同口径）
        ob_cond, ob_params = short_observe_bottom_sql()
        horizon_specs = {
            # short：龙虎榜动量信号优先占名额，抄底票按扩展度排序补足
            # （方向由 short_ext_sort_desc 控制，见 short_order_clause；与今日推荐同口径）
            "short": (short_order_clause(),
                      f" AND s.fusion_score >= ? AND {t1_cond} AND {mk_cond} AND {ob_cond}",
                      [gate, *t1_params, *mk_params, *ob_params]),
            "mid":   ("COALESCE(s.fusion_score, 0) DESC", "", []),
            # long：2026-09-20 起改用「低波票池 + 连续排序键」。原 score>=2.0 票池
            # 长期超额 −0.92%(t=−7.30)、fusion DESC 只有 3 个离散值等价随机，
            # 见 long_pool_filter_sql / long_order_clause 注释。
            "long":  (long_order_clause(), long_pool_filter_sql(), []),
        }
        # 每日名额上限：short 取 short_top_n（2026-08-20 由 4 → 3），mid/long 保持前 4
        top_n = max(1, int(get_param("short_top_n")))
        caps = {"short": top_n, "mid": 4, "long": 4}
        # 反转首日组重建前基数：供本函数末尾的自检比对（见「窗口重建自检」）。
        # ⚠ 2026-09-24 事故：当晚 19:00 调度跑在**进程启动时加载的旧代码**上
        #   （有 sync 的接线、没有下面的反转首日 INSERT），DELETE 窗口把 424 行
        #   反转首日记录净删掉且无人补回（951→485→61），表头成绩单静默失真。
        #   护栏只能挡住「代码本身的回归」，挡不住「进程跑旧代码」——
        #   后者唯一的解法是**改完后端必须重启 start.bat**（调度器在 Flask 进程内）。
        _rev_before = conn.execute(
            "SELECT COUNT(*) FROM recommend_outcome WHERE strategy = '反转首日'"
        ).fetchone()[0]

        # 先清窗口内旧记录（随引擎/过滤链/推荐口径变化而更新，旧记录一并移除）
        conn.execute(
            "DELETE FROM recommend_outcome WHERE scan_date >= ?",
            (start_date,))
        for hz, (order_clause, gate_sql_cond, gate_params) in horizon_specs.items():
            # 注意：gap_sell 过滤必须发生在 ROW_NUMBER 之前（先剔除止盈离场、
            # 再按扩展度取前 caps[hz]），与今日推荐「先剔 sell 再截取」语义一致，
            # 被剔除的票不占排名、由后续候选替补。
            _base_where = f"""
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
                      -- 反转首日（2026-09-24）同为独立观察栏目线，不计入推荐复盘胜率
                      -- （实测超额 +0.174pp 但胜率 49.5% 未过门槛，勿混入短线成绩）
                      AND COALESCE(s.strategy, '') != '反转首日'
                      -- 缩量回踩 2026-08-26 停用（实证负期望），不计入推荐复盘胜率
                      AND COALESCE(s.strategy, '') != '缩量回踩'
                      AND ({gap_sql}) = 0
                      {board_filter}
                      {gate_sql_cond}"""
            # 第二层作用于第一层的派生表，无表别名 ⇒ 必须传空串（见函数注释）
            second = long_second_order_clause("") if hz == "long" else None
            if second:
                # 两层：先在「波动收敛」维度取 topN×pool_mult 的池，再在池内按
                # 扩展度升序取 topN。⚠ 不能合并成单层字典序（见 long_order_clause）
                _pool = caps[hz] * long_sort_pool_mult()
                conn.execute(f"""
                    INSERT OR IGNORE INTO recommend_outcome
                        (code, scan_date, horizon, strategy, entry_price,
                         stop_loss, take_profit, fusion_score)
                    SELECT code, scan_date, horizon, strategy, buy_price,
                           stop_loss, take_profit, fusion_score
                    FROM (
                        SELECT code, scan_date, horizon, strategy, buy_price,
                               stop_loss, take_profit, fusion_score,
                               ROW_NUMBER() OVER (
                                   PARTITION BY scan_date, horizon
                                   ORDER BY {second}
                               ) AS rn
                        FROM (
                            SELECT
                                s.code, s.scan_date,
                                COALESCE(s.horizon, 'short') AS horizon,
                                s.strategy, s.buy_price, s.stop_loss,
                                s.take_profit, s.fusion_score,
                                s.pct_above_ma20,
                                ROW_NUMBER() OVER (
                                    PARTITION BY s.scan_date, COALESCE(s.horizon, 'short')
                                    ORDER BY {order_clause}
                                ) AS rn_pool
                            {_base_where}
                        )
                        WHERE rn_pool <= ?
                    )
                    WHERE rn <= ?
                """, (start_date, hz, *gate_params, _pool, caps[hz]))
            else:
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
                        {_base_where}
                    )
                    WHERE rn <= ?
                """, (start_date, hz, *gate_params, caps[hz]))

        # ── 反转首日：纳入推荐复盘（2026-09-24，独立成组）─────────────────
        # 用户要求把「反转首日」观察池纳入复盘查看。设计要点：
        #   1) **单独 INSERT，不参与上面 short 的 ROW_NUMBER 竞争** ⇒ 不占 Top-N 名额、
        #      不挤掉真实推荐（否则 ~29/日 的观察池会顶掉 3/日 的推荐线）。
        #   2) 数据源 = 实时 stock_signal（当日链路）∪ first_reversal_hist（历史回填表，
        #      由 tools/backfill_first_reversal.py 写入）。用独立小表而非回填 stock_signal：
        #      stock_signal 是 48 万行大表且有「按 scan_date 整日 DELETE」的重算路径，
        #      回填进去会被误删；独立表在窗口重建时同样被 UNION 取回，稳健。
        #   3) stock_signal 优先：hist 用 NOT EXISTS 反连接排除同 (code, scan_date)，
        #      保证当日链路权威、无重复行。
        # ⚠ 复盘表头/明细一律按 strategy 过滤（见 get_merged_summary 的 strategy 形参），
        #   观察池独立成组、不污染短线胜率。
        if FIRST_REVERSAL.get("track_enabled"):
            _tn = max(0, int(FIRST_REVERSAL.get("track_top_n") or 0))
            # 历史回填表按需建（幂等；无回填时天然为空，不影响当日链路）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS first_reversal_hist (
                    code TEXT NOT NULL, scan_date TEXT NOT NULL, name TEXT,
                    buy_price REAL, stop_loss REAL, take_profit REAL,
                    fusion_score REAL, ext_pct REAL, vol_ratio REAL, kdj_k REAL,
                    created_at TEXT,
                    PRIMARY KEY (code, scan_date))""")
            _src = f"""
                SELECT s.code, s.scan_date, s.buy_price, s.stop_loss, s.take_profit,
                       s.fusion_score, s.pct_above_ma20
                FROM stock_signal s
                WHERE s.scan_date >= ? AND s.strategy = '反转首日'
                  AND s.buy_price IS NOT NULL AND s.buy_price > 0
                  AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
                  {board_filter}
                UNION ALL
                SELECT h.code, h.scan_date, h.buy_price, h.stop_loss, h.take_profit,
                       h.fusion_score, NULL
                FROM first_reversal_hist h
                WHERE h.scan_date >= ?
                  AND h.buy_price IS NOT NULL AND h.buy_price > 0
                  AND COALESCE(h.name, '') NOT LIKE '%ST%'
                  AND COALESCE(h.name, '') NOT LIKE '%退%'
                  {board_filter_hist}
                  AND NOT EXISTS (
                      SELECT 1 FROM stock_signal s2
                      WHERE s2.code = h.code AND s2.scan_date = h.scan_date
                        AND s2.strategy = '反转首日')"""
            _inner = (f"SELECT *, ROW_NUMBER() OVER (PARTITION BY scan_date "
                      f"ORDER BY COALESCE(fusion_score, 0) DESC, "
                      f"COALESCE(pct_above_ma20, 999) ASC) AS rn "
                      f"FROM ({_src})") if _tn > 0 else _src
            _tail = "WHERE rn <= ?" if _tn > 0 else ""
            _params = (start_date, start_date, _tn) if _tn > 0 else (start_date, start_date)
            conn.execute(f"""
                INSERT OR IGNORE INTO recommend_outcome
                    (code, scan_date, horizon, strategy, entry_price,
                     stop_loss, take_profit, fusion_score)
                SELECT code, scan_date, 'short', '反转首日', buy_price,
                       stop_loss, take_profit, fusion_score
                FROM ({_inner})
                {_tail}
            """, _params)
            # ── 窗口重建自检（2026-09-24）───────────────────────────────
            # 该组不进 ROW_NUMBER 竞争、恒由 window_start 起的 hist ∪ live 重建
            # ⇒ 样本数只应随窗口推移「平移」，不该净减少。净减少说明某个再插入
            # 分支失效（源表被清空/板块过滤错/开关被关/列名漂移），必须冒泡到日志。
            _rev_after = conn.execute(
                "SELECT COUNT(*) FROM recommend_outcome WHERE strategy = '反转首日'"
            ).fetchone()[0]
            if _rev_after < _rev_before:
                print(f"[outcome_tracker] ⚠ 反转首日组窗口重建后净减少 "
                      f"{_rev_before} → {_rev_after} 行（window_start={start_date}）："
                      f"请检查 first_reversal_hist / stock_signal 的该线数据与 "
                      f"FIRST_REVERSAL['track_enabled']；若本次是进程跑旧代码，"
                      f"重启 start.bat 后重跑可自动补回。")

        # ── 筹码优先「影子组」前向记录（2026-09-16，步骤③）──────────────
        # 与上面 short 组**完全相同的 WHERE**（融合门槛 + T1 辅助过滤 + 大盘走弱闸门
        # + 观察线排除 + gap guard + 板块过滤 + ST 剔除），只把 ORDER BY 换成
        # chip_conc 优先（其余键位与方向完全一致，见 _short_order_keys），取同样 top_n。
        # 两组群体天然对齐 ⇒ **唯一变量是排序键**，可做干净的前向对照。
        #
        # 为什么不做历史回填：26k 历史样本已被多轮挖掘，IS/OOS 切分不再干净；且筹码 Δ
        # 对样本期敏感（子样本 Δ +0.87%→+0.47%，成因是样本构成而非口径）。故改为前向。
        # ⚠ 本表不参与任何线上推荐 / 出场跟踪 / 复盘胜率，纯记录；0 = 一键停记。
        if int(get_param("short_chip_shadow_enabled") or 0):
            sh_direction = ("DESC" if int(get_param("short_ext_sort_desc"))
                            else "ASC")
            sh_order = _short_order_keys("s", True, sh_direction)
            # ⚠⚠ 必须显式取 horizon_specs["short"]，**不能**用 for 循环残留的
            # gate_sql_cond/gate_params —— 循环最后一个元素是 "long"，其 W H E R E
            # 片段是空串、params 是 []，影子组会变成"无融合门槛/T1 闸门/大盘门控/
            # 观察线排除"的全量排序，与基线组群体完全不对齐（实测该 bug 会让
            # 2026-09-15 的基线 0 条 vs 影子 3 条 —— 假对照）。
            sh_gate_cond, sh_gate_params = horizon_specs["short"][1], horizon_specs["short"][2]
            # ⚠⚠ 无条件先清掉「不可能是合法行」的残渣（2026-09-16 补）：
            # 旁路行的 chip_conc 直接抄自 stock_signal.chip_conc，而写入起点
            # ≥ chip_ready ⇒ **合法行的 chip_conc 必然非 NULL**。故 `chip_conc IS NULL`
            # 的旁路行必定是「实验前 / 调试期写入的残渣」—— 其 COALESCE(…,9.9) 全部
            # 同值，是**基线副本而非筹码组**。它们 scan_date < chip_ready，永远不会被
            # 下面的区间重建覆盖，若不清掉会**永久污染**对照样本、重叠度与胜率统计
            # （实测库中残留 4 行：2026-09-08~09-11，in_baseline 全为 1）。
            # 位置必须在 chip_ready 判定**之前** ⇒ 即使 chip_conc 尚未产生
            # （chip_ready=None、影子组暂不记录）也能自愈。
            # 对「合法但恰好 conc 计算失败」的 NULL 行无害：它们在本次 INSERT 会被重建，
            # 稳态不丢失（池内 conc 缺失率 5.74%，top_n=3 时几乎不可能入选）。
            conn.execute(
                "DELETE FROM recommend_outcome_shadow WHERE chip_conc IS NULL")
            # ⚠ 只在「已有 chip_conc 的日期」之后记录。历史行 chip_conc 全为 NULL
            # （步骤②不做回填），若照记，影子组会因 COALESCE(…,9.9) 全部同值而
            # **退化成基线的副本** —— 既灌垃圾行，又让重叠度显示 100%、把
            # "还没开始实验"误读成"改排序没效果"。故起点取 chip_conc 首次出现的日期。
            _cr = conn.execute(
                "SELECT MIN(scan_date) FROM stock_signal "
                "WHERE chip_conc IS NOT NULL "
                "AND COALESCE(horizon, 'short') = 'short'").fetchone()
            chip_ready = _cr[0] if _cr and _cr[0] else None
            if not chip_ready:
                print("[outcome_tracker] 筹码影子组：stock_signal.chip_conc 尚无数据"
                      "（等下一次 sync 写入后自动开始前向记录）")
            else:
                sh_start = max(start_date, chip_ready)
                conn.execute(
                    "DELETE FROM recommend_outcome_shadow WHERE scan_date >= ?",
                    (sh_start,))
                conn.execute(f"""
                    INSERT OR IGNORE INTO recommend_outcome_shadow
                        (code, scan_date, horizon, strategy, entry_price,
                         stop_loss, take_profit, fusion_score, chip_conc)
                    SELECT code, scan_date, horizon, strategy, buy_price,
                           stop_loss, take_profit, fusion_score, chip_conc
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
                            s.chip_conc,
                            ROW_NUMBER() OVER (
                                PARTITION BY s.scan_date, COALESCE(s.horizon, 'short')
                                ORDER BY {sh_order}
                            ) AS rn
                        FROM stock_signal s
                        LEFT JOIN latest_price lp ON lp.code = s.code
                        WHERE s.scan_date >= ?
                          AND COALESCE(s.horizon, 'short') = ?
                          AND s.buy_price IS NOT NULL
                          AND s.buy_price > 0
                          AND s.name NOT LIKE '%ST%'
                          AND s.name NOT LIKE '%退%'
                          AND COALESCE(s.strategy, '') != '强势突破'
                          AND COALESCE(s.strategy, '') != '反转首日'
                          AND COALESCE(s.strategy, '') != '缩量回踩'
                          AND ({gap_sql}) = 0
                          {board_filter}
                          {sh_gate_cond}
                    )
                    WHERE rn <= ?
                """, (sh_start, "short", *sh_gate_params, caps["short"]))
                # 标记与线上基线 top_n 的重叠。重叠度本身就是关键诊断量：
                # 若影子组与基线组高度重合，改排序能改变的只有那几只差集，
                # Δ 的噪声会被放大 —— 3~4 周后的对照报告必须先看这个数再看收益。
                conn.execute("""
                    UPDATE recommend_outcome_shadow
                    SET in_baseline = 1
                    WHERE COALESCE(horizon, 'short') = 'short'
                      AND EXISTS (
                          SELECT 1 FROM recommend_outcome r
                          WHERE r.code = recommend_outcome_shadow.code
                            AND r.scan_date = recommend_outcome_shadow.scan_date
                            AND COALESCE(r.horizon, 'short') = 'short'
                      )
                """)


# ─────────────────────────────────────────────
# 2. 评估未完成的推荐结果
# ─────────────────────────────────────────────

def evaluate_outcomes(table: str = "recommend_outcome"):
    """遍历 recommend_outcome 中尚未完全评估的记录，用 daily_price 填充收益。

    评估逻辑（horizon 感知）：
      - short：T+1/2/3/5/10 收益 + 移动止盈出场（2026-08-20 方案A：止损收盘判定、
        启动线激活后高点回撤清仓、持满 short_max_hold_days 了结，与出场跟踪同口径）。
      - mid/long：长周期策略——用 evaluate_exit_by_prices 按真实出场纪律
        （移动止盈 + 周期持仓上限）逐日模拟出场。持仓期远超短线的 10 天：
        中线窗口 ~70 交易日（max_hold=60）、长线窗口 ~130 交易日（max_hold=None
        不设强制出场上限）。未出场时 exit_return 保持 NULL，由后续交易日继续评估，
        避免「60+ 天策略被 10 天强制了结」的误判。

    :param table: 目标表名。默认 recommend_outcome（线上）。
        2026-09-16 加入形参以复用同一套出场数学评估筹码影子组
        （recommend_outcome_shadow）—— **不重写任何出场逻辑**，
        否则影子对照就成了"两套数学比大小"，无效。
    """
    if table not in ("recommend_outcome", "recommend_outcome_shadow"):
        raise ValueError(f"evaluate_outcomes: 非法表名 {table!r}")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        # short：沿用原触发条件（t10 未评估完）；mid/long：只要还没出场就持续评估
        rows = conn.execute(f"""
            SELECT id, code, scan_date, horizon, strategy, entry_price, stop_loss, take_profit
            FROM {table}
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
                if _evaluate_mid_long(conn, row, horizon, now_str, table):
                    updated += 1
            else:
                if _evaluate_short(conn, row, now_str, table):
                    updated += 1

    return updated


def _evaluate_short(conn, row, now_str: str,
                    table: str = "recommend_outcome") -> bool:
    """短线评估：T+N 收益 + 出场模拟（2026-09-05 起双入场口径）。

    :param table: 目标表（默认线上表）。影子组复用同一函数，保证出场数学一致。

    入场口径（按信号线分流）：
      - 抄底类（strategy != '隔日动量'，short_pullback_entry=1 时启用）——
        **回踩确认入场**（复刻 strategy/deep_tracker.py 已实证的入场纪律
        +1.32%/胜率61.5%）：T+1 起 short_entry_window_days 个交易日内，
        某日 low <= 买点（信号日收盘价 = entry_price）才成交，
        成交价 = min(买点, 当日开盘)；窗口内未回踩 → no_fill 放弃，
        不计入收益统计（汇总函数剔除）。根因：次日开盘无条件建仓 = 追反弹
        高点（T+1 上涨仅 47%≈抛硬币，止损单 4/21 从未浮盈过）。
      - 隔日动量 / 回踩关闭 —— 次日开盘建仓（旧口径，动量 alpha 依赖开盘建仓）。

    出场口径（两口径一致，与 routes/investor.py 出场跟踪同源）：
      - A股 T+1：建仓日只累计持仓/状态，不检查出场
      - 止损：收盘价跌破止损价（推荐自带，缺失回退 exec_entry×(1+short_stop_loss)）
      - 移动止盈：持仓最高价（盘中 high）首次达到启动线（short_take_profit 比例×
        exec_entry，回退推荐自带 take_profit 价）后启用；移动止盈线 = 最高价×
        (1-short_trailing_pct)，只上移不下移；收盘跌破 → trailing_stop 离场
      - 到期：持满 short_max_hold_days（自建仓日起）以收盘了结
    T+N 收益/max_return/min_return 口径不变（entry=信号日收盘价，信号质量诊断用）。

    ⚠ 终态判定：回踩口径下「成交+出场」最长需要 window+max_hold 个交易日，
    早于 t10（10 天）填满，故未终态时不写 evaluated_at（保持 NULL 触发重评估），
    否则「已评估未出场」的持仓会被永久卡住。"""
    rid = row["id"]
    code = row["code"]
    scan_date = row["scan_date"]
    entry = row["entry_price"]
    stop = row["stop_loss"]
    tp = row["take_profit"]
    strategy = row["strategy"] or ""

    from config.strategy_params import get_param
    from strategy.exit_advisor import get_max_hold
    trail_pct = get_param("short_trailing_pct")
    max_hold = get_max_hold("short") or 10

    # 回踩确认入场：仅抄底线（隔日动量豁免），可由参数整体关闭。
    # ⚠ 反转首日（2026-09-24）同样豁免：该线全部价值＝「首日/次日开盘」入场，
    #    套回踩口径等于把它变成另一条线（回踩不成交 → no_fill，样本全被剔除）。
    pullback = (
        bool(int(get_param("short_pullback_entry")))
        and strategy not in ("隔日动量", "反转首日")
    )
    entry_window = max(1, int(get_param("short_entry_window_days"))) if pullback else 0

    # 拉取窗口：诊断 15 天 + 回踩窗口 + max_hold 缓冲
    prices = conn.execute("""
        SELECT trade_date, open, close, high, low
        FROM daily_price
        WHERE code = ? AND trade_date > ?
        ORDER BY trade_date ASC
        LIMIT ?
    """, (code, scan_date, 15 + entry_window + max_hold)).fetchall()

    if not prices:
        return False

    # ── 诊断口径（不变）：T+N 收益 / max/min 基于信号日收盘 entry，前 15 个交易日 ──
    diag = prices[:15]

    def _ret(n):
        if len(diag) >= n:
            c = diag[n - 1]["close"]
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
    for p in diag:
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
    final = True          # 是否终态（决定是否写 evaluated_at）

    if pullback:
        # ── 回踩确认：前 entry_window 个交易日内 low 触及买点才成交 ──
        fill_idx = None
        exec_entry = None
        for idx in range(min(entry_window, len(prices))):
            p = prices[idx]
            low = p["low"]
            if low is None or entry is None or entry <= 0:
                continue
            if low <= entry:
                open_ = p["open"] if p["open"] else entry
                exec_entry = round(min(float(entry), float(open_)), 2)
                fill_idx = idx
                break
        if fill_idx is None:
            if len(prices) >= entry_window:
                # 窗口走完仍未回踩 → 放弃（no_fill，不计入收益统计）
                exit_reason = "no_fill"
            else:
                final = False   # 窗口未走完，等下个交易日继续判定
        else:
            # 成交后从建仓日起模拟出场（j=1 = 建仓日，T+1 不判出场）
            hold_prices = prices[fill_idx:fill_idx + max_hold]
            exit_reason, exit_date, exit_return, hit_stop, launched, done = \
                _short_exit_sim(hold_prices, exec_entry, stop, tp,
                                trail_pct, max_hold)
            if not done:
                final = False
    else:
        # ── 旧口径：次日开盘建仓 ──
        first = prices[0]
        exec_entry = float(first["open"]) if first["open"] else (
            float(first["close"]) or entry)
        exit_reason, exit_date, exit_return, hit_stop, launched, done = \
            _short_exit_sim(prices[:max_hold], exec_entry, stop, tp,
                            trail_pct, max_hold)
        if not done:
            final = False

    conn.execute(f"""
        UPDATE {table}
        SET t1_return = ?, t2_return = ?, t3_return = ?, t5_return = ?, t10_return = ?,
            max_return = ?, min_return = ?,
            hit_stop = ?, hit_tp = ?,
            exit_reason = ?, exit_date = ?, exit_return = ?,
            evaluated_at = ?
        WHERE id = ?
    """, (t1, t2, t3, t5, t10, max_ret, min_ret,
          hit_stop, launched, exit_reason, exit_date, exit_return,
          now_str if final else None, rid))
    return True


def _short_exit_sim(hold_prices, exec_entry: float, stop, tp,
                    trail_pct, max_hold: int, *,
                    launch_ratio=None, stop_loss_pct=None) -> tuple:
    """短线出场状态机（建仓日起逐日，收盘判定口径）。

    hold_prices[0] 必须是建仓日（j=1，T+1 只累计不判出场）。
    返回 (exit_reason, exit_date, exit_return, hit_stop, launched, done)：
    done=False 表示数据未走完且未触发出场（非终态，等后续交易日）。

    launch_ratio / stop_loss_pct 为可选的冻结参数注入点（方案 C2 适配器化）：
    传入时用注入值（模拟交易按运行冻结参数推进），为 None 时回退读取当前 get_param
    （保持 legacy evaluate_outcomes 调用方行为完全不变）。
    """
    launch_price = None
    from config.strategy_params import get_param
    if launch_ratio is None:
        launch_ratio = get_param("short_take_profit")
    if launch_ratio and 0 < launch_ratio < 1.0 and exec_entry > 0:
        launch_price = exec_entry * (1 + launch_ratio)
    elif tp and tp > 0:
        launch_price = tp
    # 止损价：推荐自带优先，缺失回退参数比例（基于 exec_entry）
    if (stop is None or stop <= 0) and exec_entry > 0:
        if stop_loss_pct is None:
            stop_loss_pct = get_param("short_stop_loss")
        stop = exec_entry * (1 + float(stop_loss_pct))

    trail_ok = trail_pct is not None and 0.01 <= trail_pct < 1.0
    highest = None
    tline = None
    launched = 0
    hit_stop = 0
    # 移动止盈状态机（收盘判定口径）：启动线用盘中 high 触发，卖出用收盘价判定；
    # 止损优先于移动止盈线判定（与回测 simulate/出场跟踪一致）
    for j, p in enumerate(hold_prices[:max_hold], start=1):
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
        # T+1：建仓日（j=1）只累计持仓/状态，不检查出场
        if j == 1:
            continue
        if stop and close <= stop:
            return ("stop_loss", p["trade_date"],
                    round((close - exec_entry) / exec_entry * 100, 2), 1, launched, True)
        if launched and tline is not None and close <= tline:
            return ("trailing_stop", p["trade_date"],
                    round((close - exec_entry) / exec_entry * 100, 2), 0, launched, True)
        if j == max_hold:
            return ("max_hold_days", p["trade_date"],
                    round((close - exec_entry) / exec_entry * 100, 2), 0, launched, True)
    # 数据走完仍未触发（非终态）或中途 break（close 缺失，保守视为非终态）
    return (None, None, None, 0, launched, False)


def _evaluate_mid_long(conn, row, horizon: str, now_str: str,
                       table: str = "recommend_outcome") -> bool:
    """中/长线评估：用真实出场纪律（移动止盈 + 周期持仓上限）逐日模拟出场。

    :param table: 目标表（默认线上表）。筹码影子组只含 short，故实际不会走到这里，
        加上形参只为与 evaluate_outcomes 的参数化保持对称、避免将来复用时踩坑。

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

    conn.execute(f"""
        UPDATE {table}
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

LEGACY_PRODUCTION_FILTER = "COALESCE(strategy, '') NOT IN ('反转首日', '强势突破', '缩量回踩')"


def settled_return(row):
    """旧表仅确认已出场且收益有效的记录；诊断收益绝不补齐成交收益。"""
    import math
    r = dict(row)
    if not r.get("exit_date") or not r.get("exit_reason") or r["exit_reason"] in {
        "no_fill", "expired", "holding", "watching", "data_pending", "cancelled"
    }:
        return None
    value = r.get("exit_return")
    return float(value) if value is not None and math.isfinite(float(value)) else None


def settlement_stats(items):
    """结算指标与未结算样本分开；没有亏损样本时比值保持未知。"""
    rows = [dict(r) for r in items]
    closed = [(r, settled_return(r)) for r in rows]
    closed = [(r, v) for r, v in closed if v is not None]
    values = [v for _, v in closed]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    loss_sum = abs(sum(losses))
    return {
        "settled_count": len(values),
        "unsettled_count": len(rows) - len(values),
        "win_count": len(wins), "loss_count": len(losses),
        "win_rate": round(len(wins) / len(values) * 100, 1) if values else None,
        "avg_return": round(sum(values) / len(values), 2) if values else None,
        "profit_factor": round(sum(wins) / loss_sum, 2) if loss_sum else None,
        "average_win_loss_ratio": round((sum(wins) / len(wins) if wins else 0) / (loss_sum / len(losses)), 2) if losses else None,
        "ratio_note": None if losses else "无亏损样本，盈亏比不可计算",
        "best_return": max(values) if values else None,
        "worst_return": min(values) if values else None,
        "stop_loss_count": sum(bool(r.get("hit_stop")) for r, _ in closed),
        "take_profit_count": sum(bool(r.get("hit_tp")) for r, _ in closed),
        "statistics_basis": "有效已出场记录；不使用 T+n 补齐；平均单笔收益非账户收益",
    }


def get_summary(days: int = 30) -> dict:
    """返回近 N 日已出场且收益有效的旧推荐统计；历史原值不重算。"""
    cutoff = f"-{days} days"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT code, scan_date, horizon, strategy, entry_price,
                   fusion_score, t1_return, t3_return, t5_return, t10_return,
                   max_return, min_return, hit_stop, hit_tp,
                   exit_reason, exit_date, exit_return
            FROM recommend_outcome
            WHERE scan_date >= date('now', ?)
              AND entry_price > 0
              -- 独立观察线「反转首日」不计入推荐线表头统计（2026-09-24）
              AND COALESCE(strategy, '') != '反转首日'
            ORDER BY scan_date DESC
        """, (cutoff,)).fetchall()

    stats = settlement_stats(rows)
    return {"total": stats["settled_count"], "signal_count": len(rows), **stats}


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
              -- 独立观察线「反转首日」不混入推荐线明细（2026-09-24）
              AND COALESCE(o.strategy, '') != '反转首日'
            ORDER BY o.scan_date DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 5. 一键运行入口
# ─────────────────────────────────────────────

def run():
    """完整执行：导入新推荐 + 评估结果（+ 筹码影子组，若开关开启）。"""
    t0 = time.time()
    insert_new_outcomes()
    updated = evaluate_outcomes()
    elapsed = time.time() - t0
    print(f"[outcome_tracker] 评估完成: {updated} 条更新，耗时 {elapsed:.1f}s")

    # ── 筹码优先影子组：用同一套出场数学评估（2026-09-16，步骤③）─────────
    # 放在线上评估**之后**并用 try/except 包住：影子链路任何异常都不得影响
    # 线上推荐/复盘。只读 recommend_outcome_shadow 自身，不触碰线上表。
    from config.strategy_params import get_param
    if int(get_param("short_chip_shadow_enabled") or 0):
        try:
            t1 = time.time()
            sh_updated = evaluate_outcomes("recommend_outcome_shadow")
            print(f"[outcome_tracker] 筹码影子组评估: {sh_updated} 条更新，"
                  f"耗时 {time.time() - t1:.1f}s")
        except Exception as e:
            print(f"[outcome_tracker] ⚠ 筹码影子组评估失败（不影响线上）: {e}")

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
                            limit: int = 500, strategy: str | None = None) -> list:
    """近 N 日连续推荐合并后的明细列表（短线口径，按首次推荐日降序）。

    :param strategy: None（默认）= **推荐线**（排除独立观察线「反转首日」，
        避免 ~29/日 的观察池淹没 3/日 的推荐线）；给定策略名则只取该策略
        （用于观察池在复盘里独立成组查看）。
    """
    cutoff = f"-{days} days"
    if strategy is None:
        strat_cond, strat_p = "AND COALESCE(o.strategy, '') != '反转首日'", []
    else:
        strat_cond, strat_p = "AND COALESCE(o.strategy, '') = ?", [strategy]
    with get_conn() as conn:
        rows = conn.execute(f"""
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
              -- 回踩确认入场（2026-09-05）：no_fill 未建仓不构成交易
              AND COALESCE(o.exit_reason, '') != 'no_fill'
              {strat_cond}
            ORDER BY o.code ASC, o.scan_date ASC
            LIMIT ?
        """, (cutoff, horizon, *strat_p, limit)).fetchall()
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


def get_merged_summary(days: int = 30, horizon: str = "short",
                       strategy: str | None = None, limit: int = 500,
                       items: list | None = None) -> dict:
    """近 N 日连续推荐合并后的汇总：T1/T2/T3/T5 总胜率 + 原有收益统计。

    :param strategy: 见 get_merged_outcome_list；None = 推荐线（排除反转首日）。
    :param limit: 明细拉取上限（合并前）。⚠ 观察池「反转首日」日均 ~20 只、
        窗口内可达 900+ 条，默认 500 会把样本截断 ⇒ 该组必须显式放大。
    :param items: 已由调用方取好的明细（get_merged_outcome_list 的返回值）。
        传入时**跳过内部再查一次**——调用方若同时要明细表与成绩单（如
        routes/investor.py::reversal_history），共用同一份明细才能保证
        两者 total/胜率**必然一致**（否则两处 limit/排序不同会算出不同结果，
        正是铁律 18 要防的跨链路口径漂移）。传了 items 就不要再用
        days/horizon/limit/strategy 的本意——它们只影响未传时的取数。
    """
    if items is None:
        items = get_merged_outcome_list(
            days, horizon, limit=limit, strategy=strategy)

    def _win_stat(key):
        vals = [it[key] for it in items if it.get(key) is not None]
        if not vals:
            return {"n": 0, "win": 0, "win_rate": 0, "avg_return": 0}
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        return {"n": len(vals), "win": win,
                "win_rate": round(win / len(vals) * 100, 1),
                "avg_return": round(avg, 2)}

    return {
        "total": len(items),
        "t1": _win_stat("t1_return"),
        "t2": _win_stat("t2_return"),
        "t3": _win_stat("t3_return"),
        "t5": _win_stat("t5_return"),
        "t10": _win_stat("t10_return"),
        **settlement_stats(items),
    }
