"""验证假设：观察线排除(ob)专挑「会被盈亏比门杀掉」的票。

ob 条件 = `pct_above_ma20 >= 0.055`（扩展度高的票）→ 这些票波动大 → ATR 止损
（k=2.5，上限 15%）离入场价远 → 盈亏比 reward/risk < 1.5 被 signal 判 avoid。
于是 ob 与 rr 门互相打架：ob 留下的正是 rr 门要剔的，short 名额长期出不满。

输出：近 N 个交易日，门控后池子 vs ob 后残池 的 规模 / rr<1.5 占比 / 最终 buy 数。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn
from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES
from core.outcome_tracker import (short_t1_filter_sql, short_market_gate_sql,
                                  short_observe_bottom_sql)

RR_MIN = 1.5


def rr_of(entry, stop, tp):
    if not entry or not stop or not tp or entry <= 0 or stop <= 0 or tp <= 0:
        return None
    risk = (entry - stop) / entry * 100
    rew = (tp - entry) / entry * 100
    if risk <= 0:
        return None
    return rew / risk


def main(days: int = 20):
    with get_conn() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT scan_date FROM stock_signal "
            "ORDER BY scan_date DESC LIMIT ?", (days,)).fetchall()]
        bf = "".join(f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES) if MAIN_BOARD_ONLY else ""
        gate = float(get_param("short_conf_gate"))
        print(f"{'date':<12}{'门控后':>7}{'rr<1.5':>8}{'ob后':>7}{'rr<1.5':>8}{'最终buy':>8}")
        tot = [0, 0, 0, 0, 0]
        for sd in dates:
            t1, t1p = short_t1_filter_sql(conn, sd, sd)
            mk, mkp = short_market_gate_sql(conn, sd, sd)
            ob, obp = short_observe_bottom_sql()
            base = ("""FROM stock_signal s
                WHERE s.scan_date=? AND COALESCE(s.horizon,'short')='short'
                  AND s.buy_price IS NOT NULL AND s.buy_price>0
                  AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
                  AND COALESCE(s.strategy,'') NOT IN ('强势突破','缩量回踩')
                  AND s.fusion_score>=? """ + bf)
            cols = ("s.buy_price, s.stop_loss, s.take_profit")
            g_rows = conn.execute(f"SELECT {cols} " + base + f" AND {t1} AND {mk}",
                                  (sd, gate, *t1p, *mkp)).fetchall()
            o_rows = conn.execute(f"SELECT {cols} " + base + f" AND {t1} AND {mk} AND {ob}",
                                  (sd, gate, *t1p, *mkp, *obp)).fetchall()
            g_bad = sum(1 for r in g_rows
                        if rr_of(r[0], r[1], r[2]) is None or rr_of(r[0], r[1], r[2]) < RR_MIN)
            o_bad = sum(1 for r in o_rows
                        if rr_of(r[0], r[1], r[2]) is None or rr_of(r[0], r[1], r[2]) < RR_MIN)
            final = len(o_rows) - o_bad
            tot[0] += len(g_rows); tot[1] += g_bad
            tot[2] += len(o_rows); tot[3] += o_bad; tot[4] += final
            print(f"{sd:<12}{len(g_rows):>7}{g_bad:>8}{len(o_rows):>7}{o_bad:>8}{final:>8}")
        g_rate = tot[1] / tot[0] * 100 if tot[0] else 0
        o_rate = tot[3] / tot[2] * 100 if tot[2] else 0
        print(f"\n合计 {len(dates)} 天：门控池 {tot[0]} 只、rr<1.5 占 {g_rate:.1f}%"
              f"  →  ob 残池 {tot[2]} 只、rr<1.5 占 {o_rate:.1f}%"
              f"  →  最终可买 {tot[4]} 只（日均 {tot[4]/len(dates):.2f}）")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
