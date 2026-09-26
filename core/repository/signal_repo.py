"""
signal_repo.py —— signal_records / stock_signal 表的数据访问层
"""
import math
import json
from datetime import datetime, date


def _get_conn():
    from core.db import get_conn
    return get_conn()


def save_signals(signals: list[dict], sent_wechat: bool = False, trade_date: str = None,
                 *, run_context=None, store=None):
    """
    保存策略筛选结果到 signal_records 表
    """
    from config.settings import SIGNAL_MODEL_MODE, require_signal_model_ready
    require_signal_model_ready()
    if SIGNAL_MODEL_MODE != "legacy":
        if run_context is None:
            raise ValueError("统一信号写入必须传入计算开始时冻结的 run_context")
        if trade_date and trade_date != run_context.scan_date:
            raise ValueError("写入日期与冻结上下文不一致")
        return persist_signal_run(run_context, signals, source="signal_records", store=store)
    if not signals:
        return
    if trade_date is None:
        trade_date = date.today().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO signal_records
              (scan_time, trade_date, code, name, price, score,
               stop_loss, take_profit, buy_volume, buy_money, sent_wechat)
            VALUES
              (:scan_time, :trade_date, :code, :name, :price, :score,
               :stop_loss, :take_profit, :buy_volume, :buy_money, :sent_wechat)
        """, [{
            "scan_time":   now,
            "trade_date":  trade_date,
            "code":        s.get("code", ""),
            "name":        s.get("name", ""),
            "price":       s.get("price", 0),
            "score":       s.get("score", 0),
            "stop_loss":   s.get("stop_loss", 0),
            "take_profit": s.get("take_profit", 0),
            "buy_volume":  s.get("buy_volume", 0),
            "buy_money":   s.get("buy_money", 0),
            "sent_wechat": 1 if sent_wechat else 0,
        } for s in signals])
    print(f"[DB] 保存 {len(signals)} 条信号记录，推送微信={'是' if sent_wechat else '否'}")


def save_scan_signals(scan_results: list[dict], sent_wechat: bool = False, scan_date: str = None,
                      *, run_context=None, store=None):
    """独立写入口也必须显式传递事前冻结上下文；空结果同样记录运行。"""
    from config.settings import SIGNAL_MODEL_MODE, require_signal_model_ready
    require_signal_model_ready()
    if SIGNAL_MODEL_MODE != "legacy":
        if run_context is None:
            raise ValueError("统一信号写入必须传入计算开始时冻结的 run_context")
        if scan_date and scan_date != run_context.scan_date:
            raise ValueError("写入日期与冻结上下文不一致")
        return persist_signal_run(run_context, scan_results, store=store)
    if not scan_results:
        return
    if scan_date is None:
        scan_date = date.today().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for s in scan_results:
        def _si(v):
            if v is None: return 0.0
            if isinstance(v, float) and math.isnan(v): return 0.0
            return round(float(v), 2)
        trigger = s.get("trigger_list", [])
        if isinstance(trigger, list):
            trigger_json = json.dumps(trigger, ensure_ascii=False)
        else:
            trigger_json = str(trigger or "[]")
        records.append({
            "scan_date":    scan_date,
            "trade_date":   s.get("trade_date") or scan_date,
            "code":         s.get("code", ""),
            "name":         s.get("name", ""),
            "price":        _si(s.get("price")),
            "fusion_score": _si(s.get("score")),
            "vol_score":    _si(s.get("s1_vol_break")),
            "ma_score":     _si(s.get("s2_ma_conv")),
            "diverge_score": _si(s.get("s3_pv_div")),
            "bottom_score":  _si(s.get("s4_bottom")),
            "whale_score":   _si(s.get("s5_whale")),
            "trigger_list":  trigger_json,
            "buy_price":     _si(s.get("price")),
            "stop_loss":     _si(s.get("stop_loss")),
            "take_profit":   _si(s.get("take_profit")),
            "buy_volume":    int(s.get("buy_volume", 0)),
            "buy_money":     _si(s.get("buy_money")),
            "sent_wechat":   1 if sent_wechat else 0,
            "created_at":    now,
        })
    with _get_conn() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO stock_signal
              (scan_date, trade_date, code, name, price, fusion_score,
               vol_score, ma_score, diverge_score, bottom_score, whale_score,
               trigger_list, buy_price, stop_loss, take_profit,
               buy_volume, buy_money, sent_wechat, created_at)
            VALUES
              (:scan_date, :trade_date, :code, :name, :price, :fusion_score,
               :vol_score, :ma_score, :diverge_score, :bottom_score, :whale_score,
               :trigger_list, :buy_price, :stop_loss, :take_profit,
               :buy_volume, :buy_money, :sent_wechat, :created_at)
        """, records)
    print(f"[DB] 保存 {len(scan_results)} 条扫描推荐到 stock_signal")


def get_signals(trade_date: str = None, limit: int = 100) -> list[dict]:
    """查询筛选记录"""
    sql = "SELECT * FROM signal_records"
    params = []
    if trade_date:
        sql += " WHERE trade_date=?"
        params.append(trade_date)
    sql += " ORDER BY scan_time DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_today_signals() -> list[dict]:
    """获取今日筛选记录"""
    return get_signals(trade_date=date.today().strftime("%Y-%m-%d"))


class SignalConsistencyError(ValueError):
    """同一幂等身份出现不同内容，禁止覆盖原证据。"""


STRATEGY_KEYS = {
    "隔日动量": "next_day_momentum", "反转首日": "first_reversal",
    "强势突破": "surge_breakout", "缩量回踩": "pullback_dip",
    "中线综合": "mid_composite", "长线趋势": "long_trend",
    "短线融合": "short_composite", "个股深度": "stock_deep",
    "超跌反弹v3": "oversold_rebound",
}


def adapt_signal(record, context, *, source="stock_signal", scope_codes=None):
    """保留原评分和诊断价；融合五分项仍是 payload 内因子，不拆策略。"""
    from core.input_snapshot import canonical_json, content_hash
    r = dict(record)
    if source == "stock_deep_signal":
        for field in ("entry_price", "stop_loss", "take_profit"):
            r.setdefault(field, (r.get("action_plan") or {}).get(field))
    strategy = r.get("strategy") or ("个股深度" if source == "stock_deep_signal" else "")
    key = STRATEGY_KEYS.get(strategy, "legacy_unknown")
    kind = r.get("signal_kind", "candidate")
    if source == "strategy_signals":
        key = f"rule:{r['rule_id']}"
        kind = "rule_hit"
        strategy = r.get("rule_name") or strategy
    if key == "legacy_unknown":
        kind = f"{kind}:{content_hash(strategy)[:16]}"
    horizon = r.get("horizon") or ("deep" if source == "stock_deep_signal" else "short")
    if horizon not in {"short", "mid", "long", "deep"} or not r.get("code"):
        raise ValueError("信号缺少股票代码或周期无效")
    signal_date = r.get("scan_date") or r.get("trade_date") or context.scan_date
    if signal_date != context.scan_date:
        raise ValueError("不同日期的信号必须写入各自运行")
    if scope_codes is None:
        scope_codes = set(json.loads(context.scope_json))
    if str(r["code"]) not in scope_codes:
        raise ValueError("信号股票不在冻结运行范围内")
    # 旧 outcome 使用 buy_price；保留该定义，不能替换为 close/open。
    reference = r.get("signal_reference_price", r.get("entry_price", r.get("buy_price", r.get("price"))))
    payload = {k: v for k, v in r.items() if k not in {"created_at", "updated_at", "sent_wechat"}}
    payload["source_table"] = source
    if r.get("id") is not None:
        payload["source_id"] = r["id"]
    event = {
        "scan_date": context.scan_date, "code": str(r["code"]), "horizon": horizon,
        "strategy_key": key, "strategy": strategy, "signal_kind": kind,
        "signal_reference_price": reference,
        "reference_kind": r.get("reference_kind", "legacy_signal_reference"),
        "entry_target": r.get("entry_target", r.get("buy_price", r.get("entry_price"))),
        "stop_loss": r.get("stop_loss"), "take_profit": r.get("take_profit"),
        "raw_score": r.get("fusion_score", r.get("score", r.get("confidence"))),
        "score_scale": r.get("score_scale", "source_native"),
        "payload_json": canonical_json(payload),
    }
    # 将数值缺失归一化，内容哈希与实际存储保持一致。
    event = json.loads(canonical_json(event))
    event["content_hash"] = content_hash(event)
    return event


def persist_signal_run(context, records, *, source="stock_signal", status="complete", store=None):
    """原子保存整次运行全部信号。重复运行不换 ID，冲突内容直接报错。"""
    import uuid
    from core.input_snapshot import SnapshotStore, content_hash
    if status not in {"complete", "partial", "failed"}:
        raise ValueError("必须给出明确的完成状态")
    manifest = json.loads(context.manifest_json)
    if status == "failed" and records:
        raise ValueError("失败运行不能携带信号")
    if context.run_type != "legacy_import" and status != "failed":
        (store or SnapshotStore()).verify(manifest)
    events = {}
    scope_codes = set(json.loads(context.scope_json))
    for record in records:
        event = adapt_signal(record, context, source=source, scope_codes=scope_codes)
        key = tuple(event[k] for k in ("code", "horizon", "strategy_key", "signal_kind"))
        if key in events and events[key] != event:
            raise SignalConsistencyError(f"同运行同信号身份内容冲突：{key}")
        events[key] = event
    ordered = [events[k] for k in sorted(events)]
    fingerprint = content_hash({"status": status, "events": ordered})
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT * FROM signal_run WHERE idempotency_key=?", (context.key,)).fetchone()
        if previous:
            if previous["content_hash"] != fingerprint:
                raise SignalConsistencyError("同输入和参数产生了不同信号，禁止覆盖运行")
            return previous["id"]
        run_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO signal_run(
            id,idempotency_key,scan_date,as_of,run_type,scope,scope_json,code_version_json,
            params_json,params_hash,input_manifest_json,input_hash,quality_json,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'running')""", (
            run_id, context.key, context.scan_date, context.as_of, context.run_type,
            context.scope, context.scope_json, context.code_version_json, context.params_json,
            content_hash(json.loads(context.params_json)), context.manifest_json,
            content_hash(manifest), context.quality_json))
        for event in ordered:
            event.update(id=uuid.uuid4().hex, run_id=run_id)
            columns = list(event)
            conn.execute(f"INSERT INTO signal_event ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [event[c] for c in columns])
        conn.execute("UPDATE signal_run SET status=?,content_hash=?,completed_at=datetime('now') WHERE id=?",
                     (status, fingerprint, run_id))
        return run_id


def write_legacy_projection(run_id):
    """旁路期从同一次冻结计算输出兼容旧表；绝不反向重建统一信号。"""
    from config.settings import SIGNAL_MODEL_MODE
    if SIGNAL_MODEL_MODE != "shadow":
        raise ValueError("兼容投影只允许在 shadow 模式写入")
    run = get_signal_run(run_id)
    if not run or run["status"] != "complete":
        raise ValueError("未完整运行不能替换兼容候选")
    quality = json.loads(run["quality_json"])
    if (not quality.get("data_ready") or quality.get("missing_price_codes")
            or (run["scope"] == "all" and not quality.get("cross_section_complete"))):
        raise ValueError("输入未就绪不能用空榜替换兼容候选")
    source = json.loads(run["input_manifest_json"])["source"]
    records = [json.loads(r["payload_json"]) for r in get_run_signals(run_id)]
    codes = json.loads(run["scope_json"])
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 局部计算只改范围内旧候选；全市场空榜也必须覆盖旧日候选。
        subset = "" if run["scope"] == "all" else " AND code IN (SELECT value FROM json_each(?))"
        args = [run["scan_date"]] + ([] if run["scope"] == "all" else [json.dumps(codes)])
        if source == "stock_signal":
            from core.sync import _SIGNAL_INSERT_SQL
            conn.execute("DELETE FROM stock_signal WHERE scan_date=?" + subset, args)
            # 明确重现旧表追加覆盖优先级，仅用于兼容投影，新表始终保留全部信号。
            priority = {"short_composite": 0, "oversold_rebound": 0, "mid_composite": 1,
                        "long_trend": 1, "pullback_dip": 2, "next_day_momentum": 3,
                        "surge_breakout": 4, "first_reversal": 5}
            records.sort(key=lambda r: (priority.get(STRATEGY_KEYS.get(r.get("strategy")), -1), r["code"]))
            for record in records:
                record.update(created_at=datetime.now().isoformat(), sent_wechat=0)
            conn.executemany(_SIGNAL_INSERT_SQL, records)
        elif source == "strategy_signals":
            conn.execute("DELETE FROM strategy_signals WHERE trade_date=?" + subset, args)
            conn.executemany("INSERT INTO strategy_signals(trade_date,code,rule_id,rule_name,confidence,raw_score) VALUES (?,?,?,?,?,?)",
                             [(run["scan_date"], r["code"], r["rule_id"], r["rule_name"], r["confidence"], r["confidence"]) for r in records])
        elif source == "stock_deep_signal":
            from strategy.stock_deep import _insert_market_signals
            conn.execute("DELETE FROM stock_deep_signal WHERE scan_date=?" + subset, args)
            _insert_market_signals(conn, run["scan_date"], records)
        else:
            raise ValueError("未知来源不可投影")


def get_signal_run(run_id):
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM signal_run WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_run_signals(run_id):
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM signal_event WHERE run_id=? ORDER BY code,horizon,strategy_key,signal_kind", (run_id,))]
