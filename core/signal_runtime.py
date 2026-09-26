"""统一计算适配：冻结输入与参数后运行既有算法，完整保留各来源的信号。"""
from dataclasses import replace
from datetime import date, timedelta
import json
import math
import time

from config.strategy_params import freeze_effective_parameters, parameter_context
from core.db import get_conn, input_connection
from core.input_snapshot import (
    RunContext, SnapshotStore, SnapshotUnavailable, capture_inputs, open_snapshot,
    canonical_json, code_version,
)
from core.repository.signal_repo import persist_signal_run


def prepare_signal_run(scan_date, *, scope="all", codes=None, source="stock_signal",
                       store=None, runtime=None, run_type="live", parameters=None):
    """在同一读事务冻结范围与输入；批量作业可显式共享起始时冻结的参数。"""
    from config.settings import require_signal_model_ready
    require_signal_model_ready()
    if source not in {"stock_signal", "stock_deep_signal", "strategy_signals"}:
        raise ValueError("未支持的信号来源")
    if scope in {"single", "watchlist"} and codes is None:
        raise ValueError("局部运行必须显式给出股票范围")
    store = store or SnapshotStore()
    with get_conn(readonly=True) as conn:
        conn.execute("BEGIN")
        supplied_parameters = parameters is not None
        parameters = json.loads(canonical_json(parameters)) if supplied_parameters else freeze_effective_parameters(conn)
        runtime = dict(runtime or {})
        runtime.setdefault("reuse_scores", True)
        runtime.setdefault("history_mode", False)
        runtime.setdefault("lookback", parameters["strategy_config"]["DEEP_LOOKBACK"] if source == "stock_deep_signal" else 300)
        runtime.setdefault("max_rules", 50)
        parameters["runtime"] = runtime
        if source == "stock_deep_signal" and not supplied_parameters:
            from strategy.stock_deep import _EMA20_AUX
            parameters["strategy_config"]["DEEP_EMA20_AUX"] = dict(_EMA20_AUX)
        if source == "stock_signal":
            universe = [r[0] for r in conn.execute("SELECT DISTINCT code FROM daily_price WHERE trade_date=? ORDER BY code", (scan_date,))]
        else:
            universe = [r[0] for r in conn.execute("SELECT DISTINCT d.code FROM daily_price d JOIN stock_info i ON i.code=d.code WHERE i.is_active=1 AND d.trade_date<=? ORDER BY d.code", (scan_date,))]
        codes = universe if codes is None else sorted(set(codes))
        if scope == "all" and set(codes) != set(universe):
            raise ValueError("局部股票范围不能声明为全市场运行")
        manifest = capture_inputs(conn, scan_date, codes, source=source,
                                  history_mode=runtime["history_mode"],
                                  lookback=runtime["lookback"], store=store,
                                  reuse_scores=runtime["reuse_scores"])
        present = {r[0] for r in conn.execute("SELECT code FROM daily_price WHERE trade_date=?", (scan_date,))}
        missing = sorted(set(codes) - present)
        quality = {
            "point_in_time_complete": scan_date == date.today().isoformat(),
            "metadata_source": "current_stock_info", "missing_price_codes": missing,
            "data_ready": bool(present),
            "cross_section_complete": scope == "all" and bool(present) and not missing,
            "source": source,
        }
        return RunContext.create(scan_date=scan_date, as_of=scan_date, scope=scope,
                                 codes=codes, parameters=parameters, manifest=manifest,
                                 version=code_version(), quality=quality, run_type=run_type)


def _rank_long_signals(records, effective):
    """整次横截面完成后排名；并列使用代码打破，不依赖生成顺序。"""
    pool = [r for r in records if r.get("horizon") == "long" and
            (int(r.get("long_mask") or 0) & 7) == 7 and
            all(r.get(k) is not None and math.isfinite(float(r[k])) for k in ("vol60", "dd250"))]
    denominator = max(1, len(pool) - 1)
    for row in pool:
        row["long_rank_key"] = 0.0
    for feature, weight in (("vol60", "long_rank_w_vol"), ("dd250", "long_rank_w_dd")):
        for rank, row in enumerate(sorted(pool, key=lambda r: (r[feature], r["code"]))):
            row["long_rank_key"] += effective[weight] * rank / denominator


def _snapshot_rules(conn, maximum):
    from backtest.rule_engine import _iter_condition_items
    rules, errors = [], []
    for row in conn.execute("SELECT * FROM strategy_rules ORDER BY fitness DESC,id"):
        try:
            conditions = json.loads(row["conditions"] or "{}")
            if _iter_condition_items(conditions):
                rules.append({**dict(row), "conditions": conditions})
        except (TypeError, ValueError) as exc:
            errors.append({"rule_id": row["id"], "code": None, "error": str(exc), "type": type(exc).__name__})
        if maximum and maximum > 0 and len(rules) >= maximum:
            break
    return rules, errors


def calculate_signal_run(context, *, store=None, progress_callback=None, metadata=None):
    """纯计算：读取旧快照不碰当前行情，版本不匹配时明确拒绝精确重放。"""
    if json.loads(context.code_version_json) != code_version():
        raise SnapshotUnavailable("代码版本已改变，请在对应代码版本下精确重放")
    metadata = metadata if metadata is not None else {}
    manifest = json.loads(context.manifest_json)
    parameters = context.parameters
    source = manifest["source"]
    codes = json.loads(context.scope_json)
    records, failures = [], []
    runtime = parameters["runtime"]
    with open_snapshot(manifest, store) as conn, input_connection(conn), parameter_context(parameters):
        from core.repository.price_repo import get_daily_price
        from core.market_regime import compute_regime_on
        metadata["market_state"] = compute_regime_on(conn, context.scan_date)
        configs, effective = parameters["strategy_config"], parameters["effective"]
        info = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM stock_info")}
        if source == "stock_signal":
            from core.sync import _build_signal_records
            from core.repository.lhb_repo import get_lhb_map_for_date
            from strategy.adaptive_weights import detect_market_state
            market = detect_market_state()
            threshold = effective["sig_threshold"]
            if configs["ADAPTIVE_WEIGHTS_ENABLED"] and market["volatility"] > configs["HIGH_VOL_THRESHOLD"]:
                threshold = effective["high_vol_sig_threshold"]
            avg = conn.execute("SELECT avg_pct FROM market_snapshot").fetchone()[0]
            surge, first = configs["SURGE_BREAKOUT"], configs["FIRST_REVERSAL"]
            lhb = get_lhb_map_for_date(context.scan_date)
        elif source == "strategy_signals":
            from backtest.rule_engine import _evaluate_conditions_vec
            from strategy.factor_lib import compute_all_factors
            rules, errors = _snapshot_rules(conn, runtime["max_rules"])
            failures.extend(errors)
            metadata["rules"] = len(rules)
        stale = set(json.loads(context.quality_json).get("missing_price_codes", []))
        for index, code in enumerate(codes):
            try:
                # 停牌或缺数据不能拿旧日候选贴上本日标签。
                if code in stale:
                    continue
                if source == "stock_deep_signal":
                    from strategy.stock_deep import _scan_one
                    candidate = _scan_one(code, runtime["lookback"], context.as_of, conn=conn, strict=True)
                    if candidate:
                        records.append({**candidate, "scan_date": context.scan_date})
                else:
                    df = get_daily_price(code, end_date=context.as_of)
                    if df is None or len(df) < 30 or str(df.index[-1].date()) != context.scan_date:
                        continue
                    if source == "stock_signal":
                        if not runtime["history_mode"]:
                            start = (date.fromisoformat(context.scan_date) - timedelta(days=700)).isoformat()
                            window = df.loc[start:]
                            if len(window) >= 260:
                                df = window
                        meta = info.get(code, {})
                        records.extend(_build_signal_records(
                            df, code, meta.get("name") or code, meta.get("total_shares"),
                            threshold, effective["short_stop_loss"], effective["short_take_profit"],
                            lhb_row=lhb.get(code), scan_date=context.scan_date,
                            reuse_scores=runtime["reuse_scores"], circ_shares=meta.get("circ_shares"),
                            surge_ok=bool(surge["enabled"]) and (avg is None or avg < surge.get("max_market_pct", 1.0)),
                            mid_weak_ok=avg is None or avg > effective["mid_weak_market_gate"],
                            first_rev_ok=bool(first["enabled"]),
                            first_rev_market_pct=avg if first.get("max_market_pct") is not None else None))
                    elif rules:
                        factors = compute_all_factors(df).tail(1)
                        for rule in rules:
                            result = _evaluate_conditions_vec(factors, rule["conditions"]).iloc[-1]
                            if bool(result["passed"]):
                                records.append({"scan_date": context.scan_date, "code": code,
                                                "rule_id": rule["id"], "rule_name": rule["rule_name"],
                                                "horizon": rule.get("horizon") or "short",
                                                "confidence": round(float(result["strength"]), 4)})
            except Exception as exc:
                failures.append({"code": code, "error": str(exc), "type": type(exc).__name__})
            finally:
                if progress_callback and (index % 100 == 0 or index + 1 == len(codes)):
                    progress_callback(int((index + 1) / max(1, len(codes)) * 100), f"冻结信号计算 {index + 1}/{len(codes)}")
        if source == "stock_signal" and not failures:
            _rank_long_signals(records, effective)
        if runtime.get("strategy_keys"):
            from core.repository.signal_repo import STRATEGY_KEYS
            allowed = set(runtime["strategy_keys"])
            records = [r for r in records if STRATEGY_KEYS.get(r.get("strategy")) in allowed]
    return records, failures


def execute_signal_run(context, *, store=None, progress_callback=None):
    started = time.perf_counter()
    metadata = {}
    try:
        records, failures = calculate_signal_run(context, store=store, progress_callback=progress_callback, metadata=metadata)
        codes = set(json.loads(context.scope_json))
        all_failed = bool(codes) and codes.issubset({r.get("code") for r in failures})
        status = "failed" if all_failed else ("partial" if failures else "complete")
        if all_failed:
            records = []
    except Exception as exc:
        records = []
        failures = [{"code": None, "error": str(exc), "type": type(exc).__name__}]
        status = "failed"
    quality = json.loads(context.quality_json)
    quality.update(metadata)
    quality["failures"] = failures
    quality["cross_section_complete"] = quality.get("cross_section_complete", False) and not failures
    context = replace(context, quality_json=canonical_json(quality))
    source = json.loads(context.manifest_json)["source"]
    run_id = persist_signal_run(context, records, source=source, status=status, store=store)
    return {"run_id": run_id, "scan_date": context.scan_date, "signals": len(records),
            "codes": len(json.loads(context.scope_json)), "status": status, "failures": failures,
            "elapsed_s": round(time.perf_counter() - started, 3), "target": context.scope, **metadata}


def recalculate_frozen_scores(context, *, store=None, progress_callback=None):
    """全量研究分由快照计算，写入只发生在只读计算上下文之外。"""
    from core.sync import _calculate_strategy_scores
    from core.repository.price_repo import get_daily_price, update_strategy_scores_batch
    if json.loads(context.code_version_json) != code_version():
        raise SnapshotUnavailable("评分代码版本已改变")
    codes = json.loads(context.scope_json)
    result = {"success": 0, "failed": 0, "days": 0, "failures": []}
    with open_snapshot(json.loads(context.manifest_json), store) as conn:
        for i, code in enumerate(codes):
            try:
                with input_connection(conn), parameter_context(context.parameters):
                    df = get_daily_price(code, end_date=context.as_of)
                    if len(df) < 30:
                        continue
                    scores = _calculate_strategy_scores(df)
                update_strategy_scores_batch(code, scores)
                result["success"] += 1
                result["days"] += len(scores)
            except Exception as exc:
                result["failed"] += 1
                result["failures"].append({"code": code, "error": str(exc)})
            if progress_callback:
                progress_callback(int((i + 1) / max(1, len(codes)) * 100), f"冻结历史评分 {i + 1}/{len(codes)}")
    return result


def replay_signal_dates(dates, *, source="stock_signal", strategy_keys=None,
                        scope="all", codes=None, store=None, progress_callback=None,
                        parameters=None):
    """只重放显式日期；不发布、不投影旧表、不建立前向交易成绩。"""
    dates = sorted(set(dates))
    if parameters is None:
        with get_conn(readonly=True) as conn:
            parameters = freeze_effective_parameters(conn)
        if source == "stock_deep_signal":
            from strategy.stock_deep import _EMA20_AUX
            parameters["strategy_config"]["DEEP_EMA20_AUX"] = dict(_EMA20_AUX)
    results = []
    for i, day in enumerate(dates):
        context = prepare_signal_run(day, scope=scope, codes=codes, source=source,
            store=store, parameters=parameters, run_type="replay",
            runtime={"reuse_scores": False, "history_mode": True,
                     "strategy_keys": sorted(strategy_keys or [])})
        result = execute_signal_run(context, store=store)
        results.append(result)
        if progress_callback:
            progress_callback(int((i + 1) / max(1, len(dates)) * 100), f"历史重放 {day}：{result['status']}")
    return results


def replay_signal_range(start=None, end=None, *, source="stock_signal",
                        strategy_keys=None, dry_run=False):
    """旧回补工具的统一适配；干跑仅返回日期，不写库或生成输入块。"""
    with get_conn(readonly=True) as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price WHERE (? IS NULL OR trade_date>=?) "
            "AND (? IS NULL OR trade_date<=?) ORDER BY trade_date", (start, start, end, end))]
    if dry_run:
        return {"run_type": "replay", "dates": dates, "dry_run": True}
    return {"run_type": "replay", "runs": replay_signal_dates(dates, source=source, strategy_keys=strategy_keys)}
