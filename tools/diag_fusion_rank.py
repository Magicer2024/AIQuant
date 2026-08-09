"""
diag_fusion_rank.py —— 验证"融合分排序"是否反向

核心问题：fusion_score（= 纯抄底分，奖励"已反弹+放量"）高的票，后续是否涨得更好？
若是，则当前 Top8 排序有效；若否（甚至高分行后续更差），则证明用户在 2026-08-09
提出的质疑成立——融合分把"后期会涨"的低位票压到后面，排序信号无效甚至反向。

口径：stock_signal.horizon='short' AND strategy='短线融合'（落库短线票，均过趋势闸门）
每个 scan_date 内按 fusion_score 分 高/中/低 三档（top/mid/bottom 33%），
聚合多 cohort 算 T+1/T+3/T+5 开盘买(OC) 胜率与均值。
"""
import sqlite3
from collections import defaultdict

DB = "core/quant.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("加载 daily_price ...")
rows = conn.execute(
    "SELECT code, trade_date, open, close FROM daily_price"
).fetchall()
data = defaultdict(list)
for r in rows:
    data[r["code"]].append({"date": r["trade_date"], "open": r["open"], "close": r["close"]})
for code in data:
    data[code].sort(key=lambda x: x["date"])

def find_idx(rows, d):
    for i, r in enumerate(rows):
        if r["date"] == d:
            return i
    return -1

sigs = conn.execute(
    "SELECT code, scan_date, fusion_score FROM stock_signal "
    "WHERE horizon='short' AND strategy='短线融合'"
).fetchall()
by_date = defaultdict(list)
for s in sigs:
    by_date[s["scan_date"]].append(s)

S = {k: {kk: [] for kk in ("oc1", "oc3", "oc5")}
     for k in ("hi", "mid", "lo")}

cohort_n = 0
for d, lst in by_date.items():
    if len(lst) < 9:
        continue
    cohort_n += 1
    lst_sorted = sorted(lst, key=lambda x: x["fusion_score"] or 0)
    k = len(lst_sorted) // 3
    buckets = {"hi": lst_sorted[-k:], "mid": lst_sorted[k:-k], "lo": lst_sorted[:k]}
    for label, grp in buckets.items():
        for s in grp:
            rows_ = data.get(s["code"])
            if not rows_:
                continue
            idx = find_idx(rows_, d)
            if idx < 0 or idx + 6 >= len(rows_):
                continue
            base = rows_[idx + 1]["open"]  # T+1 开盘（现实可买）
            if not base or base <= 0:
                continue
            close0 = rows_[idx]["close"]
            for off, key in ((1, "oc1"), (3, "oc3"), (5, "oc5")):
                v = (rows_[idx + off]["close"] / base - 1.0) * 100.0
                S[label][key].append(v)

def avg(lst):
    return sum(lst) / len(lst) if lst else float("nan")

def win(lst):
    return sum(1 for x in lst if x > 0) / len(lst) * 100 if lst else float("nan")

print(f"有效 cohort 数 = {cohort_n}\n")
print(f"{'档位':<6}{'T+1 胜率/均值':<22}{'T+3 胜率/均值':<22}{'T+5 胜率/均值':<22}")
for label, tag in (("hi", "高融合分"), ("mid", "中"), ("lo", "低融合分")):
    o1, o3, o5 = S[label]["oc1"], S[label]["oc3"], S[label]["oc5"]
    print(f"{tag:<6}"
          f"{win(o1):>5.1f}%/{avg(o1):>+6.3f}%  "
          f"{win(o3):>5.1f}%/{avg(o3):>+6.3f}%  "
          f"{win(o5):>5.1f}%/{avg(o5):>+6.3}%  "
          f"(n={len(o1)})")

print("\n结论参照：若『高融合分』档后续不优于『低融合分』档，"
      "则证明融合分排序无效（甚至反向），应重做排序/融合定义。")
