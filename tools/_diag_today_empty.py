"""诊断 /api/investor/today 各组为空/少的原因。

复现 today_recommendations 的取数 SQL（与线上同参），逐条打印：
  - 候选条数（limit*3）
  - 每条的 signal level（avoid / sell 会被剔除，不占名额）
  - 盈亏比 risk_reward（<1.5 一票否决 ⇒ avoid 的主因）
用于定位「某组为空」是取数层没票，还是展示层被 signal 过滤清空。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn
from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES
from core.outcome_tracker import (short_t1_filter_sql, short_order_clause,
                                  short_market_gate_sql, short_observe_bottom_sql,
                                  long_order_clause, long_second_order_clause,
                                  long_pool_filter_sql, long_sort_pool_mult)
from routes.investor import _calc_signal, _compute_market_regime

LIMIT = 4


def main():
    with get_conn() as conn:
        scan_date = conn.execute("SELECT MAX(scan_date) AS d FROM stock_signal").fetchone()["d"]
        print("scan_date =", scan_date)
        regime = _compute_market_regime(conn)
        print("market_regime =", regime)

        board_filter = ""
        if MAIN_BOARD_ONLY:
            board_filter = "".join(
                f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES)

        gate = float(get_param("short_conf_gate"))
        t1_cond, t1_params = short_t1_filter_sql(conn, scan_date, scan_date)
        mk_cond, mk_params = short_market_gate_sql(conn, scan_date, scan_date)
        ob_cond, ob_params = short_observe_bottom_sql()

        for hz in ("short", "mid", "long"):
            gate_sql, gate_params = "", []
            if hz == "short":
                gate_sql = f" AND s.fusion_score >= ? AND {t1_cond} AND {mk_cond} AND {ob_cond}"
                gate_params = [gate, *t1_params, *mk_params, *ob_params]
            if hz == "short":
                order_clause = short_order_clause()
            elif hz == "long":
                order_clause = long_order_clause()
            else:
                order_clause = "COALESCE(s.fusion_score, 0) DESC"
            _long_second = long_second_order_clause("") if hz == "long" else None
            _long_pool = long_pool_filter_sql() if hz == "long" else ""
            _want = LIMIT * 3
            inner = f"""
                SELECT s.code, s.name, s.price AS signal_price, s.buy_price,
                       s.stop_loss, s.take_profit, s.fusion_score, s.strategy,
                       COALESCE(s.horizon,'short') AS horizon,
                       (SELECT d.close FROM daily_price d WHERE d.code=s.code
                        ORDER BY d.trade_date DESC LIMIT 1) AS latest_close
                FROM stock_signal s
                WHERE s.scan_date = ? AND COALESCE(s.horizon,'short') = ?
                  AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
                  AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
                  AND COALESCE(s.strategy,'') NOT IN ('强势突破','缩量回踩')
                  {board_filter} {gate_sql} {_long_pool}
                ORDER BY {order_clause} LIMIT ?
            """
            if _long_second:
                sql = f"SELECT * FROM ({inner}) ORDER BY {_long_second} LIMIT ?"
                params = (scan_date, hz, *gate_params, _want * long_sort_pool_mult(), _want)
            else:
                sql, params = inner, (scan_date, hz, *gate_params, _want)
            rows = conn.execute(sql, params).fetchall()
            print(f"\n=== {hz}: 候选 {len(rows)} 条 ===")
            lv = {}
            for r in rows:
                d = dict(r)
                entry = d.get("buy_price") or d.get("signal_price") or d.get("latest_close") or 0
                stop = d.get("stop_loss") or 0
                tp = d.get("take_profit") or 0
                risk = round((entry - stop) / entry * 100, 1) if entry > 0 and stop > 0 else None
                rew = round((tp - entry) / entry * 100, 1) if entry > 0 and tp > 0 else None
                rr = round(rew / risk, 2) if risk and rew and risk > 0 else None
                sig = _calc_signal(score=d.get("fusion_score") or 0, risk_reward=rr,
                                   risk_pct=risk, market_regime=regime,
                                   is_held=False, trend_up=False, horizon=hz)
                lv[sig["level"]] = lv.get(sig["level"], 0) + 1
                print(f"  {d['code']} {d['name'][:6]:<6} entry={entry} stop={stop} tp={tp} "
                      f"risk={risk} rew={rew} rr={rr} -> {sig['level']}")
            print("  level 分布:", lv)
            print("  过滤后剩余:", len(rows) - lv.get("avoid", 0) - lv.get("sell", 0))


if __name__ == "__main__":
    main()
