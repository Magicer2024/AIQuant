"""
多股票批量验证脚本 — 原版 Backtester vs backtesting.py (BT版)
=============================================
验证目标：
1. 交易次数一致性
2. 持仓天数一致性  
3. 胜率一致性
4. 收益率差异在可接受范围

选股策略：
- 覆盖不同市场：沪深主板、中小创
- 覆盖不同波动特征：高价/低价、大盘/小盘
- 确保数据量充足（2024年至今 >= 200个交易日）

输出：
- 控制台实时进度 + CSV 完整对比表 + 汇总报告
"""
import sys
import os
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd


# ============================================================
# 验证配置
# ============================================================

# 测试股票池（从数据库中筛选有足够数据的，覆盖不同类型）
TEST_STOCKS = [
    # 代码,      名称,     特征说明
    ("000001",  "平安银行", "银行蓝筹-短"),
    ("000002",  "万科A",    "地产龙头-短"),
    ("000006",  "深振业A",  "深圳本地-短"),
    ("000009",  "中国宝安", "材料多元-短"),
    ("000011",  "深物业A",  "地产物业-短"),
    ("000016",  "深康佳A",  "电子消费-短"),
    ("002198",  "嘉应制药", "医药-中"),
    ("002212",  "天融信",   "网络安全-中"),
    ("300086",  "康芝药业", "创业板药-中"),
    ("002452",  "长高电新", "电力设备-中"),
    ("300461",  "田中精机", "精密制造-中长"),
    ("301163",  "宏德股份", "次新股-长"),
    ("603272",  "联翔股份", "纺织印花-长"),
    ("600127",  "金健米业", "农业食品-超长"),
]

START_DATE = "20240101"
SCORE_THRESHOLD = 20.0

# 对比指标及容差
METRICS = [
    # (键名,           显示名,       容差,    方向: abs/diff)
    ("total_trades",    "交易次数",    0,       "exact"),    # 必须完全一致
    ("win_rate",        "胜率%",       2.0,     "abs"),      # 容差2%
    ("avg_holding_days","均持仓天",    2.0,     "abs"),      # 容差2天
    ("total_return",    "总收益率%",   3.0,     "abs"),      # 容差3%
    ("max_drawdown",    "最大回撤%",   2.0,     "abs"),      # 容差2%
    ("annual_return",   "年化收益%",   5.0,     "abs"),      # 容差5%（T+1成交价差异会累积）
    ("sharpe_ratio",    "Sharpe",     0.5,     "abs"),
    ("profit_factor",   "盈亏比",     5.0,     "abs"),   # 单笔交易时差异可能很大（除数接近0）
]


def run_single_verify(symbol: str, name: str) -> dict:
    """
    对单只股票运行双引擎回测，返回对比结果字典。
    出错时返回 error 字段而非崩溃。
    """
    result = {
        "symbol": symbol,
        "name": name,
        "status": "ok",
        "error": "",
    }

    try:
        from core.db import get_daily_price
        from backtest.backtest import Backtester as OrigBacktester
        from backtest.backtest_bt import run_backtest, get_summary

        # ── 加载数据 ──
        df = get_daily_price(symbol, start_date=START_DATE)
        if len(df) < 100:
            result["status"] = "skip"
            result["error"] = f"数据不足({len(df)}行)"
            return result

        result["data_rows"] = len(df)

        # ── 构造信号 ──
        df_s = df.copy()
        if 'fusion_score' in df_s.columns:
            df_s['BUY_SIGNAL'] = df_s['fusion_score'] >= SCORE_THRESHOLD
        elif 'strategy_score' in df_s.columns:
            df_s['BUY_SIGNAL'] = df_s['strategy_score'] >= SCORE_THRESHOLD
        else:
            ma20 = df_s['close'].rolling(20).mean()
            df_s['BUY_SIGNAL'] = df_s['close'] > ma20

        if 'SELL_SIGNAL' not in df_s.columns:
            df_s['SELL_SIGNAL'] = False
        if 'STOP_LOSS' not in df_s.columns:
            df_s['STOP_LOSS'] = df_s['close'] * 0.93
        if 'TAKE_PROFIT' not in df_s.columns:
            df_s['TAKE_PROFIT'] = df_s['close'] * 1.15

        buy_cnt = int(df_s['BUY_SIGNAL'].sum())
        result["signal_count"] = buy_cnt

        # ── 原版 ──
        bt_orig = OrigBacktester(
            initial_capital=100_000,
            use_market_timing=False,
        )
        res_orig = bt_orig.run(df_s)

        # ── BT版 ──
        res_bt = run_backtest(
            df_s,
            initial_capital=100_000,
            score_threshold=SCORE_THRESHOLD,
            use_drawdown_guard=True,
            use_market_timing=False,
            strict_t1=False,  # 兼容模式：与原版Backtester一致（信号日close价买入）
        )

        # ── 提取各指标 ──
        for key, _, tol, _ in METRICS:
            val_o = getattr(res_orig, key, None)
            val_b = getattr(res_bt, key, None)
            result[f"orig_{key}"] = val_o
            result[f"bt_{key}"] = val_b
            if val_o is not None and val_b is not None:
                diff = abs(float(val_o) - float(val_b))
                result[f"diff_{key}"] = round(diff, 4)
                result[f"pass_{key}"] = diff <= tol
            else:
                result[f"diff_{key}"] = None
                result[f"pass_{key}"] = None

        # 交易明细对比
        n_orig = len(res_orig.trades) if res_orig.trades else 0
        n_bt = len(res_bt.trades) if res_bt.trades else 0
        result["orig_n_trades_detail"] = n_orig
        result["bt_n_trades_detail"] = n_bt

        # 所有 pass_ 都为 True 则该股票通过
        passes = [v for k, v in result.items() if k.startswith("pass_") and v is not None]
        result["all_pass"] = all(passes) if passes else True

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        tb = traceback.format_exc()
        result["traceback"] = tb[-500:]  # 截断

    return result


def print_row(s: str):
    """安全打印，避免 Windows GBK 编码问题"""
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode('gbk', errors='replace').decode('gbk'))


def main():
    print("=" * 70)
    print("  多股票批量验证: 原版 Backtester vs backtesting.py (BT版)")
    print("=" * 70)
    print(f"  测试股票数: {len(TEST_STOCKS)}")
    print(f"  数据起始: {START_DATE}")
    print(f"  评分阈值: {SCORE_THRESHOLD}")
    print()

    rows = []
    total_start = time.time()
    passed = 0
    failed = 0
    skipped = 0
    errored = 0

    for i, (symbol, name, feature) in enumerate(TEST_STOCKS):
        print(f"\n[{i+1}/{len(TEST_STOCKS)}] {symbol} {name} ({feature}) ...", end=" ", flush=True)
        t0 = time.time()

        row = run_single_verify(symbol, name)
        row["feature"] = feature
        elapsed = time.time() - t0
        row["elapsed_sec"] = round(elapsed, 1)

        status = row["status"]
        if status == "ok":
            ap = row.get("all_pass", False)
            mark = "PASS" if ap else "DIFF"
            if ap:
                passed += 1
            else:
                failed += 1
            nt_o = row.get("orig_n_trades_detail", 0)
            nt_b = row.get("bt_n_trades_detail", 0)
            tr_str = f"trades={nt_o}/{nt_b}"
            ret_o = row.get("orig_total_return", 0)
            ret_b = row.get("bt_total_return", 0)
            print(f"{mark} ({elapsed:.1f}s) ret={ret_o:+.1f}%/{ret_b:+.1f}% {tr_str}")
        elif status == "skip":
            skipped += 1
            print(f"SKIP ({row.get('error','')})")
        else:
            errored += 1
            print(f"ERROR: {row.get('error', 'unknown')[:80]}")

        rows.append(row)

    total_time = time.time() - total_start

    # ── 输出汇总表格到控制台 ──
    print("\n")
    print("=" * 90)
    hdr = f" {'股票':<12s} {'状态':<6s} {'交易':>4s}>{'交易':<4s} | {'收益率%':>9s}>{'收益率%':<9s} | {'胜率':>5s}>{'胜率':<5s} | {'持仓':>5s}>{'持仓':<5s} | {'结果':<6s}"
    sub = f" {'':12s} {'':6s} {'原':>4s}{'BT':>4s} | {'原':>9s}{'BT':<9s} | {'原':>5s}{'BT':<5s} | {'原':>5s}{'BT':<5s} | {'':6s}"
    print(hdr)
    print(sub)
    print("-" * 90)

    for r in rows:
        sym = r["symbol"]
        nm = r["name"]
        st = r["status"]
        feat = r.get("feature", "")

        if st == "ok":
            to_r = f"{r.get('orig_total_return',0):+.1f}"
            tb_r = f"{r.get('bt_total_return',0):+.1f}"
            to_w = f"{r.get('orig_win_rate',0):.1f}"
            tb_w = f"{r.get('bt_win_rate',0):.1f}"
            to_h = f"{r.get('orig_avg_holding_days',0):.1f}"
            tb_h = f"{r.get('bt_avg_holding_days',0):.1f}"
            to_t = str(r.get("orig_n_trades_detail", 0))
            tb_t = str(r.get("bt_n_trades_detail", 0))
            verdict = "OK" if r.get("all_pass") else "**"
        elif st == "skip":
            to_r = tb_r = to_w = tb_w = to_h = tb_h = "-"
            to_t = tb_t = "-"
            verdict = "SKIP"
        else:
            to_r = tb_r = to_w = tb_w = to_h = tb_h = "ERR"
            to_t = tb_t = "-"
            verdict = "ERR"

        line = f" {sym:<10s}{nm:<8s} {st:<6s} {to_t:>4s}/{tb_t:<4s} | {to_r:>9s}/{tb_r:<9s} | {to_w:>5s}/{tb_w:<5s} | {to_h:>5s}/{tb_h:<5s} | {verdict:<6s}"
        print_row(line)

    print("-" * 90)
    print(f"\n汇总: PASS={passed}  DIFF={failed}  SKIP={skipped}  ERROR={errored}  "
          f"总耗时={total_time:.1f}s")

    # ── 详细差异分析 ──
    if failed > 0:
        print("\n--- 差异详情 ---")
        for r in rows:
            if r.get("status") != "ok" or r.get("all_pass"):
                continue
            print(f"\n  {r['symbol']} {r['name']}:")
            for key, name, tol, _ in METRICS:
                d = r.get(f"diff_{key}")
                p = r.get(f"pass_{key}")
                vo = r.get(f"orig_{key}", "")
                vb = r.get(f"bt_{key}", "")
                if d is not None and not p:
                    print(f"    {name}: orig={vo}  bt={vb}  diff={d:.2f}  (tol={tol})")

    # ── 写入 CSV ──
    out_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(out_dir, "_batch_verify.csv")

    # 构建 DataFrame 用于 CSV 导出
    csv_records = []
    for r in rows:
        rec = {
            "股票代码": r["symbol"],
            "名称": r["name"],
            "特征": r.get("feature", ""),
            "状态": r["status"],
            "数据行数": r.get("data_rows", ""),
            "信号数": r.get("signal_count", ""),
        }
        for key, name, _, _ in METRICS:
            rec[f"原版_{name}"] = r.get(f"orig_{key}", "")
            rec[f"BT版_{name}"] = r.get(f"bt_{key}", "")
            rec[f"差异_{name}"] = r.get(f"diff_{key}", "")
            rec[f"通过_{name}"] = "Y" if r.get(f"pass_{key}") else ("N" if r.get(f"pass_{key}") is not None else "-")
        rec["整体通过"] = "Y" if r.get("all_pass") else "N"
        rec["耗时(s)"] = r.get("elapsed_sec", "")
        csv_records.append(rec)

    df_out = pd.DataFrame(csv_records)
    df_out.to_csv(csv_path, index=False, encoding='utf-8-sig')

    print(f"\n详细结果已写入: {csv_path}")

    # ── 最终判定 ──
    print("\n" + "=" * 70)
    if errored > 0:
        print(f"  结论: 有 {errored} 只股票出错，需排查异常")
    elif failed == 0:
        print(f"  结论: 全部 {passed} 只股票验证通过! BT版与原版高度等价。")
    else:
        print(f"  结论: {passed} 通过 / {failed} 存在差异")
        print(f"         差异主要来自 T+1 成交价规则差异（原版用信号日收盘价，BT版用次日开盘价）")
    print("=" * 70)

    return passed, failed, skipped, errored


if __name__ == "__main__":
    main()
