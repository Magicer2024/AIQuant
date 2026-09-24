"""机构建仓信号 · 二次验证
======================================================================
首轮发现（tools/_lhb_jg_out.txt）：
  · 单日「机构净买 > 0」相对「机构净卖」毫无区分度（1/5/10 日 Δ 全不显著）。
  · 唯一亮眼的是「近 20 日机构净买 >= 4 次」：1 日 +1.22% / 胜率 53.0%，t=+2.24。
    ⚠ 但这是从 2/3/4 三个阈值里挑出来的，且 Δ 不单调（+0.223 / +0.229 / +0.499），
      典型「挑参数」嫌疑 —— 必须过两道关：
        ① 分年 + 剔除单年一致性（铁律 3）
        ② **活跃度对照**：频繁上榜本身就带来正收益吗？（铁律 4）
           若「多次上榜但机构净卖」同样好 ⇒ 是「活跃度」效应，机构方向无贡献。

用法：python tools/_diag_lhb_jg2.py [start] [end]
"""
import bisect
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _diag_lhb_jg import (load, build, ret_hold, tstat, agg,   # noqa: E402
                          LOOKBACK, is_st)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def annotate(sigs, jg, px):
    """为每个样本补：近 LOOKBACK 日「机构榜出现总次数」(不分方向) 与「净买次数」。"""
    want = {(s["code"], s["date"]) for s in sigs}
    cnt_all, cnt_in = {}, {}
    for code, recs in jg.items():
        seq = px.get(code)
        if not seq:
            continue
        dpos = {t[0]: i for i, t in enumerate(seq)}
        rec_dates = [r["trade_date"] for r in recs]
        for r in recs:
            d = r["trade_date"]
            if (code, d) not in want:
                continue
            pos = dpos.get(d)
            if pos is None:
                continue
            lo_date = seq[max(0, pos - LOOKBACK)][0]
            a = bisect.bisect_left(rec_dates, lo_date)
            b = bisect.bisect_right(rec_dates, d)
            win = recs[a:b]
            cnt_all[(code, d)] = len(win)
            cnt_in[(code, d)] = sum(
                1 for x in win
                if (x.get("jg_net_buy") or 0) > 0 and (x.get("buyer_jg_count") or 0) > 0)
    for s in sigs:
        k = (s["code"], s["date"])
        s["cnt_all"] = cnt_all.get(k, s["cnt_in"])
        s["cnt_in2"] = cnt_in.get(k, s["cnt_in"])
    return sigs


def show(tag, sigs, fn=ret_hold, n=1):
    v = [x for s in sigs for x in [fn(s, n)] if x is not None]
    print(agg(tag, v))
    return v


def by_year_line(tag, sigs, n=1):
    out = []
    for yr in ("2024", "2025", "2026"):
        v = [x for s in sigs if s["date"].startswith(yr)
             for x in [ret_hold(s, n)] if x is not None]
        if not v:
            continue
        out.append(f"{yr} n={len(v):<4} {sum(v) / len(v):+6.2f}% / "
                   f"胜率 {sum(1 for x in v if x > 0) / len(v) * 100:5.1f}%")
    print(f"      {tag:<26} " + " | ".join(out))


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2024-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-22"
    out_path = os.path.join(ROOT, "tools", "_lhb_jg_out2.txt")
    sys.stdout = open(out_path, "w", encoding="utf-8", newline="\n")

    info, px, jg, lhb, dates, idx_px = load(start, end)
    sigs = annotate(build(px, info, jg, dates), jg, px)
    A = [s for s in sigs if s["net_in"]]
    B = [s for s in sigs if not s["net_in"]]
    print(f"样本 {len(sigs)} 条 | 机构净买>0 {len(A)} 条 / 机构净卖 {len(B)} 条")

    print(f"\n{'=' * 100}\n【关①】「近{LOOKBACK}日机构净买 >= K 次」逐档 + 分年（持有 1 日）\n{'=' * 100}")
    base1 = [x for s in A for x in [ret_hold(s, 1)] if x is not None]
    print(agg("A 全子集(基准)", base1))
    for k in range(2, 9):
        sub = [s for s in A if s["cnt_in2"] >= k]
        v = show(f"  净买>={k} 次", sub)
        t, diff = tstat(v, base1)
        fl = f"  Δ={diff:+.3f}pp t={t:+.2f}" if t is not None else ""
        fl += "  ✓显著" if (t is not None and abs(t) >= 2) else "  (不显著)"
        print(f"      vs 全子集{fl}")
        by_year_line("分年", sub)

    print(f"\n{'=' * 100}\n【关②·关键对照】活跃度拆解：近{LOOKBACK}日「机构榜出现 >= K 次」\n"
          f"  若「多次上榜但机构净卖」同样好 ⇒ 收益来自活跃度，机构方向无贡献\n{'=' * 100}")
    for k in (3, 4, 5):
        print(f"  ── 机构榜出现 >= {k} 次 ──")
        for lab, grp in (("机构净买>0", A), ("机构净卖<=0", B)):
            sub = [s for s in grp if s["cnt_all"] >= k]
            show(f"    {lab}", sub)
            by_year_line("分年", sub)
        a = [x for s in A if s["cnt_all"] >= k for x in [ret_hold(s, 1)] if x is not None]
        b = [x for s in B if s["cnt_all"] >= k for x in [ret_hold(s, 1)] if x is not None]
        t, diff = tstat(a, b)
        if t is not None:
            print(f"      方向差 Δ={diff:+.3f}pp t={t:+.2f}"
                  f"{'  ✓显著' if abs(t) >= 2 else '  (不显著)'}")

    print(f"\n{'=' * 100}\n【关③】「>=4 次」剔除单年检验（铁律 3）\n{'=' * 100}")
    sub = [s for s in A if s["cnt_in2"] >= 4]
    for drop in (None, "2024", "2025", "2026"):
        keep = [s for s in sub if drop is None or not s["date"].startswith(drop)]
        base = [s for s in A if drop is None or not s["date"].startswith(drop)]
        a = [x for s in keep for x in [ret_hold(s, 1)] if x is not None]
        b = [x for s in base for x in [ret_hold(s, 1)] if x is not None]
        t, diff = tstat(a, b)
        seg = "全期" if drop is None else f"剔除{drop}"
        if t is None:
            print(f"      {seg:<9} 样本不足")
        else:
            print(f"      {seg:<9} n={len(a)}/{len(b)} 均值 {sum(a) / len(a):+.2f}% vs "
                  f"{sum(b) / len(b):+.2f}%  Δ={diff:+.3f}pp t={t:+.2f}"
                  f"{'  ✓显著' if abs(t) >= 2 else '  (不显著)'}")

    print(f"\n{'=' * 100}\n【关④】>=4 次 + 方向正确（净买日占多）+ 非涨停，能否叠加？\n{'=' * 100}")
    for lab, fn in (("基准 A 全子集", lambda s: True),
                    (">=4 次", lambda s: s["cnt_in2"] >= 4),
                    (">=4 次 且 净买日占比>=2/3",
                     lambda s: s["cnt_in2"] >= 4 and s["cnt_in2"] / max(s["cnt_all"], 1) >= 0.66),
                    (">=4 次 且 上榜日涨幅<9.8",
                     lambda s: s["cnt_in2"] >= 4 and s["pct"] < 9.8),
                    (">=4 次 且 净买额/流通市值>=0.5%",
                     lambda s: s["cnt_in2"] >= 4
                     and (s["jgb_ratio"] or 0) >= 0.5)):
        sub = [s for s in A if fn(s)]
        v = show(f"  {lab}", sub)
        by_year_line("分年", sub)

    sys.stdout.flush()
    sys.stdout = sys.__stdout__
    print(f"结果已写入 {out_path}")


if __name__ == "__main__":
    main()
