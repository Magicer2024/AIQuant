# -*- coding: utf-8 -*-
"""只读诊断：短线出场跟踪逐笔归因（与面板 build_exit_advice_payload 同口径）。

用法：python tools/_diag_short_perf.py
输出：逐笔明细 + 按出场原因/策略/月份/MFE 的归集统计。不写任何数据。
"""
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services.exit_tracking import build_exit_advice_payload  # noqa: E402


def cls_reason(reason: str) -> str:
    if not reason:
        return "持有中"
    if "止损" in reason:
        return "止损"
    if "移动止盈" in reason:
        return "移动止盈"
    if "到期" in reason:
        return "到期"
    if "止盈" in reason:
        return "固定止盈"
    return "其他"


def main():
    payload = build_exit_advice_payload()
    short = payload["groups"]["short"]
    perf = payload["summary"]["perf"]["short"]
    print(f"== short 段数 {len(short)} | perf={perf}")

    rows = []
    for r in short:
        d = r.get("detail") or {}
        entry = d.get("entry_price") or r.get("entry_price")
        high = d.get("highest_since_entry")
        mfe = None
        if entry and high:
            mfe = round((float(high) - float(entry)) / float(entry) * 100, 2)
        rows.append({
            "code": r["code"], "name": r["name"], "scan": r["scan_date"],
            "strategy": r.get("strategy") or "", "status": r["status"],
            "pnl": d.get("current_pnl_pct"), "hold": d.get("hold_days"),
            "entry": entry, "high": high, "mfe": mfe,
            "reason": d.get("exit_reason"), "cls": cls_reason(d.get("exit_reason"))
            if r["status"] == "clear" else ("持有中" if r["status"] == "hold" else r["status"]),
            "stop": d.get("stop_loss"),
        })

    rows.sort(key=lambda x: x["scan"])
    print("\n== 逐笔明细（按推荐日升序）")
    print(f"{'推荐日':<12}{'代码':<8}{'策略':<8}{'状态':<8}{'持仓':>4}{'买入':>8}"
          f"{'最高':>8}{'MFE%':>7}{'盈亏%':>8}  出场")
    for x in rows:
        print(f"{x['scan']:<12}{x['code']:<8}{x['strategy']:<8}{x['status']:<8}"
              f"{str(x['hold']):>4}{str(x['entry']):>8}{str(x['high']):>8}"
              f"{str(x['mfe']):>7}{str(x['pnl']):>8}  {x['cls']} | {x['reason']}")

    def agg(title, keyfn, subset):
        print(f"\n== {title}")
        g = defaultdict(list)
        for x in subset:
            g[keyfn(x)].append(x)
        for k in sorted(g, key=lambda k: str(k)):
            v = g[k]
            pnls = [x["pnl"] for x in v if isinstance(x["pnl"], (int, float))]
            mfes = [x["mfe"] for x in v if isinstance(x["mfe"], (int, float))]
            mean = round(sum(pnls) / len(pnls), 2) if pnls else None
            wr = round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1) if pnls else None
            mmfe = round(sum(mfes) / len(mfes), 2) if mfes else None
            print(f"  {str(k):<14} n={len(v):>3}  均笔={mean}%  胜率={wr}%  均MFE={mmfe}%")

    counted = [x for x in rows if x["status"] in ("clear", "hold")]
    cleared = [x for x in rows if x["status"] == "clear"]

    agg("按出场原因（已卖出+持有）", lambda x: x["cls"], counted)
    agg("按策略线", lambda x: x["strategy"] or "?", counted)
    agg("按月份", lambda x: x["scan"][:7], counted)
    agg("按持仓天数(已卖出)", lambda x: x["hold"], cleared)

    # 止损单里「曾经浮盈过」的比例：MFE>0 说明不是入场即错，而是出场太慢/止损太宽
    stops = [x for x in cleared if x["cls"] == "止损"]
    if stops:
        had = [x for x in stops if isinstance(x["mfe"], (int, float)) and x["mfe"] > 1]
        print(f"\n== 止损单 n={len(stops)} 均笔={round(sum(x['pnl'] for x in stops)/len(stops),2)}%"
              f"  其中 MFE>1% 的 n={len(had)} 均MFE={round(sum(x['mfe'] for x in had)/len(had),2) if had else None}%"
              f"  均亏={round(sum(x['pnl'] for x in had)/len(had),2) if had else None}%")
    trails = [x for x in cleared if x["cls"] == "移动止盈"]
    if trails:
        gb = [x["mfe"] - x["pnl"] for x in trails
              if isinstance(x["mfe"], (int, float)) and isinstance(x["pnl"], (int, float))]
        print(f"== 移动止盈单 n={len(trails)} 均笔={round(sum(x['pnl'] for x in trails)/len(trails),2)}%"
              f"  均MFE={round(sum(x['mfe'] for x in trails)/len(trails),2)}%"
              f"  均回吐={round(sum(gb)/len(gb),2) if gb else None}pp")
    exps = [x for x in cleared if x["cls"] == "到期"]
    if exps:
        print(f"== 到期单 n={len(exps)} 均笔={round(sum(x['pnl'] for x in exps)/len(exps),2)}%"
              f"  胜率={round(sum(1 for x in exps if x['pnl'] > 0)/len(exps)*100,1)}%")


if __name__ == "__main__":
    main()
