"""回测 v6（决定性实验）：T+1 开盘跳空幅度 vs 当日 OC 收益。

问题：用户担心「明天买入挂在高位」。执行层防线 T1_GAP_GUARD 已由 2.5% 放宽到
     止盈线(≈+6.5%~8%)，理由是「隔夜跳空是唯一正 edge」。该理由出自短线融合线的
     diag，是否适用于隔日动量线？直接检验：按 T+1 开盘跳空分组看 OC 收益。

若「高开组 OC 显著更差」→ 应给该线设跳空上限；
若「高开组 OC 更好」→ 高开是强势确认，不该拦。

用法: python tools/_diag_lhb_gap.py
"""
import os
import sqlite3

import pandas as pd

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")
SINCE = "2024-07-01"

conn = sqlite3.connect(DB)
dp = pd.read_sql(
    "SELECT p.code, p.trade_date, p.open, p.high, p.close, i.name "
    "FROM daily_price p LEFT JOIN stock_info i ON i.code = p.code "
    "WHERE p.trade_date >= ? ORDER BY p.code, p.trade_date", conn, params=[SINCE])
dp = dp[~dp["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
g = dp.groupby("code", sort=False)["close"]
dp["prev_close"] = g.transform(lambda s: s.shift(1))
dp["ma20"] = g.transform(lambda s: s.rolling(20).mean())
dp["ext"] = dp["close"] / dp["ma20"] - 1
upflag = (dp["close"] > dp["prev_close"]).astype(int)
_gk = (upflag != upflag.shift()).groupby(dp["code"]).cumsum()
dp["consec_up"] = upflag.groupby([dp["code"], _gk]).cumsum()
dp["nxt_open"] = dp.groupby("code", sort=False)["open"].transform(lambda s: s.shift(-1))
dp["nxt_close"] = g.transform(lambda s: s.shift(-1))

lhb = pd.read_sql(
    "SELECT trade_date, code, name, pct_change, net_buy_ratio, reason, perf_1d "
    "FROM stock_lhb_detail WHERE trade_date >= ? AND net_buy_ratio >= 10",
    conn, params=[SINCE])
lhb = lhb[~lhb["name"].fillna("").str.upper().str.contains("ST|退", regex=True)].copy()
is20 = lhb["code"].str.startswith(("300", "301", "688", "689"))
lhb["limit_thr"] = is20.map({True: 19.8, False: 9.8})
lhb = lhb[(lhb["pct_change"] < lhb["limit_thr"]) & (lhb["pct_change"] > -7.0)]
lhb = lhb[~lhb["pct_change"].between(4.6, 5.4)]

m = lhb.merge(dp[["code", "trade_date", "open", "high", "close", "prev_close",
                  "ma20", "ext", "consec_up", "nxt_open", "nxt_close"]],
              on=["code", "trade_date"], how="left")
m["nd_cc"] = m["perf_1d"]
m["nd_oc"] = (m["nxt_close"] / m["nxt_open"] - 1) * 100
m = m[~((m["nd_oc"].abs() < 0.01) & (m["nd_cc"].abs() >= 4.5))]     # 剔一字板
m["gap"] = (m["nxt_open"] / m["close"] - 1) * 100                    # T+1 开盘跳空 %
m["year"] = m["trade_date"].str[:4]
v = m.dropna(subset=["nd_oc", "gap"]).copy()
print(f"样本 {len(v)} 条（OC 与 gap 均有效）\n")

print("═══ A. 按 T+1 开盘跳空分档 ═══")
bins = [-99, -2, 0, 2, 4, 6, 99]
labs = ["<-2%(低开)", "-2~0%", "0~+2%", "+2~+4%", "+4~+6%", ">+6%(大幅高开)"]
v["gapb"] = pd.cut(v["gap"], bins, labels=labs)
for lab, s in v.groupby("gapb", observed=True):
    d = s["nd_oc"]
    print(f"  {lab:<14} n={len(d):>3}  OC均值 {d.mean():+6.2f}%  胜率 {(d > 0).mean()*100:5.1f}%  "
          f"P10 {d.quantile(.1):+7.2f}%  CC均值 {s['nd_cc'].mean():+6.2f}%")

print("\n═══ B. 关键判定：跳空 >2.5%（旧阈值）/ >6.5%（新阈值）是否该拦 ═══")
for thr in (2.5, 6.5):
    a, b = v[v["gap"] <= thr], v[v["gap"] > thr]
    print(f"  跳空阈值 {thr}%：")
    for lab, s in [("  保留(<=)", a), ("  拦截(>)", b)]:
        d = s["nd_oc"]
        if len(d):
            print(f"    {lab:<10} n={len(d):>3}  OC均值 {d.mean():+6.2f}%  "
                  f"胜率 {(d > 0).mean()*100:5.1f}%  对组合的影响: 剔掉后均值 "
                  f"{a['nd_oc'].mean():+.2f}% (Δ{a['nd_oc'].mean()-v['nd_oc'].mean():+.2f}pp)")

print("\n═══ C. 分年复核（阈值 2.5% 是否稳健）═══")
for y, s in v.groupby("year"):
    a, b = s[s["gap"] <= 2.5], s[s["gap"] > 2.5]
    if len(b) >= 5:
        print(f"  [{y}] 跳空<=2.5% n={len(a)} 均值{a['nd_oc'].mean():+5.2f}% | "
              f">2.5% n={len(b)} 均值{b['nd_oc'].mean():+5.2f}% "
              f"(Δ{a['nd_oc'].mean()-b['nd_oc'].mean():+.2f}pp)")

print("\n═══ D. 600641 执行推演（09-23 收盘 47.80，止盈 +6.5% = 50.91）═══")
entry, tp = 47.80, round(47.80 * 1.065, 2)
print(f"  buy_price={entry}  take_profit={tp}")
print(f"  {'次日开盘':<10}{'跳空%':>8}{'判定(旧2.5%)':>16}{'判定(新止盈线)':>18}")
for o in (46.0, 47.8, 48.9, 49.6, 50.5, 51.5):
    gap = (o / entry - 1) * 100
    print(f"  {o:<10.2f}{gap:>+8.2f}{'wait/暂缓' if gap > 2.5 else 'buy':>16}"
          f"{'sell/不追' if gap > (tp / entry - 1) * 100 else 'buy':>18}")

conn.close()
