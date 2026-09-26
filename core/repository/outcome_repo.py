"""outcome_repo.py —— signal_outcome / simulated_trade / simulated_trade_event 持久化。

方案 C2 的存储层。职责边界：
  - signal_outcome：诊断结果（从 signal_event 的 signal_reference_price 计算 T+n 收益），
    按 (signal_id, definition_version) 幂等；旧诊断参考价原样保存，不因字段重命名改基准。
  - simulated_trade：模拟成交单（从已发布 recommendation_item 推进），保存 exec_* 真实
    成交/出场字段；成交或出场未知保持 NULL，缺行情为 data_pending，不等同亏损/未成交/结算。
  - simulated_trade_event：每日推荐 ID 均保留，同段复推只追加事件；事件不可改不可删。

统计合同（方案 C2「统计合同」）在本层以纯函数实现，供路由/报告复用：
  - T+n 胜率 = 该 T+n 正收益数 / 该 T+n 有效值数，各自返回样本数。
  - 结算胜率 = 已平仓且收益有效的盈利数 / 对应已平仓数，按交易 ID 去重。
  - 持仓浮盈 / 未成交 / 失效分别返回，不混入结算分母。
  - profit factor = 总盈利 / 总亏损；平均盈利 / 平均亏损单独命名。
  - 无亏损样本的比值返回 None（不用 99 伪装），并附说明。
"""
import json
import uuid
from datetime import datetime

from core.db import get_conn
from core.input_snapshot import canonical_json, content_hash

# 交易事件类型
EVENT_RECOMMEND = "recommend"        # 推荐入选（首推建单 / 同段复推追加）
EVENT_ENTRY_FILL = "entry_fill"      # 建仓成交
EVENT_PLAN_CHANGE = "plan_change"    # 计划变更（按交易日生效，不改历史）
EVENT_EXIT = "exit"                  # 出场了结
EVENT_NO_FILL = "no_fill"            # 回踩窗口内未成交，放弃
EVENT_CANCEL = "cancel"              # 重新发布导致的撤销（不删除）

TRADE_STATUSES = {"watching", "holding", "closed", "expired", "cancelled", "data_pending"}


# ─────────────────────────── signal_outcome（诊断） ───────────────────────────

def upsert_signal_outcome(*, signal_id, definition_version, reference_price,
                          returns, evaluated_as_of, status):
    """幂等写入/更新一条诊断结果。returns = {"t1":..,"t2":..,"t3":..,"t5":..,"t10":..}。

    诊断结果随行情推进可被更完整的值覆盖（同一 definition_version 内），
    但参考价 reference_price 一旦写入不再改变基准。
    """
    if status not in {"data_pending", "evaluated", "no_data"}:
        raise ValueError(f"非法诊断状态：{status}")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO signal_outcome(
                signal_id,definition_version,reference_price,
                t1_return,t2_return,t3_return,t5_return,t10_return,evaluated_as_of,status)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(signal_id,definition_version) DO UPDATE SET
                t1_return=excluded.t1_return, t2_return=excluded.t2_return,
                t3_return=excluded.t3_return, t5_return=excluded.t5_return,
                t10_return=excluded.t10_return, evaluated_as_of=excluded.evaluated_as_of,
                status=excluded.status""",
            (signal_id, definition_version, reference_price,
             returns.get("t1"), returns.get("t2"), returns.get("t3"),
             returns.get("t5"), returns.get("t10"), evaluated_as_of, status))


def get_signal_outcomes(run_id=None, *, definition_version=None, signal_ids=None):
    """只读回取诊断结果；可按运行或信号集合过滤。"""
    sql = ("SELECT o.*, e.code, e.horizon, e.strategy_key, e.scan_date "
           "FROM signal_outcome o JOIN signal_event e ON e.id=o.signal_id")
    clauses, params = [], []
    if run_id is not None:
        clauses.append("e.run_id=?")
        params.append(run_id)
    if definition_version is not None:
        clauses.append("o.definition_version=?")
        params.append(definition_version)
    if signal_ids:
        clauses.append(f"o.signal_id IN ({','.join('?' * len(signal_ids))})")
        params.extend(signal_ids)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with get_conn(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ─────────────────────────── simulated_trade（模拟成交） ───────────────────────────

def create_trade(*, first_recommendation_id, code, horizon, list_key, cohort,
                 experiment_id, execution, merge_policy, status="watching", state=None):
    """幂等建单：first_recommendation_id 唯一，重复创建返回原交易 ID。"""
    if status not in TRADE_STATUSES:
        raise ValueError(f"非法交易状态：{status}")
    execution_json = canonical_json(execution.to_json() if hasattr(execution, "to_json") else execution)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute("SELECT id FROM simulated_trade WHERE first_recommendation_id=?",
                            (first_recommendation_id,)).fetchone()
        if prev:
            return prev["id"]
        trade_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO simulated_trade(
                id,first_recommendation_id,code,horizon,list_key,cohort,experiment_id,
                execution_json,merge_policy,status,state_json,evaluation_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,'{}',?)""",
            (trade_id, first_recommendation_id, code, horizon, list_key, cohort,
             experiment_id or "", execution_json, merge_policy, status,
             (execution.definition_version if hasattr(execution, "definition_version") else None)))
        if state:
            conn.execute("UPDATE simulated_trade SET state_json=? WHERE id=?",
                         (canonical_json(state), trade_id))
        return trade_id


def get_trade_by_recommendation(first_recommendation_id):
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT * FROM simulated_trade WHERE first_recommendation_id=?",
                           (first_recommendation_id,)).fetchone()
    return _decode_trade(row) if row else None


def get_trade(trade_id):
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT * FROM simulated_trade WHERE id=?", (trade_id,)).fetchone()
    return _decode_trade(row) if row else None


def find_open_trade(*, code, horizon, list_key, cohort, experiment_id, statuses=("holding", "watching")):
    """查找同身份下仍未了结的交易（供合并策略判定：同段复推追加事件而非新建）。"""
    if not statuses:
        return None
    ph = ",".join("?" * len(statuses))
    with get_conn(readonly=True) as conn:
        row = conn.execute(
            f"""SELECT * FROM simulated_trade
                WHERE code=? AND horizon=? AND list_key=? AND cohort=? AND experiment_id=?
                  AND status IN ({ph})
                ORDER BY rowid DESC LIMIT 1""",
            (code, horizon, list_key, cohort, experiment_id or "", *statuses)).fetchone()
    return _decode_trade(row) if row else None


def update_trade(trade_id, **fields):
    """更新交易可变字段（状态/成交/出场/收益/评估时点/状态机）。

    仅允许更新非身份字段；受 core/db.py 的 CHECK 约束保护（无入场不得有收益、
    holding/closed 需完整入场、closed 需出场且 exit_date>entry_date）。
    """
    allowed = {"status", "exec_entry_date", "exec_entry_price", "exec_exit_date",
               "exec_exit_price", "gross_return", "net_return",
               "last_evaluated_as_of", "evaluation_version", "state_json"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return trade_id
    if "state_json" in sets and not isinstance(sets["state_json"], str):
        sets["state_json"] = canonical_json(sets["state_json"])
    cols = ",".join(f"{k}=?" for k in sets)
    with get_conn() as conn:
        conn.execute(f"UPDATE simulated_trade SET {cols} WHERE id=?",
                     (*sets.values(), trade_id))
    return trade_id


def append_event(*, trade_id, event_date, event_key, event_kind, recommendation_id=None, payload=None):
    """幂等追加交易事件；event_key 唯一，重复追加直接忽略（保证重复推进不重复成交）。"""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT 1 FROM simulated_trade_event WHERE event_key=?",
                              (event_key,)).fetchone()
        if exists:
            return None
        payload_json = canonical_json(payload or {})
        event_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO simulated_trade_event(
                id,trade_id,recommendation_id,event_date,event_key,event_kind,payload_json,content_hash)
               VALUES (?,?,?,?,?,?,?,?)""",
            (event_id, trade_id, recommendation_id, event_date, event_key, event_kind,
             payload_json, content_hash({"trade_id": trade_id, "event_key": event_key,
                                         "event_kind": event_kind, "event_date": event_date,
                                         "payload": payload or {}})))
        return event_id


def get_trade_events(trade_id):
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM simulated_trade_event WHERE trade_id=? ORDER BY event_date,rowid",
            (trade_id,)).fetchall()
    out = []
    for r in rows:
        e = dict(r)
        e["payload"] = json.loads(e.pop("payload_json"))
        out.append(e)
    return out


def list_trades(*, cohort=None, list_key=None, horizon=None, status=None,
                experiment_id=None, limit=None):
    sql = "SELECT * FROM simulated_trade"
    clauses, params = [], []
    for col, val in (("cohort", cohort), ("list_key", list_key), ("horizon", horizon),
                     ("status", status), ("experiment_id", experiment_id)):
        if val is not None:
            clauses.append(f"{col}=?")
            params.append(val)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY rowid"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with get_conn(readonly=True) as conn:
        return [_decode_trade(r) for r in conn.execute(sql, params).fetchall()]


def _decode_trade(row):
    t = dict(row)
    t["execution"] = json.loads(t.pop("execution_json"))
    t["state"] = json.loads(t.pop("state_json"))
    return t


# ─────────────────────────── 统计合同（纯函数） ───────────────────────────

def _ratio(num, den):
    """无亏损/无有效样本时返回 None（不用 99 伪装有限结果）。"""
    return (num / den) if den else None


def tn_win_stats(outcomes, key):
    """T+n 胜率：正收益数 / 有效值数；返回 (win_rate, positive, valid, samples)。

    outcomes: [{ "t1_return":.., ... }]；key 如 "t1_return"。缺失（None）不计入分母。
    """
    values = [o.get(key) for o in outcomes if o.get(key) is not None]
    positive = sum(1 for v in values if v > 0)
    return {"win_rate": _ratio(positive, len(values)), "positive": positive,
            "valid": len(values), "samples": len(values)}


def settlement_stats(trades):
    """结算口径统计：按交易 ID 去重，只统计已平仓且收益有效的交易。

    持仓浮盈 / 未成交 / 失效分别返回，不混入结算分母。
    返回 dict：
      settled（已平仓有效数）、settled_wins、settlement_win_rate、
      gross_profit、gross_loss、profit_factor、avg_win、avg_loss、
      holding（持仓中，含浮盈列表）、unfilled（未成交/观察）、expired、cancelled、data_pending。
    """
    uniq = {}
    for t in trades:
        uniq[t["id"]] = t  # 按交易 ID 去重
    trades = list(uniq.values())

    settled, holding, unfilled, expired, cancelled, data_pending = [], [], [], [], [], []
    for t in trades:
        st = t["status"]
        if st == "closed" and t.get("net_return") is not None:
            settled.append(t)
        elif st == "holding":
            holding.append(t)
        elif st == "watching":
            unfilled.append(t)
        elif st == "expired":
            expired.append(t)
        elif st == "cancelled":
            cancelled.append(t)
        elif st == "data_pending":
            data_pending.append(t)

    wins = [t for t in settled if t["net_return"] > 0]
    losses = [t for t in settled if t["net_return"] < 0]
    gross_profit = sum(t["net_return"] for t in wins)
    gross_loss = sum(t["net_return"] for t in losses)  # 负数和
    return {
        "settled": len(settled), "settled_wins": len(wins), "settled_losses": len(losses),
        "settlement_win_rate": _ratio(len(wins), len(settled)),
        "gross_profit": round(gross_profit, 4), "gross_loss": round(gross_loss, 4),
        # profit factor = 总盈利 / |总亏损|；无亏损样本返回 None + 说明
        "profit_factor": _ratio(gross_profit, abs(gross_loss)) if gross_loss < 0 else None,
        "profit_factor_note": None if gross_loss < 0 else "无亏损样本，profit factor 无定义",
        "avg_win": _ratio(gross_profit, len(wins)),
        "avg_loss": _ratio(gross_loss, len(losses)),
        # 未实施资金占用模型的逐笔收益只叫「平均单笔收益」，不叫账户累计收益
        "avg_trade_return": _ratio(sum(t["net_return"] for t in settled), len(settled)),
        "holding": len(holding), "unfilled": len(unfilled),
        "expired": len(expired), "cancelled": len(cancelled), "data_pending": len(data_pending),
    }
