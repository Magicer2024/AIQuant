"""回测 v4：隔日动量线按「上榜原因 / 极端高位」分层。

v3 已证：ext / ret3 / 触板打开 三条过滤对该线无效或有害（口径修正后）。
v4 检验用户直觉的最贴近代理 —— 龙虎榜 reason 文本自带"已涨"信息：
    "连续三个交易日内收盘价格涨幅偏离值累计达到20%" = 官方标注的"已连涨"。
以及极端高位桶（ext>25% / 连涨>=5）的表现与样本量。

用法: python tools/_diag_lhb_v4.py
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
lhb = lhb[(lhb["pct_change"] < lhb["limit_thr"]) & (lhb["pct_change"] > -7.0)]

m = lhb.merge(dp[["code", "trade_date", "open", "high", "close", "prev_close",
                  "ma20", "ext", "ret3", "consec_up", "nxt_open", "nxt_close"]],
              on=["code", "trade_date"], how="left")
m["nd_cc"] = m["perf_1d"]
m["nd_oc"] = (m["nxt_close"] / m["nxt_open"] - 1) * 100
m["year"] = m["trade_date"].str[:4]
m["reason_s"] = m["reason"].fillna("").str.strip()


def st(df, label):
    d = df["nd_oc"].dropna()
    if len(d) == 0:
        print(f"    {label:<38} n=0")
        return
    print(f"    {label:<38} n={len(d):>4}  均值 {d.mean():+6.2f}%  "
          f"胜率 {(d > 0).mean()*100:5.1f}%  P10 {d.quantile(.1):+7.2f}%  "
          f"最差 {d.min():+7.1f}%")


print(f"样本总量 {len(m)}（线上口径）")
print("\n═══ A. 按上榜原因分组（全期 OC）═══")
grp = m.groupby("reason_s")
for r, sub in sorted(grp, key=lambda x: -len(x[1])):
    if len(sub) >= 15:
        st(sub, str(r)[:36])

print("\n═══ B. 「连续3日涨幅偏离累计20%」= 官方标注已连涨 ═══")
tag = m["reason_s"].str.contains("涨幅偏离值累计", na=False)
for y, s in [("全期", m)] + list(m.groupby("year")):
    print(f"  [{y}]")
    st(s[~s["reason_s"].str.contains("涨幅偏离值累计", na=False)], "  非涨幅偏离型")
    st(s[s["reason_s"].str.contains("涨幅偏离值累计", na=False)], "  涨幅偏离型(已连涨)")

print("\n═══ C. 极端高位桶（用户场景：600641 ext=39.9% / 连涨6天）═══")
print("  按 ext 分档：")
for lab, mask in [("ext<0%", m["ext"] < 0), ("0~12%", (m["ext"] >= 0) & (m["ext"] < .12)),
                  ("12~25%", (m["ext"] >= .12) & (m["ext"] < .25)),
                  ("25~40%", (m["ext"] >= .25) & (m["ext"] < .40)),
                  (">=40%", m["ext"] >= .40)]:
    st(m[mask], f"    {lab}")
print("  按连涨天数分档：")
for lab, mask in [("0(首阳)", m["consec_up"] == 0), ("1-2", m["consec_up"].between(1, 2)),
                  ("3-4", m["consec_up"].between(3, 4)), ("5-6", m["consec_up"].between(5, 6)),
                  (">=7", m["consec_up"] >= 7)]:
    st(m[mask], f"    {lab}")

print("\n═══ D. 组合：ext>=25% 且 连涨>=5（600641 形态）═══")
hot = (m["ext"] >= .25) & (m["consec_up"] >= 5)
st(m[hot], "  命中（强烈追高形态）")
st(m[~hot], "  未命中")
print("\n  CC 口径复核（perf_1d）：")
for lab, s2 in [("命中", m[hot]), ("未命中", m[~hot])]:
    d = s2["nd_cc"].dropna()
    if len(d):
        print(f"    {lab:<6} n={len(d):>4}  CC均值 {d.mean():+6.2f}%  胜率 {(d > 0).mean()*100:5.1f}%")

print("\n═══ E. 600641 与同类样本明细 ═══")
t = m[(m["ext"] >= .25) & (m["consec_up"] >= 5)].sort_values("trade_date")
print(f"  历史同类样本 {len(t)} 条（ext>=25% 且连涨>=5）")
print(t[["trade_date", "code", "pct_change", "consec_up", "ext", "nd_cc", "nd_oc"]]
      .to_string(index=False))

conn.close()
