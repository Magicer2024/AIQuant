# -*- coding: utf-8 -*-
"""三连板后第4天涨停打开 → 次日开盘买入 的历史形态统计
对标 000815 美利云 2026-08-05 情形：
  7-31/8-03/8-04 连续3涨停 → 8-05 收 +2.61%（涨停打开/冲高回落）→ 8-06 想开盘买入
"""
import sqlite3
import pandas as pd
import numpy as np

CONN = sqlite3.connect("core/quant.db")

df = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, pct_change FROM daily_price ORDER BY code, trade_date",
    CONN,
)
df = df[df["trade_date"] >= "2015-01-01"].copy()
print(f"数据范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}, 行数 {len(df)}")

# ── 涨停判定（主板 10%）与连续涨停段 ──
df["lim"] = (df["pct_change"] >= 9.8).astype(int)
prev = df.groupby("code")["lim"].shift()
df["new_grp"] = (df["lim"] != prev) | prev.isna()
# grp_id 必须全局唯一（不同股票的段不能合并）
df["grp_id"] = df["code"] + "_" + df.groupby("code")["new_grp"].cumsum().astype(str)
df["streak"] = df.groupby("grp_id").cumcount() + 1  # 组内序号（含非涨停行，但只在 lim=1 时有意义）

# 涨停段最后一天：lim=1 且下一交易日（同 code）不是涨停
nxt = df.groupby("code")["lim"].shift(-1)
df["seg_end"] = (df["lim"] == 1) & (nxt != 1)

# 涨停段长度 >= 3 的段尾
seg_len = df.groupby("grp_id")["lim"].transform("sum")
df["seg_len"] = seg_len

# 信号日 = 段尾的下一个交易日；买入日 = 信号日的下一个交易日
sig = df[df["seg_end"] & (df["seg_len"] >= 3)].copy()
sig_idx = sig.index
# 信号日与买入日：用索引向后找同 code 的下一行
df["next_idx"] = df.groupby("code").cumcount() + 1
df["row_no"] = df.groupby("code").cumcount()

# 简化：用 merge_asof 式查找——构造 code+row_no 键
recs = []
row_no_map = {code: g.set_index("row_no") for code, g in df.groupby("code")}
for idx in sig_idx:
    r = df.loc[idx]
    code = r["code"]
    g = row_no_map[code]
    rn = r["row_no"]
    # 信号日 = rn+1（段尾次日），买入日 = rn+2
    if rn + 1 in g.index and rn + 2 in g.index:
        s = g.loc[rn + 1]
        b = g.loc[rn + 2]
        # seg_len=连续涨停天数（3=三连板后打开 对标8-05；>=4=四连板+后打开 对标7-24）
        recs.append((code, r["trade_date"], s["trade_date"], b["trade_date"],
                     r["close"], b["open"], b["close"], s["pct_change"],
                     s["high"] / r["close"] - 1, int(r["seg_len"])))

R = pd.DataFrame(recs, columns=["code", "last_limit_date", "sig_date", "buy_date",
                                "last_limit_close", "buy_open", "buy_close", "sig_pct",
                                "sig_high_pct", "seg_len"])
print(f"基础形态样本: 三连板后打开 {int((R['seg_len']==3).sum())} / 四连板+后打开 {int((R['seg_len']>=4).sum())}, 共 {len(R)} 个")

# 从买入日 open 买入后，取后续 N 个交易日收盘
gmap = row_no_map
buy_row_no = {}
for code, g in df.groupby("code"):
    g2 = g.reset_index()
    buy_row_no[code] = g2.set_index("trade_date")["row_no"].to_dict()

def future_close(code, trade_date, offset):
    g = row_no_map[code]
    rn = buy_row_no.get(code, {}).get(trade_date)
    if rn is None or rn + offset not in g.index:
        return np.nan
    return g.loc[rn + offset, "close"]

for off, label in [(1, "h1"), (3, "h3"), (5, "h5"), (10, "h10")]:
    R[label] = [future_close(c, bd, off) for c, bd in zip(R["code"], R["buy_date"])]

for label in ["h1", "h3", "h5", "h10"]:
    R[label + "_ret"] = R[label] / R["buy_open"] - 1

# ── 止损 -6% / 止盈 +20% / 10 天持有上限 的路径模拟（000815 今日 short 参数） ──
def simulate_trade(code, buy_date, buy_open, stop=0.94, tp=1.20, max_days=10):
    g = row_no_map[code]
    rn = buy_row_no.get(code, {}).get(buy_date)
    if rn is None:
        return None
    for k in range(1, max_days + 1):
        if rn + k not in g.index:
            break
        c = g.loc[rn + k, "close"]
        if c <= buy_open * stop:
            return k, c / buy_open - 1, "stop"
        if c >= buy_open * tp:
            return k, c / buy_open - 1, "tp"
    # 到期平仓（用最后可得收盘）
    last = min(rn + max_days, g.index.max())
    c = g.loc[last, "close"]
    return max_days, c / buy_open - 1, "expire"

sim = [simulate_trade(c, bd, bo) for c, bd, bo in zip(R["code"], R["buy_date"], R["buy_open"])]
sim = [s for s in sim if s]
R["exit_days"], R["exit_ret"], R["exit_reason"] = zip(*sim) if sim else ([], [], [])

# ── 叠加"信号日在推荐(stock_signal)中"的过滤 ──
sig_sql = pd.read_sql("SELECT DISTINCT scan_date, code FROM stock_signal", CONN)
sig_sql["in_signal"] = 1
R = R.merge(sig_sql, left_on=["sig_date", "code"], right_on=["scan_date", "code"], how="left")
R["in_signal"] = R["in_signal"].fillna(0).astype(int)

# ── 主板过滤（60/00 开头，剔 300/301 创业、688 科创、8/4 北交） ──
R["main_board"] = R["code"].str.match(r"^(60|00)").astype(int)

def report(sub, title):
    print(f"\n{'='*70}\n{title}  样本数 = {len(sub)}")
    if len(sub) == 0:
        return
    for label, disp in [("h1_ret", "次日收盘"), ("h3_ret", "持有3日"), ("h5_ret", "持有5日"), ("h10_ret", "持有10日")]:
        s = sub[label].dropna()
        if len(s) == 0:
            continue
        print(f"  {disp:8s}: 均值 {s.mean()*100:6.2f}%  中位 {s.median()*100:6.2f}%  胜率 {(s>0).mean()*100:5.1f}%  "
              f"P10 {s.quantile(.1)*100:6.2f}%  P90 {s.quantile(.9)*100:6.2f}%  n={len(s)}")
    if "exit_reason" in sub.columns:
        print(f"  止损-6%/止盈+20%/10日上限: 止损 {(sub['exit_reason']=='stop').mean()*100:.0f}% | "
              f"止盈 {(sub['exit_reason']=='tp').mean()*100:.0f}% | 到期 {sub['exit_reason'].notna().mean()*100:.0f}%")
        s = sub["exit_ret"].dropna()
        if len(s):
            print(f"  按退出规则: 均值 {s.mean()*100:6.2f}%  中位 {s.median()*100:6.2f}%  盈利单占比 {(s>0).mean()*100:5.1f}%")

# 报告分组
report(R, "全市场: 三连板后第4天非涨停, 次日开盘买入")
Rmb = R[R["main_board"] == 1]
report(Rmb, "主板(60/00): 三连板后第4天非涨停, 次日开盘买入")
Rrec = R[R["in_signal"] == 1]
report(Rrec, "叠加推荐层: 三连板后第4天非涨停 且 当日有推荐信号")
Rmb_rec = Rmb[Rmb["in_signal"] == 1]
report(Rmb_rec, "主板 + 推荐信号")

# 信号日"盘中摸过涨停但收盘打开"（对标 000815 长上影）
R_open = Rmb[Rmb["sig_high_pct"] >= 0.095]
report(R_open, "主板: 信号日盘中摸涨停(高≥+9.5%)但收盘未封住")

# 近3年子集
R_recent = Rmb_rec[Rmb_rec["sig_date"] >= "2023-01-01"]
report(R_recent, "主板+推荐, 近3年(2023以来)")

# 000815 本票历史样本
R_815 = Rmb[Rmb["code"] == "000815"]
report(R_815, "000815 本票历史样本")

# ── 对照：四连板+ 后打开 → 次日买入（对标 7-23 追板后 7-24 买入） ──
R_4b = R[(R["main_board"] == 1) & (R["in_signal"] == 1) & (R["seg_len"] >= 4)]
report(R_4b, "对照-主板+推荐: 四连板+后打开 次日开盘买入")
R_4b_rec = R_4b[R_4b["sig_date"] >= "2023-01-01"]
report(R_4b_rec, "对照-主板+推荐, 近3年: 四连板+后打开 次日买入")

# 打印几个最近样本示例
print("\n最近10个主板+推荐样本(涨停打开):")
print(Rmb_rec.sort_values("sig_date").tail(10)[["code", "last_limit_date", "sig_date", "buy_date",
                                                 "sig_pct", "buy_open", "h3_ret", "h5_ret", "exit_reason", "exit_ret"]].to_string(index=False))
