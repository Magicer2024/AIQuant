"""
genetic_evolver.py —— Phase 2: 遗传规划策略进化
===============================================
功能：
- 规则编码/解码（前缀表达式树）
- 遗传算子（精英保留、交叉、子树变异、阈值变异、因子替换、随机重置）
- 多目标适应度函数（复合评分 x 新颖性 x 简洁性 x 稳定性 x 合法性）
- 遗传进化器（GeneticEvolver）
- 模板库反馈机制
"""
import pandas as pd
import numpy as np
import json
import random
from typing import Dict, List, Optional, Tuple
from dataclasses import asdict

from strategy.rule_miner import (
    StrategyRule, RuleCondition, RuleMiner, load_template_library,
)
from strategy.factor_lib import (
    FACTOR_REGISTRY, get_factor_names_by_categories,
)
from core.db import upsert_strategy_rule, get_active_rules, degrade_rule
from backtest.backtest import Backtester


# ═══════════════════════════════════════════════════════
# 常量定义
# ═══════════════════════════════════════════════════════

FUNCTIONS = ["AND", "OR"]
TERMINAL_OPS = ["<", ">"]
MAX_DEPTH = 5
MAX_CONDITIONS = 7

FACTOR_RANGES = {
    "RSI_6": (0, 100), "RSI_9": (0, 100), "RSI_14": (0, 100), "RSI_21": (0, 100),
    "KDJ_K": (0, 100), "KDJ_D": (0, 100), "KDJ_J": (-20, 120),
    "BB_PCT_B": (0, 1), "ATR_PCT": (0, 0.5), "HV_10": (0, 2.0), "HV_20": (0, 2.0),
}
FACTOR_DEFAULT_RANGE = (0, 1)

# 所有因子名称列表（供随机选择）
_ALL_FACTOR_NAMES = list(FACTOR_REGISTRY.keys())

# 同类别因子高速缓存
_CATEGORY_FACTORS: Dict[str, List[str]] = {}
for _fname, _meta in FACTOR_REGISTRY.items():
    _cat = _meta.get("category", "unknown")
    _CATEGORY_FACTORS.setdefault(_cat, []).append(_fname)

# 遗传算子概率（累加和 = 1.0）
ELITE_PROB = 0.05
CROSSOVER_PROB = 0.35
SUBTREE_MUTATE_PROB = 0.15
THRESHOLD_MUTATE_PROB = 0.20
FACTOR_SWAP_PROB = 0.15
RANDOM_RESET_PROB = 0.10

# 精英保留数
DEFAULT_ELITE_COUNT = 5


# ═══════════════════════════════════════════════════════
# 1. 编码 / 解码
# ═══════════════════════════════════════════════════════

def encode_rule(conditions: List[RuleCondition]) -> str:
    """将条件列表编码为前缀表达式字符串。

    示例: [RSI_6 > 0.5, KDJ_K < 0.3] -> "AND > RSI_6 0.5 < KDJ_K 0.3"
    """
    if not conditions:
        return "NONE"
    if len(conditions) == 1:
        c = conditions[0]
        return f"{c.operator} {c.factor} {c.threshold:.4f}"
    # 多个条件全部 AND 连接
    parts = []
    for c in conditions:
        parts.append(f"{c.operator} {c.factor} {c.threshold:.4f}")
    return "AND " + " ".join(parts)


def decode_rule(encoded: str) -> List[RuleCondition]:
    """将前缀表达式解码为条件列表。

    示例: "AND > RSI_6 0.5 < KDJ_K 0.3" -> [RuleCondition("RSI_6", ">", 0.5), ...]
    """
    if not encoded or encoded == "NONE":
        return []
    tokens = encoded.split()
    conds = []
    i = 0

    def _parse_node(idx: int) -> Tuple[Optional[RuleCondition], int]:
        nonlocal conds
        if idx >= len(tokens):
            return None, idx
        tok = tokens[idx]
        if tok in ("AND", "OR"):
            idx += 1
            while idx < len(tokens) and len(conds) < MAX_CONDITIONS:
                if tokens[idx] in ("AND", "OR"):
                    idx += 1
                    continue
                if tokens[idx] in (">", "<"):
                    op = tokens[idx]
                    if idx + 2 >= len(tokens):
                        break
                    factor = tokens[idx + 1]
                    try:
                        threshold = float(tokens[idx + 2])
                    except ValueError:
                        break
                    cond = RuleCondition(factor=factor, operator=op, threshold=threshold)
                    conds.append(cond)
                    idx += 3
                else:
                    idx += 1
            return None, idx
        elif tok in (">", "<"):
            if idx + 2 < len(tokens):
                factor = tokens[idx + 1]
                try:
                    threshold = float(tokens[idx + 2])
                except ValueError:
                    return None, idx + 3
                cond = RuleCondition(factor=factor, operator=tok, threshold=threshold)
                conds.append(cond)
                return cond, idx + 3
            return None, idx + len(tokens)
        return None, idx

    _parse_node(0)
    return conds[:MAX_CONDITIONS]


def _get_factor_range(factor_name: str) -> Tuple[float, float]:
    """获取某个因子的有效阈值范围。"""
    if factor_name in FACTOR_RANGES:
        return FACTOR_RANGES[factor_name]
    # 归一化后的因子默认在 [0, 1] 范围
    return FACTOR_DEFAULT_RANGE


def _heuristic_op_for_factor(factor: str) -> str:
    """启发式算子选择，与 TemplateBuilder._op_for_factor 保持一致。"""
    upper = factor.upper()
    if "VOL" in upper:
        if "波动" in factor or "STD" in upper or "VOLATILITY" in upper:
            return "<"
        return ">"
    if upper.startswith("HV_") or "HV_" in upper:
        return "<"
    if "PCT_B" in upper:
        return "<"
    return random.choice([">", "<"])


def random_rule(depth: int = 3) -> StrategyRule:
    """生成一条随机的有效规则树。

    Args:
        depth: 规则深度，控制条件数量（1 ~ min(2^depth-1, MAX_CONDITIONS)）

    Returns:
        新的 StrategyRule 实例
    """
    max_n = min(2 ** depth - 1, MAX_CONDITIONS)
    n_conditions = random.randint(1, max(max_n, 1))
    conditions = []
    used_factors = set()
    for _ in range(n_conditions):
        # 避免同一因子重复出现
        available = [f for f in _ALL_FACTOR_NAMES if f not in used_factors]
        if not available:
            available = _ALL_FACTOR_NAMES  # 已用完不重复因子，回退到全量池
        factor = random.choice(available)
        used_factors.add(factor)
        op = _heuristic_op_for_factor(factor)
        lo, hi = _get_factor_range(factor)
        threshold = random.uniform(lo + (hi - lo) * 0.1, hi - (hi - lo) * 0.1)
        threshold = round(threshold, 4)
        conditions.append(RuleCondition(factor=factor, operator=op, threshold=threshold))

    unique_id = _gen_unique_id()
    return StrategyRule(
        name=f"GEN_{unique_id}",
        rule_type="GEN",
        conditions=conditions,
        sell_conditions=[],
        holding_min=3,
        holding_max=20,
        source="genetic",
    )


def _gen_unique_id() -> str:
    """生成 5 位唯一 ID。"""
    return f"{random.randint(0, 99999):05d}"


# ═══════════════════════════════════════════════════════
# 2. 遗传算子
# ═══════════════════════════════════════════════════════

def crossover(parent1: StrategyRule, parent2: StrategyRule) -> StrategyRule:
    """交叉算子：在随机分割点交换两个父代的条件列表。

    Args:
        parent1, parent2: 父代规则

    Returns:
        子代规则（继承 parent1 的元信息）
    """
    conds1 = list(parent1.conditions)
    conds2 = list(parent2.conditions)
    if not conds1 or not conds2:
        # 无法交叉，返回较好的父代
        return _copy_rule(
            parent1 if len(conds1) >= len(conds2) else parent2
        )

    split1 = random.randint(1, len(conds1)) if len(conds1) > 1 else 1
    split2 = random.randint(1, len(conds2)) if len(conds2) > 1 else 1

    new_conds = conds1[:split1] + conds2[split2:]
    # 截断到 MAX_CONDITIONS
    new_conds = new_conds[:MAX_CONDITIONS]

    unique_id = _gen_unique_id()
    return StrategyRule(
        name=f"GEN_{unique_id}",
        rule_type="GEN",
        conditions=new_conds,
        sell_conditions=list(parent1.sell_conditions),
        holding_min=parent1.holding_min,
        holding_max=parent1.holding_max,
        source="genetic",
    )


def mutate_subtree(rule: StrategyRule) -> StrategyRule:
    """子树变异：随机替换一个条件。

    Args:
        rule: 待变异的规则

    Returns:
        变异后的新规则
    """
    if not rule.conditions:
        return random_rule(depth=2)

    new_conds = list(rule.conditions)
    idx = random.randint(0, len(new_conds) - 1)

    # 生成一个替换条件
    existing_factors = {c.factor for c in new_conds}
    available = [f for f in _ALL_FACTOR_NAMES if f not in existing_factors]
    if not available:
        available = _ALL_FACTOR_NAMES
    factor = random.choice(available)
    op = _heuristic_op_for_factor(factor)
    lo, hi = _get_factor_range(factor)
    threshold = random.uniform(lo + (hi - lo) * 0.1, hi - (hi - lo) * 0.1)
    threshold = round(threshold, 4)
    new_conds[idx] = RuleCondition(factor=factor, operator=op, threshold=threshold)

    unique_id = _gen_unique_id()
    return StrategyRule(
        name=f"GEN_{unique_id}",
        rule_type="GEN",
        conditions=new_conds,
        sell_conditions=list(rule.sell_conditions),
        holding_min=rule.holding_min,
        holding_max=rule.holding_max,
        source="genetic",
    )


def mutate_threshold(rule: StrategyRule, sigma: float = 0.03) -> StrategyRule:
    """阈值变异：对每个条件的阈值施加 N(0, sigma) 高斯扰动，裁剪到因子范围。

    Args:
        rule: 待变异的规则
        sigma: 扰动标准差

    Returns:
        阈值扰动后的新规则
    """
    if not rule.conditions:
        return _copy_rule(rule)

    new_conds = []
    for c in rule.conditions:
        lo, hi = _get_factor_range(c.factor)
        perturbation = np.random.normal(0, sigma)
        new_threshold = c.threshold + perturbation
        new_threshold = max(lo, min(hi, new_threshold))
        new_threshold = round(new_threshold, 4)
        new_conds.append(RuleCondition(
            factor=c.factor, operator=c.operator, threshold=new_threshold,
            ref_factor=c.ref_factor,
        ))

    unique_id = _gen_unique_id()
    return StrategyRule(
        name=f"GEN_{unique_id}",
        rule_type="GEN",
        conditions=new_conds,
        sell_conditions=list(rule.sell_conditions),
        holding_min=rule.holding_min,
        holding_max=rule.holding_max,
        source="genetic",
    )


def mutate_factor(rule: StrategyRule) -> StrategyRule:
    """因子替换：随机选一个条件，换为同类别下的另一个因子。

    Args:
        rule: 待变异的规则

    Returns:
        因子替换后的新规则
    """
    if not rule.conditions:
        return _copy_rule(rule)

    new_conds = list(rule.conditions)
    # 随机选一个可替换的条件（该类别至少有 2 个因子）
    replaceable = []
    for i, c in enumerate(new_conds):
        cat = FACTOR_REGISTRY.get(c.factor, {}).get("category", "unknown")
        siblings = _CATEGORY_FACTORS.get(cat, [])
        if len(siblings) >= 2:
            replaceable.append(i)

    if not replaceable:
        # 无可替换的类别，回退到阈值变异
        return mutate_threshold(rule, sigma=0.02)

    idx = random.choice(replaceable)
    old_c = new_conds[idx]
    cat = FACTOR_REGISTRY.get(old_c.factor, {}).get("category", "unknown")
    siblings = [f for f in _CATEGORY_FACTORS.get(cat, []) if f != old_c.factor]
    new_factor = random.choice(siblings)
    new_op = _heuristic_op_for_factor(new_factor)
    lo, hi = _get_factor_range(new_factor)
    threshold = random.uniform(lo + (hi - lo) * 0.1, hi - (hi - lo) * 0.1)
    threshold = round(threshold, 4)
    new_conds[idx] = RuleCondition(factor=new_factor, operator=new_op, threshold=threshold)

    unique_id = _gen_unique_id()
    return StrategyRule(
        name=f"GEN_{unique_id}",
        rule_type="GEN",
        conditions=new_conds,
        sell_conditions=list(rule.sell_conditions),
        holding_min=rule.holding_min,
        holding_max=rule.holding_max,
        source="genetic",
    )


def tournament_select(
    population: List[Tuple[StrategyRule, float]],
    tournament_size: int = 3,
) -> StrategyRule:
    """锦标赛选择：随机抽 tournament_size 个个体，返回适应度最高的规则。

    Args:
        population: [(rule, fitness), ...] 列表
        tournament_size: 锦标赛规模

    Returns:
        胜出的 StrategyRule
    """
    if not population:
        raise ValueError("population cannot be empty")
    contestants = random.sample(population, min(tournament_size, len(population)))
    contestants.sort(key=lambda x: x[1], reverse=True)
    return contestants[0][0]


# ═══════════════════════════════════════════════════════
# 3. 适应度函数
# ═══════════════════════════════════════════════════════

def signal_overlap(
    rule1: StrategyRule,
    rule2: StrategyRule,
    factor_df: pd.DataFrame,
) -> float:
    """计算两条规则买入信号的重叠率 (Jaccard 指数)。

    overlap = |signal1 ∩ signal2| / |signal1 ∪ signal2|

    Args:
        rule1, rule2: 两条规则
        factor_df: 因子 DataFrame

    Returns:
        重叠率 [0, 1]
    """
    s1 = rule1.get_buy_signal(factor_df).astype(bool)
    s2 = rule2.get_buy_signal(factor_df).astype(bool)
    intersection = (s1 & s2).sum()
    union = (s1 | s2).sum()
    if union == 0:
        return 0.0
    return float(intersection / union)


def novelty_bonus(
    new_rule: StrategyRule,
    existing_rules: List[StrategyRule],
    factor_df: pd.DataFrame,
) -> float:
    """计算新颖性奖励因子。

    - 与任意已有规则的信号重叠 < 20% -> 1.2
    - 20%-40% -> 1.0
    - 40%-70% -> 0.7
    - > 70% -> 0.3

    Args:
        new_rule: 新规则
        existing_rules: 已有规则列表
        factor_df: 因子 DataFrame

    Returns:
        新颖性奖励因子
    """
    if not existing_rules:
        return 1.2  # 没有任何已有规则时，视为高度新颖
    max_overlap = 0.0
    for existing in existing_rules:
        try:
            overlap = signal_overlap(new_rule, existing, factor_df)
            max_overlap = max(max_overlap, overlap)
        except Exception:
            continue
    if max_overlap < 0.2:
        return 1.2
    elif max_overlap < 0.4:
        return 1.0
    elif max_overlap <= 0.7:
        return 0.7
    else:
        return 0.3


def simplicity_penalty(n_conditions: int) -> float:
    """计算简洁性惩罚因子。

    - n <= 3 -> 1.0
    - 4 <= n <= 5 -> 0.85^(n-3)
    - n > 5 -> 0.7^(n-3)

    Args:
        n_conditions: 条件数量

    Returns:
        简洁性因子 (0, 1]
    """
    if n_conditions <= 3:
        return 1.0
    elif n_conditions <= 5:
        return 0.85 ** (n_conditions - 3)
    else:
        return 0.7 ** (n_conditions - 3)


def legality_check(rule: StrategyRule) -> float:
    """合法性检查：检测逻辑矛盾和阈值越界。

    返回值:
        0.0 — 存在逻辑矛盾（如 RSI > 80 AND RSI < 30）
        0.1 — 阈值越界
        1.0 — 干净规则

    Args:
        rule: 待检查的规则

    Returns:
        合法性因子 {0.0, 0.1, 1.0}
    """
    if not rule.conditions:
        return 0.1

    # 检查每个条件的阈值是否在因子有效范围内
    for c in rule.conditions:
        lo, hi = _get_factor_range(c.factor)
        if c.threshold < lo or c.threshold > hi:
            return 0.1

    # 检查同一因子的矛盾条件
    factor_conds: Dict[str, List[RuleCondition]] = {}
    for c in rule.conditions:
        factor_conds.setdefault(c.factor, []).append(c)

    for factor, conds in factor_conds.items():
        if len(conds) < 2:
            continue
        # 提取 > 和 < 条件的阈值
        gt_thresholds = [c.threshold for c in conds if c.operator == ">"]
        lt_thresholds = [c.threshold for c in conds if c.operator == "<"]

        # 存在 >X 且 <Y 且 X >= Y -> 逻辑矛盾
        for gt in gt_thresholds:
            for lt in lt_thresholds:
                if gt >= lt:
                    return 0.0

    return 1.0


def _composite_score(perf: dict) -> float:
    """计算复合回测评分。

    composite = 0.3 * annual_return/100 + 0.3 * win_rate/100
              + 0.25 * sharpe - 0.15 * |max_drawdown|/100 - 0.1 * turnover_rate

    Args:
        perf: evaluate_rule 返回的性能字典

    Returns:
        复合评分
    """
    ar = perf.get("annual_return", 0) / 100.0
    wr = perf.get("win_rate", 0) / 100.0
    sr = perf.get("sharpe_ratio", 0)
    md = abs(perf.get("max_drawdown", 0)) / 100.0
    # turnover_rate: 用 total_trades / 样本天数估算，如没有则 0
    turnover = perf.get("turnover_rate", 0)
    return 0.3 * ar + 0.3 * wr + 0.25 * sr - 0.15 * md - 0.1 * turnover


def _stability_penalty(
    train_perf: Optional[dict],
    valid_perf: Optional[dict],
    test_perf: Optional[dict],
) -> float:
    """计算稳定性惩罚：三段收益率波动超过 30% 则惩罚。

    Args:
        train_perf, valid_perf, test_perf: 三个分段的性能

    Returns:
        稳定性因子: 0.6 或 1.0
    """
    returns = []
    for perf in [train_perf, valid_perf, test_perf]:
        if perf is not None:
            returns.append(perf.get("annual_return", 0))

    if len(returns) < 2:
        return 1.0

    returns_arr = np.array(returns, dtype=float)
    if np.std(returns_arr) == 0:
        return 1.0
    # 使用变异系数 (CV = std/|mean|) 判断波动
    mean_abs = abs(np.mean(returns_arr))
    if mean_abs < 1e-9:
        # 均值接近0, 直接用标准差判断 (除以1避免除零)
        cv = np.std(returns_arr)
    else:
        cv = np.std(returns_arr) / mean_abs
    return 0.6 if cv > 0.30 else 1.0


def compute_fitness(
    perf: dict,
    rule: StrategyRule,
    existing_rules: List[StrategyRule],
    factor_df: Optional[pd.DataFrame] = None,
    train_perf: Optional[dict] = None,
    valid_perf: Optional[dict] = None,
    test_perf: Optional[dict] = None,
) -> float:
    """计算完整的多目标适应度。

    fitness = composite_score x novelty_bonus x simplicity_penalty
            x stability_penalty x legality_penalty

    Args:
        perf: 回测性能字典（来自 evaluate_rule 或 quick_backtest）
        rule: 策略规则
        existing_rules: 已有规则列表（用于新颖性计算）
        factor_df: 因子 DataFrame（用于信号重叠计算）
        train_perf, valid_perf, test_perf: 三段性能（用于稳定性）

    Returns:
        适应度评分
    """
    cs = _composite_score(perf)

    # 新颖性
    if factor_df is not None and existing_rules:
        nb = novelty_bonus(rule, existing_rules, factor_df)
    else:
        nb = 1.0

    # 简洁性
    sp = simplicity_penalty(len(rule.conditions))

    # 稳定性
    stp = _stability_penalty(train_perf, valid_perf, test_perf)

    # 合法性
    lp = legality_check(rule)

    return cs * nb * sp * stp * lp


# ═══════════════════════════════════════════════════════
# 4. GeneticEvolver 类
# ═══════════════════════════════════════════════════════

class GeneticEvolver:
    """Phase 2: 遗传进化器。

    流程:
    1. 初始化种群: 30 条来自 Phase 1 模板库 + 20 条随机规则
    2. 逐代进化 (最多 max_generations 代):
       a. 适应度评估
       b. 精英保留
       c. 遗传算子生成新个体
       d. 适应度再评估
       e. 早停检查
    3. 合格规则入库
    """

    def __init__(
        self,
        population_size: int = 50,
        max_generations: int = 50,
        early_stop_generations: int = 8,
        early_stop_threshold: float = 0.02,
        seed: int = None,
    ):
        """初始化进化器。

        Args:
            population_size: 种群规模
            max_generations: 最大进化代数
            early_stop_generations: 连续无改进代数阈值
            early_stop_threshold: 适应度改进的最小阈值
            seed: 随机种子
        """
        self.population_size = population_size
        self.max_generations = max_generations
        self.early_stop_generations = early_stop_generations
        self.early_stop_threshold = early_stop_threshold
        self.seed = seed

        # 遗传算子概率（累加区间）
        # elite: 0.05, crossover: 0.35, subtree: 0.15, threshold: 0.20,
        # factor_swap: 0.15, random_reset: 0.10
        self._op_probs = [
            (ELITE_PROB, "elite"),
            (ELITE_PROB + CROSSOVER_PROB, "crossover"),
            (ELITE_PROB + CROSSOVER_PROB + SUBTREE_MUTATE_PROB, "subtree_mutate"),
            (ELITE_PROB + CROSSOVER_PROB + SUBTREE_MUTATE_PROB + THRESHOLD_MUTATE_PROB,
             "threshold_mutate"),
            (ELITE_PROB + CROSSOVER_PROB + SUBTREE_MUTATE_PROB + THRESHOLD_MUTATE_PROB
             + FACTOR_SWAP_PROB, "factor_swap"),
            (1.0, "random_reset"),
        ]
        self.elite_count = DEFAULT_ELITE_COUNT

        # RuleMiner 实例（用于回测评估）
        self._miner: Optional[RuleMiner] = None

    def _get_miner(self) -> RuleMiner:
        """延迟创建 RuleMiner 实例。"""
        if self._miner is None:
            self._miner = RuleMiner(seed=self.seed)
        return self._miner

    def _select_operator(self) -> str:
        """按概率选择遗传算子。

        Returns:
            算子名称: "elite" | "crossover" | "subtree_mutate" | "threshold_mutate"
                      | "factor_swap" | "random_reset"
        """
        r = random.random()
        for threshold, name in self._op_probs:
            if r < threshold:
                return name
        return "random_reset"

    def initialize_population(self) -> List[StrategyRule]:
        """初始化种群: 30 条来自 Phase 1 模板库 + 20 条随机规则。

        Returns:
            初始种群列表
        """
        population = []

        # 从模板库加载
        try:
            templates = load_template_library()
            # 取前 30 条
            population.extend(templates[:30])
        except Exception:
            pass

        # 补充到 30 条模板（如模板库不足）
        while len(population) < 30:
            population.append(random_rule(depth=3))

        # 20 条随机规则
        for _ in range(20):
            population.append(random_rule(depth=random.randint(2, 4)))

        # 裁剪到 population_size
        return population[:self.population_size]

    def evolve_one_generation(
        self,
        population: List[Tuple[StrategyRule, float]],
    ) -> List[StrategyRule]:
        """对一代种群执行遗传操作。

        Args:
            population: [(rule, fitness), ...] 已按适应度降序排列

        Returns:
            新一代规则列表（未评估适应度）
        """
        if not population:
            return []

        new_population: List[StrategyRule] = []

        # 精英保留：前 elite_count 直接进入下一代
        elite_count = min(self.elite_count, len(population))
        for i in range(elite_count):
            new_population.append(_copy_rule(population[i][0]))

        # 生成新个体直到种群满
        while len(new_population) < self.population_size:
            op = self._select_operator()

            if op == "elite":
                # 精英已保留完毕，跳过
                if len(new_population) < self.population_size:
                    pick = tournament_select(population, tournament_size=3)
                    new_population.append(_copy_rule(pick))

            elif op == "crossover":
                p1 = tournament_select(population, tournament_size=3)
                p2 = tournament_select(population, tournament_size=3)
                # 确保父代不相同
                attempts = 0
                while p1.name == p2.name and attempts < 5:
                    p2 = tournament_select(population, tournament_size=3)
                    attempts += 1
                child = crossover(p1, p2)
                new_population.append(child)

            elif op == "subtree_mutate":
                parent = tournament_select(population, tournament_size=3)
                child = mutate_subtree(parent)
                new_population.append(child)

            elif op == "threshold_mutate":
                parent = tournament_select(population, tournament_size=3)
                child = mutate_threshold(parent, sigma=0.03)
                new_population.append(child)

            elif op == "factor_swap":
                parent = tournament_select(population, tournament_size=3)
                child = mutate_factor(parent)
                new_population.append(child)

            elif op == "random_reset":
                # 对适应度最低的 5 个体重置
                sorted_pop = sorted(population, key=lambda x: x[1])
                bottom_count = min(5, len(sorted_pop))
                # 随机选择 bottom 中一个替换
                victim = random.choice(sorted_pop[:bottom_count])
                new_population.append(random_rule(depth=random.randint(2, 4)))

        return new_population[:self.population_size]

    def run_evolution(
        self,
        stock_data: Dict,
        factor_df: Optional[pd.DataFrame] = None,
        forward_returns: Optional[pd.Series] = None,
    ) -> List[dict]:
        """执行完整的遗传进化流程。

        Args:
            stock_data: 股票数据字典 {code: (price_df, factor_df)}
            factor_df: 用于计算信号重叠的因子 DataFrame
            forward_returns: 前向收益率（保留以兼容 RuleMiner 接口）

        Returns:
            结果列表 [{rule_id, rule, fitness, generation}, ...]
        """
        if self.seed is not None:
            random.seed(self.seed)
            np.random.seed(self.seed)

        miner = self._get_miner()
        results: List[dict] = []

        # 1. 初始化种群
        population = self.initialize_population()

        # 2. 初始适应度评估
        evaluated: List[Tuple[StrategyRule, float, dict]] = []  # (rule, fitness, perf)
        for rule in population:
            perf = miner.evaluate_rule(rule, stock_data)
            if perf is None:
                perf = {
                    "annual_return": 0, "win_rate": 0, "sharpe_ratio": 0,
                    "max_drawdown": -50, "total_trades": 0, "total_return": 0,
                    "sample_count": 0,
                }
            # 获取已有规则用于新颖性
            existing_rules = [r for r, _, _ in evaluated]
            ft = compute_fitness(perf, rule, existing_rules, factor_df)
            evaluated.append((rule, ft, perf))

        # 按适应度排序
        evaluated.sort(key=lambda x: x[1], reverse=True)
        best_fitness = evaluated[0][1] if evaluated else 0.0
        no_improve_count = 0

        # 3. 进化循环
        for gen in range(1, self.max_generations + 1):
            # 生成新一代
            new_rules = self.evolve_one_generation(
                [(r, f) for r, f, _ in evaluated]
            )

            # 评估新一代
            new_evaluated: List[Tuple[StrategyRule, float, dict]] = []
            for rule in new_rules:
                perf = miner.evaluate_rule(rule, stock_data)
                if perf is None:
                    perf = {
                        "annual_return": 0, "win_rate": 0, "sharpe_ratio": 0,
                        "max_drawdown": -50, "total_trades": 0, "total_return": 0,
                        "sample_count": 0,
                    }
                existing_rules = [r for r, _, _ in evaluated] + \
                                 [r for r, _, _ in new_evaluated]
                ft = compute_fitness(perf, rule, existing_rules, factor_df)
                new_evaluated.append((rule, ft, perf))

            evaluated = new_evaluated
            evaluated.sort(key=lambda x: x[1], reverse=True)

            current_best = evaluated[0][1] if evaluated else 0.0
            improvement = current_best - best_fitness

            if improvement > self.early_stop_threshold:
                best_fitness = current_best
                no_improve_count = 0
            else:
                no_improve_count += 1

            # 早停
            if no_improve_count >= self.early_stop_generations:
                break

        # 4. 入库合格规则 (fitness > 0.5, 信号重叠 < 75%)
        inserted = 0
        for rule, fitness, perf in evaluated:
            if fitness <= 0.5:
                continue

            # 检查与已入库规则的信号重叠
            if factor_df is not None:
                existing_in_db = [
                    StrategyRule(
                        name=r["rule_name"], rule_type=r["rule_type"],
                        conditions=json.loads(r["conditions"])
                        if isinstance(r["conditions"], str) else r["conditions"],
                    )
                    for r in get_active_rules(min_fitness=0.3, limit=100)
                ]
                try:
                    max_ov = 0.0
                    for er in existing_in_db:
                        ov = signal_overlap(rule, er, factor_df)
                        max_ov = max(max_ov, ov)
                    if max_ov > 0.75:
                        continue
                except Exception:
                    pass

            rule_dict = {
                "rule_name": rule.name,
                "rule_type": rule.rule_type,
                "encoding": encode_rule(rule.conditions),
                "conditions": json.dumps([c.to_dict() for c in rule.conditions]),
                "sell_conditions": json.dumps(
                    [c.to_dict() for c in rule.sell_conditions]
                ),
                "holding_min": rule.holding_min,
                "holding_max": rule.holding_max,
                "source": "genetic",
                "generation": 2,
                "fitness": fitness,
                "annual_return": perf.get("annual_return", 0),
                "win_rate": perf.get("win_rate", 0),
                "sharpe_ratio": perf.get("sharpe_ratio", 0),
                "max_drawdown": perf.get("max_drawdown", 0),
                "total_trades": perf.get("total_trades", 0),
                "signal_overlap": 0,
            }
            try:
                rule_id = upsert_strategy_rule(rule_dict)
                results.append({
                    "rule_id": rule_id,
                    "rule": rule,
                    "fitness": fitness,
                    "generation": 2,
                })
                inserted += 1
            except Exception:
                continue

        return results


# ═══════════════════════════════════════════════════════
# 5. 模板反馈
# ═══════════════════════════════════════════════════════

def feedback_to_phase1(
    phase2_results: List[dict],
    top_n: int = 20,
) -> int:
    """Phase 2 优质规则回流至 Phase 1 模板库。

    将进化产生的高适应度规则以 source="template" 重新入库，供下一轮
    Phase 1 模板穷举使用。

    Args:
        phase2_results: Phase 2 run_evolution 返回的结果列表
        top_n: 回流的规则数量

    Returns:
        回流成功的规则数
    """
    # 按适应度降序
    sorted_results = sorted(
        phase2_results, key=lambda x: x.get("fitness", 0), reverse=True
    )
    top = sorted_results[:top_n]
    count = 0
    for item in top:
        rule: StrategyRule = item["rule"]
        try:
            rule_dict = {
                "rule_name": rule.name,
                "rule_type": rule.rule_type,
                "encoding": encode_rule(rule.conditions),
                "conditions": json.dumps([c.to_dict() for c in rule.conditions]),
                "sell_conditions": json.dumps(
                    [c.to_dict() for c in rule.sell_conditions]
                ),
                "holding_min": rule.holding_min,
                "holding_max": rule.holding_max,
                "source": "template",  # 以模板身份回流
                "generation": 1,
                "fitness": item.get("fitness", 0),
                "annual_return": 0,
                "win_rate": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
                "total_trades": 0,
            }
            upsert_strategy_rule(rule_dict)
            count += 1
        except Exception:
            continue
    return count


def cleanup_template_library() -> int:
    """清理模板库中的劣化规则。

    降级标准:
    - sharpe_ratio < 1.0
    - max_drawdown > 30% (绝对值)

    Returns:
        降级的规则数
    """
    active_rules = get_active_rules(min_fitness=0.0, limit=500)
    count = 0
    for row in active_rules:
        sharpe = row.get("sharpe_ratio", 0) or 0
        drawdown = abs(row.get("max_drawdown", 0) or 0)
        if sharpe < 1.0 or drawdown > 30:
            try:
                degrade_rule(row["id"])
                count += 1
            except Exception:
                continue
    return count


# ═══════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════

def _copy_rule(rule: StrategyRule) -> StrategyRule:
    """深拷贝一条规则，生成新的名字。"""
    unique_id = _gen_unique_id()
    return StrategyRule(
        name=f"GEN_{unique_id}",
        rule_type=rule.rule_type,
        conditions=list(rule.conditions),
        sell_conditions=list(rule.sell_conditions),
        holding_min=rule.holding_min,
        holding_max=rule.holding_max,
        source=rule.source,
    )
