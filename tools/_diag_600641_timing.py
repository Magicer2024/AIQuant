"""临时诊断：600641 的短线信号时点轨迹，定位「追高」根因。

用法: python tools/_diag_600641_timing.py
"""
import os
import sqlite3

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")
CODE = "600641"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def show(title, sql, params=()):
    print("=" * 78)
    print(title)
    print("=" * 78)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("(无数据)")
        return rows
    keys = rows[0].keys()
    print(" | ".join(keys))
    for r in rows:
        print(" | ".join("" if r[k] is None else str(r[k]) for k in keys))
    return rows


# 1) 近期 stock_signal 记录（看是哪条信号线、什么分、什么策略）
show("① stock_signal 近 40 条（600641）", """
    SELECT scan_date, COALESCE(horizon,'short') AS hz, strategy,
           ROUND(fusion_score,2) fs, ROUND(bottom_score,2) btm,
           ROUND(vol_score,2) vol, ROUND(ma_score,2) ma,
           ROUND(diverge_score,2) div, ROUND(whale_score,2) wh,
           ROUND(price,2) price, ROUND(buy_price,2) buy,
           ROUND(pct_above_ma20,4) ext, ROUND(chip_conc,3) conc,
           COALESCE(trigger_list,'') trig
    FROM stock_signal WHERE code = ?
    ORDER BY scan_date DESC LIMIT 40
""", (CODE,))

# 2) 近 30 个交易日行情 + 分数轨迹（含 MA20 偏离自算）
show("② daily_price 近 30 日（600641）", """
    SELECT trade_date, ROUND(open,2) o, ROUND(high,2) hi, ROUND(low,2) lo,
           ROUND(close,2) c, ROUND(pct_change,2) chg,
           ROUND(vol_ratio,3) vr, ROUND(fusion_score,2) fs,
           ROUND(bottom_score,2) btm, ROUND(pct_above_ma20,4) ext
    FROM daily_price WHERE code = ?
    ORDER BY trade_date DESC LIMIT 30
""", (CODE,))

# 3) 自算 MA5/MA10/MA20 与偏离，用于确认「反转第一天」
rows = conn.execute("""
    SELECT trade_date, close, volume FROM daily_price
    WHERE code = ? ORDER BY trade_date
""", (CODE,)).fetchall()
closes = [(r["trade_date"], float(r["close"]), float(r["volume"] or 0)) for r in rows]
print("=" * 78)
print("③ 自算均线/偏离/相对10日低点反弹（近 25 日）")
print("=" * 78)
print("date | close | MA5 | MA10 | MA20 | ext_ma20% | ext_ma5% | 相对10日低反弹% | ma20斜率5d")
for i in range(max(0, len(closes) - 25), len(closes)):
    d, c, v = closes[i]
    if i < 20:
        continue
    ma5 = sum(x[1] for x in closes[i - 4:i + 1]) / 5
    ma10 = sum(x[1] for x in closes[i - 9:i + 1]) / 10
    ma20 = sum(x[1] for x in closes[i - 19:i + 1]) / 20
    ma20_5ago = sum(x[1] for x in closes[i - 24:i - 4]) / 20 if i >= 24 else ma20
    low10 = min(x[1] for x in closes[i - 9:i + 1])
    print(f"{d} | {c:.2f} | {ma5:.2f} | {ma10:.2f} | {ma20:.2f} | "
          f"{(c/ma20-1)*100:+.2f} | {(c/ma5-1)*100:+.2f} | "
          f"{(c/low10-1)*100:+.2f} | {'up' if ma20>=ma20_5ago else 'dn'}")

# 4) 09-23 当天 short 组完整候选（复刻今日推荐 WHERE，看 600641 的排名与阈值）
print("=" * 78)
print("④ 2026-09-23 短线候选池（复刻今日推荐过滤前）")
print("=" * 78)
try:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config.strategy_params import get_param
    for k in ("short_conf_gate", "short_observe_bottom",
              "short_observe_bottom_min_ext", "short_ext_sort_desc",
              "short_chip_sort_prioritize", "short_pullback_entry",
              "short_max_hold_days", "short_down_market_gate",
              "short_dev_ma5_max"):
        try:
            print(f"  {k} = {get_param(k)}")
        except Exception as e:
            print(f"  {k} = <err {e}>")
except Exception as e:
    print("  [WARN] 参数读取失败:", e)

conn.close()
