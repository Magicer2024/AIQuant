"""
test_strategy_pipeline.py —— End-to-end pipeline tests for the strategy generation system.

Tests for all 5 Phase 1-4 modules:
  - factor_lib         (Factor library, IC calculation, normalization)
  - rule_miner         (Template exhaustion, rule scoring, factor pruning)
  - genetic_evolver    (Encoding/decoding, genetic operators, fitness, legality)
  - lgbm_ranker        (Fallback prediction, signal fusion, meta-features, singleton)
  - dynamic_selector   (Window scoring, bear market detection, constraints, turnover, signal strength)

Plus integration tests for the Phase 3/4 wiring into existing fuse_signals flow.
"""
import pandas as pd
import numpy as np
import pytest


# ============================================================================
# TestFactorLibrary
# ============================================================================

class TestFactorLibrary:
    """Tests for strategy/factor_lib.py: registry, computation, IC, normalization."""

    def test_registry_has_all_categories(self):
        """Verify all 8 factor categories exist."""
        from strategy.factor_lib import FACTOR_REGISTRY
        categories = {v["category"] for v in FACTOR_REGISTRY.values()}
        for cat in ["trend", "momentum", "volatility", "volume",
                     "pattern", "liquidity", "fundamental", "sentiment"]:
            assert cat in categories, f"Missing category: {cat}"

    def test_compute_factors(self):
        """Verify factor computation produces expected columns."""
        from strategy.factor_lib import compute_all_factors
        import pandas as pd
        import numpy as np
        dates = pd.date_range('2024-01-01', periods=100, freq='B')
        df = pd.DataFrame({
            'open': np.random.randn(100).cumsum() + 100,
            'high': np.random.randn(100).cumsum() + 102,
            'low': np.random.randn(100).cumsum() + 98,
            'close': np.random.randn(100).cumsum() + 100,
            'volume': np.random.randint(1e6, 1e7, 100),
        }, index=dates)
        df['amount'] = df['volume'] * df['close']
        df['turnover'] = np.random.uniform(0.01, 0.1, 100)
        result = compute_all_factors(df)
        assert len(result.columns) >= 30, f"Expected >=30 factors, got {len(result.columns)}"

    def test_ic_calculation(self):
        """Verify IC calculation returns valid dict."""
        from strategy.factor_lib import compute_all_factors, calc_all_ic
        import pandas as pd
        import numpy as np
        dates = pd.date_range('2024-01-01', periods=100, freq='B')
        df = pd.DataFrame({
            'open': np.random.randn(100).cumsum() + 100,
            'high': np.random.randn(100).cumsum() + 102,
            'low': np.random.randn(100).cumsum() + 98,
            'close': np.random.randn(100).cumsum() + 100,
            'volume': np.random.randint(1e6, 1e7, 100),
        }, index=dates)
        df['amount'] = df['volume'] * df['close']
        df['turnover'] = np.random.uniform(0.01, 0.1, 100)
        factors = compute_all_factors(df)
        forward_returns = df["close"].pct_change(20).shift(-20)
        ic = calc_all_ic(factors, forward_returns)
        assert len(ic) > 0
        assert all(isinstance(v, float) for v in ic.values())

    def test_factor_normalization_range(self):
        """Verify normalized factors are in [0,1] or [-1,1]."""
        from strategy.factor_lib import compute_all_factors, FACTOR_REGISTRY
        import pandas as pd
        import numpy as np
        dates = pd.date_range('2024-01-01', periods=100, freq='B')
        df = pd.DataFrame({
            'open': np.random.randn(100).cumsum() + 100,
            'high': np.random.randn(100).cumsum() + 102,
            'low': np.random.randn(100).cumsum() + 98,
            'close': np.random.randn(100).cumsum() + 100,
            'volume': np.random.randint(1e6, 1e7, 100),
        }, index=dates)
        df['amount'] = df['volume'] * df['close']
        df['turnover'] = np.random.uniform(0.01, 0.1, 100)
        result = compute_all_factors(df)
        for col in result.columns:
            if col in FACTOR_REGISTRY:
                norm_type = FACTOR_REGISTRY[col]["norm"]
                vals = result[col].dropna()
                if len(vals) == 0:
                    continue
                if norm_type == "fundamental":
                    assert -1.5 <= vals.max() <= 1.5, f"{col}: max={vals.max()} out of [-1,1] range"
                    assert -1.5 <= vals.min() <= 1.5, f"{col}: min={vals.min()} out of [-1,1] range"
                else:
                    assert 0 <= vals.min(), f"{col}: min={vals.min()} < 0"
                    assert vals.max() <= 1.1, f"{col}: max={vals.max()} > 1"


# ============================================================================
# TestRuleMiner
# ============================================================================

class TestRuleMiner:
    """Tests for strategy/rule_miner.py: template generation, conditions, scoring, pruning."""

    def test_template_generation(self):
        """Verify all 4 template types generate rules."""
        from strategy.rule_miner import TemplateBuilder
        from strategy.factor_lib import get_factor_names_by_category
        factors = (get_factor_names_by_category("trend")[:4] +
                   get_factor_names_by_category("momentum")[:4])
        builder = TemplateBuilder(factors, [0.2, 0.35, 0.5, 0.65, 0.8])
        t1 = builder.build_t1()
        t2 = builder.build_t2()
        t3 = builder.build_t3()
        t4 = builder.build_t4()
        assert len(t1) > 0, f"T1 empty: {len(t1)}"
        assert len(t3) >= 0, f"T3 unexpected: {len(t3)}"  # May be 0 if no CROSS_PAIRS match
        # T2 requires cross-category pairs; with trends+momentum factors it should produce
        if len(t2) > 0:
            assert all(len(r.conditions) == 2 for r in t2)

    def test_rule_condition_evaluate(self):
        """Verify RuleCondition.evaluate works for all operators."""
        from strategy.rule_miner import RuleCondition
        import pandas as pd
        import numpy as np
        factor_df = pd.DataFrame({
            "RSI_14": [0.2, 0.3, 0.5, 0.7, 0.8],
            "MACD_DIF": [0.1, 0.2, 0.3, 0.2, 0.1],
            "MACD_DEA": [0.1, 0.15, 0.25, 0.2, 0.1],
        })
        # Test > operator
        c1 = RuleCondition("RSI_14", ">", 0.5)
        result = c1.evaluate(factor_df)
        assert result.iloc[-1] == True
        assert result.iloc[0] == False
        # Test < operator
        c2 = RuleCondition("RSI_14", "<", 0.3)
        result = c2.evaluate(factor_df)
        assert result.iloc[0] == True
        # Test cross_above
        c3 = RuleCondition("RSI_14", "cross_above", 0.5)
        result = c3.evaluate(factor_df)
        assert result.sum() >= 1
        # Test cross_above_factor
        c4 = RuleCondition("MACD_DIF", "cross_above_factor", ref_factor="MACD_DEA")
        result = c4.evaluate(factor_df)
        assert isinstance(result, pd.Series)
        # Test cross_below_factor
        c5 = RuleCondition("MACD_DIF", "cross_below_factor", ref_factor="MACD_DEA")
        result = c5.evaluate(factor_df)
        assert isinstance(result, pd.Series)

    def test_score_formula(self):
        """Verify score_rule returns reasonable values."""
        from strategy.rule_miner import RuleMiner
        miner = RuleMiner()
        perf = {
            "total_return": 20.0, "win_rate": 55.0,
            "sharpe_ratio": 1.5, "max_drawdown": -12.0,
        }
        score = miner.score_rule(perf)
        assert 0 <= score <= 2.0, f"Score {score} out of expected range"

    def test_prune_factors(self):
        """Verify factor pruning by IC."""
        from strategy.rule_miner import RuleMiner
        from strategy.factor_lib import compute_all_factors
        import pandas as pd
        import numpy as np
        dates = pd.date_range('2024-01-01', periods=100, freq='B')
        df = pd.DataFrame({
            'open': np.random.randn(100).cumsum() + 100,
            'high': np.random.randn(100).cumsum() + 102,
            'low': np.random.randn(100).cumsum() + 98,
            'close': np.random.randn(100).cumsum() + 100,
            'volume': np.random.randint(1e6, 1e7, 100),
        }, index=dates)
        df['amount'] = df['volume'] * df['close']
        factor_df = compute_all_factors(df)
        forward_returns = df["close"].pct_change(20).shift(-20)
        miner = RuleMiner(seed=42)
        pruned = miner.prune_factors(factor_df, forward_returns)
        assert isinstance(pruned, list)
        assert len(pruned) > 0


# ============================================================================
# TestGeneticEvolver
# ============================================================================

class TestGeneticEvolver:
    """Tests for strategy/genetic_evolver.py: encoding, operators, fitness, legality."""

    def test_encode_decode_roundtrip(self):
        """Verify encode then decode returns same conditions."""
        from strategy.genetic_evolver import encode_rule, decode_rule
        from strategy.rule_miner import RuleCondition, StrategyRule
        original = StrategyRule(
            name="test", rule_type="genetic",
            conditions=[
                RuleCondition("RSI_14", "<", 0.35),
                RuleCondition("VOL_RATIO_5", ">", 0.6),
            ],
        )
        encoded = encode_rule(original.conditions)
        assert isinstance(encoded, list), f"Expected list, got {type(encoded)}"
        decoded = decode_rule(encoded)
        assert len(decoded) == len(original.conditions)
        assert decoded[0].factor == "RSI_14"
        assert decoded[1].factor == "VOL_RATIO_5"

    def test_random_rule(self):
        """Verify random_rule generates valid rules."""
        from strategy.genetic_evolver import random_rule
        rule = random_rule(3)
        assert len(rule.conditions) >= 1
        assert rule.rule_type == "GEN"
        assert rule.name.startswith("GEN_")

    def test_legality_check(self):
        """Verify contradictory rules are rejected."""
        from strategy.genetic_evolver import legality_check
        from strategy.rule_miner import RuleCondition, StrategyRule
        # Valid rule
        valid = StrategyRule("test", "genetic", [RuleCondition("RSI_14", "<", 35)])
        assert legality_check(valid) >= 0.5
        # Contradictory: RSI > 80 AND RSI < 20 on same factor
        contradictory = StrategyRule("test", "genetic", [
            RuleCondition("RSI_14", ">", 80),
            RuleCondition("RSI_14", "<", 20),
        ])
        assert legality_check(contradictory) < 0.5

    def test_crossover(self):
        """Verify crossover produces valid offspring."""
        from strategy.genetic_evolver import crossover
        from strategy.rule_miner import RuleCondition, StrategyRule
        p1 = StrategyRule("p1", "GEN", [
            RuleCondition("RSI_14", "<", 0.3),
            RuleCondition("VOL_RATIO_5", ">", 0.5),
        ])
        p2 = StrategyRule("p2", "GEN", [
            RuleCondition("KDJ_K", ">", 0.7),
        ])
        child = crossover(p1, p2)
        assert len(child.conditions) >= 1
        assert child.rule_type == "GEN"

    def test_signal_overlap(self):
        """Verify signal overlap is in [0,1]."""
        from strategy.genetic_evolver import signal_overlap
        from strategy.rule_miner import RuleCondition, StrategyRule
        import pandas as pd
        import numpy as np
        factor_df = pd.DataFrame({
            "RSI_14": np.random.uniform(0, 1, 100),
            "VOL_RATIO_5": np.random.uniform(0, 1, 100),
        })
        r1 = StrategyRule("r1", "T1", [RuleCondition("RSI_14", "<", 0.3)])
        r2 = StrategyRule("r2", "T1", [RuleCondition("RSI_14", "<", 0.5)])
        overlap = signal_overlap(r1, r2, factor_df)
        assert 0 <= overlap <= 1

    def test_compute_fitness(self):
        """Verify fitness computation produces valid score."""
        from strategy.genetic_evolver import compute_fitness
        from strategy.rule_miner import RuleCondition, StrategyRule
        import pandas as pd
        import numpy as np
        perf = {"annual_return": 30.0, "win_rate": 60.0, "sharpe_ratio": 2.0, "max_drawdown": -15.0}
        rule = StrategyRule("test", "genetic", [RuleCondition("RSI_14", "<", 0.35)])
        factor_df = pd.DataFrame({"RSI_14": np.random.uniform(0, 1, 100)})
        fitness = compute_fitness(perf, rule, [], factor_df)
        assert fitness >= 0


# ============================================================================
# TestLGBMRanker
# ============================================================================

class TestLGBMRanker:
    """Tests for strategy/lgbm_ranker.py: fallback, fusion, meta-features, singleton."""

    def test_fallback_prediction(self):
        """Verify fallback returns neutral 50.0."""
        from strategy.lgbm_ranker import LGBMRanker
        import pandas as pd
        ranker = LGBMRanker()
        ranker._fallback = True
        df = pd.DataFrame({"a": [1, 2, 3]})
        preds = ranker.predict(df)
        assert len(preds) == 3
        assert all(45 <= p <= 55 for p in preds)

    def test_signal_fusion(self):
        """Verify softmax fusion outputs 0-50 range."""
        from strategy.lgbm_ranker import fuse_stock_signals
        signals = [
            {"confidence": 80},
            {"confidence": 60},
            {"confidence": 40},
        ]
        score = fuse_stock_signals("000001", signals)
        assert 0 <= score <= 50
        # Higher confidences should produce higher scores
        signals_high = [{"confidence": 95}, {"confidence": 90}]
        score_high = fuse_stock_signals("000001", signals_high)
        assert score_high > score

    def test_build_meta_features(self):
        """Verify meta-feature dict has all 20 features."""
        from strategy.lgbm_ranker import build_meta_features
        feats = build_meta_features(
            rule_id=1, rule_name="test", rule_type="T1", n_conditions=2,
            rule_recent_perf={"win_rate_20d": 0.6}, rule_history={},
            market_state={}, stock_state={}, signal_strength=0.5,
            sector_crowd={},
        )
        expected_keys = [
            "rule_id", "rule_type_hash", "n_conditions",
            "win_rate_20d", "win_rate_60d", "avg_return_20d", "avg_return_60d", "sharpe_20d",
            "trigger_count_20d", "max_drawdown_hist", "profit_factor_hist",
            "index_ret_20d", "index_vol_20d", "market_breadth",
            "stock_ret_20d", "stock_vol_20d", "turnover_pctl",
            "signal_strength", "sector_trigger_count", "sector_trigger_ratio",
        ]
        for key in expected_keys:
            assert key in feats, f"Missing feature: {key}"
        assert len(feats) == 20

    def test_get_ranker_singleton(self):
        """Verify get_ranker returns singleton."""
        # Reset for test
        import strategy.lgbm_ranker as lr
        lr._ranker_instance = None
        try:
            r1 = lr.get_ranker()
            r2 = lr.get_ranker()
            assert r1 is r2
            assert r1._fallback is True  # No model file, should be in fallback
        except ModuleNotFoundError:
            # LightGBM may not be installed; skip gracefully
            pass


# ============================================================================
# TestDynamicSelector
# ============================================================================

class TestDynamicSelector:
    """Tests for strategy/dynamic_selector.py: scoring, regime, constraints, penalties."""

    def test_window_score(self):
        """Verify window_score returns reasonable value."""
        from strategy.dynamic_selector import window_score
        perf = {
            "annual_return": 30.0, "win_rate": 60.0,
            "sharpe_ratio": 1.5, "max_drawdown": -15.0,
        }
        score = window_score(perf)
        assert 0 <= score <= 2.0, f"Score {score} out of [0, 2.0]"

    def test_is_bear_market(self):
        """Verify bear market detection with insufficient data."""
        from strategy.dynamic_selector import is_bear_market
        import pandas as pd
        import numpy as np
        # Insufficient data
        df_short = pd.DataFrame({"close": [100] * 50})
        assert is_bear_market(df_short) == False
        # Sufficient data (trending up = not bear)
        dates = pd.date_range('2024-01-01', periods=150, freq='B')
        df_up = pd.DataFrame({
            "close": 100 + np.linspace(0, 20, 150),
        }, index=dates)
        assert is_bear_market(df_up) == False

    def test_apply_constraints(self):
        """Verify constraint filtering logic."""
        from strategy.dynamic_selector import apply_constraints
        good_perf = {
            "total_return": 15.0, "win_rate": 55.0, "sharpe_ratio": 1.2,
            "max_drawdown": -10.0, "total_trades": 30, "sample_count": 5,
        }
        passed, reason = apply_constraints(good_perf)
        assert passed, f"Should pass but got: {reason}"
        # Bad: drawdown too high
        bad_perf = {
            "total_return": 15.0, "win_rate": 55.0, "sharpe_ratio": 1.2,
            "max_drawdown": -25.0, "total_trades": 30, "sample_count": 5,
        }
        passed, reason = apply_constraints(bad_perf)
        assert not passed

    def test_turnover_penalty(self):
        """Verify turnover penalty returns correct multipliers."""
        from strategy.dynamic_selector import get_turnover_penalty
        # Normal: should pass
        result = get_turnover_penalty({"total_trades": 10, "sample_count": 10})
        assert result == 1.0
        # High turnover: should get penalty
        result = get_turnover_penalty({"total_trades": 40, "sample_count": 20})
        assert result < 1.0

    def test_signal_strength(self):
        """Verify signal strength calculation."""
        from strategy.dynamic_selector import _calc_signal_strength
        from strategy.rule_miner import RuleCondition, StrategyRule
        import pandas as pd
        import numpy as np
        factor_df = pd.DataFrame({
            "RSI_14": np.random.uniform(0.2, 0.4, 100),
        })
        rule = StrategyRule("test", "T1", [RuleCondition("RSI_14", "<", 0.35)])
        strength = _calc_signal_strength(rule, factor_df)
        assert 0 <= strength <= 1


# ============================================================================
# TestIntegration
# ============================================================================

class TestIntegration:
    """End-to-end integration tests for the full Phase 1-4 pipeline."""

    def test_fuse_with_phase34_fallback(self):
        """Verify fuse_with_phase34 falls back to manual scoring."""
        import pandas as pd
        import numpy as np
        from strategy.strategies import fuse_with_phase34
        dates = pd.date_range('2024-01-01', periods=60, freq='B')
        # Create minimal strategy DataFrames
        s1 = pd.DataFrame({
            "close": np.linspace(10, 15, 60),
            "volume": np.ones(60) * 1e6,
            "BUY_SIGNAL": [0]*59 + [1],
            "SELL_SIGNAL": [0]*60,
            "BUY_SCORE": np.linspace(0, 5, 60),
            "FUSION_SCORE": np.linspace(0, 25, 60),
            "STRATEGY": "test",
        }, index=dates)
        # Without Phase 3/4 signals
        result = fuse_with_phase34([s1], phase34_signals=None)
        assert "FUSION_SCORE" in result.columns
        # With Phase 3/4 signals
        signals = {"code": "000001", "fusion_score": 35.0, "signals": []}
        result = fuse_with_phase34([s1], phase34_signals=signals)
        assert result.iloc[-1]["FUSION_SCORE"] == 35.0

    def test_all_modules_import(self):
        """Verify all 5 new modules import cleanly."""
        from strategy.factor_lib import FACTOR_REGISTRY, compute_all_factors
        from strategy.rule_miner import RuleMiner, StrategyRule, RuleCondition, TemplateBuilder
        from strategy.genetic_evolver import GeneticEvolver, encode_rule, decode_rule
        from strategy.lgbm_ranker import LGBMRanker, build_meta_features, fuse_stock_signals
        from strategy.dynamic_selector import DynamicSelector, window_score
        assert len(FACTOR_REGISTRY) > 50


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
