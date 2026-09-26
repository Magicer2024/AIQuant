"""simulated_trading.py —— 模拟交易推进器：从已发布 recommendation_item 推进成交与出场。

方案 C2 核心：模拟单输入来自已发布 recommendation_item（冻结计划的 entry_target/stop_loss/
take_profit），成交价 exec_entry_price 是真实模拟成交价，与诊断参考价彻底分离。日线收盘判定
属于模拟假设，不称为真实已成交。

不变式：
  - 建仓日不得卖出（A股 T+1）；回踩未成交不进入交易成绩（expired + no_fill 事件）。
  - 成交/出场未知保持 NULL；缺行情不等同亏损/未成交/结算。
  - 每日推荐 ID 均保留；同段复推只追加 recommend 事件，不重复建单（合并策略命名且互不借用）。
  - 重复推进不重复成交或结算：交易状态由「截至 as_of 的行情」确定性重算，事件按 event_key 幂等。
  - 出场数学复用现有引擎（短线状态机 / 中长线 evaluate_exit_by_prices），按运行冻结参数注入，
    不在推进时读取当前 get_param。
"""
from datetime import date

from core.db import get_conn
from core.execution_config import (
    resolve_execution, ENTRY_NEXT_OPEN, ENTRY_PULLBACK_CONFIRM,
    EXIT_SHORT_MACHINE, EXIT_BY_PRICES, EXIT_DEEP_FIXED_TP, EXECUTION_DEFINITION_VERSION)
from core.repository import outcome_repo
from core.repository.outcome_repo import (
    EVENT_RECOMMEND, EVENT_ENTRY_FILL, EVENT_EXIT, EVENT_NO_FILL)

import json


def _today(as_of):
    return as_of or date.today().strftime("%Y-%m-%d")


def _prices_after(conn, code, scan_date, as_of, limit):
    return [dict(r) for r in conn.execute(
        """SELECT trade_date, open, high, low, close FROM daily_price
           WHERE code=? AND trade_date>? AND trade_date<=?
           ORDER BY trade_date ASC LIMIT ?""",
        (code, scan_date, as_of, limit)).fetchall()]


def _load_batches(as_of):
    """已发布/旁路对照批次（scan_date<=as_of），连带运行冻结参数。"""
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            """SELECT b.id,b.run_id,b.scan_date,b.list_key,b.cohort,b.experiment_id,
                      b.status,r.params_json
               FROM recommendation_batch b JOIN signal_run r ON r.id=b.run_id
               WHERE b.status IN ('published','comparison') AND b.scan_date<=?
               ORDER BY b.scan_date,b.list_key""", (as_of,)).fetchall()
        batches = [dict(r) for r in rows]
        for b in batches:
            b["items"] = [dict(i) for i in conn.execute(
                "SELECT * FROM recommendation_item WHERE batch_id=? ORDER BY rank",
                (b["id"],)).fetchall()]
    for b in batches:
        b["params"] = json.loads(b["params_json"])
        # 旁路对照批次的模拟单归入 shadow cohort，与真实发布单彻底隔离、不互相合并。
        b["trade_cohort"] = "shadow" if b["status"] == "comparison" else b["cohort"]
    return batches


def _item_plan(item):
    plan = json.loads(item["plan_json"]) if isinstance(item.get("plan_json"), str) else (item.get("plan") or {})
    reason = json.loads(item["reason_json"]) if isinstance(item.get("reason_json"), str) else (item.get("reason") or {})
    return plan, reason


def _ensure_trades(batches, source):
    """为每个已发布推荐建立/合并模拟单，追加 recommend 事件。返回待推进交易上下文列表。"""
    contexts = {}
    for b in batches:
        for item in b["items"]:
            plan, reason = _item_plan(item)
            horizon = item["horizon"]
            strategy_key = reason.get("strategy_key") or ""
            cfg = resolve_execution(horizon, strategy_key, b["params"])
            identity = dict(code=item["code"], horizon=horizon, list_key=b["list_key"],
                            cohort=b["trade_cohort"], experiment_id=b["experiment_id"])
            open_trade = outcome_repo.find_open_trade(**identity)
            if open_trade:
                trade_id = open_trade["id"]
            else:
                trade_id = outcome_repo.create_trade(
                    first_recommendation_id=item["id"], code=item["code"], horizon=horizon,
                    list_key=b["list_key"], cohort=b["trade_cohort"],
                    experiment_id=b["experiment_id"], execution=cfg,
                    merge_policy=cfg.merge_policy, status="watching",
                    state={"scan_date": b["scan_date"]})
            # 每日推荐 ID 均保留：同段复推只追加事件（event_key 幂等，不重复成交）。
            outcome_repo.append_event(
                trade_id=trade_id, event_date=b["scan_date"],
                event_key=f"{trade_id}:recommend:{item['id']}",
                event_kind=EVENT_RECOMMEND, recommendation_id=item["id"],
                payload={"rank": item["rank"], "strategy_key": strategy_key,
                         "batch_id": b["id"], "scan_date": b["scan_date"]})
            # 首推的计划与建仓基准冻结在交易上下文（复推不改首推基准）。
            if trade_id not in contexts:
                contexts[trade_id] = {
                    "trade_id": trade_id, "scan_date": b["scan_date"], "plan": plan,
                    "cfg": cfg, "code": item["code"],
                    "first_item_id": (open_trade or {}).get("first_recommendation_id", item["id"]),
                }
    return list(contexts.values())


def _resolve_entry(ctx, conn, as_of):
    """按入场规则判定建仓成交。返回 (status, entry_date, entry_price, event)。

    status: 'holding'（已成交）/ 'expired'（回踩窗口走完未成交）/ 'watching'（数据未到，继续等）。
    """
    cfg = ctx["cfg"]
    plan = ctx["plan"]
    entry_target = plan.get("entry_target")
    if cfg.entry_rule == ENTRY_NEXT_OPEN:
        prices = _prices_after(conn, ctx["code"], ctx["scan_date"], as_of, 1)
        if not prices:
            return "watching", None, None, None
        day = prices[0]
        px = day["open"] or day["close"]
        if not px or px <= 0:
            return "watching", None, None, None
        return "holding", day["trade_date"], round(float(px), 3), EVENT_ENTRY_FILL
    # 回踩确认入场
    window = max(1, cfg.entry_window)
    prices = _prices_after(conn, ctx["code"], ctx["scan_date"], as_of, window)
    if not entry_target or entry_target <= 0:
        # 无买点无法判定回踩，视为次日开盘兜底（保持推进，不静默丢弃）
        if not prices:
            return "watching", None, None, None
        day = prices[0]
        px = day["open"] or day["close"]
        return ("holding", day["trade_date"], round(float(px), 3), EVENT_ENTRY_FILL) if px else \
            ("watching", None, None, None)
    for idx, day in enumerate(prices):
        low = day["low"]
        if low is not None and low <= entry_target:
            open_ = day["open"] or entry_target
            px = round(min(float(entry_target), float(open_)), 3)
            return "holding", day["trade_date"], px, EVENT_ENTRY_FILL
    if len(prices) >= window:
        return "expired", None, None, EVENT_NO_FILL   # 窗口走完未回踩 → 放弃，不计入成绩
    return "watching", None, None, None               # 窗口未走完，等下个交易日


def _hold_prices_from(conn, code, entry_date, as_of, limit):
    """建仓日起（含建仓日）的行情，供出场状态机使用。"""
    return [dict(r) for r in conn.execute(
        """SELECT trade_date, open, high, low, close FROM daily_price
           WHERE code=? AND trade_date>=? AND trade_date<=?
           ORDER BY trade_date ASC LIMIT ?""",
        (code, entry_date, as_of, limit)).fetchall()]


def _resolve_exit(ctx, conn, entry_date, entry_price, as_of):
    """按出场引擎判定出场。返回 (exit_date, exit_price, reason) 或 (None,None,None) 表示仍持有。"""
    cfg = ctx["cfg"]
    plan = ctx["plan"]
    stop = plan.get("stop_loss")
    tp = plan.get("take_profit")
    if cfg.exit_engine == EXIT_SHORT_MACHINE:
        from core.outcome_tracker import _short_exit_sim
        max_hold = cfg.max_hold_days or 10
        holds = _hold_prices_from(conn, ctx["code"], entry_date, as_of, max_hold)
        reason, exit_date, exit_return, _hit_stop, _launched, done = _short_exit_sim(
            holds, entry_price, stop, tp, cfg.trailing_pct, max_hold,
            launch_ratio=cfg.take_profit_launch, stop_loss_pct=cfg.stop_loss_pct)
        if done and exit_date:
            # 短线各出场原因均以出场日收盘价成交，直接取该日收盘（不从四舍五入收益反算）。
            row = conn.execute("SELECT close FROM daily_price WHERE code=? AND trade_date=?",
                               (ctx["code"], exit_date)).fetchone()
            exit_price = round(float(row["close"]), 4) if row and row["close"] else \
                round(entry_price * (1 + exit_return / 100.0), 4)
            return exit_date, exit_price, reason
        return None, None, None
    if cfg.exit_engine == EXIT_BY_PRICES:
        import pandas as pd
        from strategy.exit_advisor import evaluate_exit_by_prices
        limit = 130 if cfg.horizon == "long" else 70
        rows = _hold_prices_from(conn, ctx["code"], entry_date, as_of, limit)
        if not rows:
            return None, None, None
        df = pd.DataFrame(rows).set_index("trade_date")
        adv = evaluate_exit_by_prices(
            entry_price=entry_price, entry_date=entry_date, df=df,
            stop_loss=stop, take_profit=tp, max_hold_days=cfg.max_hold_days,
            trailing_pct=cfg.trailing_pct, partial_tp=cfg.partial_tp,
            stop_cap_pct=cfg.stop_cap_pct)
        detail = adv.get("detail") or {}
        if adv.get("status") == "clear" and detail.get("exit_date"):
            return detail["exit_date"], detail.get("exit_price"), detail.get("exit_reason") or "exit"
        return None, None, None
    if cfg.exit_engine == EXIT_DEEP_FIXED_TP:
        # 个股深度固定止盈：建仓日不卖（T+1），收盘达止盈/破止损了结。
        rows = _hold_prices_from(conn, ctx["code"], entry_date, as_of, 250)
        for j, day in enumerate(rows):
            if j == 0:
                continue  # 建仓日只累计，不判出场
            close = day["close"]
            if not close:
                continue
            if tp and close >= tp:
                return day["trade_date"], round(float(close), 4), "take_profit"
            if stop and close <= stop:
                return day["trade_date"], round(float(close), 4), "stop_loss"
        return None, None, None
    return None, None, None


def _advance_trade(ctx, conn, as_of):
    """推进单个交易：建仓 → 出场。确定性重算，重复推进结果一致。"""
    trade_id = ctx["trade_id"]
    trade = outcome_repo.get_trade(trade_id)
    if not trade or trade["status"] in ("closed", "expired", "cancelled"):
        return trade["status"] if trade else None

    status, entry_date, entry_price, event = _resolve_entry(ctx, conn, as_of)
    if status == "watching":
        # 数据未到，保持观察（不写成交、不写收益）。
        outcome_repo.update_trade(trade_id, status="watching", last_evaluated_as_of=as_of)
        return "watching"
    if status == "expired":
        outcome_repo.update_trade(trade_id, status="expired", last_evaluated_as_of=as_of,
                                  evaluation_version=EXECUTION_DEFINITION_VERSION)
        outcome_repo.append_event(
            trade_id=trade_id, event_date=as_of, event_key=f"{trade_id}:no_fill",
            event_kind=EVENT_NO_FILL, payload={"reason": "回踩窗口内未成交"})
        return "expired"

    # 已成交：先落建仓（若尚未记录），再判出场。
    if trade["status"] != "holding" or trade.get("exec_entry_date") != entry_date:
        outcome_repo.update_trade(trade_id, status="holding",
                                  exec_entry_date=entry_date, exec_entry_price=entry_price,
                                  last_evaluated_as_of=as_of)
        outcome_repo.append_event(
            trade_id=trade_id, event_date=entry_date, event_key=f"{trade_id}:entry:{entry_date}",
            event_kind=EVENT_ENTRY_FILL, payload={"exec_entry_price": entry_price,
                                                 "entry_rule": ctx["cfg"].entry_rule})
    exit_date, exit_price, reason = _resolve_exit(ctx, conn, entry_date, entry_price, as_of)
    if not exit_date:
        outcome_repo.update_trade(trade_id, last_evaluated_as_of=as_of,
                                  evaluation_version=EXECUTION_DEFINITION_VERSION)
        return "holding"
    gross = round((exit_price - entry_price) / entry_price * 100, 4)
    # net_return 暂等于 gross_return；整手/最低佣金/印花税成本模型在方案 E 落地。
    outcome_repo.update_trade(
        trade_id, status="closed", exec_exit_date=exit_date, exec_exit_price=exit_price,
        gross_return=gross, net_return=gross, last_evaluated_as_of=as_of,
        evaluation_version=EXECUTION_DEFINITION_VERSION)
    outcome_repo.append_event(
        trade_id=trade_id, event_date=exit_date, event_key=f"{trade_id}:exit:{exit_date}",
        event_kind=EVENT_EXIT, payload={"reason": reason, "exec_exit_price": exit_price,
                                        "gross_return": gross})
    return "closed"


def advance_simulated_trades(as_of=None, *, source="simulated_trading"):
    """推进截至 as_of 的全部已发布推荐模拟单。幂等、确定性重算。

    按 scan_date 升序逐日推进：处理某日推荐前，先把已有交易推进到该日，使此前已
    出场的单变为 closed，从而不被当日复推并入——正确实现「卖出后拆段」。同段未了结
    的复推仍并入原交易（只追加 recommend 事件）。最后统一推进到 as_of 收敛终态。

    返回 {"trades": N, "closed": x, "holding": y, "watching": z, "expired": w, ...}。
    """
    as_of = _today(as_of)
    batches = _load_batches(as_of)
    by_date = {}
    for b in batches:
        by_date.setdefault(b["scan_date"], []).append(b)

    contexts = {}
    with get_conn(readonly=True) as conn:
        for d in sorted(by_date):
            # 1) 先把已有交易推进到当日 d：反映 d 之前的出场，供合并判定使用。
            for ctx in contexts.values():
                _advance_trade(ctx, conn, d)
            # 2) 建立/合并当日推荐（find_open_trade 此时已排除 d 前出场的单）。
            for ctx in _ensure_trades(by_date[d], source):
                contexts.setdefault(ctx["trade_id"], ctx)
            # 3) 推进当日（含新建）交易到 d。
            for ctx in contexts.values():
                _advance_trade(ctx, conn, d)
        # 4) 收敛：全部推进到 as_of（捕获最后一个推荐日之后到 as_of 的出场）。
        for ctx in contexts.values():
            _advance_trade(ctx, conn, as_of)

    counts = {"closed": 0, "holding": 0, "watching": 0, "expired": 0,
              "cancelled": 0, "data_pending": 0}
    for trade_id in contexts:
        trade = outcome_repo.get_trade(trade_id)
        if trade and trade["status"] in counts:
            counts[trade["status"]] += 1
    return {"trades": len(contexts), "as_of": as_of, **counts}


def cancel_superseded_trades(batch_id, *, cancel_date):
    """重新发布替代旧批次时，撤销其未成交模拟单（记录 cancel 事件，不删除）。

    仅撤销 watching（未成交）单；已成交/已了结单保留其真实成绩，不追溯撤销。
    """
    with get_conn(readonly=True) as conn:
        item_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM recommendation_item WHERE batch_id=?", (batch_id,)).fetchall()]
    cancelled = 0
    for item_id in item_ids:
        trade = outcome_repo.get_trade_by_recommendation(item_id)
        if trade and trade["status"] == "watching":
            outcome_repo.update_trade(trade["id"], status="cancelled",
                                      last_evaluated_as_of=cancel_date)
            outcome_repo.append_event(
                trade_id=trade["id"], event_date=cancel_date,
                event_key=f"{trade['id']}:cancel:{cancel_date}",
                event_kind="cancel", payload={"reason": "批次被重新发布替代"})
            cancelled += 1
    return cancelled
