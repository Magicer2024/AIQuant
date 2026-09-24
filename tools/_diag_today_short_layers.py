"""诊断 today 接口 short 组候选稀缺：按过滤链逐层计数。

short 组 = 门控(fusion≥gate) + T1 辅助过滤 + 大盘走弱闸门 + 抄底观察线排除，
四层叠加后经常只剩个位数，再经「盈亏比 <1.5 → avoid」剔除，最终常 0 条。
本脚本给出每层各砍掉多少，定位主要瓶颈。

用法：python tools/_diag_today_short_layers.py [日期]
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn
from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES
from core.outcome_tracker import (short_t1_filter_sql, short_market_gate_sql,
                                  short_observe_bottom_sql)


def main():
    with get_conn() as conn:
        sd = sys.argv[1] if len(sys.argv) > 1 else conn.execute(
            "SELECT MAX(scan_date) d FROM stock_signal").fetchone()["d"]
        bf = "".join(f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES) if MAIN_BOARD_ONLY else ""
        base = ("""FROM stock_signal s
            WHERE s.scan_date = ? AND COALESCE(s.horizon,'short') = 'short'
              AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
              AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
              AND COALESCE(s.strategy,'') NOT IN ('强势突破','缩量回踩')""" + bf)
        gate = float(get_param("short_conf_gate"))
        t1, t1p = short_t1_filter_sql(conn, sd, sd)
        mk, mkp = short_market_gate_sql(conn, sd, sd)
        ob, obp = short_observe_bottom_sql()

        def n(w="", p=()):
            return conn.execute("SELECT COUNT(*) n " + base + w, (sd, *p)).fetchone()["n"]

        print(f"scan_date={sd}  short_conf_gate={gate}")
        a = n()
        b = n(" AND s.fusion_score >= ?", (gate,))
        c = n(" AND s.fusion_score >= ? AND " + t1, (gate, *t1p))
        d = n(" AND s.fusion_score >= ? AND " + t1 + " AND " + mk, (gate, *t1p, *mkp))
        e = n(" AND s.fusion_score >= ? AND " + t1 + " AND " + mk + " AND " + ob,
              (gate, *t1p, *mkp, *obp))
        print(f"  基础                     {a}")
        print(f"  + 融合分门控             {b}   (砍 {a - b})")
        print(f"  + T1 辅助过滤            {c}   (砍 {b - c})")
        print(f"  + 大盘走弱闸门           {d}   (砍 {c - d})")
        print(f"  + 抄底观察线排除         {e}   (砍 {d - e})")

        print("\nstrategy 分布（基础集）:")
        for r in conn.execute(
                "SELECT COALESCE(s.strategy,'') st, COUNT(*) n " + base +
                " GROUP BY 1 ORDER BY n DESC LIMIT 8", (sd,)):
            print("   ", dict(r))
        print("\nT1 SQL:", t1[:220])
        print("MK SQL:", mk[:220])
        print("OB SQL:", ob[:220])


if __name__ == "__main__":
    main()
