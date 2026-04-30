"""
参数自适应优化模块
==================
功能：
- 网格搜索参数优化
- 遗传算法参数优化
- 步行优化参数
- 参数敏感性分析
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Callable
from itertools import product
import copy
from backtest.backtest import Backtester


class ParameterOptimizer:
    """
    参数优化器基类
    """

    def __init__(self, strategy_func: Callable, df: pd.DataFrame,
                 param_grid: Dict[str, List], metric: str = "sharpe_ratio"):
        """
        Args:
            strategy_func: 策略函数，接受df返回带信号的df
            df: 原始行情数据
            param_grid: 参数网格，如 {'vol_factor': [1.2, 1.5, 1.8], 'rise_3d': [0.01, 0.02]}
            metric: 优化目标指标 (sharpe_ratio/total_return/win_rate/max_drawdown)
        """
        self.strategy_func = strategy_func
        self.df = df
        self.param_grid = param_grid
        self.metric = metric
        self.results = []

    def evaluate(self, params: Dict) -> Dict:
        """评估单组参数"""
        # 复制数据
        df_test = self.df.copy()
        
        # 应用策略
        try:
            df_result = self.strategy_func(df_test, **params)
        except Exception as e:
            return {"score": -999, "error": str(e)}

        # 回测
        bt = Backtester(initial_capital=100000)
        result = bt.run(df_result)

        # 计算评分
        if self.metric == "sharpe_ratio":
            score = result.sharpe_ratio
        elif self.metric == "total_return":
            score = result.total_return
        elif self.metric == "win_rate":
            score = result.win_rate
        elif self.metric == "max_drawdown":
            score = -result.max_drawdown  # 负数，因为越小越好
        elif self.metric == "calmar_ratio":
            # 卡玛比率 = 年化收益 / 最大回撤
            score = result.annual_return / abs(result.max_drawdown) if result.max_drawdown != 0 else 0
        else:
            score = result.sharpe_ratio

        return {
            "params": params,
            "score": score,
            "total_return": result.total_return,
            "annual_return": result.annual_return,
            "max_drawdown": result.max_drawdown,
            "sharpe_ratio": result.sharpe_ratio,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
        }

    def grid_search(self, verbose: bool = True) -> List[Dict]:
        """网格搜索"""
        # 生成所有参数组合
        keys = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        combinations = list(product(*values))

        if verbose:
            print(f"[参数优化] 网格搜索 {len(combinations)} 种组合...")

        results = []
        for i, combo in enumerate(combinations):
            params = dict(zip(keys, combo))
            result = self.evaluate(params)
            results.append(result)

            if verbose and (i + 1) % 10 == 0:
                print(f"  已完成 {i+1}/{len(combinations)}")

        # 按评分排序
        results.sort(key=lambda x: x["score"], reverse=True)
        self.results = results

        if verbose:
            print(f"[参数优化] 最佳参数: {results[0]['params']}")
            print(f"[参数优化] 最佳 {self.metric}: {results[0]['score']:.2f}")

        return results


class GeneticOptimizer(ParameterOptimizer):
    """
    遗传算法参数优化
    适用于参数空间较大的情况
    """

    def __init__(self, strategy_func: Callable, df: pd.DataFrame,
                 param_ranges: Dict[str, Tuple], metric: str = "sharpe_ratio",
                 population_size: int = 20, generations: int = 10,
                 mutation_rate: float = 0.1, crossover_rate: float = 0.7):
        """
        Args:
            param_ranges: 参数范围，如 {'vol_factor': (1.0, 2.0), 'rise_3d': (0.01, 0.05)}
        """
        super().__init__(strategy_func, df, param_ranges, metric)
        self.param_ranges = param_ranges
        self.population_size = population_size
        self.generations = generations
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.best_result = None

    def _random_params(self) -> Dict:
        """生成随机参数"""
        params = {}
        for key, (low, high) in self.param_ranges.items():
            if isinstance(low, int) and isinstance(high, int):
                params[key] = np.random.randint(low, high + 1)
            else:
                params[key] = np.random.uniform(low, high)
        return params

    def _crossover(self, p1: Dict, p2: Dict) -> Dict:
        """交叉"""
        if np.random.random() > self.crossover_rate:
            return p1.copy()

        child = {}
        for key in p1.keys():
            child[key] = p1[key] if np.random.random() > 0.5 else p2[key]
        return child

    def _mutate(self, params: Dict) -> Dict:
        """变异"""
        mutated = params.copy()
        for key, (low, high) in self.param_ranges.items():
            if np.random.random() < self.mutation_rate:
                if isinstance(low, int) and isinstance(high, int):
                    mutated[key] = np.random.randint(low, high + 1)
                else:
                    mutated[key] = np.random.uniform(low, high)
        return mutated

    def optimize(self, verbose: bool = True) -> Dict:
        """运行遗传算法优化"""
        # 初始化种群
        population = [self._random_params() for _ in range(self.population_size)]

        best_result = None

        for gen in range(self.generations):
            # 评估种群
            results = [self.evaluate(p) for p in population]

            # 按评分排序
            results.sort(key=lambda x: x["score"], reverse=True)

            if best_result is None or results[0]["score"] > best_result["score"]:
                best_result = results[0].copy()
                best_result["generation"] = gen

            if verbose:
                print(f"[遗传算法] 第 {gen+1}/{self.generations} 代, 最佳评分: {best_result['score']:.2f}")

            # 选择优秀个体
            elite_count = self.population_size // 2
            elite = [r["params"] for r in results[:elite_count]]

            # 生成新一代
            new_population = elite.copy()

            while len(new_population) < self.population_size:
                # 锦标赛选择
                tournament = np.random.choice(len(elite), 2, replace=False)
                p1, p2 = elite[tournament[0]], elite[tournament[1]]

                # 交叉
                child = self._crossover(p1, p2)
                # 变异
                child = self._mutate(child)

                new_population.append(child)

            population = new_population[:self.population_size]

        self.best_result = best_result
        if verbose:
            print(f"[遗传算法] 最优参数: {best_result['params']}")
            print(f"[遗传算法] 最优评分: {best_result['score']:.2f}")

        return best_result


class WalkForwardOptimizer:
    """
    步行优化（Walk-Forward Optimization）
    模拟真实交易：使用历史数据优化参数，然后在未来数据上验证
    """

    def __init__(self, strategy_func: Callable, df: pd.DataFrame,
                 param_grid: Dict[str, List], train_ratio: float = 0.7):
        """
        Args:
            train_ratio: 训练集比例
        """
        self.strategy_func = strategy_func
        self.df = df
        self.param_grid = param_grid
        self.train_ratio = train_ratio

    def run(self, n_folds: int = 3, verbose: bool = True) -> Dict:
        """
        运行步行优化
        
        Args:
            n_folds: 滚动验证的折数
        """
        n = len(self.df)
        train_size = int(n * self.train_ratio)

        fold_results = []

        for fold in range(n_folds):
            # 计算训练集和测试集
            train_end = train_size + fold * (n - train_size) // n_folds
            test_start = train_end
            test_end = train_end + (n - train_size) // n_folds if fold < n_folds - 1 else n

            if test_end - test_start < 30:
                continue

            df_train = self.df.iloc[:train_end]
            df_test = self.df.iloc[test_start:test_end]

            if verbose:
                print(f"[步行优化] Fold {fold+1}/{n_folds}")
                print(f"  训练集: {len(df_train)} 条, 测试集: {len(df_test)} 条")

            # 在训练集上优化参数
            optimizer = ParameterOptimizer(
                self.strategy_func, df_train, self.param_grid, metric="sharpe_ratio"
            )
            train_results = optimizer.grid_search(verbose=False)
            best_params = train_results[0]["params"]

            # 在测试集上验证
            df_test_result = self.strategy_func(df_test.copy(), **best_params)
            bt = Backtester(initial_capital=100000)
            test_result = bt.run(df_test_result)

            fold_results.append({
                "fold": fold,
                "train_start": str(df_train.index[0].date()),
                "train_end": str(df_train.index[-1].date()),
                "test_start": str(df_test.index[0].date()),
                "test_end": str(df_test.index[-1].date()),
                "best_params": best_params,
                "train_score": train_results[0]["score"],
                "test_return": test_result.total_return,
                "test_sharpe": test_result.sharpe_ratio,
                "test_drawdown": test_result.max_drawdown,
                "test_trades": test_result.total_trades,
            })

            if verbose:
                print(f"  最优参数: {best_params}")
                print(f"  测试集收益: {test_result.total_return:.2f}%, 夏普: {test_result.sharpe_ratio:.2f}")

        # 汇总结果
        avg_test_return = np.mean([r["test_return"] for r in fold_results])
        avg_test_sharpe = np.mean([r["test_sharpe"] for r in fold_results])

        return {
            "fold_results": fold_results,
            "avg_test_return": avg_test_return,
            "avg_test_sharpe": avg_test_sharpe,
        }


def optimize_strategy_params(symbol: str, start_date: str,
                              strategy_name: str = "volume_breakout",
                              method: str = "grid") -> Dict:
    """
    便捷函数：优化指定策略的参数
    
    Args:
        symbol: 股票代码
        start_date: 开始日期
        strategy_name: 策略名称
        method: 优化方法 (grid/genetic/walkforward)
    """
    from data_fetcher import get_stock_history

    # 获取数据
    df = get_stock_history(symbol, start_date=start_date)

    # 选择策略函数
    if strategy_name == "volume_breakout":
        from strategies import strategy_volume_breakout
        strategy_func = strategy_volume_breakout
        param_grid = {
            "vol_factor": [1.2, 1.5, 1.8, 2.0],
            "rise_3d": [0.01, 0.02, 0.03],
            "score_min": [2, 3],
        }
    elif strategy_name == "ma_convergence":
        from strategies import strategy_ma_convergence
        strategy_func = strategy_ma_convergence
        param_grid = {
            "ma_narrow": [0.01, 0.02, 0.03, 0.05],
        }
    elif strategy_name == "bottom_fishing":
        from strategies import strategy_bottom_fishing
        strategy_func = strategy_bottom_fishing
        param_grid = {
            "drop_threshold": [-0.03, -0.05, -0.07, -0.10],
        }
    else:
        return {"error": f"未知的策略: {strategy_name}"}

    # 执行优化
    if method == "grid":
        optimizer = ParameterOptimizer(strategy_func, df, param_grid)
        results = optimizer.grid_search()
    elif method == "genetic":
        # 转换为范围格式
        param_ranges = {}
        for key, values in param_grid.items():
            param_ranges[key] = (min(values), max(values))
        optimizer = GeneticOptimizer(strategy_func, df, param_ranges)
        results = optimizer.optimize()
    else:
        return {"error": f"未知的优化方法: {method}"}

    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "method": method,
        "best_params": results[0]["params"] if isinstance(results, list) else results["params"],
        "best_score": results[0]["score"] if isinstance(results, list) else results["score"],
        "all_results": results[:10] if isinstance(results, list) else [results],
    }


if __name__ == "__main__":
    # 测试
    print("测试参数优化...")
    result = optimize_strategy_params("000001", "20220101", "volume_breakout", "grid")
    print(result)
