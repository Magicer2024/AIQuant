# -*- coding: utf-8 -*-
"""验证 RECO_REGIME_CAP 合并与各档不归零（2026-09-15 建，2026-10-14 更新）。

检查点：
  1. config 无 MID_LONG_REGIME_CAP 残留、RECO_REGIME_CAP 结构正确
     （2026-10-14：short 改为不截断，与出场跟踪/复盘同口径）；
  2. /api/investor/today 在 regime=cold 下 short/mid/long 均出票（不再归零）；
  3. mid/long cool 收缩值与设计一致（2/1）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.strategy_params as sp

fails = []

# 1) 配置结构
assert not hasattr(sp, "MID_LONG_REGIME_CAP"), "MID_LONG_REGIME_CAP 应已删除"
tab = sp.RECO_REGIME_CAP
expect = {"short": {},              # 2026-10-14：不截断 → short_top_n
          "mid": {"cool": 2},
          "long": {"cool": 1}}
if tab != expect:
    fails.append(f"RECO_REGIME_CAP 结构不符: {tab}")
print("[1] RECO_REGIME_CAP =", tab)

from app import app
c = app.test_client()
r = c.get("/api/investor/today?limit=4")
d = r.get_json()["data"]
regime = d["market_regime"]
g = d["groups"]
print(f"[2] date={d['date']} regime={regime} count={d['count']} "
      f"short={len(g['short'])} mid={len(g['mid'])} long={len(g['long'])}")

# 当日各周期在 stock_signal 里是否真有候选：池子本来就空 ≠ 被展示层归零
from core.db import get_conn
with get_conn() as conn:
    pool = {hz: conn.execute(
        """SELECT COUNT(*) FROM stock_signal
           WHERE scan_date = ? AND COALESCE(horizon, 'short') = ?""",
        (d["date"], hz)).fetchone()[0]
        for hz in ("short", "mid", "long")}
print(f"    当日信号池 {pool}")

if regime == "cold":
    if pool["short"] and len(g["short"]) == 0:
        fails.append("cold 下 short 不应再归零（2026-10-14：走弱闸门已把抄底剔掉，"
                     "剩下的隔日动量是该档唯一正期望票，展示层不该再砍）"
                     "——若确认是门控/T1 过滤所致则属正常")
    if len(g["short"]) > 3:
        fails.append("cold 下 short 超过 short_top_n=3")
    if pool["mid"] and len(g["mid"]) == 0:
        fails.append("cold 下 mid 不应再归零（本次修复目标）")
    if pool["long"] and len(g["long"]) == 0:
        fails.append("cold 下 long 不应再归零（本次修复目标）")
    if len(g["mid"]) > 4 or len(g["long"]) > 4:
        fails.append("mid/long 超过 limit=4")

# 3) 每条 mid/long 卡片的基本完整性
for it in g["mid"] + g["long"]:
    for k in ("code", "name", "horizon", "fusion_score", "action_plan", "signal"):
        if k not in it:
            fails.append(f"卡片缺字段 {k}")
    if it["signal"]["level"] in ("avoid", "sell"):
        fails.append(f"{it['code']} level={it['signal']['level']} 应被剔除")

# 单组过滤参数仍工作
r2 = c.get("/api/investor/today?horizon=mid&limit=3")
d2 = r2.get_json()["data"]
print(f"[3] horizon=mid&limit=3 → mid={len(d2['groups']['mid'])}")
if len(d2["groups"]["mid"]) > 3:
    fails.append("horizon 单组 limit 未生效")

# 默认 mid/long cool 收缩映射（不触库，纯逻辑）
for hz, n in (("mid", 2), ("long", 1)):
    v = tab[hz].get("cool")
    if v != n:
        fails.append(f"{hz} cool 收缩应为 {n}")

if fails:
    print("\nFAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("\nALL PASS")
