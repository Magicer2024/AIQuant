# -*- coding: utf-8 -*-
"""B 线结案补充：指数远期对照（管理A seq=248 指令，描述性对照，不占 α、不改判定）
口径：每个信号月按样本「等权平均入场日」（trade-weighted 平均 index_daily 位置，四舍五入到交易日）
起算，报 中证1000(000852) 与 沪深300(000300) 的 H 日远期收益；同时给出逐笔匹配均值作交叉校验。
D 曲线段：统一队列（H=40 成熟集）的等权平均入场日起算，H∈{1,2,3,5,10,20,30,40}。
池侧样本成员与 phase2_holding_scan.py 完全同路径（同过滤链、同顺延规则），只加索引列。
一行复现：python ".../shared/phase2_index_control.py"
"""
import json, math, sqlite3
from collections import defaultdict
from pathlib import Path
import numpy as np

MAIN_DB = r"E:\小项目\Project\AIQuant\core\quant.db"
SHARED = Path(__file__).resolve().parent
L_B = "2026-11-10"; S_START = "2026-06-09"; EXT_MAX = 5
H_B1 = (20, 30, 40); H_D = (1, 2, 3, 5, 10, 20, 30, 40)
IDX = {"CSI1000": "000852", "CSI300": "000300"}

con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

wl = {r["trade_date"] for r in con.execute(
    "SELECT trade_date FROM (SELECT trade_date, COUNT(*) c FROM daily_price GROUP BY trade_date) WHERE c>=4000")}
codes = {r["code"] for r in con.execute("SELECT DISTINCT code FROM stock_deep_signal WHERE level='buy'")}
series = {}
for c in sorted(codes):
    rows = con.execute("SELECT trade_date, close FROM daily_price WHERE code=? AND trade_date<=? AND close>0 ORDER BY trade_date",
                       (c, L_B)).fetchall()
    series[c] = ([r["trade_date"] for r in rows], np.array([float(r["close"]) for r in rows]))
idx_map = {c: {d: i for i, d in enumerate(v[0])} for c, v in series.items()}
eflags = {(r["code"], r["trade_date"]): (r["suspended"], r["one_word_up"], r["open_sealed_up"])
          for r in con.execute("SELECT code,trade_date,suspended,one_word_up,open_sealed_up FROM stock_daily_flags")}
xflags = {(r["code"], r["trade_date"]): (r["suspended"], r["one_word_down"], r["limit_down_sealed"])
          for r in con.execute("SELECT code,trade_date,suspended,one_word_down,limit_down_sealed FROM stock_daily_flags")}
sig = [dict(r) for r in con.execute(
    "SELECT scan_date,code FROM stock_deep_signal WHERE level='buy' AND scan_date>=? AND scan_date<=?", (S_START, L_B))]

rows = []
for s in sig:
    if s["scan_date"] not in wl: continue
    f = eflags.get((s["code"], s["scan_date"]))
    if f is None or f[0] or f[1] or f[2]: continue
    i = idx_map.get(s["code"], {}).get(s["scan_date"])
    if i is None: continue
    rows.append(dict(s, i=i, nbars=len(series[s["code"]][0]), month=s["scan_date"][:7]))

ix = {}
for nm, code in IDX.items():
    rs = con.execute("SELECT trade_date, close FROM index_daily WHERE code=? AND trade_date<=? ORDER BY trade_date",
                     (code, L_B)).fetchall()
    ds = [r["trade_date"] for r in rs]; cs = np.array([float(r["close"]) for r in rs])
    ix[nm] = (ds, cs, {d: k for k, d in enumerate(ds)})

def idx_fwd(nm, anchor_date, H):
    ds, cs, m = ix[nm]
    if anchor_date not in m:
        k = max((d for d in m if d <= anchor_date), default=None)
        if k is None: return None, None
        pos = m[k]
    else:
        pos = m[anchor_date]
    if pos + H >= len(cs): return None, None
    return (cs[pos + H] / cs[pos] - 1) * 100.0, ds[pos]

def idx_pos(nm, d):
    ds, cs, m = ix[nm]
    if d in m: return m[d]
    cands = [v for v in m.values() if ds[v] <= d]
    return max(cands) if cands else None

def ext_entry_pos(r, H):
    ds, cs = series[r["code"]]; code = r["code"]
    j = r["i"] + H
    if j >= r["nbars"]: return None
    shift = 0
    while True:
        xf = xflags.get((code, ds[j]))
        if xf is None or not (xf[0] or xf[1] or xf[2]): return j, shift
        shift += 1
        if shift > EXT_MAX: return None
        j += 1
        if j >= r["nbars"]: return None

out = {"caliber": "trade-weighted mean entry day per signal month (rounded to nearest trading day), index forward H-bar close-to-close",
       "coarse_check": {}}
for nm in IDX:
    ds, cs, m = ix[nm]
    p0 = m.get("2026-06-01") or max(v for v in m.values() if ds[v] <= "2026-06-01")
    out["coarse_check"][nm] = {
        "06-01_to_11-10_pct": round((cs[-1] / cs[p0] - 1) * 100, 2),
        "06-01_+20d_pct": round((cs[p0 + 20] / cs[p0] - 1) * 100, 2) if p0 + 20 < len(cs) else None}

for H in H_B1:
    per = {}
    for r in rows:
        res = ext_entry_pos(r, H)
        if res is None: continue
        j, sh = res
        sd = series[r["code"]][0][j]  # not used; entry day = scan_date
        per.setdefault(r["month"], []).append(r["scan_date"])
    tbl = {}
    for mth, sds in sorted(per.items()):
        # equal-weight mean entry day on CSI1000 trading calendar
        posv = [idx_pos("CSI1000", d) for d in sds]
        posv = [p for p in posv if p is not None]
        p_avg = int(round(float(np.mean(posv))))
        anchor = ix["CSI1000"][0][p_avg]
        row = {"n": len(sds), "mean_entry_day": anchor}
        for nm in IDX:
            v, used = idx_fwd(nm, anchor, H)
            row[f"{nm}_fwd{H}"] = None if v is None else round(v, 2)
            # per-trade matched mean (cross-check)
            vs = [idx_fwd(nm, d, H)[0] for d in sds]
            vs = [v2 for v2 in vs if v2 is not None]
            row[f"{nm}_matched{H}"] = round(float(np.mean(vs)), 2) if vs else None
        tbl[mth] = row
    out[f"B1_H{H}_by_month"] = tbl

# D curve: unified cohort (i+40 < nbars), mean entry day, index forward at each H
cohort_dates = [r["scan_date"] for r in rows if r["i"] + 40 < r["nbars"]]
posv = [idx_pos("CSI1000", d) for d in cohort_dates]
p_avg = int(round(float(np.mean([p for p in posv if p is not None]))))
anchor = ix["CSI1000"][0][p_avg]
out["D_curve"] = {"cohort_n": len(cohort_dates), "mean_entry_day": anchor, "points": {}}
for H in H_D:
    e = {}
    for nm in IDX:
        v, _ = idx_fwd(nm, anchor, H)
        vs = [idx_fwd(nm, d, H)[0] for d in cohort_dates]
        vs = [v2 for v2 in vs if v2 is not None]
        e[f"{nm}_fwd{H}"] = None if v is None else round(v, 2)
        e[f"{nm}_matched{H}"] = round(float(np.mean(vs)), 2) if vs else None
    out["D_curve"]["points"][str(H)] = e

Path(SHARED / "phase2_index_control.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
