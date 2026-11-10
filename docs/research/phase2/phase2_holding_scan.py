# -*- coding: utf-8 -*-
"""B 线冻结跑数：持有期 20/30/40 交易日均值回复检验（预注册 v1.0，shared/phase2_holding_preregistration_v1.0.md）
一行复现（确定性）：
    python "C:/Users/Magicer/.qoderwake/data/cloud-conversations/conv_01m70ebrxxpwxyvm0s3jhpcn4x/shared/phase2_holding_scan.py"

净额公式（预注册 §3，写死于此）：net = gross - 0.80，0.80 是整笔一次扣，绝不摊到每日。
摊薄只出现在 D 曲线的日度门槛列与年化拖累列（纯描述）。

跑数顺序（闸 0 不过即停手）：锚点 A（n=4028 且 毛=-1.842±0.05，08-11 窗、H=10、不顺延）
→ 锚点 B（全窗 H=10 成熟数=6394）→ B1 三格（顺延口径，块长=H bootstrap，三态判定）
→ D 曲线（统一队列=H40 成熟集，不顺延，描述）→（若 B1 有"通过"）B2（24×通过H数，α=0.05/m）。
metadata：block=H / B=10000 / seed=20261111，spawn 顺序 child[1..3]=B1(H20/30/40)，
child[4..11]=D(1,2,3,5,10,20,30,40)，child[12..35]=B2-H20×G01-24，[36..59]=B2-H30，[60..83]=B2-H40。
"""
import json, math, sqlite3, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

MAIN_DB = r"E:\小项目\Project\AIQuant\core\quant.db"
SHARED = Path(__file__).resolve().parent
L_ANCHOR = "2026-11-10"; L_B = "2026-11-10"; S_START = "2026-06-09"
SEED, B = 20261111, 10000
ALPHA_B1 = 0.05 / 3; Z_B1 = 2.1281
COSTS = (0.80, 0.70, 0.60)
GROSS_BASE = 1.0; GROSS_ROBUST = 1.63; MISS_FRAC = 0.02 / 0.98
EXT_MAX = 5; FLAG_MISS_STOP = 0.01
H_B1 = (20, 30, 40); H_D = (1, 2, 3, 5, 10, 20, 30, 40)
JUDGE_MONTHS_A = ("2026-08", "2026-09", "2026-10", "2026-11")

con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

wl = {r["trade_date"] for r in con.execute(
    "SELECT trade_date FROM (SELECT trade_date, COUNT(*) c FROM daily_price GROUP BY trade_date) WHERE c>=4000")}
db_rows_lB = con.execute("SELECT COUNT(*) c FROM daily_price WHERE trade_date<=?", (L_B,)).fetchone()["c"]
db_max = con.execute("SELECT MAX(trade_date) m FROM daily_price").fetchone()["m"]

codes = {r["code"] for r in con.execute("SELECT DISTINCT code FROM stock_deep_signal WHERE level='buy'")}
series = {}
for c in sorted(codes):
    rows = con.execute("SELECT trade_date, close FROM daily_price WHERE code=? AND trade_date<=? AND close>0 ORDER BY trade_date",
                       (c, L_B)).fetchall()
    series[c] = ([r["trade_date"] for r in rows], np.array([float(r["close"]) for r in rows]))
idx_map = {c: {d: i for i, d in enumerate(v[0])} for c, v in series.items()}
shares = {r["code"]: (r["total_shares"] or 0) for r in con.execute("SELECT code,total_shares FROM stock_info")}
eflags = {(r["code"], r["trade_date"]): (r["suspended"], r["one_word_up"], r["open_sealed_up"])
          for r in con.execute("SELECT code,trade_date,suspended,one_word_up,open_sealed_up FROM stock_daily_flags")}
xflags = {(r["code"], r["trade_date"]): (r["suspended"], r["one_word_down"], r["limit_down_sealed"])
          for r in con.execute("SELECT code,trade_date,suspended,one_word_down,limit_down_sealed FROM stock_daily_flags")}

sig = [dict(r) for r in con.execute(
    "SELECT scan_date,code,score,frac20,pct_above_ma20,volume_ratio,atr_pct,regime,obv_grad_pct "
    "FROM stock_deep_signal WHERE level='buy' AND scan_date>=? AND scan_date<=?", (S_START, L_B))]

drop = defaultdict(int)
rows = []
for s in sig:
    if s["scan_date"] not in wl: drop["白名单外"] += 1; continue
    f = eflags.get((s["code"], s["scan_date"]))
    if f is None or f[0] or f[1] or f[2]: drop["入场不可成交/无flag"] += 1; continue
    ds, cs = series.get(s["code"], ([], []))
    i = idx_map.get(s["code"], {}).get(s["scan_date"])
    if i is None: drop["无信号日行情"] += 1; continue
    sh = shares.get(s["code"], 0)
    rows.append(dict(s, i=i, nbars=len(ds), mv=(sh * cs[i] / 1e8 if sh else None), month=s["scan_date"][:7]))
print(f"[pool] rows={len(rows)} drops={dict(drop)}")

# ---------- 闸 0：锚点（v1.3 逐字口径，不顺延） ----------
def gross_plain(r, H):
    ds, cs = series[r["code"]]
    return (cs[r["i"] + H] / cs[r["i"]] - 1) * 100.0

anchorA = [r for r in rows if r["i"] + 10 < r["nbars"] and r["month"] in JUDGE_MONTHS_A]
a_n = len(anchorA); a_g = float(np.mean([gross_plain(r, 10) for r in anchorA]))
anchorB = [r for r in rows if r["i"] + 10 < r["nbars"]]
b_n = len(anchorB)
print(f"[anchorA] n={a_n} gross={a_g:.3f} (need 4028 / -1.842+-0.05)")
print(f"[anchorB] mature={b_n} (need 6394)")
if not (a_n == 4028 and abs(a_g + 1.842) <= 0.05 and b_n == 6394):
    Path(SHARED / "phase2_anchor_fail.json").write_text(json.dumps(
        {"anchorA": {"n": a_n, "gross": round(a_g, 3)}, "anchorB_n": b_n,
         "db_max": db_max, "db_rows_lB": db_rows_lB}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[STOP] anchor mismatch -> stop and report (prereg 4)")
    sys.exit(2)

ss = np.random.SeedSequence(SEED); child = ss.spawn(84)

def block_boot(vals_by_day, l_block, rng):
    days = sorted(vals_by_day)
    cvec = np.array([len(vals_by_day[d]) for d in days], float)
    svec = np.array([sum(vals_by_day[d]) for d in days], float)
    K = len(days); nb = max(1, math.ceil(K / l_block))
    starts = rng.integers(0, K, size=(B, nb))
    offs = np.arange(l_block)[None, None, :]
    idx = ((starts[:, :, None] + offs) % K).reshape(B, nb * l_block)[:, :K]
    means = svec[idx].sum(1) / cvec[idx].sum(1)
    return float((means <= 0).mean()), float(means.std(ddof=1)), K

def exit_ext(r, H):
    """顺延口径：返回 (status, gross, shift, flagmiss)。status ok / ext_dropped / exhausted。"""
    ds, cs = series[r["code"]]; code = r["code"]
    j = r["i"] + H
    if j >= r["nbars"]: return ("immature", None, 0, 0)
    shift = 0; miss = 0
    while True:
        xf = xflags.get((code, ds[j]))
        if xf is None:
            miss = 1
            break
        if not (xf[0] or xf[1] or xf[2]):
            break
        shift += 1
        if shift > EXT_MAX: return ("ext_dropped", None, shift, miss)
        j += 1
        if j >= r["nbars"]: return ("exhausted", None, shift, miss)
    return ("ok", (cs[j] / cs[r["i"]] - 1) * 100.0, shift, miss)

def cell_stats(sample, l_block, rng, alpha, z):
    n = len(sample)
    rec = {"n": n}
    if n == 0: return rec, None
    arr = np.array([r["gross"] for r in sample])
    gm = float(arr.mean()); rec["gross_mean"] = round(gm, 3)
    for C in COSTS: rec[f"net@{C}"] = round(gm - C, 3)
    mon = defaultdict(list)
    for r in sample: mon[r["month"]].append(r["gross"])
    rec["monthly"] = {m: (len(v), round(float(np.mean(v)), 2)) for m, v in sorted(mon.items())}
    nonempty = [m for m in sorted(mon) if len(mon[m]) >= 20]
    k = len(nonempty); pos = sum(1 for m in nonempty if float(np.mean(mon[m])) > 0)
    need_pos = math.ceil(0.75 * k) if k else 0
    rec["k_months"] = k; rec["months_pos"] = f"{pos}/{k}"; rec["months_pos_need"] = need_pos
    vbd = defaultdict(list)
    for r in sample: vbd[r["scan_date"]].append(r["gross"])
    p, se, K = block_boot(vbd, l_block, rng)
    rec["boot_p_le0"] = round(p, 5); rec["boot_se"] = round(se, 3)
    rec["MDG"] = round(z * se, 2)
    sigma = float(arr.std(ddof=1)) if n > 1 else float("nan")
    rec["sigma_trade"] = round(sigma, 3)
    rec["N_eff"] = round((sigma / se) ** 2, 1) if se > 0 else None
    rec["indep_blocks"] = math.ceil(K / l_block); rec["K_days"] = K
    dl = math.ceil(n * MISS_FRAC); rec["sens_m30"] = round((gm * n + (-30.0) * dl) / (n + dl), 2)
    delta = sum(1 for r in sample if r["flagmiss_exit"])
    rec["exit_flag_missing"] = delta; rec["exit_flag_miss_rate"] = round(delta / n, 4)
    gates = {
        "n_ge100": n >= 100,
        "gross_gt_base": gm > GROSS_BASE,
        "robust_sens_gt1": rec["sens_m30"] > GROSS_BASE,
        "months_ok": k >= 3 and pos >= need_pos,
        "boot_pass": p < alpha,
        "costs_same": all(rec[f"net@{C}"] > 0 for C in COSTS),
    }
    rec["gates"] = gates
    if all(gates.values()): verdict = "通过"
    elif rec["MDG"] > 1.0 or k < 3 or n < 100: verdict = "不可判"
    else: verdict = "不通过"
    rec["verdict"] = verdict
    return rec, verdict

# ---------- B1：三格，顺延口径 ----------
b1, samples_by_H = [], {}
for hi, H in enumerate(H_B1):
    sample, immature_by_month, ext_drop, exhausted, shifted = [], defaultdict(int), 0, 0, 0
    for r in rows:
        st, g, sh, miss = exit_ext(r, H)
        if st == "immature": immature_by_month[r["month"]] += 1; continue
        if st == "ext_dropped": ext_drop += 1; continue
        if st == "exhausted": exhausted += 1; continue
        if sh > 0: shifted += 1
        sample.append(dict(r, gross=g, shift=sh, flagmiss_exit=miss))
    samples_by_H[H] = sample
    rec, verdict = cell_stats(sample, H, np.random.default_rng(child[1 + hi]), ALPHA_B1, Z_B1)
    mature_last = max((r["scan_date"] for r in sample), default=None)
    rec.update({"cell": f"B1-H{H}", "H": H, "alpha": round(ALPHA_B1, 4), "block": H,
                "signal_window": [S_START, mature_last], "shifted_trades": shifted,
                "ext_dropped": ext_drop, "exhausted": exhausted,
                "immature_drop_by_month": dict(sorted(immature_by_month.items())),
                "gross_robust_line": GROSS_ROBUST})
    b1.append(rec)
    hi_rate = rec.get("exit_flag_miss_rate", 0) or 0
    if rec["n"] and hi_rate > FLAG_MISS_STOP:
        print(f"[STOP] exit-flag missing rate {hi_rate:.3%} > 1% in B1-H{H} (C1)")
        Path(SHARED / "phase2_holding_scan_partial.json").write_text(
            json.dumps({"b1": b1, "stop_reason": "C1_flag_miss_rate"}, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(3)
    print(f"[B1] H={H} n={rec['n']} gross={rec.get('gross_mean')} MDG={rec.get('MDG')} N_eff={rec.get('N_eff')} verdict={verdict}")

# ---------- D 曲线：统一队列（H=40 成熟），不顺延，描述 ----------
cohort = [r for r in rows if r["i"] + 40 < r["nbars"]]
curve = []
for di, H in enumerate(H_D):
    vals = [gross_plain(r, H) for r in cohort]
    byday = defaultdict(list)
    for r, g in zip(cohort, vals): byday[r["scan_date"]].append(g)
    p, se, K = block_boot(byday, max(1, H), np.random.default_rng(child[4 + di]))
    arr = np.array(vals); gm = float(arr.mean())
    mon = defaultdict(list)
    for r, g in zip(cohort, vals): mon[r["month"]].append(g)
    curve.append({"H": H, "n": len(cohort), "gross_mean": round(gm, 3), "net@0.80": round(gm - 0.80, 3),
                  "gross_per_day": round(gm / H, 4), "annual_cost_drag_pct": round(250 / H * 0.8, 2),
                  "boot_se_desc": round(se, 3), "boot_p_le0_desc": round(p, 5),
                  "monthly": {m: (len(v), round(float(np.mean(v)), 2)) for m, v in sorted(mon.items())},
                  "note": "descriptive-only, no alpha, no extension"})
    print(f"[D] H={H} gross={gm:.3f}")

# ---------- B2：仅当任一 B1「通过」 ----------
def num(v, f): return v is not None and f(v)
MASKS = [
 lambda r: num(r["score"], lambda v: v == 5),
 lambda r: num(r["score"], lambda v: v >= 6),
 lambda r: num(r["frac20"], lambda v: v < 0.5),
 lambda r: num(r["frac20"], lambda v: 0.5 <= v < 0.8),
 lambda r: num(r["frac20"], lambda v: v >= 0.8),
 lambda r: num(r["pct_above_ma20"], lambda v: v <= 3),
 lambda r: num(r["pct_above_ma20"], lambda v: v > 3),
 lambda r: num(r["volume_ratio"], lambda v: v < 1),
 lambda r: num(r["volume_ratio"], lambda v: 1 <= v < 2),
 lambda r: num(r["volume_ratio"], lambda v: v >= 2),
 lambda r: num(r["atr_pct"], lambda v: v < 2.5),
 lambda r: num(r["atr_pct"], lambda v: 2.5 <= v < 4.5),
 lambda r: num(r["atr_pct"], lambda v: v >= 4.5),
 lambda r: r["regime"] == "多头上升",
 lambda r: r["regime"] not in (None, "", "多头上升"),
 lambda r: num(r["mv"], lambda v: v < 30),
 lambda r: num(r["mv"], lambda v: 30 <= v <= 100),
 lambda r: num(r["mv"], lambda v: v > 100),
 lambda r: num(r["obv_grad_pct"], lambda v: v < 0),
 lambda r: num(r["obv_grad_pct"], lambda v: v >= 0),
 lambda r: num(r["score"], lambda v: v >= 6) and r["regime"] == "多头上升",
 lambda r: num(r["score"], lambda v: v >= 6) and r["regime"] not in (None, "", "多头上升"),
 lambda r: num(r["frac20"], lambda v: v < 0.5) and num(r["pct_above_ma20"], lambda v: v <= 3),
 lambda r: (r["frac20"] is not None and r["pct_above_ma20"] is not None
            and not (r["frac20"] < 0.5 and r["pct_above_ma20"] <= 3)),
]
NAMES = ["G01|score=5","G02|score>=6","G03|frac20<0.5","G04|frac20 0.5-0.8","G05|frac20>=0.8",
 "G06|ext<=3%","G07|ext>3%","G08|vr<1","G09|vr 1-2","G10|vr>=2",
 "G11|atr<2.5","G12|atr 2.5-4.5","G13|atr>=4.5","G14|多头上升","G15|非多头",
 "G16|mv<30亿","G17|mv 30-100亿","G18|mv>100亿","G19|obv<0","G20|obv>=0",
 "G21|>=6×多头上升","G22|>=6×非多头","G23|双低","G24|其余"]

passing_H = [rec["H"] for rec in b1 if rec.get("verdict") == "通过"]
b2, b2_status = [], "not_triggered"
if passing_H:
    m_b2 = 24 * len(passing_H); alpha_b2 = 0.05 / m_b2
    def phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
    lo, hi = 0.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if phi(mid) < 1 - alpha_b2: lo = mid
        else: hi = mid
    z_b2 = round((lo + hi) / 2, 4)
    ci = 12
    for H in sorted(passing_H):
        for gi, fn in enumerate(MASKS):
            sub = [r for r in samples_by_H[H] if fn(r)]
            rec, verdict = cell_stats(sub, H, np.random.default_rng(child[ci]), alpha_b2, z_b2)
            ci += 1
            rec.update({"cell": f"B2|H{H}|{NAMES[gi]}", "H": H, "alpha": alpha_b2, "block": H})
            b2.append(rec)
    b2_status = f"triggered on H={passing_H}, m_B2={m_b2}, alpha={round(alpha_b2,6)}, z={z_b2}"
    print(f"[B2] {b2_status}")
else:
    print("[B2] not triggered (no B1 pass) -> 结案路径")

# C4 单列：点估计>=1.0 但判不可判
c4 = [rec["cell"] for rec in b1 if rec.get("gross_mean", -99) >= GROSS_BASE and rec.get("verdict") == "不可判"]

meta = {"prereg": "phase2_holding_preregistration_v1.0.md",
        "unit": "block=H moving circular day-cluster", "B": B, "seed": SEED,
        "spawn_order": "child[1..3]=B1(H20/30/40); child[4..11]=D(1,2,3,5,10,20,30,40); child[12..35]=B2-H20xG01-24; [36..59]=B2-H30; [60..83]=B2-H40",
        "alpha_B1": round(ALPHA_B1, 4), "z_B1": Z_B1, "m_B2_formula": "24*passing_H",
        "L_anchor": L_ANCHOR, "L_B": L_B, "S_start": S_START,
        "anchorA": {"n": a_n, "gross": round(a_g, 3), "need": [4028, -1.842, 0.05]},
        "anchorB": {"mature": b_n, "need": 6394},
        "db_max": db_max, "db_rows_lB": db_rows_lB,
        "cost_bands": COSTS, "gross_line": {"base": GROSS_BASE, "robust_literal": GROSS_ROBUST,
                                            "robust_formula": "sens_m30>1.0"},
        "ext_rule": f"untradeable exit -> shift <={EXT_MAX} bars; beyond=drop",
        "pool_drops": dict(drop), "pool_rows": len(rows),
        "flag_miss_stop_line": FLAG_MISS_STOP, "c4_cells": c4}
report = {"metadata": meta, "B1": b1, "decay_curve_D": curve,
          "B2_status": b2_status, "B2": b2,
          "candidates_B1_pass": passing_H,
          "verdict_summary": ("B1 有通过 -> B2 已跑" if passing_H else
                              "B1 无「通过」格 -> kill line 单触发成立，结案，不进 B2（曲线形态只作描述）")}
Path(SHARED / "phase2_holding_scan_result.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n[DONE]", report["verdict_summary"])
print("B1:", [(r["cell"], r.get("n"), r.get("gross_mean"), r.get("MDG"), r.get("verdict")) for r in b1])
