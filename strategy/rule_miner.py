"""
rule_miner.py —— Phase 1: 模板穷举规则生成
===========================================
功能：
- IC 筛选 + 聚类去重
- 四类模板穷举 (T1/T2/T3/T4)
- 全市场快速回测验证
- 综合评分 → Top-50 入库
- 模板库回流更新
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field, asdict
from itertools import combinations
import json
import random

from strategy.factor_lib import (
    FACTOR_REGISTRY, calc_all_ic,
    filter_by_ic, get_factor_names_by_category,
)
from config.strategy_params import PHASE1_CONFIG, CROSS_PAIRS
from core.db import upsert_strategy_rule, get_active_rules
from backtest.backtest import Backtester, BacktestResult


@dataclass
class RuleCondition:
    factor: str
    operator: str      # "<" | ">" | "cross_above" | "cross_below"
    threshold: float = 0.5

    def to_dict(self):
        return asdict(self)

    def evaluate(self, factor_df: pd.DataFrame) -> pd.Series:
        if self.operator == ">":
            return factor_df[self.factor] > self.threshold
        elif self.operator == "<":
            return factor_df[self.factor] < self.threshold
        elif self.operator == "cross_above":
            return (factor_df[self.factor] > self.threshold) & \
                   (factor_df[self.factor].shift(1) <= self.threshold)
        elif self.operator == "cross_below":
            return (factor_df[self.factor] < self.threshold) & \
                   (factor_df[self.factor].shift(1) >= self.threshold)
        return pd.Series(False, index=factor_df.index)


@dataclass
class StrategyRule:
    name: str
    rule_type: str            # T1/T2/T3/T4
    conditions: List[RuleCondition] = field(default_factory=list)
    sell_conditions: List[RuleCondition] = field(default_factory=list)
    holding_min: int = 3
    holding_max: int = 20
    source: str = "template"

    def get_buy_signal(self, factor_df: pd.DataFrame) -> pd.Series:
        if not self.conditions:
            return pd.Series(False, index=factor_df.index)
        result = self.conditions[0].evaluate(factor_df)
        for cond in self.conditions[1:]:
            result = result & cond.evaluate(factor_df)
        return result.fillna(False)

    def get_sell_signal(self, factor_df: pd.DataFrame) -> pd.Series:
        if self.sell_conditions:
            result = self.sell_conditions[0].evaluate(factor_df)
            for cond in self.sell_conditions[1:]:
                result = result & cond.evaluate(factor_df)
            return result.fillna(False)
        return ~self.get_buy_signal(factor_df)


class TemplateBuilder:
    """四类模板的规则生成器"""

    def __init__(self, factor_names: List[str], thresholds: List[float]):
        self.factors = factor_names
        self.thresholds = thresholds

    def build_t1(self) -> List[StrategyRule]:
        """单因子阈值模板"""
        rules = []
        for f in self.factors:
            for t in self.thresholds:
                for op in [">", "<"]:
                    cond = RuleCondition(f, op, t)
                    rules.append(StrategyRule(
                        name=f"T1_{f}_{op}_{t:.2f}",
                        rule_type="T1",
                        conditions=[cond],
                    ))
        return rules

    def build_t2(self) -> List[StrategyRule]:
        """双因子组合模板 — 跨类别配对"""
        rules = []
        cat_factors = {}
        for f in self.factors:
            cat = FACTOR_REGISTRY.get(f, {}).get("category", "unknown")
            cat_factors.setdefault(cat, []).append(f)
        categories = list(cat_factors.keys())

        for cat_a, cat_b in combinations(categories, 2):
            for f1 in cat_factors[cat_a][:3]:
                for f2 in cat_factors[cat_b][:3]:
                    for t1 in self.thresholds[1:4]:
                        for t2 in self.thresholds[1:4]:
                            op1 = ">" if "VOL" not in f1 else ">"
                            op2 = "<" if "PCT_B" in f2 or "HV" in f2 else ">"
                            rules.append(StrategyRule(
                                name=f"T2_{f1}_{op1}{t1:.2f}_{f2}_{op2}{t2:.2f}",
                                rule_type="T2",
                                conditions=[
                                    RuleCondition(f1, op1, t1),
                                    RuleCondition(f2, op2, t2),
                                ],
                            ))
        return rules

    def build_t3(self) -> List[StrategyRule]:
        """交叉信号模板"""
        rules = []
        for f_fast, f_slow in CROSS_PAIRS:
            if f_fast not in self.factors or f_slow not in self.factors:
                continue
            rules.append(StrategyRule(
                name=f"T3_{f_fast}_cross_above_{f_slow}",
                rule_type="T3",
                conditions=[RuleCondition(f_fast, "cross_above", 0)],
            ))
        return rules

    def build_t4(self) -> List[StrategyRule]:
        """三因子确认模板"""
        t2_rules = self.build_t2()
        if len(t2_rules) > 200:
            t2_rules = random.sample(t2_rules, 200)
        rules = []
        for t2_rule in t2_rules:
            for f in self.factors[:10]:
                third_cond = RuleCondition(f, ">", self.thresholds[2])
                new_conds = t2_rule.conditions + [third_cond]
                rules.append(StrategyRule(
                    name=f"T4_{t2_rule.name}_{f}",
                    rule_type="T4",
                    conditions=new_conds,
                ))
        return rules


class RuleMiner:
    """Phase 1: 模板穷举 + 剪枝 + 回测验证"""

    def __init__(self, sample_size: int = None, min_trades: int = None,
                 top_n: int = None):
        self.config = PHASE1_CONFIG
        self.sample_size = sample_size or self.config["sample_stocks"]
        self.min_trades = min_trades or self.config["min_trades"]
        self.top_n = top_n or self.config["top_n_rules"]

    def prune_factors(self, factor_df: pd.DataFrame,
                      forward_returns: pd.Series) -> List[str]:
        """① IC 筛选 + ② 聚类去重"""
        passing = filter_by_ic(factor_df, forward_returns, self.config["ic_min_abs"])
        ic = calc_all_ic(factor_df[passing] if passing else factor_df, forward_returns)
        cat_best = {}
        for f in passing:
            cat = FACTOR_REGISTRY.get(f, {}).get("category", "unknown")
            if cat not in cat_best:
                cat_best[cat] = []
            cat_best[cat].append((f, abs(ic.get(f, 0))))
        result = []
        for cat, items in cat_best.items():
            items.sort(key=lambda x: x[1], reverse=True)
            result.extend([f for f, _ in items[:self.config["top_per_cluster"]]])
        return result

    def generate_candidates(self, factor_names: List[str]) -> List[StrategyRule]:
        """③④ 生成候选条件 + 模板穷举"""
        builder = TemplateBuilder(factor_names, self.config["thresholds"])
        rules = []
        rules.extend(builder.build_t1())
        rules.extend(builder.build_t2())
        rules.extend(builder.build_t3())
        rules.extend(builder.build_t4())
        return rules

    def quick_backtest(self, rule: StrategyRule, stock_df: pd.DataFrame,
                       factor_df: pd.DataFrame) -> Optional[dict]:
        """⑤ 单只股票单条规则快速回测"""
        buy_signal = rule.get_buy_signal(factor_df)
        sell_signal = rule.get_sell_signal(factor_df)
        if buy_signal.sum() < 3:
            return None
        signal_df = stock_df[["close", "volume"]].copy()
        signal_df["BUY_SIGNAL"] = buy_signal.astype(int)
        signal_df["SELL_SIGNAL"] = sell_signal.astype(int)
        signal_df["BUY_SCORE"] = buy_signal.astype(float)
        signal_df["STRATEGY"] = rule.name
        if "open" in stock_df.columns:
            signal_df["open"] = stock_df["open"]
        try:
            bt = Backtester(initial_capital=100000)
            result = bt.run(signal_df)
            return {
                "total_return": result.total_return,
                "annual_return": result.annual_return,
                "win_rate": result.win_rate,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "total_trades": result.total_trades,
            }
        except Exception:
            return None

    def evaluate_rule(self, rule: StrategyRule, stock_data: Dict) -> Optional[dict]:
        """全市场采样回测（分层抽样，最多 sample_size 只）"""
        codes = list(stock_data.keys())
        if len(codes) > self.sample_size:
            codes = random.sample(codes, self.sample_size)
        results = []
        for code in codes:
            price_df, factor_df = stock_data[code]
            perf = self.quick_backtest(rule, price_df, factor_df)
            if perf:
                results.append(perf)
        if not results or len(results) < 5:
            return None
        n = len(results)
        return {
            "total_return": np.mean([r["total_return"] for r in results]),
            "annual_return": np.mean([r["annual_return"] for r in results]),
            "win_rate": np.mean([r["win_rate"] for r in results]),
            "sharpe_ratio": np.mean([r["sharpe_ratio"] for r in results]),
            "max_drawdown": np.max([r["max_drawdown"] for r in results]),
            "total_trades": np.sum([r["total_trades"] for r in results]),
            "sample_count": n,
        }

    def score_rule(self, perf: dict) -> float:
        """⑥ 综合评分: 0.3*收益 + 0.3*胜率 + 0.25*夏普 - 0.15*最大回撤"""
        return (0.3 * perf["total_return"] / 100.0
                + 0.3 * perf["win_rate"]
                + 0.25 * perf["sharpe_ratio"]
                - 0.15 * abs(perf["max_drawdown"]) / 100.0)

    def run_phase1(self, stock_data: Dict,
                   forward_returns: pd.Series,
                   factor_df: pd.DataFrame) -> List[dict]:
        """执行完整 Phase 1 流程"""
        active_factors = self.prune_factors(factor_df, forward_returns)
        candidates = self.generate_candidates(active_factors)
        scored = []
        for i, rule in enumerate(candidates):
            perf = self.evaluate_rule(rule, stock_data)
            if perf and perf["total_trades"] >= self.min_trades:
                score = self.score_rule(perf)
                scored.append((score, rule, perf))
        scored.sort(key=lambda x: x[0], reverse=True)
        top_rules = scored[:self.top_n]
        results = []
        for score, rule, perf in top_rules:
            rule_dict = {
                "rule_name": rule.name,
                "rule_type": rule.rule_type,
                "encoding": json.dumps([c.to_dict() for c in rule.conditions]),
                "conditions": json.dumps([c.to_dict() for c in rule.conditions]),
                "sell_conditions": json.dumps([c.to_dict() for c in rule.sell_conditions]),
                "holding_min": rule.holding_min,
                "holding_max": rule.holding_max,
                "source": "template",
                "fitness": score,
                "annual_return": perf["annual_return"],
                "win_rate": perf["win_rate"],
                "sharpe_ratio": perf["sharpe_ratio"],
                "max_drawdown": perf["max_drawdown"],
                "total_trades": perf["total_trades"],
            }
            rule_id = upsert_strategy_rule(rule_dict)
            results.append({"rule_id": rule_id, "score": score, "rule": rule})
        return results


def load_template_library() -> List[StrategyRule]:
    """加载模板库 (供 Phase 2 初始化)"""
    active = get_active_rules(min_fitness=0.3, limit=100)
    rules = []
    for row in active:
        try:
            conds_data = json.loads(row["conditions"])
            conds = [RuleCondition(**c) for c in conds_data]
            rules.append(StrategyRule(
                name=row["rule_name"], rule_type=row["rule_type"],
                conditions=conds, source=row.get("source", "template"),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return rules
