"""回测 v5（最终清理版）：剔除 ST 股 5% 一字板污染后复核。

v4 发现：D 桶(ext>=25% 且连涨>=5) 26 条里有 12 条 nd_cc≈+5% 且 nd_oc=0.00
        —— 是 5% 涨跌幅限制股（ST/风险警示）的一字涨停，次日开盘买不进，
        却让 CC 口径胜率虚高到 79%。dp.name 来自 stock_info 当前名称，
        摘帽后过滤失效，故须按「当日涨幅≈5%」这一 ST 特征剔除。

用法: python tools/_diag_lhb_v5.py
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
dp["ret3"] = g.transform(lambda s: s / s.shift(3) - 1)
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
n0 = len(lhb)
lhb = lhb[(lhb["pct_change"] < lhb["limit_thr"]) & (lhb["pct_change"] > -7.0)]
# ★ 剔除 5% 涨跌幅限制股（ST/风险警示）：当日涨幅落在 4.6~5.4 的样本
st_like = lhb["pct_change"].between(4.6, 5.4) & ~is20.reindex(lhb.index).fillna(False)
print(f"线上信号池 {n0} 条 → 剔除 |涨幅|>7% 及涨停后 {len(lhb)} 条；"
      f"其中疑似 5% 限制股 {st_like.sum()} 条")
lhb = lhb[~st_like]

m = lhb.merge(dp[["code", "trade_date", "open", "high", "close", "prev_close",
                  "ma20", "ext", "ret3", "consec_up", "nxt_open", "nxt_close"]],
              on=["code", "trade_date"], how="left")
m["nd_cc"] = m["perf_1d"]
m["nd_oc"] = (m["nxt_close"] / m["nxt_open"] - 1) * 100
# 剔除次日一字板（买不进）：T+1 open==close 且涨幅大
oneboard = (m["nd_oc"].abs() < 0.01) & (m["nd_cc"].abs() >= 4.5)
print(f"剔除次日一字板（不可成交）{oneboard.sum()} 条")
m = m[~oneboard]
m["year"] = m["trade_date"].str[:4]


def st(df, label):
    d = df["nd_oc"].dropna()
    if len(d) == 0:
        print(f"    {label:<34} n=0")
        return
    print(f"    {label:<34} n={len(d):>4}  均值 {d.mean():+6.2f}%  "
          f"胜率 {(d > 0).mean()*100:5.1f}%  P10 {d.quantile(.1):+7.2f}%  "
          f"最差 {d.min():+7.1f}%  最好 {d.max():+7.1f}%")


print(f"\n最终样本 {len(m)} 条（OC 有效 {m['nd_oc'].notna().sum()}）")
print("\n═══ 基线 ═══")
st(m, "全期")
for y, s in m.groupby("year"):
    st(s, f"  {y}")

print("\n═══ A. 上榜原因：「累计涨幅偏离」型（= 官方标注已连涨）═══")
tag = m["reason"].fillna("").str.contains("涨幅偏离值累计")
for y, s in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    st(s[~s["reason"].fillna("").str.contains("涨幅偏离值累计")], "  非累计偏离型")
    st(s[s["reason"].fillna("").str.contains("涨幅偏离值累计")], "  累计偏离型(已连涨) ★用户场景")

print("\n═══ B. 连涨天数（剔除 ST 污染后重测）═══")
for lab, mask in [("0(首阳)", m["consec_up"] == 0), ("1-2", m["consec_up"].between(1, 2)),
                  ("3-4", m["consec_up"].between(3, 4)), (">=5", m["consec_up"] >= 5)]:
    st(m[mask], f"    连涨 {lab}")

print("\n═══ C. 600641 形态：ext>=25% 且 连涨>=5 ═══")
hot = (m["ext"] >= .25) & (m["consec_up"] >= 5)
st(m[hot], "  命中（强烈追高形态）")
st(m[~hot], "  未命中")
print("\n═══ D. 逐条明细（命中样本，已剔除一字板）═══")
t = m[hot].sort_values("trade_date")
print(t[["trade_date", "code", "pct_change", "consec_up", "ext", "nd_oc",
         "nd_cc", "reason"]].to_string(index=False, max_colwidth=26))

conn.close()
