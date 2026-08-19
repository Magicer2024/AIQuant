"""tools/eval_lhb_surge_boost.py —— 大阳突破（C3 口径）× 龙虎榜净买 交集子集验证

目的：确认龙虎榜数据对「次日强势观察」信号线的增强效果：
  - 大阳突破当日是否常上龙虎榜（交集覆盖率）
  - 交集子集（上榜且净买>0 / 净买占比>=10%）的 OC 胜率/均值/大涨率是否优于未上榜部分
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from core.db import get_conn
from tools.mine_surge_next_day import load_daily, add_features, add_regime, build_signals

SPLIT = "2026-01-25"

df = load_daily("2024-07-01")
df = add_regime(add_features(df))
sig = build_signals(df)["C3 大阳突破+市值<150亿"]
v = df[sig].dropna(subset=["nd_oc"]).copy()
print(f"大阳突破(C3) 全样本: {len(v)} 条")

with get_conn() as conn:
    lhb = pd.read_sql_query(
        "SELECT trade_date, code, net_buy, net_buy_ratio, reason "
        "FROM stock_lhb_detail", conn)
lhb["trade_date"] = lhb["trade_date"].astype(str).str[:10]
lhb = lhb.drop_duplicates(subset=["trade_date", "code"])

v = v.merge(lhb, on=["code", "trade_date"], how="left")
v["on_lhb"] = v["net_buy"].notna()
v["net_pos"] = v["on_lhb"] & (v["net_buy"] > 0)
v["net10"] = v["on_lhb"] & (v["net_buy_ratio"] >= 10)


def stat(name, sub):
    if len(sub) < 10:
        print(f"  {name:<34} 样本 {len(sub)} (不足)")
        return
    days = sub["trade_date"].nunique()
    print(f"  {name:<34} n={len(sub):>4} 日均{len(sub)/days:4.1f} "
          f"OC胜率{(sub['nd_oc']>0).mean()*100:5.1f}% "
          f"均值{sub['nd_oc'].mean():+.2f}% "
          f"大涨率{(sub['nd_oc']>=5).mean()*100:4.1f}% "
          f"触涨停{(sub['nd_high_pct']>=9.7).mean()*100:4.1f}%")


for win_name, mask in [("训练窗", v["trade_date"] < SPLIT),
                       ("验证窗", v["trade_date"] >= SPLIT)]:
    w = v[mask]
    print(f"\n【{win_name}】（C3 大阳突破样本 {len(w)}）")
    stat("全部", w)
    stat("当日上榜(任意)", w[w["on_lhb"]])
    stat("上榜且净买>0", w[w["net_pos"]])
    stat("上榜且净买占比>=10%", w[w["net10"]])
    stat("上榜但净卖<=0", w[w["on_lhb"] & ~w["net_pos"]])
    stat("未上榜", w[~w["on_lhb"]])

print(f"\n交集覆盖率：大阳突破当日上榜比例 "
      f"{v['on_lhb'].mean()*100:.1f}%（全样本）")

# ── 硬过滤候选阈值对比（用户 2026-08-18 要求：宁缺毋滥）──
print("\n【硬过滤候选阈值对比（两窗均需优于基线才算达标）】")
tiers = [
    ("基线: 上榜且净买>0", v["net_pos"]),
    ("净买占比>=5%", v["net_pos"] & (v["net_buy_ratio"] >= 5)),
    ("净买>=3000万", v["net_pos"] & (v["net_buy"] >= 3e7)),
    ("净买>=5000万", v["net_pos"] & (v["net_buy"] >= 5e7)),
    ("净买>0 且 占比>=3%", v["net_pos"] & (v["net_buy_ratio"] >= 3)),
]
for win_name, mask in [("训练窗", v["trade_date"] < SPLIT),
                       ("验证窗", v["trade_date"] >= SPLIT)]:
    print(f"\n【{win_name}】")
    for name, tmask in tiers:
        stat(name, v[mask & tmask])
