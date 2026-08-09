# -*- coding: utf-8 -*-
"""推荐策略对比报告：旧策略(v1/历史短线) vs 最新策略(v2/短线融合) 的 T1/T2/T3/T5 胜率。

口径（与 tools/diag_short_reco.py 一致）：
  - OC：次日开盘买入 → 信号日后第 N 个交易日收盘卖出（实际可执行）
  - CC：信号日收盘买入 → 信号日后第 N 个交易日收盘卖出（对照）
分组：
  - v2（最新）：stock_signal.strategy='短线融合' 且 created_at >= 2026-08-09（8-09 全量重算落库）
  - v1（旧）：stock_signal.strategy='历史短线'（8-09 前多代重算累积的旧信号，含早期 NULL）
对比窗口：2024-02-01 ~ 2026-07-02（两代引擎共同覆盖且 T5 数据齐全；v1 最新信号 07-02）
去重：(code, scan_date) 保留一条（'历史短线'是多代重算并集，可能有重复行）。
纯读取，不写库；报告输出到 reports/ 目录。
"""
import os
import sys
import sqlite3
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(PROJECT_ROOT, "core", "quant.db")
WINDOW = ("2024-02-01", "2026-07-02")   # 重叠窗口（v1 信号到 07-02）
LATEST_TD = "2026-08-07"                 # 最新交易日（T5 完整）

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1) 分组信号（short 组）
sigs = conn.execute(
    "SELECT code, scan_date, strategy FROM stock_signal "
    "WHERE horizon='short' AND strategy IN ('短线融合','历史短线') "
    "AND scan_date BETWEEN ? AND ?",
    WINDOW,
).fetchall()
groups = {"v1": set(), "v2": set()}   # (code, scan_date) 去重
for s in sigs:
    key = (s["code"], s["scan_date"])
    groups["v2" if s["strategy"] == "短线融合" else "v1"].add(key)
conn.close()
print(f"信号数（去重后）: v1={len(groups['v1'])}  v2={len(groups['v2'])}")

# 2) 日线
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
px = conn.execute(
    "SELECT code, trade_date, open, close FROM daily_price "
    "WHERE trade_date BETWEEN '2024-01-01' AND ? ORDER BY code, trade_date",
    (LATEST_TD,),
).fetchall()
data = defaultdict(list)
for r in px:
    data[r["code"]].append(r)
conn.close()

def find_idx(rows, date):
    for i, r in enumerate(rows):
        if r["trade_date"] >= date:
            return i
    return -1

def compute(group_key):
    """返回 {t1..t5: {oc: [rets], cc: [rets]}}"""
    acc = {k: {"oc": [], "cc": []} for k in ("t1", "t2", "t3", "t5")}
    for code, scan_date in groups[group_key]:
        rows = data.get(code)
        if not rows:
            continue
        idx = find_idx(rows, scan_date)
        if idx < 0 or idx + 5 >= len(rows):
            continue
        base_close = rows[idx]["close"]
        open_nxt = rows[idx + 1]["open"]
        if not base_close or not open_nxt:
            continue
        for n, key in ((1, "t1"), (2, "t2"), (3, "t3"), (5, "t5")):
            c = rows[idx + n]["close"]
            acc[key]["oc"].append((c - open_nxt) / open_nxt * 100)
            acc[key]["cc"].append((c - base_close) / base_close * 100)
    return acc

res = {"v1": compute("v1"), "v2": compute("v2")}

def stats(lst):
    if not lst:
        return (0, 0.0, 0)
    pos = sum(1 for x in lst if x > 0)
    return (pos / len(lst) * 100, sum(lst) / len(lst), len(lst))

# 3) 输出
def line(k, tag):
    s1 = stats(res["v1"][k][tag]); s2 = stats(res["v2"][k][tag])
    d_wr = s2[0] - s1[0]; d_avg = s2[1] - s1[1]
    return (f"| T+{k[1:]} | {s1[0]:.1f}% | {s1[1]:+.3f}% | {s1[2]} | "
            f"{s2[0]:.1f}% | {s2[1]:+.3f}% | {s2[2]} | "
            f"{d_wr:+.1f}pct | {d_avg:+.3f}% |")

lines = []
lines.append("# 推荐策略对比报告（旧 v1 vs 最新 v2）")
lines.append("")
lines.append(f"- 生成时间：2026-08-09（数据截至 {LATEST_TD}）")
lines.append(f"- 对比窗口：{WINDOW[0]} ~ {WINDOW[1]}（两代引擎共同覆盖且 T+5 数据齐全）")
lines.append("- v1 = 8-09 前旧推荐（`历史短线`，多代重算并集，含 v1 引擎/旧过滤链/旧参数）")
lines.append("- v2 = 8-09 全量重算的当前推荐（`短线融合`，pure_bottom_v2 引擎）")
lines.append(f"- 去重后样本：v1={len(groups['v1'])} 条，v2={len(groups['v2'])} 条")
lines.append("- 口径：OC=次日开盘买入→T+N 收盘卖出（实际可执行）；CC=信号日收盘买入→T+N 收盘卖出")
lines.append("")
for tag, taglbl in (("oc", "OC（次日开盘买入）"), ("cc", "CC（信号日收盘买入）")):
    lines.append(f"## {taglbl} 胜率 / 均值对比")
    lines.append("")
    lines.append("| 周期 | v1 胜率 | v1 均值 | v1 n | v2 胜率 | v2 均值 | v2 n | 胜率差 | 均值差 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for k in ("t1", "t2", "t3", "t5"):
        lines.append(line(k, tag))
    lines.append("")
lines.append("## 数据规模变化")
lines.append("")
lines.append(f"- 候选信号（去重后）：v1 {len(groups['v1'])} → v2 {len(groups['v2'])}"
             f"（{('减少 %.1f%%' % ((1 - len(groups['v2'])/len(groups['v1']))*100)) if groups['v1'] else '—'}）")
lines.append("- 每周期样本：v2 较 v1 的 n 变化见上表（胜率基于各自样本）")
lines.append("")
report = "\n".join(lines)
print(report)

out = os.path.join(PROJECT_ROOT, "reports", "recommendation_strategy_compare.md")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    f.write(report + "\n")
print(f"\n报告已写入: {out}")
