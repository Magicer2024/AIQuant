"""
backtest_bt.py 功能验证脚本
用真实数据对比 backtesting.py 实现与原版 Backtester
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd


def main():
    print("=" * 60)
    print("  backtesting.py 等价实现 — 功能验证")
    print("=" * 60)

    # ── Step 1: 加载数据 ──
    from core.db import get_daily_price
    symbol = "000001"
    start_date = "20240101"

    print(f"\n[1/5] \u52a0\u8f7d\u6570\u636e: {symbol} \u4ece {start_date}")
    df = get_daily_price(symbol, start_date=start_date)
    print(f"  \u6570\u636e\u884c\u6570: {len(df)}")
    print(f"  \u65e5\u671f\u8303\u56f4: {df.index[0]} ~ {df.index[-1]}")
    print(f"  \u5217\u540d: {list(df.columns[:12])}")

    if len(df) < 50:
        print("  ERROR: \u6570\u636e\u592a\u5c11")
        return

    # ── Step 2: 计算策略信号 ──
    print(f"\n[2/5] \u8ba1\u7b97\u7b56\u7565\u5206...")
    
    # 直接从数据库列读取（数据已含预计算评分）
    df_s = df.copy()
    
    # 确保 fusion_score 存在
    if 'fusion_score' in df_s.columns:
        df_s['BUY_SIGNAL'] = df_s['fusion_score'] >= 20.0
    elif 'strategy_score' in df_s.columns:
        df_s['BUY_SIGNAL'] = df_s['strategy_score'] >= 20.0
    else:
        # 没有预计算分时用简单MA策略
        if 'ma_score' in df_s.columns:
            df_s['BUY_SIGNAL'] = df_s['ma_score'] >= 3.0
        else:
            ma20 = df_s['close'].rolling(20).mean()
            df_s['BUY_SIGNAL'] = df_s['close'] > ma20
    
    if 'SELL_SIGNAL' not in df_s.columns:
        df_s['SELL_SIGNAL'] = False
    if 'STOP_LOSS' not in df_s.columns:
        df_s['STOP_LOSS'] = df_s['close'] * 0.93
    if 'TAKE_PROFIT' not in df_s.columns:
        df_s['TAKE_PROFIT'] = df_s['close'] * 1.15

    buy_count = int(df_s['BUY_SIGNAL'].sum())
    score_cols = [c for c in ['fusion_score','vol_score','ma_score',
                  'diverge_score','bottom_score','whale_score']
                  if c in df_s.columns]
    print(f"  \u53ef\u7528\u8bc4\u5206\u5217: {score_cols}")
    for c in score_cols:
        print(f"    {c}: [{df_s[c].min():.1f}, {df_s[c].max():.1f}]")
    print(f"  BUY_SIGNAL=True \u6570\u91cf: {buy_count}")

    # ── Step 3: 运行原版 Backtester ──
    from backtest.backtest import Backtester as OrigBacktester
    print(f"\n[3/5] \u8fd0\u884c\u539f\u7248 Backtester...")
    bt_orig = OrigBacktester(
        initial_capital=100_000,
        use_market_timing=False,  # 先关掉大盘择时（需要index_close列）
    )
    result_orig = bt_orig.run(df_s)
    summary_orig = bt_orig.get_summary(result_orig)

    print(f"\n  --- \u539f\u7248\u56de\u6d4b\u7ed3\u679c ---")
    for k, v in summary_orig.items():
        print(f"  {k}: {v}")

    if result_orig.trades:
        print(f"\n  \u6700\u540e3\u7b14\u4ea4\u6613:")
        for t in result_orig.trades[-3:]:
            print(f"    {t.entry_date} \u4e70{t.entry_price} "
                  f"\u2192 {t.exit_date} \u5356{t.exit_price}"
                  f"  {t.pnl_pct:+.1f}% [{t.exit_reason}] "
                  f"{t.holding_days}\u5929")

    # ── Step 4: 运行 backtesting.py 实现 ──
    from backtest.backtest_bt import run_backtest, get_summary, prepare_data, BTStrategy, Backtest
    print(f"\n[4/5] \u8fd0\u884c backtesting.py \u7b49\u4ef7\u5b9e\u73b0...")

    result_bt = run_backtest(
        df_s,
        initial_capital=100_000,
        score_threshold=20.0,
        use_drawdown_guard=True,
        use_market_timing=False,
    )
    summary_bt = get_summary(result_bt)

    print(f"\n  --- backtesting.py \u56de\u6d4b\u7ed3\u679c ---")
    for k, v in summary_bt.items():
        print(f"  {k}: {v}")

    if result_bt.trades:
        print(f"\n  \u6700\u540e3\u7b14\u4ea4\u6613:")
        for t in result_bt.trades[-3:]:
            print(f"    {t.entry_date} \u4e70{t.entry_price} "
                  f"\u2192 {t.exit_date} \u5356{t.exit_price}"
                  f"  {t.pnl_pct:+.1f}% [{t.exit_reason}] "
                  f"{t.holding_days}\u5929")

    # ── Step 5: 对比结果 ──
    print(f"\n[5/5] \u5bf9\u6bd4\u5206\u6790:")
    hdr = f"  {'指标':<18s} {'原版':<16s} {'BT版':<16s}"
    print(hdr)
    print(f"  {'-'*50}")

    metrics = [
        ('\u603b\u6536\u76ca\u7387%', result_orig.total_return, result_bt.total_return),
        ('\u5e74\u5316\u6536\u76ca\u7387', result_orig.annual_return, result_bt.annual_return),
        ('\u6700\u5927\u56de\u64a4', result_orig.max_drawdown, result_bt.max_drawdown),
        ('Sharpe', result_orig.sharpe_ratio, result_bt.sharpe_ratio),
        ('\u80dc\u7387', result_orig.win_rate, result_bt.win_rate),
        ('\u76c8\u4e8f\u6bd4', result_orig.profit_factor, result_bt.profit_factor),
        ('\u4ea4\u6613\u6b21\u6570', result_orig.total_trades, result_bt.total_trades),
        ('\u57fa\u51c6\u6536\u76ca', result_orig.benchmark_return, result_bt.benchmark_return),
        ('\u6700\u7ec8\u8d44\u91d1', result_orig.final_capital, result_bt.final_capital),
    ]

    for name, val_o, val_b in metrics:
        diff = abs(float(val_o) - float(val_b)) if val_o and val_b else 0
        marker = " *" if diff > 5 else ""
        row = f"  {name:<18s} {str(val_o):<16s} {str(val_b):<16s}{marker}"
        print(row)

    print(f"\n  (* \u8868\u793a\u5dee\u5f02 > 5\uff0c\u53ef\u80fd\u662f T+1 \u5b9e\u73b0\u7ec6\u8282\u5dee\u5f02)")
    print(f"\n{'=' * 60}")
    print(f"  \u9a8c\u8bc1\u5b8c\u6210!")
    print(f"{'=' * 60}")

    # 将完整结果写入文件（避免 Windows 控制台编码问题）
    with open('e:/小项目/jiaoyi/backtest/_bt_result.txt', 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("  backtesting.py 等价实现 — 功能验证结果\n")
        f.write("=" * 60 + "\n\n")

        f.write("--- 原版回测结果 ---\n")
        for k, v in summary_orig.items():
            f.write(f"  {k}: {v}\n")

        if result_orig.trades:
            f.write("\n  最后3笔交易(原版):\n")
            for t in result_orig.trades[-3:]:
                f.write(f"    {t.entry_date} 买{t.entry_price} "
                      f"-> {t.exit_date} 卖{t.exit_price}"
                      f"  {t.pnl_pct:+.1f}% [{t.exit_reason}] "
                      f"{t.holding_days}天\n")

        f.write("\n--- backtesting.py 回测结果 ---\n")
        for k, v in summary_bt.items():
            f.write(f"  {k}: {v}\n")

        if result_bt.trades:
            f.write("\n  最后3笔交易(BT版):\n")
            for t in result_bt.trades[-3:]:
                f.write(f"    {t.entry_date} 买{t.entry_price} "
                      f"-> {t.exit_date} 卖{t.exit_price}"
                      f"  {t.pnl_pct:+.1f}% [{t.exit_reason}] "
                      f"{t.holding_days}天\n")

        f.write("\n--- 对比分析 ---\n")
        hdr = f"  {'指标':<18s} {'原版':<16s} {'BT版':<16s}\n"
        f.write(hdr)
        f.write("  " + "-" * 50 + "\n")

        metrics = [
            ('总收益率%', result_orig.total_return, result_bt.total_return),
            ('年化收益率', result_orig.annual_return, result_bt.annual_return),
            ('最大回撤', result_orig.max_drawdown, result_bt.max_drawdown),
            ('Sharpe', result_orig.sharpe_ratio, result_bt.sharpe_ratio),
            ('胜率', result_orig.win_rate, result_bt.win_rate),
            ('盈亏比', result_orig.profit_factor, result_bt.profit_factor),
            ('交易次数', result_orig.total_trades, result_bt.total_trades),
            ('基准收益', result_orig.benchmark_return, result_bt.benchmark_return),
            ('最终资金', result_orig.final_capital, result_bt.final_capital),
        ]
        for name, val_o, val_b in metrics:
            diff = abs(float(val_o) - float(val_b)) if val_o and val_b else 0
            marker = " *" if diff > 5 else ""
            row = f"  {name:<18s} {str(val_o):<16s} {str(val_b):<16s}{marker}\n"
            f.write(row)

        f.write(f"\n  (* 表示差异 > 5)\n")
        f.write("=" * 60 + "\n")

    print("完整结果已写入: backtest/_bt_result.txt")


if __name__ == "__main__":
    main()
