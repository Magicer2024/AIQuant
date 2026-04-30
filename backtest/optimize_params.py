"""
BT引擎参数优化脚本
使用 backtesting.py 的 bt.optimize() 进行网格搜索，寻找最优策略参数组合。

可优化参数：
  - score_threshold:  融合分门槛 (15~35, 步长2)
  - stop_loss_mult:   止损倍数 (0.90~0.97, 步长0.01)
  - take_profit_mult:  止盈倍数 (1.05~1.30, 步长0.02)
  - max_holding_days:  最大持仓天数 (10~50, 步长5)
  - position_pct:      仓位比例 (0.3~1.0, 步长0.1)

约束条件：
  - 至少5笔交易 (排除过拟合到0-1笔的情况)
  - 最大回撤 < 25%
  - 夏普比率 > 0

用法:
    python backtest/optimize_params.py              # 单股(000001)快速测试
    python backtest/optimize_params.py --batch       # 多股票批量优化
    python backtest/optimize_params.py --stock 000858 # 指定股票
"""
import sys
import os
import time
import json
import argparse
from datetime import datetime
from pathlib import Path

# 确保项目根目录在路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from backtesting import Backtest
from backtesting.lib import crossover

# 项目模块
from core.db import get_daily_price
from strategy.strategies import (
    fuse_signals, DEFAULT_WEIGHTS,
    strategy_volume_breakout, strategy_ma_convergence,
    strategy_price_volume_divergence, strategy_bottom_fishing,
    strategy_whale_accumulation,
)
from backtest.backtest_bt import (
    BTStrategy, prepare_data, convert_result, get_summary,
)


# ============================================================
# 配置
# ============================================================

# 默认股票池（从批量验证中选出的有充足数据的股票）
DEFAULT_STOCKS = [
    ("000001", "平安银行"),
    ("000002", "万科A"),
    ("600127", "金健米业"),   # 超长数据集
    ("603272", "联翔股份"),
]

# 优化参数搜索空间
SEARCH_SPACE = {
    "score_threshold":    [15.0, 17.0, 19.0, 21.0, 23.0, 25.0, 28.0, 31.0, 35.0],
    "stop_loss_mult":     [0.90, 0.92, 0.94, 0.96],
    "take_profit_mult":    [1.06, 1.10, 1.15, 1.20, 1.25, 1.30],
    "max_holding_days":   [10, 20, 30, 40, 50],
    "position_pct":        [0.3, 0.5, 0.7, 0.9, 1.0],
}

# 固定参数（不优化）
FIXED_PARAMS = {
    "strict_t1": False,            # 兼容模式
    "use_drawdown_guard": True,
    "use_market_timing": False,    # 需要index_close列
    "use_dynamic_position": True,
}

# 约束条件
CONSTRAINTS = {
    "min_trades": 5,               # 最少交易次数
    "max_drawdown_pct": 25.0,      # 最大回撤上限(%)
    "min_sharpe": -1.0,            # 最低Sharpe
}

INITIAL_CASH = 100_000


# ============================================================
# 核心函数
# ============================================================

def load_stock_data(code: str, start_date: str = "20240101"):
    """加载单只股票数据并计算策略信号（5策略融合）"""
    df = get_daily_price(code, start_date=start_date)
    if len(df) < 100:
        return None, f"数据不足({len(df)}行)"
    
    # 调用5个独立策略，再融合
    s1 = strategy_volume_breakout(df)
    s2 = strategy_ma_convergence(df)
    s3 = strategy_price_volume_divergence(df)
    s4 = strategy_bottom_fishing(df)
    s5 = strategy_whale_accumulation(df)
    df_s = fuse_signals([s1, s2, s3, s4, s5], weights=DEFAULT_WEIGHTS)
    
    # 合并原始 OHLC 数据（fuse_signals 只返回 close/volume + 评分列）
    ohlc_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df_s = df_s.join(df[ohlc_cols], rsuffix="_raw")
    # 如果有重复列（close, volume），用原始值覆盖
    for c in ohlc_cols:
        if f"{c}_raw" in df_s.columns:
            df_s[c] = df_s[f"{c}_raw"]
            del df_s[f"{c}_raw"]
    
    return df_s, None


def run_optimization(df_strategy, method="grid", verbose=True):
    """
    运行参数优化。
    
    :param df_strategy: 含 fusion_score/BUY_SIGNAL 的 DataFrame
    :param method: "grid"=全网格搜索, "fast"=精简网格(快速预览)
    :param verbose: 是否打印进度
    :return: (最优结果Stats对象, 所有结果DataFrame)
    """
    data = prepare_data(df_strategy)
    
    bt = Backtest(
        data, BTStrategy,
        cash=INITIAL_CASH,
        commission=lambda price, size: max(price * size * 0.0003, 5.0),
    )
    
    # 搜索空间
    space = SEARCH_SPACE.copy()
    if method == "fast":
        space = {k: v[::2] for k, v in space.items()}  # 取一半采样点
    
    if verbose:
        total_combos = 1
        for v in space.values():
            total_combos *= len(v)
        print(f"  搜索空间: ~{total_combos} 组参数")
        print(f"  参数范围:")
        for k, vals in space.items():
            print(f"    {k}: [{vals[0]}, ..., {vals[-1]}] ({len(vals)}个值)")
    
    t0 = time.time()
    
    # 使用 bt.optimize() 进行网格搜索
    # maximize='Sharpe Ratio' — 用夏普比率作为目标函数
    # constraint 函数用于过滤不满足条件的组合
    stats = bt.optimize(
        score_threshold=space["score_threshold"],
        stop_loss_mult=space["stop_loss_mult"],
        take_profit_mult=space["take_profit_mult"],
        max_holding_days=space["max_holding_days"],
        position_pct=space["position_pct"],
        
        # 最大化目标：SQN (综合质量) — 平衡收益、交易数和稳定性
        maximize='SQN',
        
        return_heatmap=False,
        random_state=42,
    )
    
    elapsed = time.time() - t0
    
    if verbose:
        print(f"\n  优化耗时: {elapsed:.1f}s")
        print(f"  实际评估组合数: {len(stats)}")
    
    return stats, data


def extract_top_results(stats_obj, data_df, top_n=20):
    """提取 Top-N 参数组合并格式化为 DataFrame"""
    rows = []
    
    # 遍历所有被评估的参数组合
    # bt.optimize() 返回的 _StrategyResults 包含 _data 属性
    if hasattr(stats_obj, '_data'):
        result_data = stats_obj._data
    else:
        # fallback: 只有一组结果
        result_data = [vars(stats_obj)] if hasattr(stats_obj, '__dict__') else [{}]
    
    for r in result_data:
        try:
            row = {
                "score_threshold": r.get("score_threshold", BTStrategy.score_threshold),
                "stop_loss_mult": round(r.get("stop_loss_mult", BTStrategy.stop_loss_mult), 2),
                "take_profit_mult": round(r.get("take_profit_mult", BTStrategy.take_profit_mult), 2),
                "max_holding_days": int(r.get("max_holding_days", BTStrategy.max_holding_days)),
                "position_pct": round(r.get("position_pct", BTStrategy.position_pct), 2),
                
                # 回测结果指标
                "# Trades": int(r.get("# Trades", 0)),
                "Return[%]": round(r.get("Return [%]", 0), 2),
                "AnnReturn[%]": round(r.get("Return (Ann.) [%]", 0), 2),
                "MaxDD[%]": round(r.get("Max. Drawdown [%]", 0), 2),
                "Sharpe": round(r.get("Sharpe Ratio", 0), 2),
                "WinRate%": round(r.get("Win Rate [%]", 0), 1),
                "ProfitFactor": round(r.get("Profit Factor", 0), 2),
                "SQN": round(r.get("SQN", 0), 3),
            }
            
            # 约束检查
            trades_ok = row["# Trades"] >= CONSTRAINTS["min_trades"]
            dd_ok = abs(row["MaxDD[%]"]) <= CONSTRAINTS["max_drawdown_pct"]
            row["Pass"] = "Y" if (trades_ok and dd_ok) else "N"
            
            rows.append(row)
        except Exception as e:
            continue
    
    if not rows:
        # fallback: 从主结果中提取一行
        try:
            r = vars(stats_obj) if hasattr(stats_obj, '__dict__') else {}
            rows.append({
                "score_threshold": getattr(stats_obj, '_strategy_params', {}).get('score_threshold', '?'),
                "# Trades": r.get("# Trades", "?"),
                "Return[%]": r.get("Return [%]", "?"),
                "Sharpe": r.get("Sharpe Ratio", "?"),
                "MaxDD[%]": r.get("Max. Drawdown [%]", "?"),
                "Pass": "?",
            })
        except:
            pass
    
    df = pd.DataFrame(rows)
    
    if len(df) > 0 and "SQN" in df.columns:
        df = df.sort_values("SQN", ascending=False).head(top_n).reset_index(drop=True)
    
    return df


def print_optimization_result(stock_code, stock_name, stats_obj, data_df):
    """打印优化结果的摘要信息"""
    print(f"\n{'='*70}")
    print(f"📊 {stock_code} {stock_name} — 最优参数")
    print(f"{'='*70}")
    
    try:
        # bt.optimize() 返回 pd.Series（最优结果）
        # 参数值直接从 Series 中获取
        try:
            th = float(stats_obj._strategy['score_threshold'])
            sl = float(stats_obj._strategy['stop_loss_mult'])
            tp = float(stats_obj._strategy['take_profit_mult'])
            mh = int(stats_obj._strategy['max_holding_days'])
            pp = float(stats_obj._strategy['position_pct'])
        except (AttributeError, TypeError, KeyError):
            # fallback: optimize 可能返回不同结构
            th = getattr(BTStrategy, 'score_threshold', '?')
            sl = getattr(BTStrategy, 'stop_loss_mult', '?')
            tp = getattr(BTStrategy, 'take_profit_mult', '?')
            mh = getattr(BTStrategy, 'max_holding_days', '?')
            pp = getattr(BTStrategy, 'position_pct', '?')
        
        params_str = f"""
  ┌─ 最优参数 ─────────────────────────────┐
  │ score_threshold  = {th}
  │ stop_loss_mult   = {sl}
  │ take_profit_mult = {tp}
  │ max_holding_days = {mh}
  │ position_pct      = {pp}
  └────────────────────────────────────────┘
  
  ┌─ 回测表现 ─────────────────────────────┐
  │ 总收益率     = {stats_obj.get('Return [%]', 0):+.2f}%
  │ 年化收益率   = {stats_obj.get('Return (Ann.) [%]', 0):+.2f}%
  │ 最大回撤     = {stats_obj.get('Max. Drawdown [%]', 0):.2f}%
  │ Sharpe Ratio = {stats_obj.get('Sharpe Ratio', 0):.2f}
  │ SQN          = {stats_obj.get('SQN', 0):.3f}
  │ 交易次数     = {int(stats_obj.get('# Trades', 0))}
  │ 胜率         = {stats_obj.get('Win Rate [%]', 0):.1f}%
  │ 盈亏比       = {stats_obj.get('Profit Factor', 0):.2f}
  └────────────────────────────────────────┘
"""
        print(params_str)
    except Exception as e:
        # fallback: 用字符串键直接访问
        try:
            print(f"  Return: {stats_obj.get('Return [%]', 'N/A')}%")
            print(f"  Sharpe: {stats_obj.get('Sharpe Ratio', 'N/A')}")
            print(f"  Trades: {stats_obj.get('# Trades', 'N/A')}")
            print(f"  MaxDD: {stats_obj.get('Max. Drawdown [%]', 'N/A')}%")
        except:
            print(f"  (无法解析详细结果: {e})")


def optimize_single_stock(code: str, name: str, start_date="20240101",
                          method="grid") -> dict:
    """对单只股票运行完整优化流程，返回结果摘要 dict"""
    print(f"\n{'━'*60}")
    print(f"▶ {code} {name}")
    print(f"{'━'*60}")
    
    # 加载数据
    print(f"[1/3] 加载数据...")
    df_s, err = load_stock_data(code, start_date)
    if err:
        print(f"  ❌ {err}, 跳过")
        return {"code": code, "name": name, "error": err}
    
    print(f"  ✅ {len(df_s)} 行数据, "
          f"融合分: [{df_s['FUSION_SCORE'].min():.1f} ~ {df_s['FUSION_SCORE'].max():.1f}]")
    signals = (df_s['FUSION_SCORE'] >= 20.0).sum()
    print(f"  信号数(≥20): {signals}")
    
    # 运行优化
    print(f"\n[2/3] 运行优化 ({method}模式)...")
    stats, data = run_optimization(df_s, method=method, verbose=True)
    
    # 输出结果
    print(f"\n[3/3] 结果:")
    print_optimization_result(code, name, stats, data)
    
    # 提取关键数值
    try:
        # 从 _strategy 属性获取最优参数
        try:
            strat = stats._strategy
            opt_params = {
                "score_threshold": float(strat.get('score_threshold', BTStrategy.score_threshold)),
                "stop_loss_mult": round(float(strat.get('stop_loss_mult', BTStrategy.stop_loss_mult)), 3),
                "take_profit_mult": round(float(strat.get('take_profit_mult', BTStrategy.take_profit_mult)), 3),
                "max_holding_days": int(strat.get('max_holding_days', BTStrategy.max_holding_days)),
                "position_pct": round(float(strat.get('position_pct', BTStrategy.position_pct)), 2),
            }
        except (AttributeError, TypeError):
            opt_params = {
                "score_threshold": BTStrategy.score_threshold,
                "stop_loss_mult": BTStrategy.stop_loss_mult,
                "take_profit_mult": BTStrategy.take_profit_mult,
                "max_holding_days": BTStrategy.max_holding_days,
                "position_pct": BTStrategy.position_pct,
            }
        
        result = {
            "code": code,
            "name": name,
            "data_rows": len(df_s),
            "optimal_params": opt_params,
            "metrics": {
                "return_pct": round(float(stats.get('Return [%]', 0)), 2),
                "ann_return_pct": round(float(stats.get('Return (Ann.) [%]', 0)), 2),
                "max_drawdown_pct": round(float(stats.get('Max. Drawdown [%]', 0)), 2),
                "sharpe_ratio": round(float(stats.get('Sharpe Ratio', 0)), 2),
                "sqn": round(float(stats.get('SQN', 0)), 3),
                "total_trades": int(stats.get('# Trades', 0)),
                "win_rate_pct": round(float(stats.get('Win Rate [%]', 0)), 1),
                "profit_factor": round(float(stats.get('Profit Factor', 0)), 2),
            }
        }
    except Exception as e:
        result = {
            "code": code,
            "name": name,
            "error": f"解析结果失败: {e}",
        }
    
    return result


def run_batch_optimization(stocks=None, method="grid"):
    """多股票批量优化"""
    if stocks is None:
        stocks = DEFAULT_STOCKS
    
    print("=" * 70)
    print("  BT引擎参数优化 — 批量模式")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  股票数: {len(stocks)}, 方法: {method}")
    print("=" * 70)
    
    all_results = []
    total_start = time.time()
    
    for i, (code, name) in enumerate(stocks):
        result = optimize_single_stock(code, name, method=method)
        all_results.append(result)
    
    total_time = time.time() - total_start
    
    # 汇总
    print(f"\n\n{'='*70}")
    print(f"  📋 批量优化汇总")
    print(f"{'='*70}")
    print(f"  总耗时: {total_time:.1f}s")
    print(f"  完成: {len([r for r in all_results if 'error' not in r])}/{len(all_results)}")
    
    # 汇总表格
    valid_results = [r for r in all_results if 'error' not in r]
    if valid_results:
        header = (
            f"\n  {'代码':<8} {'名称':<10} {'收益率':>9} {'年化':>8} "
            f"{'最大回撤':>9} {'Sharpe':>7} {'交易':>5} {'胜率':>6}"
        )
        print(header)
        print("  " + "-" * len(header))
        for r in valid_results:
            m = r["metrics"]
            print(
                f"  {r['code']:<8} {r['name']:<10} "
                f"{m['return_pct']:>+8.2f}% {m['ann_return_pct']:>+7.2f}% "
                f"{m['max_drawdown_pct']:>8.2f}% {m['sharpe_ratio']:>7.2f} "
                f"{m['total_trades']:>5} {m['win_rate_pct']:>5.1f}%"
            )
    
    # 保存结果
    output_path = Path(__file__).parent / "_optimize_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "method": method,
            "constraints": CONSTRAINTS,
            "search_space": {k: str(v) for k, v in SEARCH_SPACE.items()},
            "results": all_results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  💾 结果已保存: {output_path}")
    
    return all_results


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BT引擎参数优化")
    parser.add_argument("--stock", type=str, default=None,
                        help="指定单只股票代码(如 000001)")
    parser.add_argument("--batch", action="store_true",
                        help="批量优化(使用默认股票池)")
    parser.add_argument("--method", type=str, default="grid",
                        choices=["grid", "fast"],
                        help="搜索方法: grid=全网格, fast=精简网格(默认grid)")
    parser.add_argument("--start", type=str, default="20240101",
                        help="数据起始日期(默认20240101)")
    args = parser.parse_args()
    
    if args.stock:
        # 单股票优化
        optimize_single_stock(args.stock, args.stock, 
                              start_date=args.start, method=args.method)
    elif args.batch:
        # 批量优化
        run_batch_optimization(method=args.method)
    else:
        # 默认：单股(000001)快速演示
        print("未指定参数，使用默认股票 000001 平安银行")
        print("提示: python backtest/optimize_params.py --batch  可进行批量优化\n")
        optimize_single_stock("000001", "平安银行", 
                              start_date=args.start, method=args.method)
