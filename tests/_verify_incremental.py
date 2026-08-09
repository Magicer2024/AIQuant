"""
tests/_verify_incremental.py —— 增量重算等价性冒烟验证（只读，不写库）
验证点：
  1) _build_signal_records(scan_date=最新交易日) 的 short 记录
     == 全量模式(scan_date=None) 中该日的 short 记录（增量/全量当日同口径）
  2) mid/long 记录在两种模式下一致（恒只评最新日），且不再抛 NameError
     （chase_filter 导入缺失 bug 修复验证）
  3) 增量写库 SQL（DELETE 当日 + INSERT）在事务内试跑并回滚，确认可用
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.sync as s
from core.db import get_conn

COMPARE_KEYS = [
    "scan_date", "trade_date", "code", "name", "price", "fusion_score",
    "buy_price", "stop_loss", "take_profit", "horizon", "strategy",
]


def main():
    with get_conn() as conn:
        latest = conn.execute("SELECT MAX(trade_date) mx FROM daily_price").fetchone()["mx"]
        n = conn.execute("SELECT COUNT(*) n FROM daily_price WHERE trade_date=?", (latest,)).fetchone()["n"]
        codes = [r["code"] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_price WHERE trade_date=? LIMIT 8",
            (latest,)).fetchall()]
    print(f"最新交易日: {latest}  当日行情行数: {n}  样本: {codes}")

    # 与 recalc_all_scores / recalc_incremental_signals 同口径的参数
    from config.strategy_params import get_param
    stop = float(get_param("short_stop_loss"))
    take = float(get_param("short_take_profit"))
    sig_th, _ms = s._resolve_sig_threshold()
    print(f"阈值 {sig_th}  止损 {stop}  止盈 {take}")

    try:
        from core.db import get_all_stocks
        df0 = get_all_stocks()
        name_map = dict(zip(df0["code"], df0["name"])) if not df0.empty else {}
    except Exception:
        name_map = {}
    from core.db import get_market_cap_map
    mcap = get_market_cap_map()

    ok = True
    all_inc_records = []
    for code in codes:
        df = s.get_daily_price(code)
        if df is None or len(df) < 30:
            print(f"  {code}: 数据不足，跳过")
            continue
        ts = (mcap.get(code) or {}).get("total_shares")
        nm = name_map.get(code, code)

        full = s._build_signal_records(df, code, nm, ts, sig_th, stop, take, lhb_row=None, scan_date=None)
        inc = s._build_signal_records(df, code, nm, ts, sig_th, stop, take, lhb_row=None, scan_date=latest)
        inc_reuse = s._build_signal_records(
            df, code, nm, ts, sig_th, stop, take,
            lhb_row=None, scan_date=latest, reuse_scores=True)
        # 窗口读取等价性：真实增量路径读 scan_date 前 500 自然日窗口，滚动指标应一致
        import pandas as pd
        from datetime import timedelta
        wdf = s.get_daily_price(
            code,
            start_date=(pd.Timestamp(latest) - timedelta(days=500)).strftime("%Y-%m-%d"))
        inc_win = s._build_signal_records(
            wdf, code, nm, ts, sig_th, stop, take,
            lhb_row=None, scan_date=latest, reuse_scores=True)
        all_inc_records.extend(inc)

        full_day_short = [r for r in full if r["scan_date"] == latest and r["horizon"] == "short"]
        inc_short = [r for r in inc if r["horizon"] == "short"]
        inc_reuse_short = [r for r in inc_reuse if r["horizon"] == "short"]
        inc_win_short = [r for r in inc_win if r["horizon"] == "short"]

        def norm(recs):
            return sorted(tuple(r[k] for k in COMPARE_KEYS) for r in recs)

        short_eq = norm(full_day_short) == norm(inc_short)
        reuse_eq = norm(full_day_short) == norm(inc_reuse_short)
        win_eq = norm(inc_reuse_short) == norm(inc_win_short)
        ml_full = sorted(r["horizon"] for r in full if r["horizon"] in ("mid", "long"))
        ml_inc = sorted(r["horizon"] for r in inc if r["horizon"] in ("mid", "long"))
        ml_eq = ml_full == ml_inc
        ok = ok and short_eq and reuse_eq and win_eq and ml_eq
        print(f"  {code}: 当日short 全量={len(full_day_short)} 增量={len(inc_short)}"
              f" reuse={len(inc_reuse_short)} 窗口={len(inc_win_short)}"
              f" 一致={short_eq} reuse一致={reuse_eq} 窗口一致={win_eq}"
              f" | mid/long 全量={ml_full} 增量={ml_inc} 一致={ml_eq}")

    # 事务回滚式验证写库 SQL（用真实增量记录，不真正改库）
    with get_conn() as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM stock_signal WHERE scan_date=?", (latest,))
            if all_inc_records:
                conn.executemany(s._SIGNAL_INSERT_SQL, all_inc_records)
            conn.execute("ROLLBACK")
            print(f"写库 SQL 事务试跑（回滚，{len(all_inc_records)} 条）: OK")
        except Exception as e:
            conn.execute("ROLLBACK")
            print(f"写库 SQL 事务试跑失败: {e}")
            ok = False

    print("\n结论:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
