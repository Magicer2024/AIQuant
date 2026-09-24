"""对账：复刻 tools/mine_next_day.py 的龙虎榜口径，核实「隔日动量」真实期望。

疑问：next_day_momentum.py 文档称验证窗 OC 胜率 54.3%/均值 +0.95%；
     本机复算全期 OC 为 -0.36%。必须判定是「口径差异」还是「样本期差异」。

复刻要点（与 mine_next_day.run_lhb 完全一致）：
  - since=2024-07-01、剔 ST/退、|perf_1d|<=21
  - **dropna(perf_1d)** —— 只留有表内标签的样本（v1 脚本漏了这一步）
  - 分层「净买占比>=10% 且 pct_change<9.8」
  - CC = perf_1d（表内标签）；OC = 自算 T+1 open→close
用法: python tools/_diag_lhb_recon.py
"""
import os
import sqlite3

import pandas as pd

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "core", "quant.db")
SINCE = "2024-07-01"
SPLIT = "2026-01-25"

conn = sqlite3.connect(DB)

dp = pd.read_sql(
    "SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close, p.pct_change, i.name "
    "FROM daily_price p LEFT JOIN stock_info i ON i.code = p.code "
    "WHERE p.trade_date >= ? ORDER BY p.code, p.trade_date",
    conn, params=[SINCE])
dp = dp[~dp["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
dp = dp[dp["pct_change"].abs() <= 21]
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
dp["nd_oc"] = (dp["nxt_close"] / dp["nxt_open"].replace(0, pd.NA) - 1) * 100

lhb = pd.read_sql(
    "SELECT trade_date, code, name, pct_change, net_buy, net_buy_ratio, reason, perf_1d "
    "FROM stock_lhb_detail WHERE trade_date >= ?", conn, params=[SINCE])
lhb = lhb[~lhb["name"].fillna("").str.upper().str.contains("ST|退", regex=True)]
print(f"龙虎榜原始: {len(lhb)}")
lhb_a = lhb.dropna(subset=["perf_1d"]).copy()
lhb_a = lhb_a[lhb_a["perf_1d"].abs() <= 21]
print(f"dropna(perf_1d) 后: {len(lhb_a)}  ← mine_next_day 口径")
print(f"未过滤（全部, 含无标签）: {len(lhb)}\n")

m = lhb_a.merge(dp[["code", "trade_date", "pct_change", "ext", "ret3", "consec_up",
                    "nd_oc", "high", "prev_close", "close"]],
                on=["code", "trade_date"], how="left", suffixes=("", "_px"))
m["nd_cc"] = m["perf_1d"]
sel = (m["net_buy_ratio"] >= 10) & (m["pct_change"] < 9.8)


def st(df, label):
    if len(df) == 0:
        print(f"  {label:<34} n=0")
        return
    cc, oc = df["nd_cc"].dropna(), df["nd_oc"].dropna()
    print(f"  {label:<34} n={len(df):>5} | CC 胜率{(cc > 0).mean()*100:5.1f}% "
          f"均值{cc.mean():+6.2f}% | OC 胜率{(oc > 0).mean()*100:5.1f}% "
          f"均值{oc.mean():+6.2f}% (OC n={len(oc)})")


print("═══ 复刻 mine_next_day：净买占比>=10% 且 非涨停(收盘<9.8) ═══")
sub = m[sel]
st(sub[sub["trade_date"] < SPLIT], "训练窗(<2026-01-25)")
st(sub[sub["trade_date"] >= SPLIT], "验证窗(>=2026-01-25)  ← 文档口径")
st(sub, "全期")
for y, s2 in sub.groupby(sub["trade_date"].str[:4]):
    st(s2, f"  {y}")

print("\n═══ 逐月（OC 口径，看 2026 上半年是否特例）═══")
sub2 = sub.copy()
sub2["ym"] = sub2["trade_date"].str[:7]
for ym, s2 in sub2.groupby("ym"):
    oc = s2["nd_oc"].dropna()
    if len(oc) >= 8:
        print(f"  {ym}  n={len(oc):>4}  OC均值 {oc.mean():+6.2f}%  胜率 {(oc > 0).mean()*100:5.1f}%")

print("\n═══ 600641 全部龙虎榜记录（含 perf_1d） ═══")
t = lhb[lhb["code"] == "600641"].sort_values("trade_date")
print(t[["trade_date", "pct_change", "net_buy_ratio", "perf_1d"]].to_string(index=False))

conn.close()
