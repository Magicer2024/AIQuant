"""临时校验：ATR 止损下「全量重算 / 单日增量 / 窗口增量」三条路径产出的止损价完全一致。
（ATR 依赖索引对齐，窗口截断后若 ATR 取值漂移会造成增量与全量不一致 → 必须验证）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import timedelta

import core.sync as s
from core.db import get_conn, get_market_cap_map, get_all_stocks
from config.strategy_params import get_param

KEYS = ["scan_date", "code", "price", "stop_loss", "take_profit", "horizon", "strategy"]


def norm(recs):
    return sorted(tuple(r[k] for k in KEYS) for r in recs)


def main():
    with get_conn() as conn:
        codes = [r["code"] for r in conn.execute(
            "SELECT code, COUNT(*) n FROM stock_signal WHERE strategy='短线融合' "
            "GROUP BY code ORDER BY n DESC LIMIT 8").fetchall()]
        dates = [r["scan_date"] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal WHERE strategy='短线融合' "
            "ORDER BY scan_date DESC LIMIT 40").fetchall()]
    print(f"样本股（短线信号最多）: {codes}")
    print(f"抽样信号日: {len(dates)} 个（{dates[-1]} ~ {dates[0]}）")

    sig_th, _ = s._resolve_sig_threshold()
    stop = float(get_param("short_stop_loss"))
    take = float(get_param("short_take_profit"))
    try:
        df0 = get_all_stocks()
        name_map = dict(zip(df0["code"], df0["name"])) if not df0.empty else {}
    except Exception:
        name_map = {}
    mcap = get_market_cap_map()

    n_cmp, n_bad, n_rec = 0, 0, 0
    for code in codes:
        df_full = s.get_daily_price(code)
        if df_full is None or len(df_full) < 60:
            continue
        ts_sh = (mcap.get(code) or {}).get("total_shares")
        nm = name_map.get(code, code)
        full = norm(s._build_signal_records(
            df_full, code, nm, ts_sh, sig_th, stop, take, scan_date=None))
        for d in dates:
            if int(d[:4]) < 2024:
                continue
            # 单日增量（全历史 df）
            inc = norm([r for r in s._build_signal_records(
                df_full, code, nm, ts_sh, sig_th, stop, take, scan_date=d)
                if r["horizon"] == "short"])
            # 窗口增量（真实增量路径读 scan_date 前 700 自然日）
            wdf = s.get_daily_price(
                code, start_date=(pd.Timestamp(d) - timedelta(days=700)).strftime("%Y-%m-%d"),
                end_date=d)
            win = norm([r for r in s._build_signal_records(
                wdf, code, nm, ts_sh, sig_th, stop, take, scan_date=d)
                if r["horizon"] == "short"])
            exp = [t for t in full if t[0] == d and t[5] == "short"]
            n_cmp += 3
            n_rec += len(exp)
            if not (exp == inc == win):
                n_bad += 1
                print(f"  [BAD] {code} {d}\n    全量={exp}\n    增量={inc}\n    窗口={win}")
        if n_rec >= 200:
            break

    print(f"\n比对 {n_cmp} 次（全量 vs 增量 vs 窗口），覆盖 {n_rec} 条短线记录，"
          f"不一致 {n_bad} 次")
    print("结论:", "PASS" if n_bad == 0 and n_rec > 0 else "FAIL")
    return 0 if (n_bad == 0 and n_rec > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
