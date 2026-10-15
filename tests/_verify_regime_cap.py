# -*- coding: utf-8 -*-
"""验证 RECO_REGIME_CAP 合并与各档不归零（2026-09-15 建，2026-10-14 更新）。

检查点：
  1. config 无 MID_LONG_REGIME_CAP 残留、RECO_REGIME_CAP 三档均为空
     （2026-10-14：展示层不再做任何 regime 截断）；
  2. /api/investor/today 各组数量不超过策略层上限（short≤3 / mid、long≤limit）；
  3. 三档截断表为空（纯逻辑）；
  4. 出场跟踪 perf.hidden_n == 0 —— 面板统计与用户可执行口径完全一致。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.strategy_params as sp

fails = []

# 1) 配置结构
assert not hasattr(sp, "MID_LONG_REGIME_CAP"), "MID_LONG_REGIME_CAP 应已删除"
tab = sp.RECO_REGIME_CAP
expect = {"short": {}, "mid": {}, "long": {}}   # 2026-10-14：三档均不截断
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
    # 展示层已不截断 ⇒ short 出 0 只只可能是策略层所致（走弱闸门只留隔日动量，
    # 当日没有动量信号就是 0）。这里只提示、不算失败；真正的不变量见 [4]。
    if pool["short"] and len(g["short"]) == 0:
        print("    note: cold 下 short=0（当日无隔日动量信号或门控未过，"
              "属策略层产出而非展示层截断）")
    if len(g["short"]) > 3:
        fails.append("short 超过 short_top_n=3")
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

# 三档均不再做 regime 截断（2026-10-14，展示层 = 策略层产出）
for hz in ("short", "mid", "long"):
    if tab.get(hz):
        fails.append(f"{hz} 应无 regime 截断，实际 {tab[hz]}")

# 4) 真正的不变量：出场跟踪里不存在「今日推荐当天不会推给你」的笔
#    （展示层截断清空后，面板统计 = 用户可执行口径；hidden_n>0 说明某处又分叉了）
from core.services.exit_tracking import build_exit_advice_payload

_p = build_exit_advice_payload()["summary"]["perf"]
print("[4] 出场跟踪 hidden_n（应为 0）：" +
      " ".join(f"{hz}={_p[hz].get('hidden_n')}" for hz in ("short", "mid", "long")))
for hz in ("short", "mid", "long"):
    if (_p[hz].get("hidden_n") or 0) > 0:
        fails.append(f"{hz} 出场跟踪仍有 {_p[hz]['hidden_n']} 笔展示层不会推荐"
                     "（RECO_REGIME_CAP 应为空，或某处重新引入了截断）")

if fails:
    print("\nFAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("\nALL PASS")
