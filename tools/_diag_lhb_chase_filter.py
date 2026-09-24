"""回测：隔日动量线缺失「防追高过滤」的代价（OC 口径，与线上一致）。

问题：sync.py 隔日动量写入路径只做 passes_quality，未做 chase_filter /
     extension_filter / trend_gate。而 600641（2026-09-23）恰好三条全中：
       盘中触板打开 +9.28%（high=+10.0%）、近3日涨幅 +28.2%、偏离 MA20 +39.9%。
     引擎自身的 exclude_limit_up 只看收盘涨幅（<9.8% 即放过），拦不住。

本脚本量化：对「净买占比>=10% 且引擎自身过滤后」的龙虎榜样本，
按 ext / ret3 / 触板打开 分组，比较 T+1 OC 收益（次日开盘买入，次日收盘卖出）。

用法: python tools/_diag_lhb_chase_filter.py
"""
import os
import sqlite3

import numpy as np
import pandas as pd

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")

MIN_RATIO = 10.0      # NEXT_DAY_MOMENTUM.min_net_buy_ratio
LIMIT_THR = 9.8       # 主板涨停阈值
EXT_CAP = 0.12        # EXTENSION_FILTER.max_pct_above_ma20
RET3_CAP = 0.25       # CHASE_FILTER.max_ret_3d
TOUCH_THR = 9.5       # CHASE_FILTER.reject_limit_open 触板阈值

conn = sqlite3.connect(DB)

print("[1/3] 加载行情并计算 T 日截面特征（时点正确：只用 T 日及之前）...")
dp = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, pct_change "
    "FROM daily_price ORDER BY code, trade_date", conn)
dp["trade_date"] = dp["trade_date"].astype(str).str[:10]
g = dp.groupby("code", sort=False)["close"]
dp["ma20"] = g.transform(lambda s: s.rolling(20).mean())
dp["prev_close"] = g.transform(lambda s: s.shift(1))
dp["ret3"] = g.transform(lambda s: s / s.shift(3) - 1)
dp["ext"] = dp["close"] / dp["ma20"] - 1
# T+1 开盘买入 → T+1 收盘卖出（OC 口径，与 next_day_momentum 文档一致）
dp["nxt_open"] = dp.groupby("code", sort=False)["open"].transform(lambda s: s.shift(-1))
dp["nxt_close"] = g.transform(lambda s: s.shift(-1))
dp = dp.dropna(subset=["ma20"])

print("[2/3] 取龙虎榜样本并做引擎自身过滤（非涨停、非大跌）...")
lhb = pd.read_sql(
    "SELECT trade_date, code, name, pct_change AS lhb_pct, net_buy_ratio, reason "
    "FROM stock_lhb_detail WHERE net_buy_ratio >= ?", conn, params=(MIN_RATIO,))
lhb["trade_date"] = lhb["trade_date"].astype(str).str[:10]
print(f"      净买占比>={MIN_RATIO:.0f}% 原始样本: {len(lhb)}")

m = lhb.merge(dp[["code", "trade_date", "open", "high", "close", "prev_close",
                  "pct_change", "ma20", "ext", "ret3", "nxt_open", "nxt_close"]],
              on=["code", "trade_date"], how="inner")
m = m.dropna(subset=["nxt_open", "nxt_close"])
m["oc"] = m["nxt_close"] / m["nxt_open"] - 1                       # 次日开盘→次日收盘
m["prev_chg"] = m["prev_close"] / m["prev_close"].shift(0)         # 占位
# 引擎自身过滤：当日非涨停（收盘涨幅）
m = m[m["pct_change"] < LIMIT_THR]
m = m[m["pct_change"] > -7.0]                                       # max_down_pct
# 触板打开：盘中触及 >=+9.5% 但收盘未封住
m["touch_open"] = ((m["high"] / m["prev_close"] - 1) * 100 >= TOUCH_THR) & \
                  (m["pct_change"] < LIMIT_THR)
print(f"      与行情对齐并过引擎自身过滤后: {len(m)}")


def stat(df, label):
    if len(df) == 0:
        print(f"  {label:<28} n=0")
        return
    oc = df["oc"]
    print(f"  {label:<28} n={len(df):>5}  T+1OC均值 {oc.mean()*100:+6.2f}%  "
          f"胜率 {(oc > 0).mean()*100:5.1f}%  中位 {oc.median()*100:+6.2f}%  "
          f"P10 {oc.quantile(0.10)*100:+7.2f}%")


print("[3/3] 分组对比（基线 = 当前线上口径，即不过滤）\n")
print("── A. 按「偏离 MA20」分组 ──")
stat(m, "基线(全部)")
stat(m[m["ext"] < EXT_CAP], f"  ext < {EXT_CAP:.0%}（要保留）")
stat(m[m["ext"] >= EXT_CAP], f"  ext >= {EXT_CAP:.0%}（应否决）")

print("\n── B. 按「近3日涨幅」分组 ──")
stat(m[m["ret3"] < RET3_CAP], f"  ret3 < {RET3_CAP:.0%}（要保留）")
stat(m[m["ret3"] >= RET3_CAP], f"  ret3 >= {RET3_CAP:.0%}（应否决）")

print("\n── C. 按「盘中触板打开」分组 ──")
stat(m[~m["touch_open"]], "  非触板打开（要保留）")
stat(m[m["touch_open"]], "  触板打开（应否决）")

print("\n── D. 修复前后（三条过滤一起上）──")
bad = (m["ext"] >= EXT_CAP) | (m["ret3"] >= RET3_CAP) | m["touch_open"]
stat(m, "修复前（现线上）")
stat(m[~bad], "修复后（剔除三条命中）")
print(f"\n  被剔除样本占比: {bad.mean()*100:.1f}%（{bad.sum()}/{len(m)}）")

print("\n── E. 600641 是否会被拦住 ──")
t = m[m["code"] == "600641"].sort_values("trade_date").tail(8)
if len(t):
    for _, r in t.iterrows():
        flags = []
        if r["ext"] >= EXT_CAP:
            flags.append("ext超限")
        if r["ret3"] >= RET3_CAP:
            flags.append("ret3超限")
        if r["touch_open"]:
            flags.append("触板打开")
        print(f"  {r['trade_date']} ext={r['ext']*100:+6.1f}% ret3={r['ret3']*100:+6.1f}% "
              f"当日{r['pct_change']:+6.2f}% T+1OC={r['oc']*100:+6.2f}%  "
              f"→ {'拦截: ' + '/'.join(flags) if flags else '放行'}")
else:
    print("  （600641 无匹配样本）")

conn.close()
