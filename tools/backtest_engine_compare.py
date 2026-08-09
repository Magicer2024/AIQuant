"""
backtest_engine_compare.py —— v1(已反弹) vs v2(买回踩) vs v2+放宽闸门 全链路回测对照

复刻 core/sync.py::_build_signal_records 的纯抄底落库逻辑（fuse + 4过滤），
仅切换 抄底打分函数(bottom_fn) 与 趋势闸门(gate) 两个变量，其余完全对齐线上：

  候选 = (FUSION_SCORE>=15) & 质量(qual) & 闸门(gate) & 追高(chase) & 扩展度(ext)
  FUSION_SCORE = BUY_SCORE × (10/3) × 5   （PURE_BOTTOM_WEIGHTS=[0,0,0,1,0], mode=max）
  每日推荐 = 候选池内按 FUSION_SCORE 降序取 Top8（对齐读取端 ORDER BY DESC LIMIT 24→剔avoid→截前8；
            当前止损-5%/止盈+8% 盈亏比1.6>1.5 不过 avoid，故直接取前8）

三组：
  v1-strict : strategy_bottom_fishing（奖励"已反弹5%"）+ 严格闸门(close>MA20 & MA20↑)
  v2-strict : strategy_bottom_fishing_v2（买回踩甜蜜区）+ 严格闸门  ← 现状(已转正)
  v2-relaxed: strategy_bottom_fishing_v2               + 放宽闸门(close>=MA20×0.98 & MA20↑)

收益口径：T+1 开盘买入(OC, 现实可买) → T+1/T+3/T+5 收盘收益，多 cohort 聚合。
附加：v2 候选按 FUSION 分高/中/低三档，验证"融合分排序反向"质疑是否成立。

纯读取，不写库。
"""
import sys, os, sqlite3
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from strategy.strategies import strategy_bottom_fishing, strategy_bottom_fishing_v2
from strategy.rec_filters import (quality_series, trend_gate_series,
                                  chase_filter_series, extension_filter_series)
from config.strategy_params import (PURE_BOTTOM_WEIGHTS, SHORT_TREND_GATE,
                                    CHASE_FILTER, EXTENSION_FILTER)

DB = os.path.join(PROJECT_ROOT, "core", "quant.db")
SIG = 15.0          # 融合分阈值（默认，无高波动调整）
MA_N = 20
SLOPE = 5
RELAX_TOL = 0.02    # 放宽闸门：允许回踩至 MA20 下方 2%
TOPN = 8

conn = sqlite3.connect(DB)

print("加载 daily_price ...")
df_all = pd.read_sql(
    "SELECT code, trade_date, open, high, low, close, volume, amount, pct_change "
    "FROM daily_price", conn)
df_all["trade_date"] = pd.to_datetime(df_all["trade_date"])
groups = {c: g.sort_values("trade_date").reset_index(drop=True)
          for c, g in df_all.groupby("code")}
print(f"  股票数 = {len(groups)}, 总行数 = {len(df_all)}")

print("加载 stock_info.total_shares ...")
ts = {r[0]: (r[1] if r[1] and r[1] > 0 else None)
      for r in conn.execute("SELECT code, total_shares FROM stock_info").fetchall()}
conn.close()

def fusion_from_buyscore(bs: pd.Series) -> pd.Series:
    return bs * (10.0 / 3.0) * 5.0   # 0~3 → 0~50

def relaxed_gate(df: pd.DataFrame) -> pd.Series:
    close = df["close"].astype(float)
    ma20 = close.rolling(MA_N).mean()
    up = ma20 >= ma20.shift(SLOPE)
    return ((close >= ma20 * (1 - RELAX_TOL)) & up).reindex(df.index).fillna(False)

def new_acc():
    return {"sum": 0.0, "n": 0, "pos": 0}

def add(acc, v):
    acc["sum"] += v; acc["n"] += 1; acc["pos"] += (1 if v > 0 else 0)

def stat(acc):
    return (acc["pos"] / acc["n"] * 100, acc["sum"] / acc["n"]) if acc["n"] else (float("nan"), float("nan"))

ACC_KEYS = ["oc1", "oc3", "oc5", "ma20dev"]
def blank_group():
    return {k: new_acc() for k in ACC_KEYS}

acc = {"v1": blank_group(), "v2": blank_group(), "v2r": blank_group()}
n_cand = {"v1": 0, "v2": 0, "v2r": 0}
n_rec = {"v1": 0, "v2": 0, "v2r": 0}

# 全局分档收集（验证"融合分排序反向"）：当前阈值下每日候选稀疏（多数日子<3个），
# 按 scan_date 内分档样本稀薄无意义，改用 全局按 fusion 绝对值分高/中/低三档。
cand_v1_all = []   # (fusion, oc1, oc3, oc5, ma20dev%)
cand_v2_all = []

def collect_global(mask, fusion, g, dev, sink):
    if not mask.any():
        return
    for di in g.index[mask]:
        idx = g.index.get_loc(di)
        oc = eval_oc(g, idx)
        if oc is None:
            continue
        sink.append((float(fusion.loc[di]), oc[0], oc[1], oc[2], float(dev.iloc[idx] * 100.0)))

def top8_recs(mask, fusion, g):
    """候选 mask → 按交易日分组、组内按 fusion 降序取 TopN 的推荐索引列表"""
    if not mask.any():
        return []
    sub_fusion = fusion[mask]
    recs = []
    for _, grp in sub_fusion.groupby(level=0):
        top = grp.sort_values(ascending=False).head(TOPN)
        recs.extend(top.index.tolist())
    return recs

def eval_oc(g, idx):
    """idx 为信号日位置，返回 (oc1,oc3,oc5) 或 None（无足够后续）"""
    if idx + 5 >= len(g):
        return None
    base = g["open"].iloc[idx + 1]
    if not base or base <= 0:
        return None
    return tuple((g["close"].iloc[idx + off] / base - 1.0) * 100.0 for off in (1, 3, 5))

print("开始回测（v1/v2/v2r 三组并行复算）...")
n_stocks = 0
for code, g in groups.items():
    g = g.copy()
    g.index = g["trade_date"]
    if len(g) < 30:
        continue
    n_stocks += 1

    b1 = strategy_bottom_fishing(g)["BUY_SCORE"]
    b2 = strategy_bottom_fishing_v2(g)["BUY_SCORE"]
    f1 = fusion_from_buyscore(b1)
    f2 = fusion_from_buyscore(b2)

    q = quality_series(code, g, ts.get(code))
    gs = trend_gate_series(g)
    gr = relaxed_gate(g)
    ch = chase_filter_series(g)
    ex = extension_filter_series(g)
    ma20 = g["close"].astype(float).rolling(MA_N).mean()
    dev = (g["close"] / ma20 - 1.0).fillna(0.0)

    cand_v1 = (f1 >= SIG) & q & gs & ch & ex
    cand_v2 = (f2 >= SIG) & q & gs & ch & ex
    cand_v2r = (f2 >= SIG) & q & gr & ch & ex

    n_cand["v1"] += int(cand_v1.sum())
    n_cand["v2"] += int(cand_v2.sum())
    n_cand["v2r"] += int(cand_v2r.sum())

    for tag, mask, fusion, grp_acc in (("v1", cand_v1, f1, acc["v1"]),
                                       ("v2", cand_v2, f2, acc["v2"]),
                                       ("v2r", cand_v2r, f2, acc["v2r"])):
        recs = top8_recs(mask, fusion, g)
        for di in recs:
            idx = g.index.get_loc(di)
            oc = eval_oc(g, idx)
            if oc is None:
                continue
            add(grp_acc["oc1"], oc[0]); add(grp_acc["oc3"], oc[1]); add(grp_acc["oc5"], oc[2])
            add(grp_acc["ma20dev"], dev.iloc[idx] * 100.0)
            n_rec[tag] += 1

    # 全局分档收集（对比 v1/v2 排序方向性）
    collect_global(cand_v1, f1, g, dev, cand_v1_all)
    collect_global(cand_v2, f2, g, dev, cand_v2_all)

print(f"  处理股票数 = {n_stocks}\n")

def show(tag, label):
    a = acc[tag]
    w1, m1 = stat(a["oc1"]); w3, m3 = stat(a["oc3"]); w5, m5 = stat(a["oc5"])
    wd, md = stat(a["ma20dev"])
    print(f"  {label:<18} 候选{n_cand[tag]:>7}  推荐{n_rec[tag]:>7}  "
          f"T+1 {w1:>5.1f}%/{m1:>+6.3f}%  "
          f"T+3 {w3:>5.1f}%/{m3:>+6.3f}%  "
          f"T+5 {w5:>5.1f}%/{m5:>+6.3f}%  "
          f"候选MA20偏离 {md:>+5.2f}%")

print("=" * 78)
print("一、每日 Top8 推荐真实表现（OC 开盘买，多 cohort 聚合）")
print("=" * 78)
show("v1", "v1 已反弹+严格")
show("v2", "v2 买回踩+严格(现状)")
show("v2r", "v2 买回踩+放宽闸")

print()
print("=" * 78)
print("二、候选按 FUSION_SCORE 全局分三档（验证\"融合分排序反向\"，v1 vs v2 对比）")
print("=" * 78)
print("  （当前阈值下每日候选稀疏，按 scan_date 内分档样本不足，故用全局分档；")
print("   高分=融合分最高1/3，低分=最低1/3。若高分档 T+3/T+5 更优 → 排序正向）\n")

def show_global(lst, label):
    if not lst:
        print(f"  [{label}] 无数据")
        return
    lst.sort(key=lambda x: x[0])
    k = len(lst) // 3
    print(f"  [{label}]  (n={len(lst)})")
    for tag, grp in (("hi 高", lst[-k:]), ("mid 中", lst[k:-k]), ("lo 低", lst[:k])):
        oc1 = [x[1] for x in grp]; oc3 = [x[2] for x in grp]; oc5 = [x[3] for x in grp]; md = [x[4] for x in grp]

        def st(L):
            return (sum(1 for v in L if v > 0) / len(L) * 100, sum(L) / len(L))
        w1, m1 = st(oc1); w3, m3 = st(oc3); w5, m5 = st(oc5); _, mm = st(md)
        print(f"    {tag}档(n={len(grp):>6})  T+1 {w1:>5.1f}%/{m1:>+6.3f}%  "
              f"T+3 {w3:>5.1f}%/{m3:>+6.3f}%  T+5 {w5:>5.1f}%/{m5:>+6.3f}%  "
              f"MA20偏离 {mm:>+5.2f}%")

show_global(cand_v1_all, "v1 已反弹引擎")
show_global(cand_v2_all, "v2 买回踩引擎(现状)")

print("\n  解读：")
print("   - v1 下『高融合分』档 T+3/T+5 若不优于『低融合分』→ 印证用户『融合分把后期会涨的票排后面』质疑；")
print("   - v2 下『高融合分』档 T+3/T+5 若最优 → 说明 v2 已修复排序方向性，质疑在现状下不成立。")

print("\n完成。")
