# 个股深度「止损」两条计算路径的口径分歧（立项）

> 状态：**待办（未修）** · 立项 2026-09-14 · 由 `deep_atr_stop_cap=8%` 落地时发现
> 结论一句话：**同一个「深析止损」有两个独立实现，一处有摆动低点兜底、一处没有；
> 实测当前分歧极小（80% 完全相同，20% 差 ≤0.38pp），但这是"暂时被 8% 上限吸收"的巧合，
> 不是设计一致 —— 属于会随参数改动而重新张开的隐患。**

---

## 1. 事实：两个实现

| | A. `_signal_plan_at` | B. `_current_signal` |
|---|---|---|
| 位置 | `strategy/stock_deep.py` L703-723 | 同文件 L920-960 |
| 止损公式 | `round(close − 2.5×ATR, 2)` → `_deep_stop_cap` | `close − 2.5×ATR` → **`max(·, swing_low)`** → `round(·,2)` → `_deep_stop_cap` |
| 摆动低点兜底 | **无** | **有**（`stop = max(stop, swing_low)`，L953-954） |
| 产出 | `recent_advice(df, rhythm, days)` → 逐日计划行 | `signal` → 落库 + 面板当日建议 |
| 下游 | K 线**历史计划虚线**、面板「近 N 日建议」、`compute_position_stats` | `_scan_one` → **`stock_deep_signal.stop_loss`** → **deep_tracker 跟踪单**；个股深度面板当日卡片 |

调用链（`analyze_stock`，L1038-1066）：

```
trend    = trend_state(df)
rhythm   = detect_rhythm(df)
signal   = _current_signal(df, trend, volprice, rhythm)      # ← B，落库/跟踪单
...
advice   = recent_advice(df, rhythm, days=recent_days)       # ← A，K线历史计划
```

因此**同一天**在界面上会有两个止损数字：

- K 线虚线（历史计划，路径 A）——较宽（纯 ATR）
- 当日卡片 / 跟踪单 / 扫描候选表（路径 B）——被 `swing_low` 抬高，较窄

用户读到的就是「K 线说止损在 x，跟踪单却是 y」。

## 2. `swing_low` 是什么（决定了它多容易绑定）

`detect_rhythm(df)`（L169）里 `swing_low = float(np.nanmin(close))`（L273），
`df` 是 `analyze_stock` 的整段回看窗口（`DEEP_LOOKBACK = 260` 根 ≈ 1 年）。

→ 它**不是**近期摆动低点，而是**一年内最低收盘价**。

绑定条件：`close − 2.5×ATR < swing_low < close`
（即：一年最低收盘价落在「ATR 止损位」与「现价」之间）。

- 一只年内翻倍的票：`swing_low` 远低于 ATR 止损位 → **不绑定**（多数情况）
- 一只刚创出年内新低的票：`swing_low ≈ close`，不满足 `swing_low < close` → **不绑定**
- 一只「一年低点就在近旁、但已小幅反弹」的票：**绑定**，且止损被抬到 `swing_low`
  → 可能出现**极窄止损**（例：close=100、ATR=8 → ATR 止损 80；若 `swing_low=97`，
  止损变 97，宽度仅 3%）。`_deep_stop_cap` 只收紧不放松，**不会**把这个过窄止损拉回来。

⚠ 这是本次立项里最需要澄清的一点：`swing_low` 兜底的**设计意图**是「止损不深于近期低点」，
但落地用的是**一年最低收盘**，对「刚脱离年内低点」的票会把止损压到贴脸位置。

## 3. 实测：当前分歧有多大

口径：最新扫描日 **2026-09-14**，`stock_deep_signal` 中 `level ∈ (buy, add)` 按 Q12 键排序前 **60** 只，
用 `analyze_stock(..., recent_days=3)` 同时取两路（`recent_advice[-1]` vs `signal.action_plan`），
比同一交易日的止损价与宽度。复现见 §5。

| 指标 | 结果 |
|---|---|
| 可比样本 | 60 / 60 |
| 两路**完全一致** | **48 只（80.0%）** |
| 不一致 | 12 只（20.0%），价差仅 **±0.01 ~ 0.02 元** |
| 宽度差 | 中位 **0.00pp**、P90 +0.19pp、区间 **−0.38 ~ +0.19pp** |
| 方向 | 计划更宽 6 只 / 当日更宽 6 只（**对称**） |

**解读**：这 60 只里 `swing_low` **从未绑定** —— 差异方向对称、量级落在分位舍入上，
更像两处各自 `round` 与 ATR 取数顺序不同，而不是单边的「兜底抬高」。
所以这次实测**不能**证明兜底无害，只能说：在当前候选池 + `cap=8%` 下它没有被触发。

## 4. 为什么现在几乎看不出来（以及它为什么会重新张开）

`2.5×ATR%` 的原宽度中位是 **10.2%**，而 `deep_atr_stop_cap = 8%` 是它的**上限（收紧）**：
今天起绝大多数买入候选的止损被钉死在 8% 宽度上，两路算出的都是同一个 `round(entry×0.92, 2)`
→ 分歧被 cap **吸收**了。换句话说：

> 2026-09-14 落地的深析止损上限，顺带把这条口径分歧遮住了。

一旦发生下面任一变动，分歧会立刻重新张开，且**没有任何告警**：

- 调 `deep_atr_stop_cap`（放宽到 12%/15%，回到 cap 不绑定的区间）
- 调 `stop_mult`（`2.5`）或换 ATR 口径
- 候选池风格漂移（更多「刚脱离年内低点」的票进入 buy/add）

## 5. 复现命令

```bash
PY=/c/Users/Magicer/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe
# 见 logs/_swinglow_gap.txt（本次实测输出）
"$PY" - <<'EOF'
from core.db import get_conn
from strategy.stock_deep import analyze_stock, candidate_order_by
with get_conn() as c:
    D = c.execute("SELECT MAX(scan_date) d FROM stock_deep_signal").fetchone()["d"]
    ob = candidate_order_by(c)
    rows = [dict(r) for r in c.execute(
        f"SELECT code,name,level,entry_price,stop_loss FROM stock_deep_signal "
        f"WHERE scan_date=? AND level IN ('buy','add') AND entry_price>0 AND stop_loss>0 "
        f"ORDER BY {ob} LIMIT 60", (D,)).fetchall()]
for r in rows:
    with get_conn() as c:
        res = analyze_stock(c, r["code"], lookback=260, recent_days=3)
    ap = (res.get("signal") or {}).get("action_plan") or {}
    last = (res.get("recent_advice") or [{}])[-1]
    print(r["code"], r["name"], ap.get("stop_loss"), last.get("stop_loss"))
EOF
```

## 6. 待办

- [ ] **T1 量化历史绑定率（先做）**：新建 `tools/eval_deep_stop_paths.py`，在全部 ~70 个扫描日
      （2026-06-09~09-14）上对每个 buy/add 候选同时算 A/B 两路，统计
      ① 不一致比例 ② `swing_low` 实际绑定的比例 ③ 绑定时的止损宽度分布（找"贴脸止损"极端值）。
      → 产出「是否需要动代码」的判据。**若绑定率 <1% 且极值可控，可降级为文档说明（方案 C）。**
- [ ] **T2 决策**：按 T1 结果三选一
      - **方案 A（推荐）**：把止损抽成**单一函数** `_deep_stop(close, atr, swing_low, entry, level)`，
        A/B 两处共用（`_signal_plan_at` 也传当日 `swing_low`）。历史计划需注意：每根蜡烛要用
        **该日为止的 rolling 最低收盘**，不能用今日值（否则是未来函数）。
      - **方案 B**：给 `_signal_plan_at` 也加兜底 —— 必须解决上面的 rolling 问题，风险高于 A。
      - **方案 C**：承认二者语义不同（历史计划 = 纯 ATR 计划；当日 = 加摆动低点保护），
        不动代码，改为在 K 线提示文案里显式标注两套口径。
- [ ] **T3 附带澄清**：`swing_low` 是否应改成**近期（如 20/60 日）最低价**而非一年最低收盘？
      现行语义在「刚脱离年内低点」的票上会给出贴脸止损，建议一并评估；若改，
      影响面覆盖 A/B 两路 + 面板文案，需单独回测。

## 7. 与本次 ATR 改造的关系

- 本次只做了**止损宽度上限**（`_deep_stop_cap`），A/B 两处都接上了，**未引入新的分歧方向**。
- 但两处的**前置公式**依旧各自一份 —— 这正是本立项要消除的东西。
- 关联：`reports/deep_atr_stop_cap_rollout_2026-09-14.md`、`reports/atr_followups_2026-09-14.md`
