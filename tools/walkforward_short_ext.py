# -*- coding: utf-8 -*-
"""walkforward_short_ext.py —— 扩展度"死区"稳健性与混杂隔离（样本外走步）

目的：回答上一轮提出的三个问题
  Q1  扩展度 5~12% 的负期望，跨时间窗是否稳定？（把"发现死区"的窗与"验证修复"的窗分开）
  Q2  收紧到 ext<=0.05 的收益改善，是否为样本外稳定复现（正反向都看），还是只对"发现死区"的那半窗成立？
  Q3  5~12% 的差，是扩展度本身的效应，还是"那些天/那些月份/那个 regime 本身就差"的混杂？

口径：完全复用 tools/backtest_short_live_1y.py 的选股/出场逻辑（runpy 拿到其命名空间，
      不重复实现，保证与"生产同口径"一致）。只读，不改库、不改原工具脚本。
输出：走步折叠表（train/test × 配置 × 桶） + 同日配对比较 + 判定结论。
"""
import sys, os, contextlib, io
from collections import defaultdict

sys.path.insert(0, ".")

# 复刻 backtest_short_live_1y 时会把整个模块 print 一遍（主回测输出），这里静默掉。
# 该模块在顶部调用 sys.stdout.reconfigure(...)，故自定义一个支持 reconfigure 的静默流。
class _Quiet(io.TextIOBase):
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s); return len(s)
    def reconfigure(self, *a, **k):
        return None
    def flush(self):
        return None

with contextlib.redirect_stdout(_Quiet()):
    import runpy
    ns = runpy.run_path("tools/backtest_short_live_1y.py", run_name="bt_mod")

REGIME_OF = ns["REGIME_OF"]
per_day = ns["per_day"]
EVAL_CUTOFF = ns["EVAL_CUTOFF"]
select_day = ns["select_day"]
SHORT_TOP_N = ns["SHORT_TOP_N"]
ext_bucket = ns["ext_bucket"]

def select_range(lo, hi, ext_cap=None):
    """在 [lo,hi] 日均窗口内做选股，返回 evp 列表（与主回测同路径）。"""
    sel = []
    for scan in sorted(per_day):
        if scan > EVAL_CUTOFF:
            continue
        if not (lo <= scan <= hi):
            continue
        for r, evp in select_day(scan, ext_cap=ext_cap):
            sel.append(evp)
    return sel

def metrics(evp_list):
    ex = [e["exit_return"] for e in evp_list if e["exit_return"] is not None]
    n = len(ex)
    if n == 0:
        return None
    wins = sum(1 for x in ex if x > 0)
    pos = sum(x for x in ex if x > 0)
    neg = -sum(x for x in ex if x < 0)
    pf = (pos / neg) if neg > 0 else float("inf")
    return {
        "n": n, "win": wins / n * 100, "mean": sum(ex) / n, "pf": pf,
        "npos": wins, "nneg": n - wins,
    }

def fmt(m):
    if m is None:
        return "  n=0"
    return (f"n={m['n']:3d} 胜率={m['win']:5.1f}% 均值={m['mean']:+6.3f}% PF={m['pf']:5.2f} "
            f"(pos={m['npos']}/neg={m['nneg']})")

# ── 走步折叠：两段连续窗口 ──
H1_LO, H1_HI = "2025-08-25", "2026-02-25"
H2_LO, H2_HI = "2026-02-26", "2026-08-07"   # H2_HI 用 EVAL_CUTOFF（剔除尾部未满仓）

print("=" * 78)
print("【Q1/Q2】走步折叠：扩展度收紧收益在训练/测试窗是否稳定复现")
print("  (正向: H1训练→H2测试 ｜ 反向: H2训练→H1测试，正反向都要看)")
print("=" * 78)

for label, lo, hi in [("H1 窗 (2025-08-25~2026-02-25)", H1_LO, H1_HI),
                      ("H2 窗 (2026-02-26~2026-08-07)", H2_LO, H2_HI)]:
    print(f"\n-- {label} --")
    for cap_name, cap in [("base(现网,只>12%否决, ext<=0.12)", None),
                          ("ext_cap=0.08", 0.08),
                          ("ext_cap=0.05", 0.05)]:
        sel = select_range(lo, hi, cap)
        m = metrics(sel)
        print(f"    {cap_name:<38} {fmt(m)}")

# ── 各桶在 H1 / H2 分窗的表现（判断"5-12%差"是否跨窗稳定） ──
print("\n" + "=" * 78)
print("【Q1 细部】扩展度桶 × 时间窗（基线选股,仅按桶分层）")
print("=" * 78)
buckets = ["<2%", "2-5%", "5-8%", "8-12%", ">=12%"]
for label, lo, hi in [("H1 窗", H1_LO, H1_HI), ("H2 窗", H2_LO, H2_HI)]:
    sel = select_range(lo, hi, None)
    by_b = defaultdict(list)
    for e in sel:
        by_b[ext_bucket(e["ext"])].append(e)
    print(f"\n-- {label} --")
    for b in buckets:
        m = metrics(by_b.get(b, []))
        print(f"    {b:>6}: {fmt(m)}")

# ── Q3 同日配对：当"5-12%信号"被选中那天，同日选中的 <5% 信号表现如何？ ──
#    若同日 <5% 信号同样差 → 「差」来自那些天/那个市场，而非扩展度本身。
print("\n" + "=" * 78)
print("【Q3】同日配对：高扩展(>=5%)信号 vs 同日低扩展(<5%)信号")
print("     只看 5-12% 被选中那一天的全体选入，检验是否「那些天本身就差」")
print("=" * 78)

all_sel = select_range(H1_LO, H2_HI, None)
day_events = defaultdict(list)
for e in all_sel:
    day_events[e["scan_date"]].append(e)

# 当日同时出现 低扩展(<5%) 和 高扩展(>5%) 的"配对日"
paired_days = {d: evs for d, evs in day_events.items()
               if (any(ext_bucket(e["ext"]) in ("<2%", "2-5%") for e in evs)
                   and any(ext_bucket(e["ext"]) in ("5-8%", "8-12%", ">=12%") for e in evs))}
print(f"\n  配对日数: {len(paired_days)}（这些天同时选入了低扩展与高扩展信号）")

low_all, hi_all = [], []
for d, evs in paired_days.items():
    low_all += [e for e in evs if ext_bucket(e["ext"]) in ("<2%", "2-5%")]
    hi_all += [e for e in evs if ext_bucket(e["ext"]) in ("5-8%", "8-12%", ">=12%")]
print(f"\n  [同日配对]  低扩展(<5%)  {fmt(metrics(low_all))}")
print(f"  [同日配对]  高扩展(>=5%) {fmt(metrics(hi_all))}")
print(f"  (若两者同日几乎一样差 → 差异源于这些天的市场状态,而非扩展度等级)")

# 反过来：5-12% 信号的"配对日"分布到哪些月份
print("\n  5-12% 信号所在月份分布（确认集中于坏月）：")
month_cnt = defaultdict(int)
for e in all_sel:
    if ext_bucket(e["ext"]) in ("5-8%", "8-12%"):
        month_cnt[e["scan_date"][:7]] += 1
for m in sorted(month_cnt):
    print(f"    {m}: {month_cnt[m]}")

print("\n完成。")
