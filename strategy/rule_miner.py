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
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
from itertools import combinations
import json
import random

from strategy.factor_lib import (
    FACTOR_REGISTRY, calc_all_ic, filter_by_ic,
)
from config.strategy_params import PHASE1_CONFIG, CROSS_PAIRS
from core.db import upsert_strategy_rule, get_active_rules
from backtest.backtest import Backtester


@dataclass
class RuleCondition:
    factor: str
    operator: str      # "<" | ">" | "cross_above" | "cross_below" | "cross_above_factor" | "cross_below_factor"
    threshold: float = 0.5
    ref_factor: Optional[str] = None  # reference factor for cross_*_factor operators

    def to_dict(self):
        d = asdict(self)
        if self.ref_factor is None:
            del d["ref_factor"]
        return d

    def evaluate(self, factor_df: pd.DataFrame) -> pd.Series:
        if self.factor not in factor_df.columns:
            return pd.Series(False, index=factor_df.index)
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
        elif self.operator == "cross_above_factor":
            if self.ref_factor is None or self.ref_factor not in factor_df.columns:
                return pd.Series(False, index=factor_df.index)
            return (factor_df[self.factor] > factor_df[self.ref_factor]) & \
                   (factor_df[self.factor].shift(1) <= factor_df[self.ref_factor].shift(1))
        elif self.operator == "cross_below_factor":
            if self.ref_factor is None or self.ref_factor not in factor_df.columns:
                return pd.Series(False, index=factor_df.index)
            return (factor_df[self.factor] < factor_df[self.ref_factor]) & \
                   (factor_df[self.factor].shift(1) >= factor_df[self.ref_factor].shift(1))
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

    @staticmethod
    def _op_for_factor(factor: str, *, default: str = ">") -> str:
        """Heuristic operator selection for a given factor name.

        - Volume factors: ">" for VOL_RATIO, OBV (rising volume is bullish);
          "<" for VOL_波动率 (high vol-of-vol is bearish).
        - HV_* and BB_PCT_B: "<" (overbought / high volatility signals caution).
        - Default: the caller-supplied default.
        """
        upper = factor.upper()
        if "VOL" in upper:
            if "波动" in factor or "STD" in upper or "VOLATILITY" in upper:
                return "<"
            return ">"  # VOL_RATIO, OBV, etc.
        if upper.startswith("HV_") or "HV_" in upper:
            return "<"
        if "PCT_B" in upper:
            return "<"
        return default

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
            for f1 in cat_factors[cat_a][:2]:
                for f2 in cat_factors[cat_b][:2]:
                    for t1 in self.thresholds[1:4]:
                        for t2 in self.thresholds[1:4]:
                            op1 = self._op_for_factor(f1, default=">")
                            op2 = self._op_for_factor(f2, default=">")
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
                conditions=[RuleCondition(f_fast, "cross_above_factor", ref_factor=f_slow)],
                sell_conditions=[RuleCondition(f_fast, "cross_below_factor", ref_factor=f_slow)],
            ))
        return rules

    def build_t4(self) -> List[StrategyRule]:
        """三因子确认模板"""
        t2_rules = self.build_t2()
        if len(t2_rules) > 80:
            t2_rules = random.sample(t2_rules, 80)
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
                 top_n: int = None, seed: int = None):
        self.config = PHASE1_CONFIG
        self.sample_size = sample_size or self.config["sample_stocks"]
        self.min_trades = min_trades or self.config["min_trades"]
        self.top_n = top_n or self.config["top_n_rules"]
        self.seed = seed

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
        if buy_signal.sum() < 2:
            return None
        signal_df = stock_df[["trade_date", "close", "volume"]].copy()
        signal_df["trade_date"] = pd.to_datetime(signal_df["trade_date"])
        signal_df = signal_df.set_index("trade_date")
        signal_df["BUY_SIGNAL"] = buy_signal.astype(int).values
        signal_df["SELL_SIGNAL"] = sell_signal.astype(int).values
        signal_df["BUY_SCORE"] = buy_signal.astype(float).values
        signal_df["STRATEGY"] = rule.name
        if "open" in stock_df.columns:
            signal_df["open"] = stock_df["open"].values
        try:
            bt = Backtester(initial_capital=100000,
                           use_stop_loss=False, use_take_profit=False,
                           use_drawdown_guard=False, use_market_timing=False,
                           use_dynamic_position=False)
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
        for i, code in enumerate(codes):
            price_df, factor_df = stock_data[code]
            perf = self.quick_backtest(rule, price_df, factor_df)
            if perf:
                results.append(perf)
            # 早期终止：评估15只后仍无有效回测则跳过
            if i >= 15 and len(results) < 1:
                return None
        if not results or len(results) < 2:
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
                + 0.3 * perf["win_rate"] / 100.0
                + 0.25 * perf["sharpe_ratio"]
                - 0.15 * abs(perf["max_drawdown"]) / 100.0)

    def run_phase1(self, stock_data: Dict,
                   forward_returns: pd.Series,
                   factor_df: pd.DataFrame) -> List[dict]:
        """执行完整 Phase 1 流程"""
        import time
        t0 = time.time()
        if self.seed is not None:
            random.seed(self.seed)
            np.random.seed(self.seed)
        active_factors = self.prune_factors(factor_df, forward_returns)
        print(f"[Phase1] 因子剪枝完成: {len(active_factors)} 个因子 (耗时 {time.time()-t0:.1f}s)")
        candidates = self.generate_candidates(active_factors)
        print(f"[Phase1] 候选规则生成: {len(candidates)} 条 (耗时 {time.time()-t0:.1f}s)")
        scored = []
        # 数据抽查：打印第一只股票的前几个因子值
        if stock_data:
            sample_code = next(iter(stock_data))
            _, sample_fd = stock_data[sample_code]
            sample_cols = [c for c in active_factors[:5] if c in sample_fd.columns]
            if sample_cols:
                print(f"[Phase1] 数据抽查({sample_code}): {sample_fd[sample_cols].iloc[-1].to_dict()}")
            else:
                print(f"[Phase1] 警告: 样本因子列不存在! active_factors前5={active_factors[:5]}, sample_fd列={list(sample_fd.columns)[:5]}")
        for i, rule in enumerate(candidates):
            if i > 0 and i % 200 == 0:
                print(f"[Phase1] 评估进度: {i}/{len(candidates)} (找到 {len(scored)} 条有效规则, 耗时 {time.time()-t0:.1f}s)")
            # 调试：前3条规则输出详细信息
            if i < 3 and stock_data:
                sc = next(iter(stock_data))
                _, sfd = stock_data[sc]
                bs = rule.get_buy_signal(sfd)
                c_info = []
                for c in rule.conditions:
                    if c.factor in sfd.columns:
                        col = sfd[c.factor]
                        c_info.append(f"{c.factor}[{col.min():.2f}~{col.max():.2f}] {c.operator} {c.threshold}")
                    else:
                        c_info.append(f"{c.factor}[MISSING]")
                print(f"[Phase1] 规则#{i}: {rule.name} | {', '.join(c_info)} | 买入信号={bs.sum()}")
            perf = self.evaluate_rule(rule, stock_data)
            if perf and perf["total_trades"] >= self.min_trades:
                score = self.score_rule(perf)
                scored.append((score, rule, perf))
        print(f"[Phase1] 评估完成: {len(scored)} 条有效规则 (耗时 {time.time()-t0:.1f}s)")
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
            sell_conds = []
            sell_raw = row.get("sell_conditions", "[]")
            if sell_raw and sell_raw != "[]":
                try:
                    sell_data = json.loads(sell_raw)
                    sell_conds = [RuleCondition(**s) for s in sell_data]
                except (json.JSONDecodeError, TypeError):
                    pass
            rules.append(StrategyRule(
                name=row["rule_name"], rule_type=row["rule_type"],
                conditions=conds, sell_conditions=sell_conds,
                holding_min=row.get("holding_min", 3),
                holding_max=row.get("holding_max", 20),
                source=row.get("source", "template"),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return rules
