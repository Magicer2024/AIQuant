"""探针：线上短线候选池的换手率分布（只读）。

目的
----
`tests/_probe_chip_warmup.py` 证明筹码 `conc` 对 warmup 长度敏感，且偏差**集中在低换手股**：
  换手 <0.2%  → |差| 中位 0.236（conc 量级才 0.1~0.5）
  换手 0.2-0.5% → 0.036
  换手 1-2%   → 0.000（11% 的观测 >0.005）
  换手 >2%    → 0.000（≤1%）

若线上短线候选池里所有票换手都 >1%，则增量路径的 ~480 行窗口够用，
影子列不必补读更长历史；否则必须显式保证 warmup。

本探针用 stock_signal 的 short 记录 + daily_price.volume / stock_info.circ_shares
算实际换手率，看池子的下沿在哪。

用法
----
    python tests/_probe_chip_pool_turnover.py
"""
from __future__ import annotations

import io
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import get_conn           # noqa: E402

OUT = os.path.join(ROOT, "logs", "_chip_pool_turnover.txt")


def main():
    buf = io.StringIO()

    def w(s=""):
        print(s)
        buf.write(s + "\n")

    with get_conn() as conn:
        df = pd.read_sql_query("""
            SELECT s.code, s.scan_date, s.strategy,
                   p.volume, p.close, i.circ_shares, i.name
            FROM stock_signal s
            JOIN daily_price p
              ON p.code = s.code AND p.trade_date = s.scan_date
            JOIN stock_info  i ON i.code = s.code
            WHERE COALESCE(s.horizon, 'short') = 'short'
              AND s.scan_date >= '2026-01-01'
        """, conn)

    if df.empty:
        w("!! 无样本")
        io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
        return

    df["to_pct"] = df["volume"] / df["circ_shares"] * 100.0
    df["mktcap_yi"] = df["circ_shares"] * df["close"] / 1e8     # 流通市值（亿元）
    df = df[np.isfinite(df["to_pct"]) & (df["to_pct"] > 0)]

    w("=" * 76)
    w("线上短线候选池 · 换手率与流通市值分布")
    w(f"样本：stock_signal short 记录 {len(df):,} 条 "
      f"（2026-01-01 起，{df['code'].nunique():,} 只）")
    w("=" * 76)

    w("")
    w("【换手率分位】单位 %")
    qs = [0, 1, 2, 5, 10, 20, 30, 50, 70, 90, 100]
    v = df["to_pct"]
    w("  " + "  ".join(f"P{q}" for q in qs))
    w("  " + "  ".join(f"{np.nanpercentile(v, q):.3f}" for q in qs))

    w("")
    w("【落在 warmup 敏感区间的占比】")
    w(f"{'换手档':>12} {'记录数':>8} {'占比':>8} {'warmup 480 是否够':>20}")
    bands = [(0, 0.2, "不够（|差|中位 0.236）"),
             (0.2, 0.5, "不够（0.036）"),
             (0.5, 1.0, "边缘（0.005）"),
             (1.0, 2.0, "基本够（0.000，11% 略偏）"),
             (2.0, 5.0, "够"),
             (5.0, 1e9, "够")]
    for lo, hi, note in bands:
        m = (v >= lo) & (v < hi)
        w(f"{f'{lo}~{hi}%':>12} {int(m.sum()):>8} "
          f"{m.mean()*100:>7.1f}% {note:>20}")

    sens = (v < 1.0).mean() * 100
    w("")
    w(f"⇒ 换手 <1% 的信号占比 **{sens:.1f}%**（这些是 warmup 敏感样本）")

    w("")
    w("【流通市值分位】单位亿元（对照 QUALITY_FILTER 的 [30, 3000] 区间）")
    m = df["mktcap_yi"]
    w("  " + "  ".join(f"P{q}" for q in qs))
    w("  " + "  ".join(f"{np.nanpercentile(m, q):.0f}" for q in qs))
    w(f"  触到 3000亿 上限的记录：{int((m > 3000).sum())} 条")
    w(f"  低于 30亿 下限的记录：{int((m < 30).sum())} 条")

    w("")
    w("【换手率最低的 15 条记录】")
    w(f"{'code':>8} {'name':>10} {'scan_date':>12} {'换手%':>8} "
      f"{'流通市值亿':>10}  warmup480偏差提示")
    for _, r in df.nsmallest(15, "to_pct").iterrows():
        tip = "★高偏差" if r["to_pct"] < 0.5 else ("边缘" if r["to_pct"] < 1.0 else "")
        w(f"{r['code']:>8} {str(r['name'])[:10]:>10} {r['scan_date']:>12} "
          f"{r['to_pct']:>8.3f} {r['mktcap_yi']:>10.0f}  {tip}")

    w("")
    w("判读：换手 <1% 占比若接近 0 → 增量路径 480 行窗口够用，影子列不必补读；")
    w("      若不可忽略（>5%）→ 影子列必须显式保证 warmup（补读到 ~1300 交易日）")

    io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
    print(f"\n[写出] {OUT}")


if __name__ == "__main__":
    main()
