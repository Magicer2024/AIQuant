"""
services/optimizer_service.py —— 参数优化服务
"""
from typing import Dict, Any

from core.data_fetcher import get_stock_history


def run_optimizer(symbol: str, start_date: str = "20220101",
                  strategy_name: str = "volume_breakout",
                  method: str = "grid", metric: str = "sharpe_ratio") -> Dict[str, Any]:
    """运行参数优化"""
    df = get_stock_history(symbol, start_date=start_date)

    if strategy_name == "volume_breakout":
        from strategy.strategies import strategy_volume_breakout
        strategy_func = strategy_volume_breakout
        param_grid = {
            "vol_factor": [1.2, 1.5, 1.8, 2.0],
            "rise_3d": [0.01, 0.02, 0.03],
            "score_min": [2, 3],
        }
    elif strategy_name == "ma_convergence":
        from strategy.strategies import strategy_ma_convergence
        strategy_func = strategy_ma_convergence
        param_grid = {"ma_narrow": [0.01, 0.02, 0.03, 0.05]}
    elif strategy_name == "bottom_fishing":
        from strategy.strategies import strategy_bottom_fishing
        strategy_func = strategy_bottom_fishing
        param_grid = {"drop_threshold": [-0.03, -0.05, -0.07, -0.10]}
    else:
        raise ValueError(f"未知的策略: {strategy_name}")

    if method == "grid":
        from backtest.param_optimizer import ParameterOptimizer
        optimizer = ParameterOptimizer(strategy_func, df, param_grid, metric)
        results = optimizer.grid_search()
        return {
            "symbol": symbol,
            "strategy": strategy_name,
            "method": method,
            "metric": metric,
            "best_params": results[0]["params"],
            "best_score": results[0]["score"],
            "top_results": results[:10],
        }
    elif method == "genetic":
        from backtest.param_optimizer import GeneticOptimizer
        param_ranges = {k: (min(v), max(v)) for k, v in param_grid.items()}
        optimizer = GeneticOptimizer(strategy_func, df, param_ranges, metric)
        result = optimizer.optimize()
        return {
            "symbol": symbol,
            "strategy": strategy_name,
            "method": method,
            "metric": metric,
            "best_params": result["params"],
            "best_score": result["score"],
        }
    else:
        raise ValueError(f"未知的优化方法: {method}")


def run_walkforward(symbol: str, start_date: str = "20220101",
                    strategy_name: str = "volume_breakout", n_folds: int = 3) -> Dict[str, Any]:
    """运行步行优化"""
    df = get_stock_history(symbol, start_date=start_date)

    if strategy_name == "volume_breakout":
        from strategy.strategies import strategy_volume_breakout
        strategy_func = strategy_volume_breakout
        param_grid = {"vol_factor": [1.2, 1.5, 1.8], "rise_3d": [0.01, 0.02]}
    elif strategy_name == "ma_convergence":
        from strategy.strategies import strategy_ma_convergence
        strategy_func = strategy_ma_convergence
        param_grid = {"ma_narrow": [0.01, 0.02, 0.03]}
    else:
        raise ValueError(f"未知的策略: {strategy_name}")

    from backtest.param_optimizer import WalkForwardOptimizer
    optimizer = WalkForwardOptimizer(strategy_func, df, param_grid)
    result = optimizer.run(n_folds=n_folds)

    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "n_folds": n_folds,
        "fold_results": result["fold_results"],
        "avg_test_return": result["avg_test_return"],
        "avg_test_sharpe": result["avg_test_sharpe"],
    }
