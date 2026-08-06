# -*- coding: utf-8 -*-
"""三连板后第4天(非涨停)信号的特征细分分析
目的：判断"一刀切否决三连板"是否误杀可参与的子集。
口径：次日开盘买入，持有3/5日 + 止损-6%/止盈+20%/10日规则
分组特征：信号日涨跌 / 缩量放量 / 是否站上MA5 / 连板段长度 / 大盘冷暖 / 信号日收盘距当日高点
"""
import sqlite3
import pandas as pd
import numpy as np

conn = sqlite3.connect("core/quant.db")
df = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, pct_change "
    "FROM daily_price WHERE trade_date >= '2015-01-01' ORDER BY code, trade_date",
    conn,
)
print(f"数据: {len(df)} 行")

df["lim"] = (df["pct_change"] >= 9.8).astype(int)
prev = df.groupby("code")["lim"].shift()
df["new_grp"] = (df["lim"] != prev) | prev.isna()
df["grp_id"] = df["code"] + "_" + df.groupby("code")["new_grp"].cumsum().astype(str)
df["streak"] = df.groupby("grp_id").cumcount() + 1
nxt = df.groupby("code")["lim"].shift(-1)
df["seg_end"] = (df["lim"] == 1) & (nxt != 1)
df["seg_len"] = df.groupby("grp_id")["lim"].transform("sum")

# 行号（同 code 内）
df["row_no"] = df.groupby("code").cumcount()
row_no_map = {code: g.set_index("row_no") for code, g in df.groupby("code")}

# 收集三连板+后的信号日（段尾次日，非涨停）与买入日（再次日）
recs = []
for idx in df[df["seg_end"] & (df["seg_len"] >= 3)].index:
    r = df.loc[idx]
    g = row_no_map[r["code"]]
    rn = r["row_no"]
    if rn + 1 not in g.index or rn + 2 not in g.index:
        continue
    s = g.loc[rn + 1]
    b = g.loc[rn + 2]
    if s["pct_change"] >= 9.8:
        continue
    recs.append((r["code"], r["trade_date"], s["trade_date"], b["trade_date"],
                 int(r["seg_len"]), s["pct_change"], s["high"] / s["close"] - 1,
                 s["close"] / r["close"] - 1, b["open"]))

R = pd.DataFrame(recs, columns=["code", "last_limit_date", "sig_date", "buy_date",
                                "seg_len", "sig_pct", "sig_upper_shadow", "sig_ret_to_last_limit",
                                "buy_open"])
print(f"三连板+后第4天非涨停样本: {len(R)}")

# 买入日及后续收盘
for k in range(1, 11):
    R[f"f{k}"] = [np.nan] * len(R)
for k in range(1, 11):
    vals = []
    for c, bd in zip(R["code"], R["buy_date"]):
        g = row_no_map[c]
        rn = None
        # buy_date -> row_no
        sub = g.reset_index()
        m = sub[sub["trade_date"] == bd]
        if len(m) == 0 or m.index[0] + k >= len(g):
            vals.append(np.nan)
        else:
            rn = m.index[0]
            vals.append(g.iloc[rn + k]["close"])
    R[f"f{k}"] = vals

for k in (3, 5, 10):
    R[f"ret{k}"] = R[f"f{k}"] / R["buy_open"] - 1

# 信号日特征补充：成交量对比（第4天 vs 前5日均量）用 buy_open 前的数据不好取，改由 sig 日补算
# 简单起见：信号日缩量/放量（相对涨停段最后一天 r）——用 sig_ret_to_last_limit 代替趋势强弱

# 大盘冷暖：信号日全市场平均 pct
mkt = df.groupby("trade_date")["pct_change"].mean().rename("mkt_pct")
R = R.merge(mkt, left_on="sig_date", right_index=True, how="left")

# 止损-6%/止盈+20%/10日规则
def sim(row):
    bo = row["buy_open"]
    if np.isnan(bo):
        return np.nan
    for k in range(1, 11):
        fc = row[f"f{k}"]
        if np.isnan(fc):
            break
        if fc <= bo * 0.94:
            return fc / bo - 1
        if fc >= bo * 1.20:
            return fc / bo - 1
    last = 10
    while last >= 1 and np.isnan(row[f"f{last}"]):
        last -= 1
    return row[f"f{last}"] / bo - 1 if last >= 1 else np.nan

R["exit_ret"] = R.apply(sim, axis=1)


def report(sub, title):
    n = len(sub)
    if n < 20:
        print(f"{title}: n={n} (样本不足)")
        return
    for k, label in [(3, "持有3日"), (5, "持有5日"), (10, "持有10日")]:
        s = sub[f"ret{k}"].dropna()
        print(f"  {label}: 胜率 {(s>0).mean()*100:5.1f}%  均值 {s.mean()*100:6.2f}%  n={len(s)}")
    s = sub["exit_ret"].dropna()
    print(f"  止损-6%/止盈+20%/10日: 胜率 {(s>0).mean()*100:5.1f}%  均值 {s.mean()*100:6.2f}%  止损率 "
          f"{((sub['buy_open']>0)&(sub['f1']<=sub['buy_open']*0.94)).mean()*100:4.1f}%  n={len(s)}")


print("\n════ 基准：全部样本 ════")
report(R, "全部")

print("\n════ 按信号日收盘涨跌 ════")
for lo, hi, label in [(-99, -3, "大跌<-3%"), (-3, 0, "小跌-3~0%"), (0, 3, "小涨0~3%"), (3, 99, "大涨>3%")]:
    report(R[(R["sig_pct"] >= lo) & (R["sig_pct"] < hi)], f"信号日{label}")

print("\n════ 按连板段长度 ════")
report(R[R["seg_len"] == 3], "恰好3连板")
report(R[R["seg_len"] == 4], "4连板")
report(R[R["seg_len"] >= 5], "5连板+")

print("\n════ 按信号日上影线（冲高回落幅度） ════")
for lo, hi, label in [(-99, 0.03, "上影<3%(收在高位)"), (0.03, 0.06, "上影3~6%"), (0.06, 99, "上影>6%(长上影)")]:
    report(R[(R["sig_upper_shadow"] >= lo) & (R["sig_upper_shadow"] < hi)], label)

print("\n════ 按信号日距涨停段最后收盘涨幅 ════")
for lo, hi, label in [(-99, 0, "低于涨停收盘"), (0, 0.05, "高于涨停收盘0~5%"), (0.05, 99, "高于涨停收盘>5%")]:
    report(R[(R["sig_ret_to_last_limit"] >= lo) & (R["sig_ret_to_last_limit"] < hi)], label)

print("\n════ 按大盘冷热（信号日全市场平均涨幅） ════")
for lo, hi, label in [(-99, -1, "大盘跌>1%"), (-1, 0.5, "大盘-1~0.5%"), (0.5, 99, "大盘涨>0.5%")]:
    report(R[(R["mkt_pct"] >= lo) & (R["mkt_pct"] < hi)], label)

print("\n════ 组合：缩量阴线回踩（信号日小跌+上影小） vs 放量长上影 ════")
report(R[(R["sig_pct"] < 0) & (R["sig_upper_shadow"] < 0.03)], "回踩型(收跌+上影<3%)")
report(R[(R["sig_pct"] > 0) & (R["sig_upper_shadow"] > 0.05)], "冲高回落型(收涨+长上影)")
