"""
dynamic_selector.py —— Phase 4: 动态策略选择器
===============================================
功能：
- 双窗口回测 (60日 + 120日) 评分
- 约束过滤 (最低交易次数、最大回撤等)
- 相关性去重 (signal_overlap)
- 动态 K 选择 (k_min ~ k_max)
- 三级后备机制 (L1/L2/L3)
- 每日信号生成 + Phase 3 置信度融合
"""
import json
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

from strategy.rule_miner import RuleMiner, StrategyRule, RuleCondition, load_template_library
from strategy.lgbm_ranker import get_ranker, fuse_stock_signals, build_meta_features
from core.db import (
    get_active_rules, upsert_active_strategies, get_current_active_strategies,
    save_strategy_signal, get_conn,
)

logger = logging.getLogger(__name__)

# Try to import signal_overlap from genetic_evolver (Task 4 — may not exist yet)
try:
    from strategy.genetic_evolver import signal_overlap as _ge_signal_overlap
    _HAS_GE_SIGNAL_OVERLAP = True
except ImportError:
    _ge_signal_overlap = None
    _HAS_GE_SIGNAL_OVERLAP = False


# ═══════════════════════════════════════════════════════════════
# Scoring & Market State
# ═══════════════════════════════════════════════════════════════

def window_score(perf: dict) -> float:
    """Single window composite score.

    Weights:
        0.2 * annual_return% / 100 + 0.25 * win_rate% / 100
        + 0.3 * sharpe - 0.2 * max_drawdown% / 100 + 0.05 * calmar
    """
    ann_ret = perf.get("annual_return", 0) or 0
    wr = perf.get("win_rate", 0) or 0
    sharpe = perf.get("sharpe_ratio", 0) or 0
    mdd = abs(perf.get("max_drawdown", 0) or 0)

    calmar = ann_ret / max(mdd, 1e-9)

    return (
        0.2 * ann_ret / 100.0
        + 0.25 * wr / 100.0
        + 0.3 * sharpe
        - 0.2 * mdd / 100.0
        + 0.05 * calmar
    )


def is_bear_market(index_df: pd.DataFrame) -> bool:
    """Check bear-market regime.

    Returns True when:
        - 60-day MA is declining (latest < 20 days ago)
        - Price is below 250-day MA (or 60-day MA if insufficient history)
    """
    if index_df is None or len(index_df) < 120:
        return False
    close = index_df["close"]
    ma60 = close.rolling(60).mean()
    if len(close) >= 250:
        ma250 = close.rolling(250).mean()
    else:
        ma250 = ma60
    return bool((ma60.iloc[-1] < ma60.iloc[-20]) and (close.iloc[-1] < ma250.iloc[-1]))


# ═══════════════════════════════════════════════════════════════
# Constraint helpers
# ═══════════════════════════════════════════════════════════════

def _calc_max_consecutive_loss(equity: List[float]) -> int:
    """Calculate maximum consecutive loss days from an equity curve."""
    if len(equity) < 2:
        return 0
    max_streak = 0
    cur = 0
    for i in range(1, len(equity)):
        if equity[i] < equity[i - 1]:
            cur += 1
            if cur > max_streak:
                max_streak = cur
        else:
            cur = 0
    return max_streak


def apply_constraints(rule_perf: dict) -> Tuple[bool, str]:
    """Apply mandatory constraints to a rule's performance.

    Returns (passed, reason).  If passed is False the rule should be excluded.

    Checks:
        1. Minimum total trades >= 3 (avoid noise from per-stock normalization)
        2. Max drawdown < 20 %
        3. 60-day return > -5 %
        4. (Max consecutive loss days <= 12 — SKIPPED: evaluate_rule does
           not return equity_curve data. Requires backtester upgrade.)
    """
    # 1. Minimum total trades — use absolute count instead of per-stock ratio.
    #    In short windows (60d/120d), per-stock trades are often 0~1 which
    #    was filtering out all rules. A total of 3+ trades is a reasonable
    #    noise floor.
    total_trades = rule_perf.get("total_trades", 0) or 0
    if total_trades < 3:
        return False, f"insufficient_trades(total={total_trades}<3)"

    # 2. Max drawdown
    mdd = abs(rule_perf.get("max_drawdown", 0) or 0)
    if mdd >= 20.0:
        return False, f"max_drawdown_exceeded({mdd:.1f}%>=20%)"

    # 3. 60-day return floor
    ret_60 = rule_perf.get("total_return", 0) or 0
    if ret_60 <= -5.0:
        return False, f"return_too_low({ret_60:.1f}%<=-5%)"

    # 4. Consecutive-loss check skipped — RuleMiner.evaluate_rule() does
    #    not return equity_curve data. Requires backtester upgrade to
    #    surface per-trade equity curves before this check can be enabled.

    return True, "ok"


def get_turnover_penalty(rule_perf: dict) -> float:
    """Return a multiplier (<= 1.0) for turnover penalty.

    Estimates turnover from total_trades / sample_count since the backtester
    does not yet surface turnover_rate directly.

    - estimated turnover > 5.0  → eliminated (return 0.0)
    - 3.0 < estimated turnover <= 5.0 → score * 0.7
    - otherwise → 1.0
    """
    # NOTE: turnover_rate is not returned by RuleMiner.evaluate_rule().
    # Estimate from trades-per-stock as a proxy until backtester is upgraded.
    total_trades = rule_perf.get("total_trades", 0) or 0
    sample_count = rule_perf.get("sample_count", 1) or 1
    if sample_count < 1:
        return 1.0
    est_turnover = total_trades / sample_count
    if est_turnover > 5.0:
        return 0.0
    if 3.0 < est_turnover <= 5.0:
        return 0.7
    return 1.0


# ═══════════════════════════════════════════════════════════════
# Signal strength
# ═══════════════════════════════════════════════════════════════

def _calc_signal_strength(rule: StrategyRule, factor_df: pd.DataFrame) -> float:
    """Calculate signal strength as normalized deviation from threshold.

    For each condition: |value - threshold| / (|threshold| + eps), capped at 1.0.
    Returns 1.0 - mean deviation (higher = signal closer to threshold = more reliable).
    """
    if not rule.conditions or factor_df is None or factor_df.empty:
        return 0.5
    strengths = []
    latest = factor_df.iloc[-1]
    for cond in rule.conditions:
        if cond.factor not in latest.index:
            strengths.append(0.5)
            continue
        val = float(latest[cond.factor])
        thresh = abs(cond.threshold) if abs(cond.threshold) > 1e-9 else 0.5
        dev = min(abs(val - cond.threshold) / thresh, 1.0)
        strengths.append(1.0 - dev)
    return float(np.mean(strengths)) if strengths else 0.5


# ═══════════════════════════════════════════════════════════════
# Signal-overlap fallback (when genetic_evolver is unavailable)
# ═══════════════════════════════════════════════════════════════

def _compute_signal_overlap(rule_a: StrategyRule, rule_b: StrategyRule,
                             stock_data: Dict) -> float:
    """Compute signal overlap between two strategies across all stocks.

    Uses genetic_evolver.signal_overlap per stock when available (operates on a
    single factor_df), then averages.  Falls back to a pure-Python Jaccard
    implementation otherwise.
    """
    if not stock_data:
        return 0.0
    overlaps = []
    for _code, (_price_df, factor_df) in stock_data.items():
        try:
            if _HAS_GE_SIGNAL_OVERLAP:
                overlap = float(_ge_signal_overlap(rule_a, rule_b, factor_df))
            else:
                sig_a = rule_a.get_buy_signal(factor_df).astype(bool)
                sig_b = rule_b.get_buy_signal(factor_df).astype(bool)
                both = (sig_a & sig_b).sum()
                either = (sig_a | sig_b).sum()
                overlap = both / either if either > 0 else 0.0
            overlaps.append(overlap)
        except Exception:
            continue
    return float(np.mean(overlaps)) if overlaps else 0.0


# ═══════════════════════════════════════════════════════════════
# Dual-window slice helper
# ═══════════════════════════════════════════════════════════════

def _slice_stock_data(stock_data: Dict, window_days: int) -> Dict:
    """Return a copy of stock_data with each stock truncated to the last N rows."""
    result = {}
    for code, (price_df, factor_df) in stock_data.items():
        try:
            p_df = price_df.tail(window_days).copy()
            f_df = factor_df.tail(window_days).copy()
            result[code] = (p_df, f_df)
        except Exception:
            continue
    return result


# ═══════════════════════════════════════════════════════════════
# Shared deserialization helper
# ═══════════════════════════════════════════════════════════════

def _rule_from_db_row(row: dict) -> Optional[StrategyRule]:
    """Deserialize a StrategyRule from a DB row (strategy_rules or active_strategies).

    Handles JSON-decoding of conditions and sell_conditions fields.
    Returns None on parse failure.
    """
    try:
        conds_data = json.loads(row.get("conditions", "[]"))
        conds = [RuleCondition(**c) for c in conds_data]
    except (json.JSONDecodeError, TypeError, KeyError):
        return None

    sell_conds: List[RuleCondition] = []
    sell_raw = row.get("sell_conditions", "[]")
    if sell_raw and sell_raw != "[]":
        try:
            sd = json.loads(sell_raw)
            sell_conds = [RuleCondition(**s) for s in sd]
        except (json.JSONDecodeError, TypeError):
            pass

    return StrategyRule(
        name=row["rule_name"],
        rule_type=row.get("rule_type", "T1"),
        conditions=conds,
        sell_conditions=sell_conds,
        holding_min=row.get("holding_min", 3),
        holding_max=row.get("holding_max", 20),
        source=row.get("source", "template"),
    )


# ═══════════════════════════════════════════════════════════════
# DynamicSelector
# ═══════════════════════════════════════════════════════════════

class DynamicSelector:
    """Phase 4: weekly strategy selection with dual-window backtest,
    constraint filtering, correlation dedup, dynamic K, and 3-tier fallback."""

    def __init__(self, k_min: int = 3, k_max: int = 7):
        self.k_min = k_min
        self.k_max = k_max
        self.miner = RuleMiner()
        self._consecutive_low_weeks = 0   # L2/L3 tracker

    # ── Public API ───────────────────────────────────────────

    def run_weekly_selection(self, stock_data: Dict, index_df: pd.DataFrame,
                             factor_df: pd.DataFrame) -> List[dict]:
        """Weekly Friday execution.

        1. Load active rules from DB
        2. If none → L1 fallback (5 manual baseline strategies)
        3. Bear/bull → correlation threshold (bear=0.50, bull=0.70)
        4. Dual-window (60d + 120d) backtest per rule
        5. Score = 60d * 0.6 + 120d * 0.4
        6. Apply turnover penalty
        7. Correlation dedup
        8. Dynamic K
        9. Save to active_strategies
        10. L2/L3 checks
        """
        today = datetime.now().strftime("%Y-%m-%d")
        valid_until = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

        # 1. Load active rules
        db_rules = get_active_rules(limit=200)
        rules: List[Tuple[int, StrategyRule]] = []
        if db_rules:
            for row in db_rules:
                rule = _rule_from_db_row(row)
                if rule is not None:
                    rules.append((row["id"], rule))

        # 2. L1 fallback
        if not rules:
            logger.warning("No active rules in DB — triggering L1 fallback")
            baseline = self._fallback_l1()
            self._save_selections(baseline, today, valid_until)
            return baseline

        # 3. Correlation threshold
        bear = is_bear_market(index_df)
        corr_threshold = 0.50 if bear else 0.70
        logger.info("Market regime: %s, correlation threshold=%.2f",
                    "bear" if bear else "bull", corr_threshold)

        # Slice for dual window
        stock_60 = _slice_stock_data(stock_data, 60)
        stock_120 = _slice_stock_data(stock_data, 120)

        # 4-5. Evaluate each rule on dual windows
        scored = []
        for rule_id, rule in rules:
            perf_60 = self.miner.evaluate_rule(rule, stock_60)
            if perf_60 is None:
                continue

            passed, reason = apply_constraints(perf_60)
            if not passed:
                logger.debug("Rule %s excluded: %s", rule.name, reason)
                continue

            score_60 = window_score(perf_60)

            # 120-day window
            perf_120 = self.miner.evaluate_rule(rule, stock_120)
            score_120 = window_score(perf_120) if perf_120 else score_60

            final_score = score_60 * 0.6 + score_120 * 0.4

            # 6. Turnover penalty
            penalty = get_turnover_penalty(perf_60)
            if penalty == 0.0:
                logger.debug("Rule %s excluded: excessive turnover", rule.name)
                continue
            final_score *= penalty

            scored.append({
                "rule": rule,
                "rule_id": rule_id,
                "perf_60": perf_60,
                "perf_120": perf_120,
                "score_60": score_60,
                "score_120": score_120,
                "final_score": final_score,
            })

        if not scored:
            logger.warning("All rules excluded by constraints — triggering L1 fallback")
            baseline = self._fallback_l1()
            self._save_selections(baseline, today, valid_until)
            return baseline

        # Sort by final_score descending
        scored.sort(key=lambda x: x["final_score"], reverse=True)

        # 7. Correlation dedup
        selected = self._deduplicate(scored, stock_data, corr_threshold)

        # 8. Dynamic K
        k = min(self.k_max, max(self.k_min, len(selected)))
        selected = selected[:k]

        # 9. Save to DB
        selections = []
        for rank, item in enumerate(selected, 1):
            selections.append((
                today,
                item["rule_id"],
                item["rule"].name,
                item["score_60"],
                item["score_120"],
                item["final_score"],
                rank,
                valid_until,
            ))
        try:
            # 清除所有当前有效的策略（同一周期内多次执行应覆盖旧结果）
            with get_conn() as conn:
                conn.execute("DELETE FROM active_strategies WHERE valid_until >= ?", (today,))
            upsert_active_strategies(selections)
            logger.info("Saved %d active strategies for week %s", len(selections), today)
        except Exception:
            logger.exception("Failed to save active strategies")

        # 10. L2/L3 check
        self._handle_l2_l3(len(selected))

        return selected

    # ── Dedup ────────────────────────────────────────────────

    def _deduplicate(self, scored: List[dict], stock_data: Dict,
                     threshold: float) -> List[dict]:
        """Correlation dedup: keep the higher-scoring rule when overlap > threshold."""
        if not scored:
            return []
        selected = [scored[0]]
        for candidate in scored[1:]:
            is_dup = False
            for sel in selected:
                overlap = _compute_signal_overlap(
                    candidate["rule"], sel["rule"], stock_data
                )
                if overlap > threshold:
                    is_dup = True
                    break
            if not is_dup:
                selected.append(candidate)
        return selected

    # ── Save helper ──────────────────────────────────────────

    def _save_selections(self, items: List[dict], today: str,
                         valid_until: str) -> None:
        """Persist selection list as active_strategies rows."""
        rows = []
        for rank, item in enumerate(items, 1):
            rows.append((
                today,
                item.get("rule_id", 0),
                item.get("rule_name", item.get("rule", StrategyRule(name="unknown", rule_type="T1")).name),
                item.get("score_60", 0),
                item.get("score_120", 0),
                item.get("final_score", 0),
                rank,
                valid_until,
            ))
        try:
            with get_conn() as conn:
                conn.execute("DELETE FROM active_strategies WHERE valid_until >= ?", (today,))
            upsert_active_strategies(rows)
        except Exception:
            logger.exception("Failed to save baseline selections")

    # ── L1 fallback ──────────────────────────────────────────

    def _fallback_l1(self) -> List[dict]:
        """L1 fallback: return 5 manual baseline strategies.

        These mirror the original five-strategy framework using factor-library
        conditions so they can be evaluated even with an empty template library.

        Each baseline is persisted into strategy_rules to obtain a real ID,
        which allows the backtest pipeline to load them by ID later.
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        valid_until = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        baselines = [
            StrategyRule(
                name="L1_放量突破", rule_type="T2", source="baseline",
                conditions=[
                    RuleCondition("VOL_RATIO_5", ">", 1.5),
                    RuleCondition("RET_5D", ">", 0.02),
                ],
            ),
            StrategyRule(
                name="L1_均线粘合", rule_type="T1", source="baseline",
                conditions=[
                    RuleCondition("MA_多头强度", ">", 0.6),
                ],
            ),
            StrategyRule(
                name="L1_量价背离", rule_type="T2", source="baseline",
                conditions=[
                    RuleCondition("OBV_偏离度", ">", 0.3),
                    RuleCondition("RET_10D", "<", -0.01),
                ],
            ),
            StrategyRule(
                name="L1_抄底", rule_type="T2", source="baseline",
                conditions=[
                    RuleCondition("RSI_14", "<", 30),
                    RuleCondition("RET_20D", "<", -0.08),
                ],
            ),
            StrategyRule(
                name="L1_主力建仓", rule_type="T2", source="baseline",
                conditions=[
                    RuleCondition("AMOUNT_RATIO_20", ">", 2.0),
                    RuleCondition("PV_CORREL", ">", 0.5),
                ],
            ),
        ]

        results = []
        with get_conn() as conn:
            for i, rule in enumerate(baselines):
                conds_json = json.dumps(
                    [{"factor": c.factor, "operator": c.operator, "threshold": c.threshold}
                     for c in rule.conditions]
                )
                try:
                    cur = conn.execute("""
                        INSERT INTO strategy_rules
                            (rule_name, rule_type, encoding, conditions, sell_conditions,
                             holding_min, holding_max, source, generation, fitness,
                             annual_return, win_rate, sharpe_ratio, max_drawdown,
                             total_trades, signal_overlap, is_active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(rule_name) DO UPDATE SET
                            rule_type=excluded.rule_type, conditions=excluded.conditions,
                            source=excluded.source, is_active=1,
                            degraded_at=NULL, updated_at=?
                    """, (
                        rule.name, rule.rule_type, "[]", conds_json, "[]",
                        rule.holding_min, rule.holding_max, "baseline", 0, 0.0,
                        0.0, 0.0, 0.0, 0.0, 0, 0.0,
                        now_str,
                    ))
                    # lastrowid gives the inserted row id; for UPDATE on conflict,
                    # re-query to be safe
                    rule_id = cur.lastrowid
                    if rule_id == 0:
                        row = conn.execute(
                            "SELECT id FROM strategy_rules WHERE rule_name=?",
                            (rule.name,),
                        ).fetchone()
                        rule_id = row["id"] if row else 0
                except Exception:
                    logger.exception("Failed to upsert L1 baseline rule: %s", rule.name)
                    rule_id = 0

                results.append({
                    "rule": rule,
                    "rule_name": rule.name,
                    "rule_id": rule_id,
                    "score_60": 0,
                    "score_120": 0,
                    "final_score": 0,
                    "rank": i + 1,
                    "select_date": today,
                    "valid_until": valid_until,
                    "source": "L1_fallback",
                })

        logger.warning("L1 fallback: using %d manual baseline strategies", len(results))
        return results

    # ── L2 / L3 checks ───────────────────────────────────────

    def _handle_l2_l3(self, k: int) -> List[dict]:
        """L2/L3 escalation when active-strategy count stays low.

        L2: 3+ consecutive weeks with < 3 active strategies (non-bear).
        L3: 4+ consecutive weeks still in L2 → suggest relaxing entry threshold.
        """
        alerts = []
        if k < self.k_min:
            self._consecutive_low_weeks += 1
            logger.warning("L2: week %d with <%d active strategies",
                           self._consecutive_low_weeks, self.k_min)
            if self._consecutive_low_weeks >= 3:
                alerts.append({
                    "level": "L2",
                    "message": f"{self._consecutive_low_weeks} consecutive weeks "
                               f"with <{self.k_min} active strategies",
                })
            if self._consecutive_low_weeks >= 4:
                alerts.append({
                    "level": "L3",
                    "message": "Suggest relaxing entry threshold 0.6 -> 0.45",
                    "action": "relax_threshold",
                })
        else:
            self._consecutive_low_weeks = 0
        return alerts


# ═══════════════════════════════════════════════════════════════
# Daily Signal Generation
# ═══════════════════════════════════════════════════════════════

def generate_daily_signals(stock_data: Dict, market_data: dict,
                           index_df: pd.DataFrame = None) -> List[dict]:
    """Daily after-close: market-wide signal trigger + Phase 3 confidence scoring.

    1. Get current active strategies from DB
    2. Fall back to top-5 template rules if none active
    3. For each stock: check which active rules trigger (latest day buy signal)
    4. For triggered rules: build features, get LGBMRanker confidence scores
    5. Fuse multiple signals per stock using softmax weighting
    6. Return per-stock fusion scores
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        ranker = get_ranker()
    except Exception:
        logger.warning("LGBMRanker unavailable (lightgbm may not be installed) — using fallback")
        ranker = None

    # 1. Get active strategies
    active = get_current_active_strategies()
    rule_map: Dict[int, Tuple[StrategyRule, dict]] = {}
    if active:
        for row in active:
            rule = _rule_from_db_row(row)
            if rule is not None:
                rule_map[row["rule_id"]] = (rule, dict(row))

    # 2. Fallback: load template rules
    if not rule_map:
        logger.info("No active strategies — loading template library")
        templates = load_template_library()
        if not templates:
            logger.warning("No templates available, using L1 baseline")
            ds = DynamicSelector()
            baseline = ds._fallback_l1()
            for item in baseline:
                rule = item["rule"]
                rule_map[0] = (rule, item)
        else:
            for rule in templates[:10]:  # top 10 by fitness
                rule_map[0] = (rule, {"rule_id": 0, "rule_name": rule.name,
                                       "rule_type": rule.rule_type})

    if not rule_map:
        return []

    # 3-4. Per-stock signal check + feature building
    stock_signals: Dict[str, List[dict]] = {}  # code -> list of signal dicts

    for code, (price_df, factor_df) in stock_data.items():
        if factor_df is None or factor_df.empty:
            continue

        for rule_id, (rule, meta) in rule_map.items():
            try:
                buy_signal = rule.get_buy_signal(factor_df)
                if not buy_signal.iloc[-1]:
                    continue
            except Exception:
                continue

            # Signal triggered — compute strength
            strength = _calc_signal_strength(rule, factor_df)

            # Build stock-state features
            stock_state = _build_stock_state(price_df, factor_df)

            # Build meta-features for ranker
            features = build_meta_features(
                rule_id=rule_id,
                rule_name=rule.name,
                rule_type=rule.rule_type,
                n_conditions=len(rule.conditions),
                rule_recent_perf=meta.get("rule_recent_perf", {}),
                rule_history=meta.get("rule_history", {}),
                market_state=market_data or {},
                stock_state=stock_state,
                signal_strength=strength,
                sector_crowd=meta.get("sector_crowd", {}),
            )

            stock_signals.setdefault(code, []).append({
                "code": code,
                "trade_date": today,
                "rule_id": rule_id,
                "rule_name": rule.name,
                "signal_strength": strength,
                "features": features,
            })

    # 4b. Batch ranker inference
    all_signals_flat = []
    for code, sigs in stock_signals.items():
        all_signals_flat.extend(sigs)

    if all_signals_flat:
        if ranker is not None:
            try:
                features_df = pd.DataFrame([s["features"] for s in all_signals_flat])
                confidences = ranker.predict(features_df)
                for i, sig in enumerate(all_signals_flat):
                    sig["confidence"] = float(confidences[i])
                    sig["raw_score"] = sig["confidence"]
            except Exception:
                logger.warning("Ranker inference failed — using signal_strength fallback")
                for sig in all_signals_flat:
                    sig["confidence"] = sig.get("signal_strength", 0.5) * 100.0
                    sig["raw_score"] = sig["confidence"]
        else:
            for sig in all_signals_flat:
                sig["confidence"] = sig.get("signal_strength", 0.5) * 100.0
                sig["raw_score"] = sig["confidence"]

        # Apply bear-market discount to all confidence scores
        if index_df is not None and is_bear_market(index_df):
            bear_discount = 0.85
            for sig in all_signals_flat:
                sig["confidence"] = sig["confidence"] * bear_discount
                sig["raw_score"] = sig.get("raw_score", 0) * bear_discount
            logger.info("Bear-market regime detected — confidence discounted by %.0f%%",
                        (1 - bear_discount) * 100)

    # 5. Per-stock fusion
    results = []
    for code, sigs in stock_signals.items():
        fused = float(fuse_stock_signals(code, sigs)) if sigs else 0.0
        results.append({
            "code": code,
            "date": today,
            "fusion_score": fused,
            "triggered_count": len(sigs),
            "signals": sigs,
        })

    # 6. Persist individual signals
    for sig in all_signals_flat:
        try:
            save_strategy_signal({
                "trade_date": sig["trade_date"],
                "code": sig["code"],
                "rule_id": sig["rule_id"],
                "rule_name": sig["rule_name"],
                "confidence": sig.get("confidence"),
                "raw_score": sig.get("raw_score"),
                "features_json": json.dumps(sig.get("features", {}), default=str),
            })
        except Exception:
            logger.debug("Failed to save signal for %s rule %s",
                         sig["code"], sig.get("rule_name"))

    logger.info("Daily signals generated: %d stocks, %d total triggers",
                len(results), len(all_signals_flat))
    return results


def _build_stock_state(price_df: pd.DataFrame, factor_df: pd.DataFrame) -> dict:
    """Build stock-level state features for meta-feature construction."""
    state = {}
    try:
        close = price_df["close"]
        if len(close) >= 20:
            state["stock_ret_20d"] = float(close.pct_change(20).iloc[-1] or 0)
            state["stock_vol_20d"] = float(close.pct_change().rolling(20).std().iloc[-1] or 0)
        else:
            state["stock_ret_20d"] = 0.0
            state["stock_vol_20d"] = 0.0
    except Exception:
        state["stock_ret_20d"] = 0.0
        state["stock_vol_20d"] = 0.0

    try:
        if "TURNOVER_PCTL" in factor_df.columns:
            state["turnover_pctl"] = float(factor_df["TURNOVER_PCTL"].iloc[-1] or 0)
        else:
            state["turnover_pctl"] = 0.0
    except Exception:
        state["turnover_pctl"] = 0.0

    return state
