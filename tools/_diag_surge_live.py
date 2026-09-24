"""次日强势观察 · 线上成绩单实况打印
======================================================================
用途：改完 `SURGE_BREAKOUT` 出场参数后，直接看「接口真实返回」的成绩单，
      对照 `tools/_diag_surge_tune.py` 的重放预期。

用法：
  python tools/_diag_surge_live.py [days]     # days 默认 60，上限 180
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask                                    # noqa: E402
from routes.investor import investor_bp                    # noqa: E402

DB = os.path.join(ROOT, "core", "quant.db")


def _signal_ctx(conn, code, scan_date):
    """信号日收盘涨幅 + 次日开盘跳空（解释「为什么这批信号入场就差」）。"""
    row = conn.execute(
        """SELECT s.trade_date, s.price,
                  (SELECT pct_change FROM daily_price d
                   WHERE d.code = s.code AND d.trade_date = s.trade_date) AS pct_day
           FROM stock_signal s
           WHERE s.code = ? AND s.scan_date = ? AND s.strategy = '强势突破'""",
        (code, scan_date),
    ).fetchone()
    if not row:
        return None, None, None
    sig_day, sig_close, pct_day = row[0], row[1], row[2]
    nxt = conn.execute(
        "SELECT open FROM daily_price WHERE code = ? AND trade_date > ? "
        "ORDER BY trade_date ASC LIMIT 1", (code, sig_day),
    ).fetchone()
    nxt_open = nxt[0] if nxt else None
    gap = (round((nxt_open - sig_close) / sig_close * 100, 2)
           if nxt_open and sig_close else None)
    return pct_day, nxt_open, gap


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    app = Flask(__name__)
    app.register_blueprint(investor_bp)
    r = app.test_client().get(f"/api/investor/surge_picks?limit=12&days={days}")
    data = (r.get_json() or {}).get("data") or {}
    hs = data.get("history_summary") or {}
    hist = data.get("history") or []

    print(f"信号日 {data.get('date')} · 当日卡片 {data.get('count')} 条 · "
          f"历史窗口 {hs.get('window_days')} 天")
    print(f"出场口径：{hs.get('exit_mode_label')} · "
          f"建仓当日纳入判定 = {hs.get('judge_entry_day')}")
    print(f"\n历史 {len(hist)} 条 | 已结算 {hs.get('closed')} | "
          f"持有中 {hs.get('holding')} | 放弃 {hs.get('skipped')} | 待买入 {hs.get('pending')}")
    print(f"结算胜率 {hs.get('win_rate')}%  结算平均 {hs.get('avg_return')}%  "
          f"盈亏比 {hs.get('profit_factor')}")
    print(f"最佳 {hs.get('best_return')}%  最差 {hs.get('worst_return')}%  "
          f"| 止损 {hs.get('stop_loss_count')}  止盈 {hs.get('take_profit_count')}  "
          f"次日了结 {hs.get('expire_count')}")
    print("\n持有漂移对照（T+n 为买入后第 n 个交易日收盘，非实际出场）：")
    for k in ("t1", "t2", "t3", "t4", "t5"):
        s = hs.get(k) or {}
        if s.get("n"):
            print(f"  {k.upper()}  n={s['n']:<3} 胜率 {s['win_rate']:5.1f}%  "
                  f"均值 {s['avg_return']:+6.2f}%")

    print("\n逐条明细：")
    print(f"{'signal_date':<12}{'code':<8}{'name':<10}{'sig_pct':>8}{'gap':>8}"
          f"{'entry':>9}{'hold':>5}  {'return':>8}  status")
    conn = sqlite3.connect(DB)
    gaps = []
    for h in hist:
        pct_day, _o, gap = _signal_ctx(conn, h["code"], h["signal_date"])
        if gap is not None and h.get("hold_days") is not None:
            gaps.append(gap)
        ret = h.get("return_pct")
        print(f"{h.get('signal_date') or '':<12}{h.get('code') or '':<8}"
              f"{(h.get('name') or '')[:8]:<10}"
              f"{(f'{pct_day:+.2f}' if pct_day is not None else '--'):>8}"
              f"{(f'{gap:+.2f}' if gap is not None else '--'):>8}"
              f"{str(h.get('entry_price') if h.get('entry_price') is not None else '--'):>9}"
              f"{str(h.get('hold_days') if h.get('hold_days') is not None else '--'):>5}  "
              f"{(f'{ret:+.2f}%' if ret is not None else '--'):>8}  "
              f"{h.get('status_label')}")
    conn.close()
    if gaps:
        lo = sum(1 for g in gaps if g < 0)
        print(f"\n开户跳空：{lo}/{len(gaps)} 为低开，均值 "
              f"{sum(gaps) / len(gaps):+.2f}%（诊断 §6：低开不必然是坏事，但 ≤-4% 的"
              f"跳空破位已是止损线之下，本口径直接放弃）")


if __name__ == "__main__":
    main()
