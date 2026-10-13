# -*- coding: utf-8 -*-
"""只读诊断：短线 61 笔的信号日特征归因（找入场侧闸门切割点）。

特征：fusion_score / pct_above_ma20(扩展度) / 止损宽度 / 策略线 /
      信号日大盘（指数涨跌、市场宽度、指数5日趋势、指数vs MA20）。
用法：python tools/_diag_short_features.py
"""
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

from core.db import get_conn  # noqa: E402
from core.services.exit_tracking import build_exit_advice_payload  # noqa: E402


def bucket(title, pairs):
    g = defaultdict(list)
    for k, p in pairs:
        g[k].append(p)
    print(f"\n== {title}")
    for k in sorted(g, key=lambda x: str(x)):
        v = [x for x in g[k] if x is not None]
        if not v:
            continue
        mean = round(sum(v) / len(v), 2)
        wr = round(sum(1 for x in v if x > 0) / len(v) * 100, 1)
        print(f"  {str(k):<18} n={len(v):>3} 均笔={mean:>7}% 胜率={wr:>5}%")


def main():
    payload = build_exit_advice_payload()
    items = [r for r in payload["groups"]["short"] if r["status"] in ("clear", "hold")]
    with get_conn() as conn:
        sig = {}
        for r in conn.execute(
            "SELECT code, scan_date, fusion_score, pct_above_ma20, stop_loss, "
            "buy_price, strategy, vol60, dd250, chip_conc, vol_ratio "
            "FROM stock_signal WHERE horizon='short'"):
            sig[(r["code"], r["scan_date"])] = dict(r)
        px = pd.read_sql_query(
            "SELECT code, trade_date, close FROM daily_price "
            "WHERE trade_date >= '2026-05-01' ORDER BY code, trade_date", conn)
        idx = pd.read_sql_query(
            "SELECT trade_date, close, pct_change FROM index_daily WHERE code='000001' "
            "ORDER BY trade_date", conn)
        idx["ma20"] = idx["close"].rolling(20).mean()
        idx["ret5"] = idx["pct_change"].rolling(5).sum()
        idxmap = {r["trade_date"]: r for _, r in idx.iterrows()}
        br = pd.read_sql_query(
            "SELECT trade_date, AVG(CASE WHEN pct_change>0 THEN 1.0 ELSE 0.0 END) AS br, "
            "AVG(pct_change) AS avgchg FROM daily_price "
            "WHERE trade_date >= '2026-07-01' GROUP BY trade_date", conn)
        brmap = {r["trade_date"]: r for _, r in br.iterrows()}
        # 个股信号日前动量：ret5/10/20 + 距20日高
        pxmap = {}
        for code, g in px.groupby("code", sort=False):
            dates = g["trade_date"].tolist()
            closes = g["close"].tolist()
            pos = {d: i for i, d in enumerate(dates)}
            pxmap[code] = (dates, closes, pos)

    def stock_prior(code, scan):
        t = pxmap.get(code)
        if not t:
            return {}
        dates, closes, pos = t
        i = pos.get(scan)
        if i is None or i < 20:
            return {}
        c0 = closes[i]
        out = {}
        for n in (5, 10, 20):
            out[f"ret{n}"] = round((c0 / closes[i - n] - 1) * 100, 1)
        hi = max(closes[max(0, i - 19):i + 1])
        out["dist20h"] = round((c0 / hi - 1) * 100, 1)
        return out

    rows = []
    for r in items:
        s = sig.get((r["code"], r["scan_date"]))
        pnl = (r.get("detail") or {}).get("current_pnl_pct")
        i = idxmap.get(r["scan_date"])
        b = brmap.get(r["scan_date"])
        sp = stock_prior(r["code"], r["scan_date"])
        stop_w = None
        if s and s["stop_loss"] and s["buy_price"]:
            stop_w = round((1 - s["stop_loss"] / s["buy_price"]) * 100, 1)
        rows.append({
            "code": r["code"], "scan": r["scan_date"], "pnl": pnl,
            "strategy": r.get("strategy") or "",
            "fusion": round(s["fusion_score"], 1) if s else None,
            "ext": round(s["pct_above_ma20"], 2) if s and s["pct_above_ma20"] is not None else None,
            "stop_w": stop_w,
            "vol60": round(s["vol60"], 2) if s and s["vol60"] is not None else None,
            "dd250": round(s["dd250"], 2) if s and s["dd250"] is not None else None,
            "ret5": sp.get("ret5"), "ret10": sp.get("ret10"),
            "ret20": sp.get("ret20"), "dist20h": sp.get("dist20h"),
            "idx_ret": round(i["pct_change"], 2) if i is not None else None,
            "idx_ret5": round(i["ret5"], 2) if i is not None else None,
            "above_ma20": (i["close"] >= i["ma20"]) if i is not None else None,
            "br": round(b["br"] * 100, 1) if b is not None else None,
        })

    print(f"n={len(rows)} 均笔={round(sum(x['pnl'] for x in rows)/len(rows),2)}%")
    for x in sorted(rows, key=lambda x: x["scan"]):
        print(f"{x['scan']} {x['code']} {x['strategy']:<6} pnl={x['pnl']:>7} "
              f"fusion={x['fusion']} ext={x['ext']} stopw={x['stop_w']} "
              f"r5={x['ret5']} r10={x['ret10']} r20={x['ret20']} h20={x['dist20h']} "
              f"v60={x['vol60']} idx5={x['idx_ret5']} br={x['br']}")

    bucket("策略线", [(x["strategy"], x["pnl"]) for x in rows])
    bucket("fusion分桶", [(f"{int(x['fusion']//4)*4}-{int(x['fusion']//4)*4+4}"
                           if x["fusion"] is not None else None, x["pnl"]) for x in rows])
    bucket("扩展度ext分桶", [(f"{int(x['ext']//2)*2}-{int(x['ext']//2)*2+2}"
                             if x["ext"] is not None else None, x["pnl"]) for x in rows])
    bucket("止损宽度分桶", [(f"{int(x['stop_w']//2)*2}-{int(x['stop_w']//2)*2+2}%"
                            if x["stop_w"] is not None else None, x["pnl"]) for x in rows])
    bucket("信号日指数涨跌", [(None if x["idx_ret"] is None else
                             ("<=-1" if x["idx_ret"] <= -1 else
                              "-1~0" if x["idx_ret"] < 0 else
                              "0~+1" if x["idx_ret"] < 1 else ">=+1"), x["pnl"])
                            for x in rows])
    bucket("信号日指数5日趋势", [(None if x["idx_ret5"] is None else
                               ("<-2" if x["idx_ret5"] < -2 else
                                "-2~0" if x["idx_ret5"] < 0 else
                                "0~2" if x["idx_ret5"] < 2 else ">2"), x["pnl"])
                              for x in rows])
    bucket("指数在MA20上方", [(x["above_ma20"], x["pnl"]) for x in rows])
    bucket("市场宽度(上涨占比)", [("<30" if x["br"] < 30 else
                               "30-50" if x["br"] < 50 else
                               "50-70" if x["br"] < 70 else ">70", x["pnl"]) for x in rows])
    bucket("个股前5日涨幅", [(None if x["ret5"] is None else
                            ("<-5" if x["ret5"] < -5 else
                             "-5~0" if x["ret5"] < 0 else
                             "0~5" if x["ret5"] < 5 else
                             "5~10" if x["ret5"] < 10 else ">10"), x["pnl"]) for x in rows])
    bucket("个股前10日涨幅", [(None if x["ret10"] is None else
                             ("<0" if x["ret10"] < 0 else
                              "0~10" if x["ret10"] < 10 else
                              "10~20" if x["ret10"] < 20 else ">20"), x["pnl"]) for x in rows])
    bucket("个股前20日涨幅", [(None if x["ret20"] is None else
                             ("<0" if x["ret20"] < 0 else
                              "0~10" if x["ret20"] < 10 else
                              "10~20" if x["ret20"] < 20 else
                              "20~40" if x["ret20"] < 40 else ">40"), x["pnl"]) for x in rows])
    bucket("距20日高", [(None if x["dist20h"] is None else
                        ("<-10" if x["dist20h"] < -10 else
                         "-10~-5" if x["dist20h"] < -5 else
                         "-5~0" if x["dist20h"] < 0 else "新高"), x["pnl"]) for x in rows])
    bucket("vol60分桶", [(None if x["vol60"] is None else
                         ("<3" if x["vol60"] < 3 else
                          "3~4" if x["vol60"] < 4 else
                          "4~5" if x["vol60"] < 5 else ">5"), x["pnl"]) for x in rows])
    # 组合切割预览
    print("\n== 组合切割预览（被剔除子集的均笔）")
    cuts = {
        "指数MA20下": lambda x: x["above_ma20"] is False,
        "idx5<-2": lambda x: x["idx_ret5"] is not None and x["idx_ret5"] < -2,
        "idx5<0": lambda x: x["idx_ret5"] is not None and x["idx_ret5"] < 0,
        "br<30": lambda x: x["br"] is not None and x["br"] < 30,
        "br<50": lambda x: x["br"] is not None and x["br"] < 50,
        "fusion<24(抄底)": lambda x: x["strategy"] != "隔日动量" and (
            x["fusion"] is not None and x["fusion"] < 24),
        "ret20>20": lambda x: x["ret20"] is not None and x["ret20"] > 20,
        "ret20>30": lambda x: x["ret20"] is not None and x["ret20"] > 30,
        "ret10>15": lambda x: x["ret10"] is not None and x["ret10"] > 15,
        "ret10>20": lambda x: x["ret10"] is not None and x["ret10"] > 20,
        "dist20h>-3": lambda x: x["dist20h"] is not None and x["dist20h"] > -3,
        "vol60>4": lambda x: x["vol60"] is not None and x["vol60"] > 4,
        "vol60>4.5": lambda x: x["vol60"] is not None and x["vol60"] > 4.5,
        "ret10>15或vol60>4.5": lambda x: (x["ret10"] is not None and x["ret10"] > 15)
            or (x["vol60"] is not None and x["vol60"] > 4.5),
        "MA20下或idx5<0": lambda x: x["above_ma20"] is False or (
            x["idx_ret5"] is not None and x["idx_ret5"] < 0),
        "MA20下或br<50": lambda x: x["above_ma20"] is False or (
            x["br"] is not None and x["br"] < 50),
    }
    for name, fn in cuts.items():
        cut = [x for x in rows if fn(x)]
        keep = [x for x in rows if not fn(x)]
        if not keep:
            continue
        cm = round(sum(x["pnl"] for x in cut) / len(cut), 2) if cut else None
        km = round(sum(x["pnl"] for x in keep) / len(keep), 2)
        kw = round(sum(1 for x in keep if x["pnl"] > 0) / len(keep) * 100, 1)
        print(f"  {name:<16} 剔n={len(cut):>3}(均{cm}%)  留n={len(keep):>3} "
              f"均笔={km:>6}% 胜率={kw:>5}%")


if __name__ == "__main__":
    main()
