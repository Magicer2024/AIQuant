"""次日强势观察「历史表现」回归验证：接口输出 vs 独立复算。

用法（项目根目录，用带依赖的解释器）：
    python tests/_verify_surge_history.py

覆盖：
  ① T+1~T+5 收益 = 买入后第 n 个交易日收盘相对「次日开盘买入价」的涨幅（持有漂移对照）
  ② 出场判定（隔日了结：T+1 收盘 ≤ 止损 → 止损 / ≥ 止盈 → 止盈 / 否则次日了结；
     建仓当日纳入判定）、高开越止盈 → 放弃不计盈亏
  ③ history_summary 算术自洽（已结算笔数/胜率/平均/盈亏比/各期统计口径）
  ④ 无信号日也返回 history（长期成绩单不随当日有无信号消失）
  ⑤ 窗口口径 = 过去 N 天且不含当日信号日（当日那批在卡片区，不双份计数）
"""
import os
import sys
import json
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "core", "quant.db")
MAX_HOLD = 5          # T+n 漂移对照期数（出场规则用 SURGE_BREAKOUT.max_hold_days）

from config.strategy_params import SURGE_BREAKOUT    # noqa: E402
_MAX_HOLD_CFG = max(1, int(SURGE_BREAKOUT.get("max_hold_days", 1)))
_JUDGE_ENTRY_DAY = bool(SURGE_BREAKOUT.get("judge_entry_day", True))

_pass = 0
_fail = 0


def check(cond, msg):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print("   FAIL:", msg)


def _client():
    """优先用完整 app；app.py 会拉起 qlib 等重依赖，缺失时退到最小 Flask + 该蓝图。

    两条路都走真实 HTTP 栈（test_client），确保覆盖路由 + ok()/_sanitize 序列化。
    """
    try:
        from app import app as _a
        return _a, "app.py 完整栈"
    except Exception as e:
        from flask import Flask
        from routes.investor import investor_bp
        a = Flask(__name__)
        a.register_blueprint(investor_bp)
        return a, f"最小栈（app.py 不可导入：{type(e).__name__}）"


_APP, _STACK = _client()
_CLIENT = _APP.test_client()


def fetch_hist(days=60, limit=6):
    r = _CLIENT.get(f"/api/investor/surge_picks?limit={limit}&days={days}")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    return (r.get_json() or {}).get("data") or {}


def replay_independently(conn, code, scan_date):
    """独立复算单条信号：返回 (entry_price, t_returns, exit_reason, return_pct)

    出场规则从 SURGE_BREAKOUT 现读，确保改参数后复算与后端同步。
    """
    max_hold = _MAX_HOLD_CFG
    judge_entry_day = _JUDGE_ENTRY_DAY
    drift = max(MAX_HOLD, max_hold)

    sig = conn.execute(
        "SELECT trade_date, stop_loss, take_profit FROM stock_signal "
        "WHERE code = ? AND scan_date = ? AND strategy = '强势突破'",
        (code, scan_date),
    ).fetchone()
    if not sig:
        return None
    sig_day = sig["trade_date"] or scan_date
    stop, tp = sig["stop_loss"], sig["take_profit"]
    px = conn.execute(
        "SELECT trade_date, open, close FROM daily_price WHERE code = ? "
        "AND trade_date > ? ORDER BY trade_date ASC LIMIT ?",
        (code, sig_day, drift + 1),
    ).fetchall()
    if not px:
        return ("pending", None, None, None, None)
    entry = px[0]["open"]
    if not entry or entry <= 0:
        return None
    t = {n: (round((px[n - 1]["close"] - entry) / entry * 100, 2)
             if len(px) >= n and px[n - 1]["close"] else None)
         for n in range(1, drift + 1)}
    if stop and entry <= stop:
        return (round(entry, 2), t, "skipped", None, None)   # 开盘破止损 → 不买入
    if tp and entry > tp:
        return (round(entry, 2), t, "skipped", None, None)   # 高开越止盈 → 不追
    reason = exit_px = hold = None
    for j, p in enumerate(px[:max_hold], start=1):
        close = p["close"]
        if not close or close <= 0:
            break
        if j == 1 and not judge_entry_day:
            continue                      # 建仓当日不判出场（多日持仓模式）
        if stop and close <= stop:
            reason, exit_px, hold = "stop_loss", close, j
            break
        if tp and close >= tp:
            reason, exit_px, hold = "take_profit", close, j
            break
        if j >= max_hold:
            reason, exit_px, hold = "max_hold_days", close, j
            break
    if reason:
        return (round(entry, 2), t, reason, hold,
                round((exit_px - entry) / entry * 100, 2))
    last = px[min(len(px), max_hold) - 1]
    return (round(entry, 2), t, "hold", min(len(px), max_hold),
            round((last["close"] - entry) / entry * 100, 2))


def main():
    data = fetch_hist(60)
    hist = data.get("history") or []
    hs = data.get("history_summary") or {}
    print(f"栈：{_STACK}")
    print(f"信号日 {data.get('date')} · 窗口 {data.get('days')} 天 · "
          f"历史 {len(hist)} 条 · 当日卡片 {data.get('count')} 条")

    # ⑤ 不含当日信号日
    check(all(h["signal_date"] < data["date"] for h in hist),
          "历史里混入了当日信号日的记录")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    t_keys = [f"t{n}_return" for n in range(1, MAX_HOLD + 1)]
    mism = 0
    for h in hist:
        mine = replay_independently(conn, h["code"], h["signal_date"])
        if mine is None:
            continue
        entry, t, reason, hold, ret = mine
        if h["status"] == "pending":
            check(h.get("entry_price") is None, f"{h['code']} pending 却有买入价")
            continue
        if entry is None:
            continue
        if h["status"] == "skipped":
            check(h.get("return_pct") is None,
                  f"{h['code']} 放弃却计了收益（不该进成绩单）")
            check(all(h.get(k) is None for k in t_keys),
                  f"{h['code']} 放弃却有 T+n 收益")
            check(h.get("status_label") in ("放弃·开盘破止损", "放弃·高开越过止盈"),
                  f"{h['code']} 放弃标签异常: {h.get('status_label')}")
            continue
        # ① T+n 独立复算比对
        for k in t_keys:
            if h.get(k) != t[int(k[1])]:
                mism += 1
                print(f"   T 收益不符 {h['code']} {h['signal_date']} {k}: "
                      f"接口 {h.get(k)} vs 复算 {t[int(k[1])]}")
        # ② 出场口径比对
        check(h["exit_reason"] == (None if reason == "hold" else reason),
              f"{h['code']} 出场原因不符: {h['exit_reason']} vs {reason}")
        check(h["return_pct"] == ret,
              f"{h['code']} 结算收益不符: {h['return_pct']} vs {ret}")
        if reason != "hold":
            check(h["hold_days"] == hold,
                  f"{h['code']} 持仓天数不符: {h['hold_days']} vs {hold}")
            check(h["hold_days"] <= _MAX_HOLD_CFG,
                  f"{h['code']} 持仓 {h['hold_days']} 日超过配置上限 {_MAX_HOLD_CFG}")

    # ③ summary 算术自洽
    closed = [h for h in hist if h["status"] == "clear"]
    wins = [h for h in closed if (h["return_pct"] or 0) > 0]
    check(hs.get("total") == len(hist), "total 不符")
    check(hs.get("closed") == len(closed), "closed 不符")
    if closed:
        check(hs.get("avg_return") == round(
            sum(h["return_pct"] for h in closed) / len(closed), 2), "avg_return 不符")
        check(hs.get("win_rate") == round(len(wins) / len(closed) * 100, 1),
              "win_rate 不符")
        check(hs.get("best_return") == max(h["return_pct"] for h in closed), "best 不符")
        check(hs.get("worst_return") == min(h["return_pct"] for h in closed), "worst 不符")
        gain = sum(h["return_pct"] for h in closed if h["return_pct"] > 0)
        loss = abs(sum(h["return_pct"] for h in closed if h["return_pct"] <= 0))
        check(hs.get("profit_factor") == (round(gain / loss, 2) if loss > 0 else None),
              "profit_factor 不符")
    check(hs.get("stop_loss_count") == sum(
        1 for h in closed if h["exit_reason"] == "stop_loss"), "止损计数不符")
    check(hs.get("take_profit_count") == sum(
        1 for h in closed if h["exit_reason"] == "take_profit"), "止盈计数不符")
    check(hs.get("expire_count") == sum(
        1 for h in closed if h["exit_reason"] == "max_hold_days"), "到期计数不符")
    check(hs.get("holding") == sum(1 for h in hist if h["status"] == "hold"), "持有中计数不符")
    check(hs.get("skipped") == sum(1 for h in hist if h["status"] == "skipped"), "放弃计数不符")

    # 各期统计与逐条数据自洽
    for n in range(1, MAX_HOLD + 1):
        key = f"t{n}"
        vals = [h[f"t{n}_return"] for h in hist if h.get(f"t{n}_return") is not None]
        s = hs.get(key) or {}
        check(s.get("n") == len(vals), f"{key}.n 不符: {s.get('n')} vs {len(vals)}")
        if vals:
            check(s.get("win") == sum(1 for v in vals if v > 0), f"{key}.win 不符")
            check(s.get("win_rate") == round(
                sum(1 for v in vals if v > 0) / len(vals) * 100, 1), f"{key}.win_rate 不符")
            check(s.get("avg_return") == round(sum(vals) / len(vals), 2),
                  f"{key}.avg_return 不符")

    check(hs.get("max_hold_days") == _MAX_HOLD_CFG,
          f"max_hold_days 不符: {hs.get('max_hold_days')} vs {_MAX_HOLD_CFG}")
    check(hs.get("window_days") == 60, "window_days 不符")
    check(not hs.get("cum_return"), "cum_return 仍存在（单笔百分比不可相加）")

    # ④ 当日无信号（limit=0 → 走 early-return 分支）也要返回成绩单，不随卡片消失
    #    （原实现此处直接 return 空 items，成绩单跟着消失 —— 已修）
    off = fetch_hist(60, limit=0)
    check(off.get("count") == 0, "limit=0 仍返回了卡片")
    check(len(off.get("history") or []) == len(hist),
          f"无当日信号时成绩单消失/变少: {len(off.get('history') or [])} vs {len(hist)}")
    check((off.get("history_summary") or {}).get("total") == hs.get("total"),
          "无当日信号时 summary 不一致")

    # ⑥ 窗口参数生效：窄窗口只能更少，且不含窗口外记录
    narrow = fetch_hist(7)
    boundary = conn.execute("SELECT date('now','-7 days')").fetchone()[0]
    n_hist = narrow.get("history") or []
    check(len(n_hist) <= len(hist), "缩小窗口后条数反而变多")
    check(all(h["signal_date"] >= boundary for h in n_hist),
          f"窗口过滤未生效（出现早于 {boundary} 的记录）")
    check((narrow.get("history_summary") or {}).get("window_days") == 7, "window_days 未回传")

    conn.close()
    print(f"\n{'=' * 46}\n{_pass} PASS / {_fail} FAIL"
          f"{'  (T 收益不匹配 %d 处)' % mism if mism else ''}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
