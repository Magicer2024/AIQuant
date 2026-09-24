# -*- coding: utf-8 -*-
"""检验 RECO_REGIME_CAP（原 MID_LONG_REGIME_CAP）的实证依据：cold 日 mid/long 期望？

方法：
  1. 取 stock_signal 全历史 mid/long 信号（scan_date 去重）；
  2. 用 compute_regime_series 复现每个 scan_date 当日 regime；
  3. 前向收益：T+1 开盘买入 → T+N 收盘卖出（OC 口径，与项目其它评估一致）；
  4. 按 regime 分组统计均值/胜率。

若 cold 组为正期望 → cold→0 天花板是错的（或至少在 cut 掉优质票）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn
from core.market_regime import compute_regime_series

HOLD = {"mid": 20, "long": 60}


def fwd_oc(conn, code, scan_date, n):
    """T+1 开盘买入 → 之后第 n 个交易日收盘卖出，返回百分比收益。"""
    rows = conn.execute("""
        SELECT trade_date, open, close FROM daily_price
        WHERE code=? AND trade_date>? ORDER BY trade_date LIMIT ?
    """, (code, scan_date, n + 1)).fetchall()
    if len(rows) < 2:
        return None
    buy = rows[0]["open"]
    sell = rows[-1]["close"]
    if not buy or not sell:
        return None
    return (sell - buy) / buy * 100


with get_conn() as conn:
    dates = [r["scan_date"] for r in conn.execute(
        "SELECT DISTINCT scan_date FROM stock_signal ORDER BY scan_date"
    ).fetchall()]
    dates = [d for d in dates if d >= "2025-01-01"]
    reg = compute_regime_series(conn, dates[0], dates[-1])

    stats = {}
    for hz in ("mid", "long"):
        rows = conn.execute("""
            SELECT s.code, s.scan_date FROM stock_signal s
            WHERE COALESCE(s.horizon,'short')=?
              AND s.scan_date >= ?
              AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
              AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
        """, (hz, dates[0])).fetchall()
        for r in rows:
            sg = reg.get(r["scan_date"])
            if not sg:
                continue
            ret = fwd_oc(conn, r["code"], r["scan_date"], HOLD[hz])
            if ret is None:
                continue
            stats.setdefault((hz, sg), []).append(ret)

    print(f"前向持有: mid T+{HOLD['mid']} / long T+{HOLD['long']}（T+1 开盘买 → 收盘卖）")
    print(f"{'周期':<6}{'regime':<10}{'笔数':>7}{'均值%':>9}{'中位%':>9}{'胜率%':>8}")
    print("-" * 52)
    for hz in ("mid", "long"):
        allr = []
        for sg in ("hot", "warm", "neutral", "cool", "cold"):
            v = stats.get((hz, sg), [])
            if not v:
                continue
            allr += v
            win = sum(1 for x in v if x > 0) / len(v) * 100
            sv = sorted(v)
            med = sv[len(sv) // 2]
            print(f"{hz:<6}{sg:<10}{len(v):>7}{sum(v)/len(v):>9.2f}{med:>9.2f}{win:>8.1f}")
        if allr:
            win = sum(1 for x in allr if x > 0) / len(allr) * 100
            print(f"{hz:<6}{'ALL':<10}{len(allr):>7}{sum(allr)/len(allr):>9.2f}"
                  f"{'':>9}{win:>8.1f}")
        print()
