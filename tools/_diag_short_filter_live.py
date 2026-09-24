"""诊断 6：short 过滤链在「实战口径」下的表现（回踩确认入场 + 线上出场纪律）。

前序诊断用的是 T+10 裸持有，本脚本换成与 _evaluate_short 完全相同的数学
（回踩确认入场 short_pullback_entry + _short_exit_sim 收盘判定出场），
确认「过滤链在选差票」这个结论在实战口径下是否依然成立。
"""
import sys
import statistics as st

sys.path.insert(0, ".")
from core.db import get_conn
from core.outcome_tracker import (short_t1_filter_sql, short_market_gate_sql,
                                  short_observe_bottom_sql, short_order_clause,
                                  _short_exit_sim)
from strategy.exit_advisor import get_max_hold
from config.strategy_params import get_param
from config.personal_config import MAIN_BOARD_ONLY, EXCLUDED_BOARD_PREFIXES

START = "2026-07-20"
CAP = max(1, int(get_param("short_top_n")))
GATE = float(get_param("short_conf_gate"))
TRAIL = get_param("short_trailing_pct")
MAX_HOLD = get_max_hold("short") or 10
WIN = max(1, int(get_param("short_entry_window_days")))

with get_conn() as conn:
    t1, t1p = short_t1_filter_sql(conn, START)
    mk, mkp = short_market_gate_sql(conn, START)
    ob, obp = short_observe_bottom_sql()
    board = "".join(f" AND s.code NOT LIKE '{p}%'" for p in EXCLUDED_BOARD_PREFIXES) if MAIN_BOARD_ONLY else ""
    order = short_order_clause("s")
    BASE = f"""
        FROM stock_signal s
        WHERE s.scan_date >= '{START}'
          AND COALESCE(s.horizon,'short') = 'short'
          AND s.buy_price IS NOT NULL AND s.buy_price > 0
          AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
          AND COALESCE(s.strategy,'') NOT IN ('强势突破','缩量回踩')
          {board}
    """

    def picks(extra_sql, extra_params):
        sql = f"""
        SELECT code, scan_date, strategy, buy_price, stop_loss, take_profit FROM (
            SELECT s.code, s.scan_date, s.strategy, s.buy_price, s.stop_loss, s.take_profit,
                   ROW_NUMBER() OVER (PARTITION BY s.scan_date ORDER BY {order}) rn
            {BASE} AND ({extra_sql})
        ) WHERE rn <= {CAP}"""
        return [dict(r) for r in conn.execute(sql, extra_params).fetchall()]

    def trade(p):
        """实战口径：回踩确认入场 + 收盘判定出场。返回 (状态, 收益%)。"""
        px = [dict(r) for r in conn.execute(
            "SELECT trade_date, open, close, high, low FROM daily_price "
            "WHERE code=? AND trade_date>? ORDER BY trade_date ASC LIMIT ?",
            (p["code"], p["scan_date"], 15 + WIN + MAX_HOLD)).fetchall()]
        if not px:
            return None, None
        entry = p["buy_price"]
        pullback = p["strategy"] != "隔日动量"
        if pullback:
            fill = None
            for i in range(min(WIN, len(px))):
                if px[i]["low"] is not None and px[i]["low"] <= entry:
                    o = px[i]["open"] or entry
                    fill = (i, round(min(float(entry), float(o)), 2))
                    break
            if fill is None:
                return "no_fill", None
            i, ee = fill
            hold = px[i:i + MAX_HOLD]
        else:
            ee = float(px[0]["open"]) if px[0]["open"] else entry
            hold = px[:MAX_HOLD]
        r = _short_exit_sim(hold, ee, p["stop_loss"], p["take_profit"], TRAIL, MAX_HOLD)
        return (r[0] or "hold"), r[2]

    def evaluate(label, sql_, prm):
        sel = picks(sql_, prm)
        rets, stops, nofill = [], 0, 0
        for p in sel:
            status, ret = trade(p)
            if status == "no_fill":
                nofill += 1
            elif ret is not None:
                rets.append(ret)
                stops += 1 if status == "stop_loss" else 0
        if not rets:
            print(f"  {label:<32} n=0")
            return
        win = sum(1 for v in rets if v > 0) / len(rets) * 100
        print(f"  {label:<32} 信号{len(sel):<4} 成交{len(rets):<4} no_fill{nofill:<4} "
              f"均值 {st.mean(rets):+6.2f}%  中位 {st.median(rets):+6.2f}%  "
              f"胜率 {win:5.1f}%  止损率 {stops/len(rets)*100:5.1f}%")

    print("=" * 108)
    print(f"short 实战口径对照（回踩入场 window={WIN} / 止损+移动止盈{int(TRAIL*100)}% / 上限{MAX_HOLD}日）")
    print("=" * 108)
    evaluate("A 线上全链", f"s.fusion_score >= ? AND {t1} AND {mk} AND {ob}",
             [GATE, *t1p, *mkp, *obp])
    evaluate("B 无过滤", "1", [])
    evaluate("C 全链去观察线门槛", f"s.fusion_score >= ? AND {t1} AND {mk}", [GATE, *t1p, *mkp])
    evaluate("D 只观察线门槛", ob, obp)
    evaluate("E 只 fusion 门控", "s.fusion_score >= ?", [GATE])
    evaluate("F 只 T1 辅助过滤", t1, t1p)
    evaluate("G 只大盘走弱闸门", mk, mkp)
