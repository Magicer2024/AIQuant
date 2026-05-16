"""
services/strategy_lab_service.py —— 策略实验室业务逻辑
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from strategy.factor_lib import FACTOR_REGISTRY, get_factor_category, get_factor_names_by_category, calc_all_ic
from strategy.rule_miner import load_template_library
from core.db import (
    get_active_rules, degrade_rule as db_degrade_rule,
    get_current_active_strategies, get_conn,
)
from datetime import datetime as _dt

logger = logging.getLogger(__name__)


def get_overview() -> dict:
    active_rules = get_active_rules()
    active_strategies = get_current_active_strategies()
    return {
        "rule_count": len(active_rules),
        "active_strategy_count": len(active_strategies),
        "factor_count": len(FACTOR_REGISTRY),
        "last_updated": datetime.now().isoformat(),
    }


def get_factors(category: Optional[str] = None) -> list:
    factors = []
    for name, meta in FACTOR_REGISTRY.items():
        if category and meta.get("category") != category:
            continue
        factors.append({
            "name": name,
            "category": meta.get("category", "unknown"),
            "norm": meta.get("norm", "price"),
            "description": meta.get("description", ""),
        })
    return factors


def get_factor_categories() -> list:
    cats = set()
    for meta in FACTOR_REGISTRY.values():
        cats.add(meta.get("category", "unknown"))
    return sorted(cats)


def get_ic_analysis(factor_name: Optional[str] = None) -> dict:
    import pandas as pd

    conn = get_conn()
    try:
        df = pd.read_sql_query(
            "SELECT code, trade_date, close FROM daily_price ORDER BY trade_date DESC LIMIT 50000",
            conn,
        )
    finally:
        conn.close()

    if df.empty:
        return {"error": "No price data available"}

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["code", "trade_date"])
    df["forward_return"] = df.groupby("code")["close"].transform(
        lambda x: x.shift(-5) / x - 1
    )

    from strategy.factor_lib import compute_all_factors
    factor_df = compute_all_factors(df, index_close=None)

    ic_results = {}
    for col in factor_df.columns:
        if col in FACTOR_REGISTRY:
            valid = factor_df[col].notna() & df["forward_return"].notna()
            if valid.sum() < 20:
                continue
            from strategy.factor_lib import calc_factor_ic
            ic = calc_factor_ic(factor_df.loc[valid, col], df.loc[valid, "forward_return"])
            ic_results[col] = round(ic, 4)

    if factor_name:
        return {"factor": factor_name, "ic": ic_results.get(factor_name)}

    sorted_ic = sorted(ic_results.items(), key=lambda x: abs(x[1]), reverse=True)
    return {
        "factors": [{"name": k, "ic": v, "category": get_factor_category(k)} for k, v in sorted_ic],
    }


def _get_degraded_rules() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_rules WHERE is_active=0 ORDER BY fitness DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_rules(status_filter: Optional[str] = None) -> list:
    if status_filter == "active":
        rules = get_active_rules()
    elif status_filter == "degraded":
        rules = _get_degraded_rules()
    else:
        rules = get_active_rules() + _get_degraded_rules()

    result = []
    for r in rules:
        result.append({
            "id": r.get("id"),
            "name": r.get("rule_name", r.get("name", "")),
            "rule_type": r.get("rule_type", ""),
            "conditions": r.get("conditions", ""),
            "fitness": r.get("fitness", 0),
            "status": r.get("status", "active"),
            "created_at": r.get("created_at", ""),
            "generation": r.get("generation", 0),
        })
    return result


def get_active_strategies_detail() -> list:
    strategies = get_current_active_strategies()
    active_rules = {r["id"]: r for r in get_active_rules()}
    result = []
    for s in strategies:
        rule = active_rules.get(s.get("rule_id"))
        result.append({
            "rank": s.get("rank", 0),
            "rule_id": s.get("rule_id"),
            "rule_name": s.get("rule_name", ""),
            "rule_type": rule.get("rule_type", "") if rule else "",
            "score_60": s.get("window_60_score", 0),
            "score_120": s.get("window_120_score", 0),
            "final_score": s.get("final_score", 0),
            "select_date": s.get("select_date", ""),
        })
    return result


def activate_rule(rule_id: int) -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE strategy_rules SET is_active=1, degraded_at=NULL WHERE id=?",
            (rule_id,),
        )
    return {"success": True, "rule_id": rule_id, "action": "activated"}


def degrade_rule_action(rule_id: int) -> dict:
    db_degrade_rule(rule_id)
    return {"success": True, "rule_id": rule_id, "action": "degraded"}


# ── Qlib data loading ──────────────────────────────────

def _load_ohlcv_from_qlib(codes: List[str], start_date: str) -> "Optional[pd.DataFrame]":
    """Try loading OHLCV data via Qlib DataHandler.

    Returns DataFrame with columns [code, trade_date, open, high, low, close, volume]
    or None if Qlib data is unavailable.
    """
    import pandas as pd
    try:
        from qlib_engine import init_qlib
        from qlib.data import D

        init_qlib()

        end_date = datetime.now().strftime("%Y-%m-%d")
        instruments = [c for c in codes if D.features(
            [c], ["$close"], start_time=start_date, end_time=end_date
        ).size > 0]

        if not instruments:
            return None

        fields = ["$open", "$high", "$low", "$close", "$volume"]
        df = D.features(instruments, fields, start_time=start_date, end_time=end_date)

        if df is None or df.empty:
            return None

        df = df.reset_index()
        df.columns = ["trade_date", "code"] + [f.lstrip("$") for f in fields]
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df[["code", "trade_date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.info("Qlib data not available: %s", e)
        return None


def _load_ohlcv_from_sqlite(codes: List[str], start_date: str) -> "pd.DataFrame":
    """Fallback: load OHLCV data from SQLite daily_price table."""
    import pandas as pd
    with get_conn() as conn:
        placeholders = ",".join(["?" for _ in codes])
        rows = conn.execute(
            f"SELECT code, trade_date, open, high, low, close, volume FROM daily_price "
            f"WHERE code IN ({placeholders}) AND trade_date >= ? "
            f"ORDER BY code, trade_date",
            codes + [start_date],
        ).fetchall()

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


# ── 数据加载辅助 ──────────────────────────────────

def _load_stock_data_for_pipeline(max_stocks: int = 100) -> tuple:
    """为 Phase 1/2/4 加载股票行情数据，返回 (stock_data, factor_df, forward_returns, index_df)

    优先使用 Qlib DataHandler，数据不可用时回退到 SQLite daily_price 表。
    """
    import time
    import pandas as pd
    from core.db import get_all_stocks
    from strategy.factor_lib import compute_all_factors

    stocks_df = get_all_stocks()
    codes = stocks_df["code"].tolist()[:max_stocks]
    print(f"[Pipeline] 开始加载 {len(codes)} 只股票数据...")

    # Try Qlib first, fall back to SQLite
    df = _load_ohlcv_from_qlib(codes, "2024-01-01")
    if df is None or df.empty:
        print("[Pipeline] Qlib 数据不可用，回退到 SQLite daily_price")
        df = _load_ohlcv_from_sqlite(codes, "2024-01-01")

    if df.empty:
        print("[Pipeline] 没有找到行情数据")
        return {}, pd.DataFrame(), pd.Series(), pd.DataFrame()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["code", "trade_date"])
    print(f"[Pipeline] 加载 {len(df)} 条行情记录")

    stock_data = {}
    t0 = time.time()
    for code, group in df.groupby("code"):
        if len(group) < 60:
            continue
        group = group.sort_values("trade_date")
        factor_df = compute_all_factors(group, index_close=None)
        stock_data[code] = (group, factor_df)
    print(f"[Pipeline] 因子计算完成: {len(stock_data)} 只股票 (耗时 {time.time()-t0:.1f}s)")

    factor_df_all = pd.concat(
        [fd for _, fd in stock_data.values()], keys=list(stock_data.keys()), names=["code", "idx"]
    ) if stock_data else pd.DataFrame()

    fr_list = []
    for code, (price_df, _) in stock_data.items():
        fr = price_df["close"].shift(-5) / price_df["close"] - 1
        fr.name = "forward_return"
        fr_list.append(fr)
    forward_returns = pd.concat(
        fr_list, keys=list(stock_data.keys()), names=["code", "idx"]
    ) if fr_list else pd.Series(dtype=float)

    # Index data — try Qlib first
    index_df = _load_index_data("2024-01-01")

    return stock_data, factor_df_all, forward_returns, index_df


def _load_index_data(start_date: str) -> "pd.DataFrame":
    """Load index (benchmark) data, trying Qlib first then SQLite."""
    import pandas as pd

    # Try Qlib
    try:
        from qlib_engine import init_qlib
        from qlib.data import D
        init_qlib()
        for benchmark in ["SH000300", "000001.SH", "000001"]:
            try:
                idx = D.features([benchmark], ["$close"],
                                 start_time=start_date,
                                 end_time=datetime.now().strftime("%Y-%m-%d"))
                if idx is not None and not idx.empty:
                    idx = idx.reset_index()
                    idx.columns = ["trade_date", "code", "close"]
                    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
                    return idx[["trade_date", "close"]]
            except Exception:
                continue
    except Exception:
        pass

    # Fallback to SQLite
    try:
        with get_conn() as conn:
            for code in ["000001.SH", "000001"]:
                idx_rows = conn.execute(
                    "SELECT trade_date, close FROM index_daily WHERE code=? AND trade_date >= ? ORDER BY trade_date",
                    (code, start_date),
                ).fetchall()
                if idx_rows:
                    idx_df = pd.DataFrame([dict(r) for r in idx_rows])
                    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"])
                    return idx_df
    except Exception:
        pass

    return pd.DataFrame()


# ── Phase 流水线 ──────────────────────────────────

def run_phase1_mine() -> dict:
    from strategy.rule_miner import RuleMiner

    start = _dt.now()
    stock_data, factor_df, forward_returns, _ = _load_stock_data_for_pipeline(80)

    if not stock_data or forward_returns.empty:
        return {"phase": 1, "error": "Insufficient data", "stocks_loaded": len(stock_data)}

    miner = RuleMiner()
    rules = miner.run_phase1(stock_data, forward_returns, factor_df)

    elapsed = (_dt.now() - start).total_seconds()
    return {
        "phase": 1,
        "stocks_loaded": len(stock_data),
        "rules_generated": len(rules),
        "elapsed_seconds": round(elapsed, 1),
        "top_rules": [
            {"name": r.name if hasattr(r, "name") else r.get("name", ""),
             "rule_type": r.rule_type if hasattr(r, "rule_type") else r.get("rule_type", ""),
             "fitness": round(r.fitness if hasattr(r, "fitness") else r.get("fitness", 0), 4)}
            for r in rules[:10]
        ],
    }


def run_phase2_evolve(rule_ids: Optional[List[int]] = None, generations: int = 20) -> dict:
    from strategy.genetic_evolver import GeneticEvolver
    from strategy.rule_miner import StrategyRule, RuleCondition
    import json as _json

    start = _dt.now()
    stock_data, factor_df, forward_returns, _ = _load_stock_data_for_pipeline(60)

    if not stock_data:
        return {"phase": 2, "error": "Insufficient data", "stocks_loaded": 0}

    seed_rules = None
    if rule_ids:
        active_rules = get_active_rules()
        loaded = []
        for r in active_rules:
            if r["id"] in rule_ids:
                try:
                    conditions_raw = r.get("conditions", "[]")
                    conds_data = _json.loads(conditions_raw) if isinstance(conditions_raw, str) else conditions_raw
                    conditions = [RuleCondition(**c) for c in conds_data]
                    loaded.append(StrategyRule(
                        name=r.get("rule_name", r.get("name", "")),
                        rule_type=r.get("rule_type", "T1"),
                        conditions=conditions,
                    ))
                except Exception:
                    continue
        if loaded:
            seed_rules = loaded

    evolver = GeneticEvolver(seed_population=seed_rules, max_generations=generations)
    result = evolver.run_evolution(stock_data, factor_df, forward_returns)

    elapsed = (_dt.now() - start).total_seconds()
    return {
        "phase": 2,
        "stocks_loaded": len(stock_data),
        "generations": generations,
        "best_fitness": round(result[-1].get("fitness", 0) if result else 0, 4),
        "population_size": len(result),
        "elapsed_seconds": round(elapsed, 1),
    }


def run_lgbm_train() -> dict:
    import pandas as pd
    from strategy.lgbm_ranker import get_ranker
    from core.db import get_signals_for_training

    start = _dt.now()

    # 1. 自动回填未打标的信号
    _backfill_signal_labels()

    # 2. 查询已打标信号
    signals = get_signals_for_training("2024-01-01", _dt.now().strftime("%Y-%m-%d"))

    if not signals:
        return {"phase": 3, "error": "No training signals available", "samples": 0}

    ranker = get_ranker()
    success = ranker.train()

    elapsed = (_dt.now() - start).total_seconds()
    return {
        "phase": 3,
        "samples": len(signals),
        "trained": success,
        "elapsed_seconds": round(elapsed, 1),
    }


def _backfill_signal_labels(horizon: int = 5):
    """回填 strategy_signals 表中 label_return 和 is_win 字段。

    优先使用 Qlib 数据，回退到 SQLite daily_price。
    """
    import pandas as pd

    with get_conn() as conn:
        unlabeled = conn.execute("""
            SELECT id, trade_date, code
            FROM strategy_signals
            WHERE label_return IS NULL
            ORDER BY trade_date
        """).fetchall()

    if not unlabeled:
        print(f"[Phase3] 所有信号已打标，无需回填")
        return

    print(f"[Phase3] 需要回填 {len(unlabeled)} 条信号的 label_return")

    codes = list(set(r["code"] for r in unlabeled))
    dates = list(set(r["trade_date"] for r in unlabeled))
    max_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

    # Try Qlib first, fall back to SQLite
    price_df = _load_prices_for_backfill(codes, min(dates), max_date)

    if price_df is None or price_df.empty:
        print("[Phase3] 无法加载价格数据，跳过回填")
        return

    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    price_df = price_df.sort_values(["code", "trade_date"])

    # Load index data
    index_close = _load_index_dict()

    # 逐条计算 label_return
    updates = []
    backfilled = 0
    for sig in unlabeled:
        sig_id = sig["id"]
        sig_date = pd.Timestamp(sig["trade_date"])
        sig_code = sig["code"]

        stock_prices = price_df[
            (price_df["code"] == sig_code) &
            (price_df["trade_date"] >= sig_date)
        ].sort_values("trade_date")

        if len(stock_prices) <= horizon:
            continue

        entry_price = stock_prices.iloc[0]["close"]
        exit_price = stock_prices.iloc[horizon]["close"]
        stock_return = exit_price / entry_price - 1

        sig_date_str = sig["trade_date"]
        exit_date = stock_prices.iloc[horizon]["trade_date"]
        exit_date_str = str(exit_date.date()) if hasattr(exit_date, "date") else str(exit_date)

        index_return = 0.0
        if index_close:
            idx_entry = index_close.get(sig_date_str)
            idx_exit = index_close.get(exit_date_str)
            if idx_entry and idx_exit and idx_entry > 0:
                index_return = idx_exit / idx_entry - 1

        label_return = stock_return - index_return
        is_win = 1 if label_return > 0 else 0
        updates.append((round(label_return, 6), is_win, sig_id))
        backfilled += 1

    if updates:
        from core.db import update_signal_labels
        update_signal_labels(updates)
        print(f"[Phase3] 回填完成: {backfilled}/{len(unlabeled)} 条信号已打标")
    else:
        print(f"[Phase3] 无有效数据可回填 (所有信号价格数据不足)")


def _load_prices_for_backfill(codes: List[str], min_date: str, max_date: str) -> "pd.DataFrame":
    """Load close prices for label backfill. Tries Qlib first, then SQLite."""
    import pandas as pd

    # Try Qlib
    try:
        from qlib_engine import init_qlib
        from qlib.data import D
        init_qlib()
        df = D.features(codes, ["$close"], start_time=min_date, end_time=max_date)
        if df is not None and not df.empty:
            df = df.reset_index()
            df.columns = ["trade_date", "code", "close"]
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            return df
    except Exception:
        pass

    # Fallback to SQLite
    try:
        with get_conn() as conn:
            placeholders = ",".join(["?" for _ in codes])
            rows = conn.execute(f"""
                SELECT code, trade_date, close
                FROM daily_price
                WHERE code IN ({placeholders})
                  AND trade_date >= ?
                  AND trade_date <= ?
                ORDER BY code, trade_date
            """, (codes, min_date, max_date)).fetchall()

        if rows:
            return pd.DataFrame([dict(r) for r in rows])
    except Exception:
        pass

    return pd.DataFrame()


def _load_index_dict() -> dict:
    """Load index close prices as {date_str: close} dict for excess return calc."""
    try:
        from qlib_engine import init_qlib
        from qlib.data import D
        init_qlib()
        for benchmark in ["SH000300", "000001.SH"]:
            try:
                idx = D.features([benchmark], ["$close"],
                                 start_time="2020-01-01",
                                 end_time=datetime.now().strftime("%Y-%m-%d"))
                if idx is not None and not idx.empty:
                    idx = idx.reset_index()
                    return {str(row["datetime"]).split("T")[0]: row["$close"]
                            for _, row in idx.iterrows()}
            except Exception:
                continue
    except Exception:
        pass

    # Fallback to SQLite
    try:
        with get_conn() as conn:
            idx_rows = conn.execute("""
                SELECT trade_date, close FROM index_daily
                WHERE code = '000001.SH' OR code = '000001'
                ORDER BY trade_date
            """).fetchall()
        if idx_rows:
            return {str(r["trade_date"]): r["close"] for r in idx_rows}
    except Exception:
        pass

    return {}


def run_full_pipeline() -> dict:
    """一键执行完整流水线: P1 规则挖掘 → P2 遗传进化 → P3 LGBM训练 → P4 动态选股"""
    start = _dt.now()
    results = {}

    phases = [
        ("phase1", "规则挖掘", run_phase1_mine),
        ("phase2", "遗传进化", run_phase2_evolve),
        ("phase3", "LGBM训练", run_lgbm_train),
        ("phase4", "动态选股", run_phase4_select),
    ]

    for phase_key, phase_name, fn in phases:
        t0 = _dt.now()
        try:
            result = fn()
            result["status"] = "success"
            results[phase_key] = result
            print(f"[FullPipeline] {phase_name} 完成 ({(_dt.now() - t0).total_seconds():.1f}s)")
        except Exception as e:
            logger.error(f"[FullPipeline] {phase_name} 失败: {e}")
            results[phase_key] = {"status": "failed", "error": str(e)}

    elapsed = (_dt.now() - start).total_seconds()
    return {
        "pipeline": "full",
        "status": "success",
        "elapsed_seconds": round(elapsed, 1),
        "phases": results,
    }


def run_phase4_select() -> dict:
    from strategy.dynamic_selector import DynamicSelector

    start = _dt.now()
    stock_data, factor_df, forward_returns, index_df = _load_stock_data_for_pipeline(100)

    if not stock_data:
        return {"phase": 4, "error": "No stock data available", "stocks_loaded": 0}

    selector = DynamicSelector()
    selections = selector.run_weekly_selection(stock_data, index_df, factor_df)

    elapsed = (_dt.now() - start).total_seconds()
    return {
        "phase": 4,
        "stocks_loaded": len(stock_data),
        "strategies_selected": len(selections),
        "top_selections": [
            {"code": s.get("code", ""), "rule_name": s.get("rule_name", ""),
             "score": s.get("score", 0)}
            for s in selections[:10]
        ],
        "elapsed_seconds": round(elapsed, 1),
    }
