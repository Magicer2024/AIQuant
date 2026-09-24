"""临时探针 2：确认「机构买卖每日统计」的金额口径（单日 vs 多日窗口）。

做法：把接口的「市场总成交额」与 daily_price.amount（原始元）对齐比较。
  - 若与当日 amount 一致 ⇒ 单日口径
  - 若约等于近 N 日 amount 之和 ⇒ N 日累计口径
"""
import os
import sqlite3

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

import akshare as ak                    # noqa: E402

df = ak.stock_lhb_jgmmtj_em(start_date="20260901", end_date="20260922")
df["上榜日期"] = df["上榜日期"].astype(str)

con = sqlite3.connect("core/quant.db")
con.row_factory = sqlite3.Row

db = {}
for r in con.execute(
        "SELECT code, trade_date, high, low, close, volume, amount FROM daily_price "
        "WHERE trade_date >= '2026-08-20' AND trade_date <= '2026-09-22'"):
    db.setdefault(r["code"], []).append(dict(r))

out = open("logs/_lhb_jg_caliber.txt", "w", encoding="utf-8")


def w(s=""):
    out.write(s + "\n")


samples = df[df["上榜日期"] == "2026-09-15"].head(400)
checked = 0
for _, row in samples.iterrows():
    code = str(row["代码"])
    seq = db.get(code)
    if not seq:
        continue
    idx = [i for i, x in enumerate(seq) if x["trade_date"] == row["上榜日期"]]
    if not idx:
        continue
    i = idx[0]
    amt_today = seq[i]["amount"]
    amt_3d = sum(seq[j]["amount"] or 0 for j in range(max(0, i - 2), i + 1))
    api_amt = row["市场总成交额"]
    if not amt_today:
        continue
    w(f"{code} {row['名称']} reason={row['上榜原因']}")
    w(f"   接口市场总成交额 = {api_amt:>18,.0f}   /当日amount = {api_amt / amt_today:>6.2f}x"
      f"   /近3日amount = {api_amt / amt_3d:>6.2f}x")
    checked += 1
    if checked >= 14:
        break

w("")
w("=== 各 reason 口径归类（接口额/当日额 的中位数）===")
stats = {}
for _, row in df.iterrows():
    code = str(row["代码"])
    seq = db.get(code)
    if not seq:
        continue
    idx = [i for i, x in enumerate(seq) if x["trade_date"] == row["上榜日期"]]
    if not idx:
        continue
    amt_today = seq[idx[0]]["amount"]
    if not amt_today:
        continue
    stats.setdefault(row["上榜原因"], []).append(row["市场总成交额"] / amt_today)

for reason, vals in sorted(stats.items(), key=lambda x: -len(x[1])):
    vals.sort()
    med = vals[len(vals) // 2]
    w(f"  n={len(vals):<4} 中位倍数 {med:>5.2f}x   {reason}")

out.close()
print(open("logs/_lhb_jg_caliber.txt", encoding="utf-8").read())
