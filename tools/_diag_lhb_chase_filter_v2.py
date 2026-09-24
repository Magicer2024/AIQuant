"""回测 v2：隔日动量线缺失防追高过滤的代价 —— 分年 + 分项 + 连涨天数。

修正/增强 v1：
  1) 分年统计（铁律3：单年剔除也要成立）
  2) 三条过滤**分别**评估（v1 叠加后 n=95 过薄，且三者高度共线）
  3) 新增「连续上涨天数」维度（用户直接指出的"已经上涨4天"）
  4) perf_1d 交叉验证自算 OC
  5) 20cm 板块用 19.8 涨停阈值

用法: python tools/_diag_lhb_chase_filter_v2.py
"""
import os
import sqlite3

import pandas as pd

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")

MIN_RATIO = 10.0
EXT_CAP = 0.12
RET3_CAP = 0.25
TOUCH_THR = 9.5

conn = sqlite3.connect(DB)

dp = pd.read_sql("SELECT code, trade_date, open, high, low, close, pct_change "
                 "FROM daily_price ORDER BY code, trade_date", conn)
dp["trade_date"] = dp["trade_date"].astype(str).str[:10]
g = dp.groupby("code", sort=False)["close"]
dp["ma20"] = g.transform(lambda s: s.rolling(20).mean())
dp["prev_close"] = g.transform(lambda s: s.shift(1))
dp["ret3"] = g.transform(lambda s: s / s.shift(3) - 1)
dp["ext"] = dp["close"] / dp["ma20"] - 1
# 连续上涨天数（收盘连涨 run length，全向量化）
upflag = (dp["close"] > dp["prev_close"]).astype(int)
_grpkey = (upflag != upflag.shift()).groupby(dp["code"]).cumsum()
dp["consec_up"] = upflag.groupby([dp["code"], _grpkey]).cumsum()
dp["nxt_open"] = dp.groupby("code", sort=False)["open"].transform(lambda s: s.shift(-1))
dp["nxt_close"] = g.transform(lambda s: s.shift(-1))
dp["nxt_date"] = dp.groupby("code", sort=False)["trade_date"].transform(lambda s: s.shift(-1))
dp = dp.dropna(subset=["ma20"])

lhb = pd.read_sql("SELECT trade_date, code, name, pct_change AS lhb_pct, "
                  "net_buy_ratio, perf_1d FROM stock_lhb_detail "
                  "WHERE net_buy_ratio >= ?", conn, params=(MIN_RATIO,))
lhb["trade_date"] = lhb["trade_date"].astype(str).str[:10]

m = lhb.merge(dp, on=["code", "trade_date"], how="inner")
m = m.dropna(subset=["nxt_open", "nxt_close"])
m["oc"] = m["nxt_close"] / m["nxt_open"] - 1
m["cc"] = m["nxt_close"] / m["close"] - 1
m["year"] = m["trade_date"].str[:4]

# 引擎自身过滤：非涨停（区分 20cm）+ 非大跌
m["is20"] = m["code"].str.startswith(("300", "301", "688", "689"))
_limit = m["is20"].map({True: 19.8, False: 9.8})
m = m[m["pct_change"] < _limit]
m = m[m["pct_change"] > -7.0]

_limit = m["is20"].map({True: 19.8, False: 9.8})
m["touch_open"] = (((m["high"] / m["prev_close"] - 1) * 100 >= TOUCH_THR) &
                   (m["pct_change"] < _limit))

print(f"样本: {len(m)} 条（净买占比>={MIN_RATIO:.0f}% 且非涨停/非大跌）")
print(f"日期范围: {m['trade_date'].min()} ~ {m['trade_date'].max()}")
print(f"自算OC vs 表内perf_1d 均值差: {(m['oc']*100 - m['perf_1d']).mean():+.2f}pp "
      f"(perf_1d 非空 {(m['perf_1d'].notna()).sum()} 条) — perf_1d=次日CC口径\n")


def stat(df, label, key="oc"):
    if len(df) == 0:
        print(f"  {label:<26} n=0")
        return
    r = df[key]
    print(f"  {label:<26} n={len(df):>5}  均值 {r.mean()*100:+6.2f}%  "
          f"胜率 {(r > 0).mean()*100:5.1f}%  P10 {r.quantile(.10)*100:+7.2f}%  "
          f"P90 {r.quantile(.90)*100:+6.2f}%")


print("═══ 全期基线（T+1 开盘买入→次日收盘，OC 口径）═══")
stat(m, "基线(全部)")
print()
print("═══ 分年基线 ═══")
for y, sub in m.groupby("year"):
    stat(sub, f"{y}")

print("\n═══ A. 偏离 MA20 (EXTENSION_FILTER) ═══")
for y, sub in [("全期", m)] + list(m.groupby("year")):
    a, b = sub[sub["ext"] < EXT_CAP], sub[sub["ext"] >= EXT_CAP]
    print(f"  [{y}]")
    stat(a, f"   ext < {EXT_CAP:.0%}", "oc")
    stat(b, f"   ext >= {EXT_CAP:.0%}", "oc")

print("\n═══ B. 近3日涨幅 (CHASE_FILTER.max_ret_3d) ═══")
for y, sub in [("全期", m)] + list(m.groupby("year")):
    a, b = sub[sub["ret3"] < RET3_CAP], sub[sub["ret3"] >= RET3_CAP]
    print(f"  [{y}]")
    stat(a, f"   ret3 < {RET3_CAP:.0%}", "oc")
    stat(b, f"   ret3 >= {RET3_CAP:.0%}", "oc")

print("\n═══ C. 连续上涨天数（用户指出的核心维度）═══")
m["up_bucket"] = pd.cut(m["consec_up"], [-1, 0, 2, 4, 100],
                        labels=["0天(首阳)", "1-2天", "3-4天", ">=5天"])
for y, sub in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    for lab, sub2 in sub.groupby("up_bucket", observed=True):
        stat(sub2, f"   连涨 {lab}", "oc")

print("\n═══ D. 盘中触板打开 (CHASE_FILTER.reject_limit_open) ═══")
for y, sub in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    stat(sub[~sub["touch_open"]], "   非触板打开", "oc")
    stat(sub[sub["touch_open"]], "   触板打开", "oc")

print("\n═══ E. 单条过滤的边际效果（全期）═══")
base = m["oc"].mean() * 100
for name, bad in [
    (f"ext >= {EXT_CAP:.0%}", m["ext"] >= EXT_CAP),
    (f"ret3 >= {RET3_CAP:.0%}", m["ret3"] >= RET3_CAP),
    ("连涨 >= 3 天", m["consec_up"] >= 3),
    ("触板打开", m["touch_open"]),
]:
    keep = m[~bad]["oc"]
    print(f"  剔除 [{name:<14}] 剔除 {bad.sum():>4} 条({bad.mean()*100:4.1f}%) → "
          f"留存 n={len(keep):>4} 均值 {keep.mean()*100:+6.2f}% "
          f"(基线 {base:+.2f}%, Δ {keep.mean()*100-base:+.2f}pp) 胜率 {(keep>0).mean()*100:.1f}%")

print("\n═══ F. 600641 逐条明细 ═══")
t = m[m["code"] == "600641"].sort_values("trade_date")
if len(t):
    print(f"  {'日期':<12}{'当日%':>7}{'连涨':>5}{'ext%':>8}{'ret3%':>8}{'T+1OC%':>9}  判定")
    for _, r in t.iterrows():
        flags = []
        if r["ext"] >= EXT_CAP:
            flags.append("ext")
        if r["ret3"] >= RET3_CAP:
            flags.append("ret3")
        if r["touch_open"]:
            flags.append("触板")
        if r["consec_up"] >= 3:
            flags.append("连涨")
        print(f"  {r['trade_date']:<12}{r['pct_change']:>7.2f}{int(r['consec_up']):>5}"
              f"{r['ext']*100:>8.1f}{r['ret3']*100:>8.1f}{r['oc']*100:>9.2f}  "
              f"{'拦截(' + '+'.join(flags) + ')' if flags else '放行'}")
else:
    print("  （无样本，600641 仅 09-23 一条 net_buy_ratio>=10 且被对齐丢弃？）")

conn.close()
