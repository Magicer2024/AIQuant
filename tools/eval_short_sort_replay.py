# -*- coding: utf-8 -*-
"""eval_short_sort_replay.py —— 短线排序变体「真实复盘口径」重放对比（只读）

2026-08-22 用于推翻 eval_short_fusion_cap.py 的降序结论（真实执行窗
recon: desc -2.27% vs asc -0.29%），据此回退 short_ext_sort_desc=0。
与 eval_short_fusion_cap.py 的关键区别：
  - 候选源 = stock_signal 真实信号（非 .cache 候选缓存）
  - 出场 = _evaluate_short 逐条复刻（entry=次日开盘、信号自带 stop_loss、
    启动线 exec_entry*(1+short_take_profit)、trail、T+1、持满 10 天收盘了结）
    ——缓存评估用理想化 5% 止损，漏算信号自带止损口径下高扩展票的跳空大跌尾部
T1 regime 过滤在 Python 侧实现（cold/cool 日：全市场均涨跌<0 且 MA5 偏离<=-2%，
隔日动量豁免），参数与 short_t1_filter_sql 同源。
注意：gap guard 用 latest_price 当前快照，与 insert_new_outcomes 重建口径一致；
对远期历史日存在时代错位，绝对值仅供参考，变体间相对关系可信。
末段输出 recon 窗逐日核对（重放 vs DB recommend_outcome，按当前参数方向），
0 不一致即证明重放链路与生产入库完全同口径。
"""
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

from config.strategy_params import get_param
from core.market_regime import compute_regime_series

DB = r"core/quant.db"
RECON = "2026-07-20"
W6M = "2026-02-20"
TRAIN_END = "2025-12-31"
START = "2024-01-01"  # 信号评估起点（更早数据不参与）
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rng = conn.execute(
    "SELECT MIN(scan_date), MAX(scan_date), COUNT(DISTINCT scan_date) "
    "FROM stock_signal WHERE COALESCE(horizon,'short')='short'").fetchone()
print(f"stock_signal short 范围: {rng[0]} ~ {rng[1]} 共 {rng[2]} 个信号日", flush=True)

start = START
gate = float(get_param("short_conf_gate"))
launch_ratio = get_param("short_take_profit")
trail_pct = get_param("short_trailing_pct")
mkt_gate = float(get_param("short_down_market_gate"))
dev_max = float(get_param("short_dev_ma5_max"))
max_hold = 10

regime = compute_regime_series(conn, start)
active = {d for d, r in regime.items() if r in ("cold", "cool")}
print(f"regime 启用 T1 过滤的日期数: {len(active)}", flush=True)

# 启用日的市场均涨跌
mkt = {}
if active and mkt_gate < 99:
    ph = ",".join("?" * len(active))
    for r in conn.execute(
            f"SELECT trade_date, AVG(pct_change) m FROM daily_price "
            f"WHERE trade_date IN ({ph}) AND pct_change IS NOT NULL "
            f"GROUP BY trade_date", list(active)):
        mkt[r["trade_date"]] = r["m"]

rows = conn.execute("""
    SELECT s.code, s.scan_date, s.strategy, s.buy_price, s.stop_loss,
           s.take_profit, s.fusion_score, s.pct_above_ma20 AS ext,
           CASE WHEN lp.close IS NOT NULL AND lp.close > 0 AND s.buy_price > 0
                AND s.take_profit IS NOT NULL AND s.take_profit > s.buy_price
                AND (lp.close / s.buy_price - 1) > (s.take_profit / s.buy_price - 1)
           THEN 1 ELSE 0 END AS gap_sell
    FROM stock_signal s
    LEFT JOIN latest_price lp ON lp.code = s.code
    WHERE s.scan_date >= ?
      AND COALESCE(s.horizon,'short') = 'short'
      AND s.buy_price IS NOT NULL AND s.buy_price > 0
      AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
      AND COALESCE(s.strategy,'') NOT IN ('强势突破')
      AND s.code NOT LIKE '300%' AND s.code NOT LIKE '301%'
      AND s.code NOT LIKE '688%' AND s.code NOT LIKE '689%'
""", (start,)).fetchall()
print(f"基础候选行: {len(rows)}", flush=True)

# MA5 偏离缓存（仅启用日需要）
dev5 = {}
if active and dev_max < 99:
    need = {r["code"] for r in rows if r["scan_date"] in active
            and r["strategy"] != "隔日动量"}
    for code in need:
        px = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE code=? "
            "ORDER BY trade_date", (code,)).fetchall()
        if len(px) < 10:
            continue
        closes = pd.Series([p["close"] for p in px], dtype=float)
        dv = (closes / closes.rolling(5).mean() - 1) * 100
        dev5[code] = {p["trade_date"]: (None if pd.isna(dv.iloc[i]) else dv.iloc[i])
                      for i, p in enumerate(px)}

per_day = defaultdict(list)
for r in rows:
    d = r["scan_date"]
    # gap guard：与 insert_new_outcomes 同式（latest_price 当前快照，
    # 与今日重建的 DB 同口径；对远期历史日存在时代错位，仅近期窗可信）
    if r["gap_sell"]:
        continue
    if r["strategy"] != "隔日动量" and d in active:
        if mkt_gate < 99 and (mkt.get(d) or 0) >= mkt_gate:
            continue
        if dev_max < 99:
            v = dev5.get(r["code"], {}).get(d)
            if v is None or v > dev_max:
                continue
    if r["fusion_score"] is None or r["fusion_score"] < gate:
        continue
    per_day[d].append(dict(r))
print(f"过滤后出票候选日数: {len(per_day)}", flush=True)


def evaluate(pick) -> float | None:
    """复刻 _evaluate_short 出场模拟，返回 exit_return(%) 或 None(未出场)。"""
    prices = conn.execute(
        "SELECT trade_date, open, close, high, low FROM daily_price "
        "WHERE code=? AND trade_date>? ORDER BY trade_date ASC LIMIT 15",
        (pick["code"], pick["scan_date"])).fetchall()
    if not prices:
        return None
    first = prices[0]
    entry = pick["buy_price"]
    exec_entry = float(first["open"]) if first["open"] else (
        float(first["close"]) or entry)
    stop = pick["stop_loss"]
    launch = None
    if launch_ratio and 0 < launch_ratio < 1.0 and exec_entry > 0:
        launch = exec_entry * (1 + launch_ratio)
    elif pick["take_profit"] and pick["take_profit"] > 0:
        launch = pick["take_profit"]
    if (stop is None or stop <= 0) and exec_entry > 0:
        stop = exec_entry * (1 + float(get_param("short_stop_loss")))
    highest = tline = None
    launched = False
    trail_ok = trail_pct is not None and 0.01 <= trail_pct < 1.0
    for j, p in enumerate(prices[:max_hold], start=1):
        close = p["close"]
        if not close or close <= 0:
            return None
        high = p["high"] or close
        if highest is None or high > highest:
            highest = high
        if not launched and launch and highest >= launch:
            launched = True
        if launched and trail_ok:
            line = highest * (1 - trail_pct)
            if tline is None or line > tline:
                tline = line
        if j == 1:
            continue
        if stop and close <= stop:
            return round((close - exec_entry) / exec_entry * 100, 2)
        if launched and tline is not None and close <= tline:
            return round((close - exec_entry) / exec_entry * 100, 2)
    if len(prices) >= max_hold:
        c = prices[max_hold - 1]["close"]
        return round((c - exec_entry) / exec_entry * 100, 2)
    return None


def pick_day(d, mode):
    cands = per_day.get(d, [])
    if mode == "asc05":
        cands = [c for c in cands
                 if c["strategy"] == "隔日动量" or (c["ext"] or 0) >= 0.005]
    if mode == "desc":
        key = lambda c: (0 if c["strategy"] == "隔日动量" else 1,
                         -(c["ext"] or 0), -(c["fusion_score"] or 0))
    elif mode == "regime_desc" and regime.get(d) not in ("cold", "cool"):
        key = lambda c: (0 if c["strategy"] == "隔日动量" else 1,
                         -(c["ext"] or 0), -(c["fusion_score"] or 0))
    else:  # asc / regime_desc 冷市日
        key = lambda c: (0 if c["strategy"] == "隔日动量" else 1,
                         (c["ext"] or 0), -(c["fusion_score"] or 0))
    return sorted(cands, key=key)[:3]


for mode in ("asc", "asc05", "desc", "regime_desc"):
    win_stats = {w: [0, 0.0, 0] for w in ("train", "test", "w6m", "recon")}
    for d in sorted(per_day):
        for c in pick_day(d, mode):
            ret = evaluate(c)
            if ret is None:
                continue
            wks = ["train" if d <= TRAIN_END else "test"]
            if d >= W6M:
                wks.append("w6m")
            if d >= RECON:
                wks.append("recon")
            for w in wks:
                s = win_stats[w]
                s[0] += 1
                s[1] += ret
                s[2] += 1 if ret > 0 else 0
    print(f"[{mode}]", flush=True)
    for w in ("train", "test", "w6m", "recon"):
        n, tot, wins = win_stats[w]
        if n:
            print(f"  {w:<6} n={n:<4} 均值={tot/n:+.2f}% 胜率={wins/n*100:.1f}%",
                  flush=True)
        else:
            print(f"  {w:<6} n=0", flush=True)
    print(flush=True)

# recon 窗核对：重放选股 vs DB recommend_outcome（按当前 short_ext_sort_desc 方向）
cur_mode = "desc" if int(get_param("short_ext_sort_desc")) else "asc"
print("=" * 60, flush=True)
print(f"recon 窗逐日核对（重放[{cur_mode}] vs DB）：", flush=True)
db_days = [r[0] for r in conn.execute(
    "SELECT DISTINCT scan_date FROM recommend_outcome "
    "WHERE COALESCE(horizon,'short')='short' AND scan_date >= ? "
    "ORDER BY scan_date", (RECON,))]
n_diff = 0
for d in db_days:
    db_codes = {r[0] for r in conn.execute(
        "SELECT code FROM recommend_outcome "
        "WHERE COALESCE(horizon,'short')='short' AND scan_date=?", (d,))}
    rp_codes = {c["code"] for c in pick_day(d, cur_mode)}
    mark = "" if db_codes == rp_codes else "  <-- 不一致"
    if db_codes != rp_codes:
        n_diff += 1
    print(f"  {d} DB={sorted(db_codes)} 重放={sorted(rp_codes)}{mark}", flush=True)
print(f"不一致天数: {n_diff}/{len(db_days)}", flush=True)
