"""
core/services/signal_picks.py —— 首页独立「观察信号线」编排（T7 从 routes/investor.py 下沉）
=========================================================================================

四个观察栏目的取数 + 回放 + 成绩单组装（均非今日推荐名额内的买入线）：
  - 次日强势观察（strategy='强势突破'，隔日了结回放）
  - 反转首日观察（first_reversal，观察池非买入推荐；实时 stock_signal ∪ 历史回填表）
  - 反转首日历史（直接读 recommend_outcome，与实盘跟踪同源，不另造回放口径）
  - 龙虎榜机构席位建仓跟踪（成本位/资金动向参考，非收益信号）

路由只做参数解析与 ok/fail；各 build_*_payload 返回纯 dict。
"""
from __future__ import annotations

import json
import re
from collections import Counter

from config.personal_config import main_board_filter
from config.strategy_params import SURGE_BREAKOUT, FIRST_REVERSAL
from core.db import get_conn
from utils.api import identity_meta
from utils.serialization import sanitize_numeric


# ── 反转首日 · 出场原因 →（中文标签, 语义色调）────────────────────────────
# 色调只表达「这笔交易的结果性质」，由前端映射成**本项目红涨绿跌**的配色：
#   positive = 红（止盈类，赚了）/ negative = 绿（止损，亏了）
#   neutral  = 灰（到期了结，无所谓好坏）/ holding = 金（尚在持仓，未结算）
# ⚠ 不要抄 `recommendations/history` 前端那套 —— 那里用 var(--green) 画「已止盈」，
#   与本项目 A 股约定（红涨绿跌）相反，属历史遗留；本栏目一律用语义色。
_REV_EXIT_META = {
    "take_profit":   ("止盈离场", "positive"),
    "trailing_stop": ("移动止盈离场", "positive"),
    "stop_loss":     ("止损离场", "negative"),
    "max_hold_days": ("到期了结", "neutral"),
    "no_fill":       ("未建仓", "neutral"),
}
# 出场原因固定展示顺序（分布条用；未列出的原因排在末尾）
_REV_EXIT_ORDER = ["take_profit", "trailing_stop", "stop_loss",
                   "max_hold_days", "no_fill"]


# ─────────────────────────────────────────────
# 次日强势观察
# ─────────────────────────────────────────────
def surge_history(conn, before_scan_date, board_filter, days):
    """次日强势观察 · 历史表现：过去 days 天内全部强势突破信号，按现实口径逐条回放。

    呈现格式与「推荐复盘」一致：T+1~T+5 多期持有漂移 + 结算成绩单。
    入场 = 信号日次日开盘价（信号收盘后才产生，只能 T+1 开盘买）；
    出场 = **隔日了结**（`SURGE_BREAKOUT["max_hold_days"]` = 1）：
      次日（T+1）收盘为最终出场基准 —— 收盘 ≤ 止损 → 止损、收盘 ≥ 止盈 → 止盈、
      否则收盘了结。`judge_entry_day` 开启时建仓当日即纳入判定（跳空破位的票当天就出）。
    次日开盘已高开越过止盈 → 放弃追高（与卡片「错过不追」同口径，不计盈亏）。
    T+n = 买入后第 n 个交易日收盘相对买入价的涨幅**（与出场规则无关的持有漂移对照，
    用于观察「拿更久是更好还是更差」；放弃/待买入为 None，不进统计）**。

    2026-09-22 由「5 日持有 + 建仓当日不判出场」改为隔日了结（见 reference/surge-observation.md §7）。
    全族 544 日 n=1695 实测：胜率 36.9% → 45.6%、均值 -0.19% → +0.09%、最差 -26.28% → -12.47%。

    返回 (history, history_summary)。
    """
    max_hold = max(1, int(SURGE_BREAKOUT.get("max_hold_days", 1)))
    judge_entry_day = bool(SURGE_BREAKOUT.get("judge_entry_day", True))
    drift_days = max(5, max_hold)      # T+1~T+5 持有漂移始终算满 5 期，便于对比
    # 不含当日信号日：当日那批在卡片区展示，避免同一信号双份计数
    upper = before_scan_date or "9999-12-31"
    hist_rows = conn.execute(
        f"""
        SELECT s.code, s.name, s.price AS signal_price,
               s.stop_loss, s.take_profit, s.scan_date, s.trade_date
        FROM stock_signal s
        WHERE s.strategy = '强势突破'
          AND s.scan_date < ?
          AND s.scan_date >= date('now', ?)
          AND s.buy_price IS NOT NULL
          AND s.name NOT LIKE '%ST%'
          AND s.name NOT LIKE '%退%'
          {board_filter}
        ORDER BY s.scan_date DESC, COALESCE(s.fusion_score, 0) DESC
        LIMIT 300
        """,
        (upper, f"-{days} days"),
    ).fetchall()

    history = []
    for r in hist_rows:
        d = dict(r)
        sig_day = d.get("trade_date") or d.get("scan_date")
        stop = d.get("stop_loss")
        tp = d.get("take_profit")
        h = {
            "code": d.get("code"), "name": d.get("name"),
            "signal_date": d.get("scan_date"), "signal_price": d.get("signal_price"),
            "stop_loss": stop, "take_profit": tp,
            "t1_return": None, "t2_return": None,
            "t3_return": None, "t4_return": None, "t5_return": None,
        }
        px = conn.execute(
            """SELECT trade_date, open, close, high, low FROM daily_price
               WHERE code = ? AND trade_date > ? ORDER BY trade_date ASC LIMIT ?""",
            (d["code"], sig_day, drift_days + 1),
        ).fetchall()
        if not px:
            h.update(status="pending", status_label="待买入",
                     entry_date=None, entry_price=None,
                     exit_date=None, exit_price=None, exit_reason=None,
                     hold_days=None, return_pct=None)
            history.append(h)
            continue
        entry_price = px[0]["open"]
        if not entry_price or entry_price <= 0:
            continue   # 停牌/数据缺口，无法回放也不该假装有结果
        h["entry_date"] = px[0]["trade_date"]
        h["entry_price"] = round(entry_price, 2)
        if stop and entry_price <= stop:
            # 次日开盘已跌破止损线（跳空破位）→ 买入即亏，现实不会买（开盘即可见，无前视）
            h.update(status="skipped", status_label="放弃·开盘破止损",
                     exit_date=None, exit_price=None, exit_reason=None,
                     hold_days=None, return_pct=None)
            history.append(h)
            continue
        if tp and entry_price > tp:
            # 高开直接越过止盈位 → 现实中不会追，放弃（不给 T+n：根本没持仓）
            h.update(status="skipped", status_label="放弃·高开越过止盈",
                     exit_date=None, exit_price=None, exit_reason=None,
                     hold_days=None, return_pct=None)
            history.append(h)
            continue
        # T+n：买入后第 n 个交易日收盘相对买入价的涨幅（px[0] 即买入当日 = T+1）
        # 与出场规则无关的「持有漂移」对照：用来观察拿更久是更好还是更差
        for n in range(1, drift_days + 1):
            close_n = px[n - 1]["close"] if len(px) >= n else None
            h[f"t{n}_return"] = (round((close_n - entry_price) / entry_price * 100, 2)
                                 if close_n and close_n > 0 else None)
        exit_date = exit_price = exit_reason = None
        exit_j = None
        for j, p in enumerate(px[:max_hold], start=1):
            close = p["close"]
            if not close or close <= 0:
                break
            if j == 1 and not judge_entry_day:
                continue   # 建仓当日不判出场（多日持仓模式；隔日了结时此分支不生效）
            if stop and close <= stop:
                exit_date, exit_price, exit_reason, exit_j = p["trade_date"], close, "stop_loss", j
                break
            if tp and close >= tp:
                exit_date, exit_price, exit_reason, exit_j = p["trade_date"], close, "take_profit", j
                break
            if j >= max_hold:
                exit_date, exit_price, exit_reason, exit_j = p["trade_date"], close, "max_hold_days", j
                break
        if exit_reason:
            h.update(status="clear",
                     status_label={"stop_loss": "止损", "take_profit": "止盈",
                                   "max_hold_days": ("次日了结" if max_hold == 1 else "到期了结")}[exit_reason],
                     exit_date=exit_date, exit_price=round(exit_price, 2),
                     exit_reason=exit_reason,
                     hold_days=exit_j,
                     return_pct=round((exit_price - entry_price) / entry_price * 100, 2))
        else:
            last = px[min(len(px), max_hold) - 1]
            h.update(status="hold", status_label="持有中",
                     exit_date=None, exit_price=None, exit_reason=None,
                     hold_days=min(len(px), max_hold),
                     return_pct=round((last["close"] - entry_price) / entry_price * 100, 2),
                     last_close=round(last["close"], 2))
        history.append(h)

    # —— 成绩单：仅已结算进胜率/均值（持有中、放弃不算成绩）——
    # ⚠ 不提供「单笔收益率累加」：百分比直接相加无资金含义，且与平均值重复表达同一批数据。
    closed = [x for x in history if x["status"] == "clear"]
    wins = [x for x in closed if (x["return_pct"] or 0) > 0]
    losses = [x for x in closed if (x["return_pct"] or 0) <= 0]
    gain = sum(x["return_pct"] for x in wins)
    absloss = abs(sum(x["return_pct"] for x in losses))

    def _period(key):
        vals = [x[key] for x in history if x.get(key) is not None]
        if not vals:
            return {"n": 0, "win": 0, "win_rate": None, "avg_return": None}
        w = sum(1 for v in vals if v > 0)
        return {"n": len(vals), "win": w,
                "win_rate": round(w / len(vals) * 100, 1),
                "avg_return": round(sum(vals) / len(vals), 2)}

    history_summary = {
        "total": len(history),
        "closed": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_return": round(sum(x["return_pct"] for x in closed) / len(closed), 2) if closed else None,
        "profit_factor": round(gain / absloss, 2) if absloss > 0 else None,
        "best_return": round(max(x["return_pct"] for x in closed), 2) if closed else None,
        "worst_return": round(min(x["return_pct"] for x in closed), 2) if closed else None,
        "stop_loss_count": sum(1 for x in closed if x["exit_reason"] == "stop_loss"),
        "take_profit_count": sum(1 for x in closed if x["exit_reason"] == "take_profit"),
        "expire_count": sum(1 for x in closed if x["exit_reason"] == "max_hold_days"),
        "holding": sum(1 for x in history if x["status"] == "hold"),
        "skipped": sum(1 for x in history if x["status"] == "skipped"),
        "pending": sum(1 for x in history if x["status"] == "pending"),
        "t1": _period("t1_return"), "t2": _period("t2_return"),
        "t3": _period("t3_return"), "t4": _period("t4_return"),
        "t5": _period("t5_return"),
        "max_hold_days": max_hold,
        "judge_entry_day": judge_entry_day,
        "exit_mode_label": "隔日了结（T+1 收盘）" if max_hold == 1 else f"{max_hold} 日到期了结",
        "window_days": days,
    }
    return history, history_summary


_SURGE_NOTE = ("高风险博弈型信号（龙虎榜净买硬过滤）：验证窗次日大涨率 25%、"
               "触涨停 21.4%；次日开盘买入、严守止损，仓位从轻；无信号日属正常严格筛选")


def build_surge_picks_payload(*, limit: int, days: int, target_date) -> dict:
    """次日强势观察卡片 + 历史成绩单。limit/days 由路由完成边界裁剪。"""
    board_filter = main_board_filter("s.code")

    with get_conn() as conn:
        if target_date:
            scan_date = target_date
        else:
            row = conn.execute(
                "SELECT MAX(scan_date) AS d FROM stock_signal "
                "WHERE strategy = '强势突破'"
            ).fetchone()
            scan_date = row["d"] if row else None

        # 历史成绩单先算：它不依赖当日有无信号
        history, history_summary = surge_history(
            conn, scan_date, board_filter, days)

        if not scan_date or limit == 0:
            return {
                "date": scan_date, "count": 0, "items": [],
                "history": sanitize_numeric(history),
                "history_summary": history_summary,
                "days": days,
                "note": _SURGE_NOTE,
                "meta": identity_meta(cohort="observation", list_key="observe_surge",
                                      data_as_of=scan_date, conn=conn),
            }

        rows = conn.execute(
            f"""
            SELECT s.code, s.name, s.price AS signal_price,
                   s.buy_price, s.stop_loss, s.take_profit,
                   s.fusion_score, s.trigger_list, s.scan_date, s.trade_date,
                   -- 信号日当日涨幅（栏目卡片展示用）
                   (SELECT d.pct_change FROM daily_price d
                    WHERE d.code = s.code AND d.trade_date = s.trade_date) AS pct_day,
                   -- 最新收盘/日期（判断次日是否已开盘/错过）
                   (SELECT d.close FROM daily_price d
                    WHERE d.code = s.code ORDER BY d.trade_date DESC LIMIT 1) AS latest_close,
                   (SELECT MAX(d.trade_date) FROM daily_price d
                    WHERE d.code = s.code) AS latest_date,
                   i.industry
            FROM stock_signal s
            LEFT JOIN stock_info i ON i.code = s.code
            WHERE s.scan_date = ?
              AND s.strategy = '强势突破'
              AND s.buy_price IS NOT NULL
              AND s.name NOT LIKE '%ST%'
              AND s.name NOT LIKE '%退%'
              {board_filter}
            ORDER BY COALESCE(s.fusion_score, 0) DESC
            LIMIT ?
            """,
            (scan_date, limit),
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            entry = d.get("buy_price") or d.get("signal_price") or 0
            stop = d.get("stop_loss") or 0
            tp = d.get("take_profit") or 0
            risk_pct = round((entry - stop) / entry * 100, 1) if entry > 0 and stop > 0 else None
            reward_pct = round((tp - entry) / entry * 100, 1) if entry > 0 and tp > 0 else None
            risk_reward = round(reward_pct / risk_pct, 2) if risk_pct and reward_pct and risk_pct > 0 else None
            triggers = []
            tl = d.get("trigger_list")
            if tl:
                try:
                    parsed = json.loads(tl)
                    if isinstance(parsed, list):
                        triggers = [str(x) for x in parsed]
                except (json.JSONDecodeError, TypeError):
                    triggers = [s.strip() for s in str(tl).split(",") if s.strip()]
            # 错过判定：次日已收盘且收盘价越过止盈价（追高无意义）
            latest_close = d.get("latest_close")
            started = bool(d.get("latest_date") and d["latest_date"] > (d.get("trade_date") or ""))
            missed = bool(started and latest_close and tp and latest_close > tp)
            items.append({
                "code": d.get("code"),
                "name": d.get("name"),
                "industry": d.get("industry"),
                "fusion_score": round(d.get("fusion_score") or 0, 1),
                "pct_day": round(d["pct_day"], 2) if d.get("pct_day") is not None else None,
                "signal_price": entry,
                "stop_loss": stop,
                "take_profit": tp,
                "risk_pct": risk_pct,
                "reward_pct": reward_pct,
                "risk_reward_ratio": risk_reward,
                "latest_close": latest_close,
                "started": started,
                "missed": missed,
                "triggers": triggers,
                "scan_date": d.get("scan_date"),
                "trade_date": d.get("trade_date"),
            })

        # 历史成绩单已在上方 surge_history() 中构建完毕（推荐复盘格式：T+1~T+5 + 结算统计）
        return {
            "date": scan_date,
            "count": len(items),
            "items": sanitize_numeric(items),
            "history": sanitize_numeric(history),
            "history_summary": history_summary,
            "days": days,
            "note": _SURGE_NOTE,
            "meta": identity_meta(cohort="observation", list_key="observe_surge",
                                  data_as_of=scan_date, conn=conn),
        }


# ─────────────────────────────────────────────
# 反转首日观察
# ─────────────────────────────────────────────
def _parse_trigger_list(tl):
    """trigger_list 字符串/JSON → 文本列表（两栏目共用）。"""
    if not tl:
        return []
    try:
        parsed = json.loads(tl)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return [s.strip() for s in str(tl).split(",") if s.strip()]


def build_reversal_picks_payload(*, limit: int, target_date) -> dict:
    """反转首日观察卡片（实时 stock_signal ∪ first_reversal_hist）。"""
    # 板块过滤作用在 UNION 合并后的外层别名 r 上（实时/历史统一口径）
    board_filter = main_board_filter("r.code")

    _stats_note = ("观察池，非买入推荐：实测市场中性超额 +0.174pp(t=+2.80)、"
                   "分年 +0.23/+0.14/+0.18pp 三年一致，但胜率 49.5% 未达 52% 门槛，"
                   "扣成本后不足 0.1%/笔。本线唯一确定价值＝入场位低"
                   "（信号日中位偏离 MA20 +3.8%）且只响一次。")
    # 长期成绩单：不随当日有无信号消失（与强势突破的 history 同定位），两分支都返回
    _stats = {
        "n": 15841, "per_day": 29.1,
        "oc1_mean": 0.328, "win_rate": 49.5,
        "excess_pp": 0.174, "t": 2.80,
        "excess_by_year": {"2024": 0.23, "2025": 0.14, "2026": 0.18},
        "median_ext": 3.8,
    }
    # 实盘前向跟踪（2026-09-24 起纳入推荐复盘，独立成组）：把 recommend_outcome 中
    # strategy='反转首日' 的行按同一套出场数学评估后汇总。样本随交易日累积。
    try:
        from core.outcome_tracker import get_merged_summary
        # ⚠ limit 必须放大：该线日均 ~20 只，90 天窗可达 1500+ 条，默认 500 会截断样本
        _tracked = get_merged_summary(90, "short", strategy="反转首日", limit=5000)
    except Exception:
        _tracked = {"total": 0}

    # ── 数据源：实时 stock_signal（当日链路）∪ first_reversal_hist（历史回填）──────
    _rev_live = """
        SELECT s.code, s.name, s.scan_date, s.trade_date,
               s.price AS signal_price, s.buy_price, s.stop_loss, s.take_profit,
               s.fusion_score, s.trigger_list,
               -- ⚠ pct_above_ma20 是**分数**(0.051) 而 first_reversal_hist.ext_pct 是
               --   百分数(5.1) ⇒ 这里统一 *100，前端只认一个口径
               CASE WHEN s.pct_above_ma20 IS NOT NULL
                    THEN s.pct_above_ma20 * 100 END AS ext_pct,
               s.vol_ratio, NULL AS kdj_k, 0 AS hist_src
        FROM stock_signal s
        WHERE s.strategy = '反转首日'
    """
    _rev_hist = """
        SELECT h.code, h.name, h.scan_date, h.scan_date AS trade_date,
               NULL AS signal_price, h.buy_price, h.stop_loss, h.take_profit,
               h.fusion_score, NULL AS trigger_list,
               h.ext_pct, h.vol_ratio, h.kdj_k, 1 AS hist_src
        FROM first_reversal_hist h
        WHERE NOT EXISTS (
            SELECT 1 FROM stock_signal s2
            WHERE s2.code = h.code AND s2.scan_date = h.scan_date
              AND s2.strategy = '反转首日')
    """

    with get_conn() as conn:
        # 该表可能尚未创建（从未跑过回填脚本）⇒ 容错，仅用实时分支
        _has_hist = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='first_reversal_hist'").fetchone())
        _rev_src = (f"{_rev_live} UNION ALL {_rev_hist}" if _has_hist else _rev_live)

        if target_date:
            scan_date = target_date
        else:
            # 最新信号日 = 实时 ∪ 历史 的最大 scan_date
            row = conn.execute(
                f"SELECT MAX(scan_date) AS d FROM ({_rev_src})"
            ).fetchone()
            scan_date = row["d"] if row else None

        if not scan_date or limit == 0:
            return {
                "date": scan_date, "count": 0, "items": [],
                "enabled": bool(FIRST_REVERSAL.get("enabled")),
                "stats": _stats,
                "tracked": _tracked,
                "days": 0,
                "note": _stats_note + ("（信号线当前为关闭状态，"
                                       "上线需将 FIRST_REVERSAL['enabled'] 置 True）"
                                       if not FIRST_REVERSAL.get("enabled") else ""),
                "meta": identity_meta(cohort="observation",
                                      list_key="observe_first_reversal",
                                      data_as_of=scan_date, conn=conn),
            }

        rows = conn.execute(
            f"""
            SELECT r.*, i.industry,
                   -- 信号日当日涨幅：close/prev_close 权威口径。daily_price.pct_change
                   -- 有大量未写入的 0（铁律 19），个股价位用它会把涨幅显示成 0.00%
                   (SELECT d.close FROM daily_price d
                    WHERE d.code = r.code AND d.trade_date = r.trade_date) AS close_day,
                   (SELECT d.close FROM daily_price d
                    WHERE d.code = r.code AND d.trade_date < r.trade_date
                    ORDER BY d.trade_date DESC LIMIT 1) AS prev_close,
                   -- 信号日全市场平均涨幅（只用于「普涨日信号偏多」提示，非个股口径）
                   (SELECT AVG(d.pct_change) FROM daily_price d
                    WHERE d.trade_date = r.trade_date) AS mkt_pct_day,
                   -- 最新收盘/日期（判断次日是否已开盘/错过）
                   (SELECT d.close FROM daily_price d
                    WHERE d.code = r.code ORDER BY d.trade_date DESC LIMIT 1) AS latest_close,
                   (SELECT MAX(d.trade_date) FROM daily_price d
                    WHERE d.code = r.code) AS latest_date
            FROM ({_rev_src}) r
            LEFT JOIN stock_info i ON i.code = r.code
            WHERE r.scan_date = ?
              AND r.buy_price IS NOT NULL
              AND COALESCE(r.name, '') NOT LIKE '%ST%'
              AND COALESCE(r.name, '') NOT LIKE '%退%'
              {board_filter}
            -- 次排序按入场偏离升序（越低越是「首日」），与报告实测的「越低并不更优」
            -- 并不冲突——本栏目排序服务于「找最低入场位」，不服务于收益预测
            ORDER BY COALESCE(r.fusion_score, 0) DESC,
                     COALESCE(r.ext_pct, 999) ASC
            LIMIT ?
            """,
            (scan_date, limit),
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            entry = d.get("buy_price") or d.get("signal_price") or 0
            stop = d.get("stop_loss") or 0
            tp = d.get("take_profit") or 0
            risk_pct = round((entry - stop) / entry * 100, 1) if entry > 0 and stop > 0 else None
            reward_pct = round((tp - entry) / entry * 100, 1) if entry > 0 and tp > 0 else None
            risk_reward = (round(reward_pct / risk_pct, 2)
                           if risk_pct and reward_pct and risk_pct > 0 else None)
            triggers = _parse_trigger_list(d.get("trigger_list"))
            # 历史回填行没有 trigger_list（first_reversal_hist 不存文本）⇒ 用与
            # scan_first_reversal 同口径重建，避免历史卡片信息密度低于实时卡片
            if not triggers and d.get("hist_src"):
                _ext = d.get("ext_pct")
                if _ext is not None:
                    triggers.append(
                        f"反转首日：首次站上 MA20（偏离 {_ext:+.1f}%，入场位低）")
                _vr = d.get("vol_ratio")
                if _vr is not None:
                    triggers.append(f"量比 {_vr:.1f}（放量确认）")
                _kk = d.get("kdj_k")
                if _kk is not None:
                    _tag = "⚠ 高位" if _kk > 80 else ("低位" if _kk < 20 else "")
                    triggers.append(
                        f"KDJ K={_kk:.0f}" + (f" · {_tag}" if _tag else ""))
            # 信号日涨幅：close/prev_close 权威口径（勿用 pct_change，铁律 19）
            _cd, _pc = d.get("close_day"), d.get("prev_close")
            pct_day = (round((_cd / _pc - 1) * 100, 2)
                       if _cd and _pc and _pc > 0 else None)
            latest_close = d.get("latest_close")
            started = bool(d.get("latest_date") and
                           d["latest_date"] > (d.get("trade_date") or ""))
            # 错过判定：次日已收盘且收盘价越过止盈价（首日优势已消失）
            missed = bool(started and latest_close and tp and latest_close > tp)
            items.append({
                "code": d.get("code"),
                "name": d.get("name"),
                "industry": d.get("industry"),
                "fusion_score": round(d.get("fusion_score") or 0, 1),
                "pct_day": pct_day,
                "mkt_pct_day": (round(d["mkt_pct_day"], 2)
                                if d.get("mkt_pct_day") is not None else None),
                "ext_pct": (round(d["ext_pct"], 2)
                            if d.get("ext_pct") is not None else None),
                "vol_ratio": (round(d["vol_ratio"], 2)
                              if d.get("vol_ratio") is not None else None),
                "kdj_k": (round(d["kdj_k"], 1)
                          if d.get("kdj_k") is not None else None),
                "signal_price": entry,
                "stop_loss": stop,
                "take_profit": tp,
                "risk_pct": risk_pct,
                "reward_pct": reward_pct,
                "risk_reward_ratio": risk_reward,
                "latest_close": latest_close,
                "started": started,
                "missed": missed,
                "triggers": triggers,
                "scan_date": d.get("scan_date"),
                "trade_date": d.get("trade_date"),
                # 'live' = 当日链路产出；'hist' = 历史回填（面板据此提示来源）
                "source": "hist" if d.get("hist_src") else "live",
            })

        _src_kinds = {it["source"] for it in items}
        return {
            "date": scan_date,
            "count": len(items),
            "items": sanitize_numeric(items),
            "enabled": bool(FIRST_REVERSAL.get("enabled")),
            "source": (None if not _src_kinds
                       else ("hist" if _src_kinds == {"hist"}
                             else ("live" if _src_kinds == {"live"} else "mixed"))),
            "stats": _stats,
            "tracked": _tracked,
            "note": _stats_note,
            "meta": identity_meta(cohort="observation",
                                  list_key="observe_first_reversal",
                                  data_as_of=scan_date, conn=conn),
        }


# ─────────────────────────────────────────────
# 反转首日 · 历史明细（读复盘表，不另造回放）
# ─────────────────────────────────────────────
def build_reversal_history_payload(*, days: int, limit: int) -> dict:
    """近 N 天反转首日复盘明细 + 成绩单（直接读 recommend_outcome）。

    读取失败向上抛异常，由路由转 fail（保留与原接口一致的错误消息）。
    """
    from core.outcome_tracker import (get_merged_outcome_list,
                                      get_merged_summary)
    # 取数上限恒 5000：本线主板口径日均约 10 条，180 天窗最多 ~1800 条，
    # 5000 足够且不会截断（截断会静默改小样本 = 成绩单失真）。
    merged = get_merged_outcome_list(days, "short", limit=5000,
                                     strategy="反转首日")
    summary = get_merged_summary(days, "short", strategy="反转首日",
                                 limit=5000, items=merged)

    # ── 补 ext/kdj：复盘表不存这三列，得回信号源取 ─────────────────────
    # 先 hist 后 live ⇒ 同一 (code, scan_date) 以实时行为准（与
    # outcome_tracker.insert_new_outcomes 的 NOT EXISTS 反连接同口径）。
    meta = {}
    try:
        with get_conn() as conn:
            _has_hist = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='first_reversal_hist'").fetchone())
            if _has_hist:
                for r in conn.execute(
                        "SELECT code, scan_date, ext_pct, vol_ratio, kdj_k "
                        "FROM first_reversal_hist WHERE scan_date >= date('now', ?)",
                        (f"-{days} days",)):
                    meta[(r["code"], r["scan_date"])] = {
                        "ext_pct": r["ext_pct"], "vol_ratio": r["vol_ratio"],
                        "kdj_k": r["kdj_k"]}
            for r in conn.execute(
                    "SELECT code, scan_date, pct_above_ma20, vol_ratio, "
                    "       trigger_list FROM stock_signal "
                    "WHERE strategy = '反转首日' AND scan_date >= date('now', ?)",
                    (f"-{days} days",)):
                # stock_signal 没有 kdj_k 列、vol_ratio 也未写入 ⇒ 从 trigger 文本兜底
                _tl = r["trigger_list"] or ""
                _vr = r["vol_ratio"]
                if _vr is None:
                    _m = re.search(r"量比\s*([\d.]+)", _tl)
                    _vr = float(_m.group(1)) if _m else None
                _mk = re.search(r"KDJ\s*K\s*=\s*([\d.]+)", _tl)
                meta[(r["code"], r["scan_date"])] = {
                    "ext_pct": (r["pct_above_ma20"] * 100
                                if r["pct_above_ma20"] is not None else None),
                    "vol_ratio": _vr,
                    "kdj_k": float(_mk.group(1)) if _mk else None}
    except Exception:
        meta = {}   # 补充信息失败不得影响主列表

    def _r2(v):
        return round(v, 2) if v is not None else None

    items = []
    for it in merged[:limit]:
        code, sd = it.get("code"), it.get("scan_date")
        m = meta.get((code, sd), {})
        reason = it.get("exit_reason")
        if reason:
            label, tone = _REV_EXIT_META.get(reason, (reason, "neutral"))
        else:
            label, tone = "持仓中", "holding"
        items.append({
            "code": code,
            "name": it.get("name"),
            # first_scan_date 与 scan_date 对本线恒等（同票不会连两天首破 MA20），
            # 仍按复盘口径取 first_scan_date，便于与「推荐复盘」列表对齐
            "scan_date": it.get("first_scan_date") or sd,
            "entry_price": _r2(it.get("entry_price")),
            "stop_loss": _r2(it.get("stop_loss")),
            "take_profit": _r2(it.get("take_profit")),
            "ext_pct": _r2(m.get("ext_pct")),
            "vol_ratio": _r2(m.get("vol_ratio")),
            "kdj_k": _r2(m.get("kdj_k")),
            "fusion_score": round(it.get("fusion_score") or 0, 1),
            "t1_return": _r2(it.get("t1_return")),
            "t2_return": _r2(it.get("t2_return")),
            "t3_return": _r2(it.get("t3_return")),
            "t5_return": _r2(it.get("t5_return")),
            "exit_return": _r2(it.get("exit_return")),
            "exit_reason": reason,
            "exit_date": it.get("exit_date"),
            "exit_label": label,
            "exit_tone": tone,
            "hit_stop": bool(it.get("hit_stop")),
            "hit_tp": bool(it.get("hit_tp")),
            "settled": bool(it.get("exit_date") and reason and it.get("exit_return") is not None),
            "signal_reference_price": _r2(it.get("entry_price")),
            "reference_kind": "legacy_signal_reference",
            "exec_entry_price": None,
            "data_source": "legacy_snapshot",
        })

    # 出场原因分布。空 reason = 未结算（持仓中/待建仓），恒排最后。
    _cnt = Counter(it["exit_reason"] for it in items)   # 含 None 键
    breakdown = []
    for k in _REV_EXIT_ORDER + sorted(x for x in _cnt
                                      if x and x not in _REV_EXIT_ORDER):
        if _cnt.get(k):
            label, tone = _REV_EXIT_META.get(k, (k, "neutral"))
            breakdown.append({"reason": k, "label": label,
                              "tone": tone, "n": _cnt[k]})
    if _cnt.get(None):
        breakdown.append({"reason": None, "label": "持仓中/待建仓",
                          "tone": "holding", "n": _cnt[None]})

    _dates = [it["scan_date"] for it in items if it.get("scan_date")]
    return sanitize_numeric({
        "days": days,
        "limit": limit,
        "range": {"start": min(_dates) if _dates else None,
                  "end": max(_dates) if _dates else None},
        "total": len(items),
        "truncated": len(merged) > len(items),
        "summary": summary,
        "exit_breakdown": breakdown,
        "items": items,
        "note": ("反转首日观察池的历史明细（主板口径，与上方实盘跟踪同源同数学）："
                 "信号日次日开盘建仓 → 移动止盈/止损/到期。"
                 "本线不是买入推荐 —— 实测超额 +0.174pp(t=+2.80)、"
                 "胜率 49.5% 未达 52% 门槛、扣成本后不足 0.1%/笔。"),
    })


# ─────────────────────────────────────────────
# 龙虎榜机构席位建仓跟踪
# ─────────────────────────────────────────────
def build_lhb_jg_track_payload(*, days: int, min_cnt: int, limit: int,
                               sort_key: str, only_priced: bool) -> dict:
    """机构席位单日口径聚合：成本位估算 + 上榜区间 + 现价对比。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM stock_lhb_jg_detail").fetchone()
        as_of = row["d"] if row else None
        if not as_of:
            return {"as_of": None, "window_days": days, "count": 0, "items": [],
                    "note": "机构席位表暂无数据，先执行 tools/backfill_lhb_jg.py"}

        # 窗口起点 = 最新交易日往前 days-1 个交易日
        start = conn.execute(
            "SELECT MIN(trade_date) FROM (SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?)",
            (as_of, days)).fetchone()[0] or as_of

        # 一次 JOIN 取窗口内全部单日口径记录 + 当日前复权行情
        rows = conn.execute(
            """
            SELECT j.trade_date, j.code, j.name, j.jg_net_buy, j.jg_net_ratio,
                   j.buyer_jg_count, j.seller_jg_count, j.float_mkt_cap,
                   j.reason, j.turnover_rate,
                   d.close AS close, d.low AS low, d.high AS high,
                   d.pct_change AS pct
            FROM stock_lhb_jg_detail j
            LEFT JOIN daily_price d
                   ON d.code = j.code AND d.trade_date = j.trade_date
            WHERE j.window_days = 1 AND j.trade_date >= ? AND j.trade_date <= ?
            ORDER BY j.code, j.trade_date
            """, (start, as_of)).fetchall()

        latest = {r["code"]: dict(r) for r in conn.execute(
            "SELECT code, close, trade_date, pct_change FROM latest_price")}
        name_map = {r["code"]: r["name"] for r in conn.execute(
            "SELECT code, name FROM stock_info")}

    agg = {}
    for r in rows:
        code = r["code"]
        net = r["jg_net_buy"] or 0
        a = agg.setdefault(code, {
            "code": code, "name": name_map.get(code) or r["name"] or code,
            "cnt_in": 0, "cnt_total": 0, "cum_net": 0.0, "cum_out": 0.0,
            "_wsum": 0.0, "_wpx": 0.0, "_lo": None, "_hi": None,
            "buyers_max": 0, "sellers_max": 0, "last_net": None,
            "first_date": r["trade_date"], "last_date": r["trade_date"],
            "float_mkt_cap": r["float_mkt_cap"], "reasons": [], "signals": [],
        })
        a["cnt_total"] += 1
        a["last_date"] = r["trade_date"]
        a["buyers_max"] = max(a["buyers_max"], r["buyer_jg_count"] or 0)
        a["sellers_max"] = max(a["sellers_max"], r["seller_jg_count"] or 0)
        if r["float_mkt_cap"]:
            a["float_mkt_cap"] = r["float_mkt_cap"]
        if r["reason"] and r["reason"] not in a["reasons"]:
            a["reasons"].append(r["reason"])
        close = r["close"]
        if close and close > 0:
            if a["_lo"] is None or (r["low"] and r["low"] < a["_lo"]):
                a["_lo"] = r["low"] or close
            if a["_hi"] is None or (r["high"] and r["high"] > a["_hi"]):
                a["_hi"] = r["high"] or close
        if net > 0:
            a["cnt_in"] += 1
            a["cum_net"] += net
            a["last_net"] = net
            if close and close > 0:
                a["_wsum"] += net
                a["_wpx"] += net * close
            a["signals"].append({
                "date": r["trade_date"], "net": round(net, 0),
                "close": round(close, 2) if close else None,
                "pct": sanitize_numeric(r["pct"]),
                "buyers": r["buyer_jg_count"], "sellers": r["seller_jg_count"],
                "net_ratio": round(r["jg_net_ratio"], 2) if r["jg_net_ratio"] is not None else None,
                "reason": r["reason"],
            })
        else:
            a["cum_out"] += -(net) if net < 0 else 0.0

    items = []
    n_no_price = 0
    for code, a in agg.items():
        if a["cnt_in"] < min_cnt:
            continue
        cost = (a["_wpx"] / a["_wsum"]) if a["_wsum"] > 0 else None
        fmc = (a["float_mkt_cap"] or 0) * 1e8           # 亿元 → 元
        lp = latest.get(code) or {}
        last_close = lp.get("close")
        if only_priced and not (cost and last_close):
            n_no_price += 1
            continue
        items.append({
            "code": code, "name": a["name"],
            "cnt_in": a["cnt_in"], "cnt_total": a["cnt_total"],
            "cum_net": round(a["cum_net"], 0),
            "cum_out": round(-a["cum_out"], 0),
            "cum_ratio": round(a["cum_net"] / fmc * 100, 3) if fmc > 0 else None,
            "cost_est": round(cost, 2) if cost else None,
            "band_low": round(a["_lo"], 2) if a["_lo"] else None,
            "band_high": round(a["_hi"], 2) if a["_hi"] else None,
            "last_close": round(last_close, 2) if last_close else None,
            "last_price_date": lp.get("trade_date"),
            "last_close_pct": sanitize_numeric(lp.get("pct_change")),
            "vs_cost": (round((last_close - cost) / cost * 100, 2)
                        if (cost and last_close and cost > 0) else None),
            "buyers_max": a["buyers_max"], "sellers_max": a["sellers_max"],
            "float_mkt_cap": a["float_mkt_cap"],
            "first_date": a["first_date"], "last_date": a["last_date"],
            "reasons": a["reasons"][:3],
            "signals": a["signals"][-8:],
        })

    def _key(it):
        if sort_key == "vs_cost":
            return it["vs_cost"] if it["vs_cost"] is not None else 9e9
        if sort_key == "cost_est":
            return it["cost_est"] if it["cost_est"] is not None else 9e9
        return -(it["cum_ratio"] if it["cum_ratio"] is not None else 0)

    items.sort(key=_key)
    items = items[:limit]

    n_above = sum(1 for x in items if (x["vs_cost"] or 0) > 0)
    return {
        "as_of": as_of, "window_days": days, "start_date": start,
        "count": len(items), "min_cnt": min_cnt, "sort": sort_key,
        "above_cost": n_above, "below_cost": len(items) - n_above,
        "skipped_no_price": n_no_price,
        "note": "资金动向与成本位参考，**非收益信号**：实测机构买卖方向对次日收益无区分度"
                "（净买 +0.72% vs 净卖 +0.66%，Δ 不显著）；有效的是「频繁上榜」本身。"
                "cost_est 为按净买额加权的上榜日收盘价估算，非机构真实成交价。"
                "科创板/北交所因无本地行情未纳入。",
        "items": sanitize_numeric(items),
    }
