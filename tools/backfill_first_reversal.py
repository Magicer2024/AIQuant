"""回填「反转首日」历史信号 → first_reversal_hist（供推荐复盘独立成组显示）。

为什么用独立表而不是回填 stock_signal：
  stock_signal 是 48 万行大表，且**大量诊断/回测脚本**直接按 `horizon='short'`
  取数（diag_short_reco / eval_short_sort_replay / calibrate_short_topn /
  backtest_short_live_1y …），注入 ~1300 行观察池会**静默污染**这些分析口径。
  独立表让 `core/outcome_tracker.insert_new_outcomes` 用
  `stock_signal(当日链路) ∪ first_reversal_hist(历史)` 取数，两不相扰。

做法（复用线上同源模块，不新造条件）：
  1) 一次性读 daily_price（窗口 + 前 120 自然日铺垫），向量化算 mask 取候选
  2) 对每个候选调 strategy.first_reversal.scan_first_reversal（与线上同函数）
  3) passes_quality 过滤（与线上 _build_signal_records 一致）
  4) DELETE 窗口内旧行 → 写 first_reversal_hist
  5) --refresh：重跑 insert_new_outcomes + evaluate_outcomes，复盘立即可见

用法：
  python tools/backfill_first_reversal.py              # 干跑：只报候选数
  python tools/backfill_first_reversal.py --apply      # 写入 first_reversal_hist
  python tools/backfill_first_reversal.py --apply --refresh   # 写入并重建复盘
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config.personal_config import EXIT_TRACK_START_DATE  # noqa: E402
from config.strategy_params import FIRST_REVERSAL  # noqa: E402
from core.db import get_all_stocks, get_conn, get_market_cap_map  # noqa: E402
from strategy.first_reversal import scan_first_reversal  # noqa: E402
from strategy.indicators import calc_true_ret  # noqa: E402
from strategy.rec_filters import passes_quality  # noqa: E402


def _load_window(start: str, end: str) -> pd.DataFrame:
    from core.db import connect_db
    con = connect_db(readonly=True)
    try:
        df = pd.read_sql(
            "SELECT code, trade_date, open, high, low, close, volume, amount "
            "FROM daily_price WHERE trade_date >= ? AND trade_date <= ? "
            "ORDER BY code, trade_date",
            con, params=(
                (pd.Timestamp(start) - pd.Timedelta(days=120)).strftime("%Y-%m-%d"),
                end))
    finally:
        con.close()
    df = df[~df["code"].str.startswith(("688", "689", "8", "9"))].copy()
    df = df[(df["close"] > 0) & (df["open"] > 0)
            & (df["high"] > 0) & (df["low"] > 0)].reset_index(drop=True)
    return df


def _candidates(df: pd.DataFrame, start: str, end: str) -> list:
    """向量化取候选 (code, trade_date)：与 scan_first_reversal 的主条件同口径。

    只用于**缩小调用范围**（把 45 天 × 4400 只压到 ~1300 个候选），命中与否
    最终仍以 scan_first_reversal 的返回为准 ⇒ 不存在两套判定。
    """
    g = df.groupby("code", sort=False)
    df = df.copy()
    df["ret"] = calc_true_ret(df)
    df["cp"] = g["close"].shift(1)
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
    df["ma20p"] = df["ma20"].shift(1)
    df["ama5"] = g["amount"].transform(lambda s: s.shift(1).rolling(5).mean()) \
        if "amount" in df.columns else np.nan
    df["vr"] = df["amount"] / df["ama5"].replace(0, np.nan)
    df["notlim"] = df["ret"] < np.where(
        df["code"].str.startswith(("300", "301")), 19.8, 9.8)
    p = FIRST_REVERSAL
    m = (
        (df["trade_date"] >= start) & (df["trade_date"] <= end)
        & df["ma20"].notna() & df["ret"].notna() & df["vr"].notna()
        & (df["close"] > df["ma20"]) & (df["cp"] <= df["ma20p"])
        & (df["ret"] >= float(p.get("min_pct", 5.0)))
        & (df["vr"] >= float(p.get("vol_ratio_min", 1.5)))
        & df["notlim"]
    )
    return list(zip(df.loc[m, "code"], df.loc[m, "trade_date"]))


def main() -> None:
    ap = argparse.ArgumentParser(description="回填反转首日历史信号 → first_reversal_hist")
    ap.add_argument("--apply", action="store_true", help="写入 first_reversal_hist（默认干跑）")
    ap.add_argument("--refresh", action="store_true", help="写入后重建 recommend_outcome 并重评")
    ap.add_argument("--start", default=EXIT_TRACK_START_DATE)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    from config.settings import SIGNAL_MODEL_MODE, require_signal_model_ready
    require_signal_model_ready()
    if SIGNAL_MODEL_MODE != "legacy":
        if args.refresh:
            ap.error("统一模型历史重放不能重建前向推荐或交易成绩")
        from core.signal_runtime import replay_signal_range
        result = replay_signal_range(args.start, args.end,
                                     strategy_keys=["first_reversal"], dry_run=not args.apply)
        print(result)
        return result

    with get_conn(readonly=True) as conn:
        end = args.end or conn.execute(
            "SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
    print("=" * 78)
    print(f"反转首日历史回填  |  模式 = {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"窗口: {args.start} ~ {end}")
    print("=" * 78)

    t0 = time.time()
    df = _load_window(args.start, end)
    print(f"  daily_price 载入 {len(df):,} 行（{df['code'].nunique()} 只，含 120 天铺垫）"
          f"  {time.time()-t0:.0f}s")
    cands = _candidates(df, args.start, end)
    print(f"  向量化候选: {len(cands)} 条")

    try:
        sdf = get_all_stocks()
        name_map = dict(zip(sdf["code"], sdf["name"])) if not sdf.empty else {}
    except Exception:
        name_map = {}
    try:
        mktcap_map = get_market_cap_map()
    except Exception:
        mktcap_map = {}

    by_code = {c: s for c, s in df.groupby("code", sort=False)}
    rows, n_quality_drop = [], 0
    for code, date in cands:
        sub = by_code.get(code)
        if sub is None:
            continue
        upto = sub[sub["trade_date"] <= date]
        if len(upto) < 21:
            continue
        d = upto[["open", "high", "low", "close", "volume", "amount"]].copy()
        d.index = pd.to_datetime(upto["trade_date"])
        name = name_map.get(code) or code
        ts = (mktcap_map.get(code) or {}).get("total_shares")
        sig = scan_first_reversal(d, name, ts, params=FIRST_REVERSAL,
                                  market_pct=None, code=code)
        if not sig:
            continue
        if not passes_quality(name, d, ts):
            n_quality_drop += 1
            continue
        rows.append((
            code, date, name, sig["buy_price"], sig["stop_loss"], sig["take_profit"],
            sig["fusion_score"], sig.get("ext_pct"), sig.get("vol_ratio"),
            sig.get("kdj_k"), time.strftime("%Y-%m-%d %H:%M:%S")))

    by_date = {}
    for r in rows:
        by_date[r[1]] = by_date.get(r[1], 0) + 1
    print(f"  模块确认: {len(rows)} 条（quality 剔除 {n_quality_drop} 条）"
          f"  日均 {len(rows)/max(1,len(by_date)):.1f} 只 / {len(by_date)} 个信号日")
    if by_date:
        ds = sorted(by_date)
        print(f"  日期范围: {ds[0]} ~ {ds[-1]}")

    if not args.apply:
        print("\n[DRY-RUN] 未写库。加 --apply 执行。")
        return

    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS first_reversal_hist (
                code TEXT NOT NULL, scan_date TEXT NOT NULL, name TEXT,
                buy_price REAL, stop_loss REAL, take_profit REAL,
                fusion_score REAL, ext_pct REAL, vol_ratio REAL, kdj_k REAL,
                created_at TEXT,
                PRIMARY KEY (code, scan_date))""")
        conn.execute("DELETE FROM first_reversal_hist WHERE scan_date >= ?",
                     (args.start,))
        conn.executemany("""
            INSERT OR REPLACE INTO first_reversal_hist
                (code, scan_date, name, buy_price, stop_loss, take_profit,
                 fusion_score, ext_pct, vol_ratio, kdj_k, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM first_reversal_hist").fetchone()[0]
    print(f"  ✅ first_reversal_hist 写入完成，表内 {n} 行")

    if args.refresh:
        from datetime import date as _date
        from core.outcome_tracker import evaluate_outcomes, insert_new_outcomes
        # ⚠ 必须显式放大回溯天数：默认 60 天只能覆盖「今天-60」，会漏掉窗口最早几天
        _days = (_date.today() - _date.fromisoformat(args.start)).days + 2
        insert_new_outcomes(days_back=_days)
        upd = evaluate_outcomes()
        print(f"  ✅ recommend_outcome 重建 + 重评完成（days_back={_days}，{upd} 条更新）")
        from core.outcome_tracker import get_merged_summary
        s = get_merged_summary(90, "short", strategy="反转首日", limit=5000)
        print("  ── 反转首日 · 复盘汇总 ──")
        print(f"     样本 {s.get('total')}  胜率 {s.get('win_rate')}%  "
              f"平均 {s.get('avg_return')}%  盈亏比 {s.get('profit_factor')}  "
              f"T+1 胜率 {(s.get('t1') or {}).get('win_rate')}%")
    print(f"\n总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
