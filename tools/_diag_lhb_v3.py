"""回测 v3（口径修正）：隔日动量线防追高过滤评估。

v2 缺陷：用 daily_price.pct_change 判断「非涨停」，与线上引擎口径不符
        （next_day_momentum.py 第71行用的是 lhb_row["pct_change"]）。
        导致样本 601 → 2348 条，混入真涨停票，触板打开占比虚高到 92%。
v3 修正：涨停/大跌判定一律用 stock_lhb_detail 自带的 pct_change（= 线上口径）。

样本：2024-07-01 起、net_buy_ratio>=10、lhb.pct_change < 涨停阈值、> -7%
      （= 线上 scan_next_day_momentum 的全部条件）
出口：CC = perf_1d（表内标签，样本全）；OC = 自算 T+1 open→close（可交易口径）
用法: python tools/_diag_lhb_v3.py
"""
import os
import sqlite3

import pandas as pd

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")
SINCE = "2024-07-01"
EXT_CAP = 0.12
RET3_CAP = 0.25

conn = sqlite3.connect(DB)

dp = pd.read_sql(
    "SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close, p.pct_change AS px_chg, "
    "i.name FROM daily_price p LEFT JOIN stock_info i ON i.code = p.code "
    "WHERE p.trade_date >= ? ORDER BY p.code, p.trade_date", conn, params=[SINCE])
dp = dp[~dp["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
g = dp.groupby("code", sort=False)["close"]
dp["prev_close"] = g.transform(lambda s: s.shift(1))
dp["ma20"] = g.transform(lambda s: s.rolling(20).mean())
dp["ext"] = dp["close"] / dp["ma20"] - 1
dp["ret3"] = g.transform(lambda s: s / s.shift(3) - 1
                         if False else s / s.shift(3) - 1)
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

# ── 线上 scan_next_day_momentum 的全部硬条件 ──
is20 = lhb["code"].str.startswith(("300", "301", "688", "689"))
lhb["limit_thr"] = is20.map({True: 19.8, False: 9.8})
n0 = len(lhb)
lhb = lhb[lhb["pct_change"] < lhb["limit_thr"]]
n1 = len(lhb)
lhb = lhb[lhb["pct_change"] > -7.0]
n2 = len(lhb)
print(f"net_buy_ratio>=10: {n0} → 非涨停: {n1} → 非大跌: {n2} ← 线上信号池")

m = lhb.merge(dp[["code", "trade_date", "open", "high", "close", "prev_close",
                  "ma20", "ext", "ret3", "consec_up", "nxt_open", "nxt_close"]],
              on=["code", "trade_date"], how="left")
m["nd_cc"] = m["perf_1d"]
m["nd_oc"] = (m["nxt_close"] / m["nxt_open"] - 1) * 100
m["touch_open"] = (((m["high"] / m["prev_close"] - 1) * 100 >= 9.5) &
                   (m["pct_change"] < m["limit_thr"]))
m["year"] = m["trade_date"].str[:4]


def st(df, label, key="nd_oc"):
    d = df[key].dropna()
    if len(d) == 0:
        print(f"    {label:<24} n=0")
        return
    print(f"    {label:<24} n={len(d):>4}  均值 {d.mean():+6.2f}%  "
          f"胜率 {(d > 0).mean()*100:5.1f}%  P10 {d.quantile(.1):+7.2f}%  "
          f"P90 {d.quantile(.9):+6.2f}%")


print("\n═══ 基线（线上口径 T+1 open→close, OC）═══")
st(m, "全期")
for y, s in m.groupby("year"):
    st(s, y)
print("  分月（每月 OC 均值）：")
m["ym"] = m["trade_date"].str[:7]
mo = m.groupby("ym")["nd_oc"].agg(["count", "mean"])
mo = mo[mo["count"] >= 6]
print("    " + "  ".join(f"{i}:{r['mean']:+.1f}({int(r['count'])})"
                         for i, r in mo.iterrows()))

print("\n═══ A. 连续上涨天数（核心维度，用户指出）═══")
m["upb"] = pd.cut(m["consec_up"], [-1, 0, 2, 4, 1000],
                  labels=["0(首阳)", "1-2天", "3-4天", ">=5天"])
for y, s in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    for lab, s2 in s.groupby("upb", observed=True):
        st(s2, f"连涨 {lab}")

print("\n═══ B. 偏离 MA20 ═══")
for y, s in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    st(s[s["ext"] < EXT_CAP], f"ext<{EXT_CAP:.0%}")
    st(s[s["ext"] >= EXT_CAP], f"ext>={EXT_CAP:.0%}")

print("\n═══ C. 近3日涨幅 ═══")
for y, s in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    st(s[s["ret3"] < RET3_CAP], f"ret3<{RET3_CAP:.0%}")
    st(s[s["ret3"] >= RET3_CAP], f"ret3>={RET3_CAP:.0%}")

print("\n═══ D. 触板打开 ═══")
for y, s in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    st(s[~s["touch_open"]], "非触板打开")
    st(s[s["touch_open"]], "触板打开")

print("\n═══ E. 单条过滤边际（全期，OC）═══")
base = m["nd_oc"].mean()
for name, bad in [(f"ext>={EXT_CAP:.0%}", m["ext"] >= EXT_CAP),
                  (f"ret3>={RET3_CAP:.0%}", m["ret3"] >= RET3_CAP),
                  ("连涨>=5天", m["consec_up"] >= 5),
                  ("连涨>=3天", m["consec_up"] >= 3),
                  ("触板打开", m["touch_open"])]:
    keep = m[~bad]["nd_oc"].dropna()
    print(f"  剔除[{name:<12}] {bad.sum():>4}条({bad.mean()*100:4.1f}%) → "
          f"n={len(keep):>4} 均值 {keep.mean():+6.2f}% (基线{base:+.2f}% "
          f"Δ{keep.mean()-base:+.2f}pp) 胜率 {(keep > 0).mean()*100:.1f}%")

print("\n═══ F. 600641 ═══")
t = m[m["code"] == "600641"]
if len(t):
    print(t[["trade_date", "pct_change", "consec_up", "ext", "ret3", "nd_cc", "nd_oc"]]
          .to_string(index=False))
else:
    print("  （600641 在样本内无命中：09-23 净买 10.54% 但 perf_1d 未回填/无 T+1）")
    l = pd.read_sql("SELECT trade_date,pct_change,net_buy_ratio,perf_1d FROM "
                    "stock_lhb_detail WHERE code='600641' ORDER BY trade_date", conn)
    print(l.to_string(index=False))

conn.close()
