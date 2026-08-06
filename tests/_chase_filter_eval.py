# -*- coding: utf-8 -*-
"""追高否决过滤(chase filter)回测评估 —— 过滤前后信号池次日 OC 表现对比

口径（与 eval_short_engine 的 OC 口径对齐，简化版）：
  信号日 T：fusion_score >= 18 且 趋势闸门 且 质量过滤（ST剔除+20日均额>=8000万）
  买入日 T+1 开盘价买入，T+1 收盘价卖出 → OC 收益
对比：
  现网（无 chase）vs 加 chase 后 vs 被 chase 过滤掉的信号
"""
import sqlite3
import pandas as pd
import numpy as np

conn = sqlite3.connect("core/quant.db")

# ── 数据加载（近2年全池）──
df = pd.read_sql(
    """SELECT d.code, d.trade_date, d.open, d.high, d.low, d.close, d.volume,
              COALESCE(d.amount, d.volume*d.close) AS amount,
              COALESCE(d.pct_change, 0) AS pct_change,
              COALESCE(d.fusion_score, 0) AS fusion_score
       FROM daily_price d
       WHERE d.trade_date >= '2024-06-01' AND d.trade_date <= '2026-08-05'
       ORDER BY d.code, d.trade_date""",
    conn,
)
names = pd.read_sql("SELECT code, name FROM stock_info", conn)
df = df.merge(names, on="code", how="left")
print(f"数据: {len(df)} 行, {df['code'].nunique()} 只, {df['trade_date'].min()} ~ {df['trade_date'].max()}")

g = df.groupby("code")

# ── 趋势闸门（MA20 向上，斜率回看 8 日，与 override 一致）──
ma20 = g["close"].transform(lambda s: s.rolling(20).mean())
gate = (df["close"] > ma20) & (ma20 >= g["close"].transform(lambda s: s.rolling(20).mean().shift(8)))

# ── 质量过滤（简化：ST 剔除 + 20日均额）──
amt20 = g["amount"].transform(lambda s: s.rolling(20).mean())
is_st = df["name"].astype(str).str.upper().str.contains("ST|退", na=False)
qual = (~is_st) & (amt20 >= 80_000_000)

# ── 追高否决 ──
limit_pct = 9.8
is_limit = (df["pct_change"] >= limit_pct).astype(int)
prev_limit = g["pct_change"].transform(lambda s: (s >= limit_pct).astype(int).shift(1))
seg = (is_limit != prev_limit.fillna(0)).cumsum()
streak = is_limit.groupby(seg).cumsum()
streak = streak.where(is_limit == 1, 0)
recent_max = g["streak"].transform(lambda s: s.rolling(5, min_periods=1).max()) if "streak" in df.columns else None

# 重新用 groupby 算 streak 的 rolling（上面的 transform 不可靠，重写）
df["is_limit"] = is_limit
df["seg"] = seg
df["streak"] = df.groupby("code")["is_limit"].transform(lambda s: s.groupby((s != s.shift(1)).cumsum()).cumsum())
df["streak"] = df["streak"].where(df["is_limit"] == 1, 0)
df["recent_max"] = df.groupby("code")["streak"].rolling(5, min_periods=1).max().reset_index(level=0, drop=True)

prev_close = g["close"].transform(lambda s: s.shift(1))
touched = df["high"] >= prev_close * (1 + (limit_pct - 0.3) / 100.0)
sealed = df["pct_change"] >= limit_pct
limit_open = touched & (~sealed) & prev_close.notna()
ret3 = g["close"].transform(lambda s: s / s.shift(3) - 1)

chase = (
    (df["streak"] < 3)
    & (df["recent_max"] < 3)
    & (~limit_open.fillna(False))
    & (ret3.fillna(0.0) < 0.25)
)
df["chase"] = chase

# ── 信号与次日 OC 收益 ──
base_sig = (df["fusion_score"] >= 18) & gate.fillna(False) & qual.fillna(False)
nxt_open = g["open"].transform(lambda s: s.shift(-1))
nxt_close = g["close"].transform(lambda s: s.shift(-1))
df["nxt_open"] = nxt_open
df["nxt_oc"] = nxt_close / nxt_open - 1.0
df["sig"] = base_sig
df["sig_chase"] = base_sig & chase


def report(mask, title):
    sub = df[mask].dropna(subset=["nxt_oc"])
    n = len(sub)
    if n == 0:
        print(f"{title}: 无样本")
        return
    r = sub["nxt_oc"]
    print(f"{title}: n={n}  胜率 {(r>0).mean()*100:5.1f}%  均值 {r.mean()*100:6.2f}%  "
          f"中位 {r.median()*100:6.2f}%  P10 {r.quantile(.1)*100:6.2f}%  P90 {r.quantile(.9)*100:6.2f}%")

# ── 真实持仓口径：T+1 开盘买入 → 止损-6%/止盈+20%/10交易日上限（对标今日推荐卡片参数）──
for k in range(1, 11):
    df[f"f{k}"] = g["close"].transform(lambda s, kk=k: s.shift(-kk))
# 路径模拟（向量化近似：逐日先判断止损/止盈，任一先到即退出）
def simulate_exit(sub_df):
    res = []
    codes = sub_df["code"].values
    buy_opens = sub_df["nxt_open"].values
    idx = sub_df.index.values
    for c, bo, ix in zip(codes, buy_opens, idx):
        if np.isnan(bo):
            res.append((np.nan, np.nan, "no_data"))
            continue
        exited = None
        for k in range(1, 11):
            fc = sub_df.loc[ix, f"f{k}"]
            if np.isnan(fc):
                break
            if fc <= bo * 0.94:
                exited = (k, fc / bo - 1, "stop")
                break
            if fc >= bo * 1.20:
                exited = (k, fc / bo - 1, "tp")
                break
        if exited is None:
            last_k = 10
            while last_k >= 1 and np.isnan(sub_df.loc[ix, f"f{last_k}"]):
                last_k -= 1
            if last_k >= 1:
                exited = (last_k, sub_df.loc[ix, f"f{last_k}"] / bo - 1, "expire")
            else:
                exited = (np.nan, np.nan, "no_data")
        res.append(exited)
    return pd.DataFrame(res, columns=["days", "ret", "reason"], index=idx)

print("\n── 真实持仓口径（T+1开盘买入，止损-6%/止盈+20%/10日上限）──")
for label, mask in [
    ("现网信号池", df["sig"]),
    ("加追高否决后", df["sig_chase"]),
    ("被过滤掉的信号", df["sig"] & ~df["chase"]),
]:
    sub = df[mask].copy()
    sim = simulate_exit(sub)
    sub = sub.join(sim)
    sub = sub.dropna(subset=["ret"])
    n = len(sub)
    if n == 0:
        print(f"{label}: 无样本")
        continue
    r = sub["ret"]
    stop_r = (sub["reason"] == "stop").mean() * 100
    tp_r = (sub["reason"] == "tp").mean() * 100
    print(f"{label}: n={n:6d}  胜率 {(r>0).mean()*100:5.1f}%  均值 {r.mean()*100:6.2f}%  "
          f"中位 {r.median()*100:6.2f}%  止损率 {stop_r:4.1f}%  止盈率 {tp_r:4.1f}%")

print("\n近1年:")
for label, mask in [
    ("现网信号池", df["sig"] & (df["trade_date"] >= "2025-08-01")),
    ("加追高否决后", df["sig_chase"] & (df["trade_date"] >= "2025-08-01")),
    ("被过滤掉的信号", df["sig"] & ~df["chase"] & (df["trade_date"] >= "2025-08-01")),
]:
    sub = df[mask].copy()
    sim = simulate_exit(sub)
    sub = sub.join(sim)
    sub = sub.dropna(subset=["ret"])
    n = len(sub)
    if n == 0:
        print(f"{label}: 无样本")
        continue
    r = sub["ret"]
    stop_r = (sub["reason"] == "stop").mean() * 100
    tp_r = (sub["reason"] == "tp").mean() * 100
    print(f"{label}: n={n:6d}  胜率 {(r>0).mean()*100:5.1f}%  均值 {r.mean()*100:6.2f}%  "
          f"中位 {r.median()*100:6.2f}%  止损率 {stop_r:4.1f}%  止盈率 {tp_r:4.1f}%")

# 各原因过滤掉的信号（持仓口径）
print("\n被过滤信号按原因（持仓口径）:")
killed = df[df["sig"] & ~df["chase"]].copy()
sim = simulate_exit(killed)
killed = killed.join(sim).dropna(subset=["ret"])
reason = []
for i, r in killed.iterrows():
    rs = []
    if r["streak"] >= 3:
        rs.append("连板>=3")
    if r["recent_max"] >= 3:
        rs.append("连板冷却期")
    if limit_open.loc[i]:
        rs.append("涨停打开")
    if ret3.loc[i] >= 0.25:
        rs.append("3日急涨>=25%")
    reason.append("+".join(rs) if rs else "其他")
killed["reason"] = reason
for rsn, sub in killed.groupby("reason"):
    r = sub["ret"]
    if len(r) < 20:
        continue
    print(f"  {rsn:16s}: n={len(r):5d}  胜率 {(r>0).mean()*100:5.1f}%  均值 {r.mean()*100:6.2f}%  "
          f"止损率 {(sub['reason']=='stop').mean()*100:4.1f}%")

report(df["sig"], "现网信号池（无追高否决）")
report(df["sig_chase"], "加追高否决后")
report(df["sig"] & ~df["chase"], "被追高否决过滤掉的信号")
report(df["sig_chase"] & (df["chase"]), "chase保留的剔除样本（未过滤部分）")
