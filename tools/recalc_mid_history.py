"""中/长线信号历史重算：重建 [EXIT_TRACK_START_DATE, 最新交易日] 的历史信号

背景（2026-08-22）：
  - 中线入场于 2026-08-20 切换为三过滤确认风格（锚点上穿 + 首确认日破锚点
    高点 + 偏离MA20≤5% + 弱市闸门），切换前的历史信号仍为旧口径，
    出场跟踪/推荐复盘累计收益被旧信号拖累 → 需按新策略整体重算。
  - 长线选股逻辑未变，但止损于 2026-08-18 由 MA120×0.99 改为参数
    long_ma_stop_mult（默认0.95），8-17 及之前的信号行内 stop_loss 仍为
    旧口径 → 重算统一止损价（选股集合不变）。

逐日流程（与 _build_signal_records 同链）：
  1) 弱市闸门按该历史日全市场均涨判定（仅作用于 mid）
  2) 行情读取 end_date 截断到该日（防未来数据穿越，与增量信号路径同口径）
  3) scan_mid_term / scan_long_term + passes_quality + chase_filter
  4) DELETE 当日对应 horizon 信号后 INSERT 新结果

用法：
  python tools/recalc_mid_history.py                       # 默认重算 mid 全窗口
  python tools/recalc_mid_history.py --horizons long       # 只重算长线
  python tools/recalc_mid_history.py --horizons mid,long   # 两个周期一趟跑完
  python tools/recalc_mid_history.py --refresh             # 完成后重建 recommend_outcome+重评
"""
import os
import sys
import time
import json
import argparse
from datetime import timedelta

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.db import get_conn, get_daily_price, get_all_stocks, get_market_cap_map
from core.sync import _mid_weak_market_ok, _SIGNAL_INSERT_SQL
from strategy.mid_long import scan_mid_term, scan_long_term
from strategy.rec_filters import passes_quality, chase_filter
from config.personal_config import EXIT_TRACK_START_DATE


def _trading_days(start: str, end: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
            (start, end)).fetchall()
    return [r["trade_date"] for r in rows]


def _codes_on(date: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily_price WHERE trade_date=?",
            (date,)).fetchall()
    return [r["code"] for r in rows]


def _pct_above_ma20(df: pd.DataFrame, ts, close_val: float) -> float:
    """与 _build_signal_records._pct_above_ma20 同口径"""
    try:
        ma20 = df["close"].astype(float).rolling(20).mean()
        m = float(ma20.get(pd.Timestamp(ts), float("nan")))
    except Exception:
        m = float("nan")
    if m and m > 0 and close_val:
        return round(float(close_val) / m - 1.0, 4)
    return 0.0


def _scan_horizon(horizon: str, df: pd.DataFrame):
    if horizon == "mid":
        return scan_mid_term(df, weak_market_ok=True)  # 闸门已在日级判定
    return scan_long_term(df)


def recalc_date(date: str, name_map: dict, mktcap_map: dict,
                horizons: list) -> dict:
    """重算单个交易日指定周期的信号（DELETE horizon 后 INSERT）"""
    t0 = time.time()
    weak_ok = _mid_weak_market_ok(date)  # 仅 mid 使用
    codes = _codes_on(date)
    counts = {h: 0 for h in horizons}
    records = []
    # mid 被闸门拦截时跳过扫描（只清不写）；long 不受闸门约束
    scan_hzs = [h for h in horizons if h != "mid" or weak_ok]
    if scan_hzs:
        start_win = (pd.Timestamp(date) - timedelta(days=700)).strftime("%Y-%m-%d")
        for code in codes:
            try:
                df = get_daily_price(code, start_date=start_win, end_date=date)
                if df is None or len(df) < 30:
                    continue
                if len(df) < 260:
                    df = get_daily_price(code, end_date=date)
                    if df is None or len(df) < 30:
                        continue
                # 先扫描、命中后才跑质量/追高过滤（与 _build_signal_records
                # 同顺序；长线扫描轻，先过滤会浪费全市场计算）
                sigs = {}
                for hz in scan_hzs:
                    sig = _scan_horizon(hz, df)
                    if sig:
                        sigs[hz] = sig
                if not sigs:
                    continue
                name = name_map.get(code) or code
                ts = (mktcap_map.get(code) or {}).get("total_shares")
                if not passes_quality(name, df, ts):
                    continue
                if not chase_filter(df):
                    continue
                for hz, sig in sigs.items():
                    counts[hz] += 1
                    records.append({
                        "scan_date": sig["trade_date"],
                        "trade_date": sig["trade_date"],
                        "code": code,
                        "name": name,
                        "price": sig["buy_price"],
                        "fusion_score": sig["fusion_score"],
                        "vol_score": 0, "ma_score": 0, "diverge_score": 0,
                        "bottom_score": 0, "whale_score": 0,
                        "trigger_list": json.dumps(sig["triggers"], ensure_ascii=False),
                        "buy_price": sig["buy_price"],
                        "stop_loss": sig["stop_loss"],
                        "take_profit": sig["take_profit"],
                        "buy_volume": 0, "buy_money": 0, "sent_wechat": 0,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "horizon": sig["horizon"],
                        "strategy": sig["strategy"],
                        "pct_above_ma20": _pct_above_ma20(
                            df, sig["trade_date"], sig["buy_price"]),
                    })
            except Exception as e:
                print(f"  [{code}] {date} 重算失败: {e}")
    with get_conn() as conn:
        conn.execute("BEGIN TRANSACTION")
        for hz in horizons:
            conn.execute(
                "DELETE FROM stock_signal WHERE scan_date=? AND horizon=?",
                (date, hz))
        if records:
            conn.executemany(_SIGNAL_INSERT_SQL, records)
        conn.commit()
    return {"date": date, "codes": len(codes), "counts": counts,
            "weak_ok": weak_ok, "elapsed_s": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=EXIT_TRACK_START_DATE)
    ap.add_argument("--end", default=None, help="默认最新交易日")
    ap.add_argument("--horizons", default="mid",
                    help="逗号分隔，mid/long，默认 mid")
    ap.add_argument("--refresh", action="store_true",
                    help="完成后重建 recommend_outcome 并重评收益")
    args = ap.parse_args()

    horizons = [h.strip() for h in args.horizons.split(",") if h.strip()]
    assert all(h in ("mid", "long") for h in horizons), \
        f"--horizons 仅支持 mid/long: {horizons}"

    with get_conn() as conn:
        end = args.end or conn.execute(
            "SELECT MAX(trade_date) FROM daily_price").fetchone()[0]
    days = _trading_days(args.start, end)
    print(f"历史信号重算({'+'.join(horizons)}): {args.start} ~ {end}，"
          f"共 {len(days)} 个交易日")

    try:
        stocks_df = get_all_stocks()
        name_map = dict(zip(stocks_df["code"], stocks_df["name"])) if not stocks_df.empty else {}
    except Exception:
        name_map = {}
    try:
        mktcap_map = get_market_cap_map()
    except Exception:
        mktcap_map = {}

    totals = {h: 0 for h in horizons}
    t_all = time.time()
    for i, d in enumerate(days, 1):
        r = recalc_date(d, name_map, mktcap_map, horizons)
        detail = " / ".join(f"{h}:{r['counts'][h]}" for h in horizons)
        for h in horizons:
            totals[h] += r["counts"][h]
        gate = ""
        if "mid" in horizons:
            gate = f"，中线闸门{'放行' if r['weak_ok'] else '拦截'}"
        print(f"[{i}/{len(days)}] {d} 候选{r['codes']} 只 → {detail}{gate}"
              f"（{r['elapsed_s']}s）", flush=True)

    summary = " / ".join(f"{h}:{totals[h]}" for h in horizons)
    print(f"\n重算完成：{len(days)} 个交易日，新信号合计 {summary}，"
          f"总耗时 {time.time()-t_all:.0f}s")

    if args.refresh:
        from core.outcome_tracker import insert_new_outcomes, evaluate_outcomes
        insert_new_outcomes()
        n = evaluate_outcomes()
        print(f"recommend_outcome 已重建并重评（evaluate 更新 {n} 条）")


if __name__ == "__main__":
    main()
