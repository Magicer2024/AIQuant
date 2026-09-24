# 筹码优先排序 · 前向对照实验（运维手册）

> 状态：**步骤 ②③ 已落地（2026-09-16）**，线上排序**零改动**。
> 目标：用干净的前向样本外，验证「筹码集中度 `conc` 作第一排序键」是否真能提升短线推荐质量。
> 相关：`config/strategy_params.py::short_chip_sort_prioritize`（未开）、`docs/chip-peak-short-strategy.md`（因子诊断）

---

## 1. 为什么不直接上线

| 顾虑 | 事实 |
|---|---|
| 样本已被挖过 | 26k 历史样本经多轮挖掘，IS/OOS 切分**不再干净** |
| Δ 对样本期敏感 | 只含真实 turnover 的子样本 Δ **+0.87% → +0.47%**，成因是样本构成（占 32.7%、偏 2026 逆风期、基线仅 +0.11% vs 全样本 +0.31%） |
| 无法归因 | 若与市值收紧（3000亿→400亿）同批上线，无法判断收益来自哪一项 |

历史结论仍是：`conc↑, ext↑, fusion↓` 全期 **+0.32% → +1.05%**、样本外 **+0.91% → +1.34%**、止损率 **37.1% → 23.1%**。但它需要一份**没被挖过**的证据。

## 2. 每天自动发生什么

```
sync（每日）
  └─ _build_signal_records  → stock_signal.chip_conc      【步骤② 影子列】
       · 固定尾部窗口 tail(1500) 计算 conc，仅 short 写值
  └─ insert_new_outcomes（60 天滚动窗口）
       ├─ 基线 top3（现有排序）      → recommend_outcome
       └─ 筹码优先 top3（同 WHERE，只换 ORDER BY） → recommend_outcome_shadow
            · 起点 ≥ chip_ready（chip_conc 首次非 NULL 日）
            · UPDATE in_baseline 标记与基线的重叠
  └─ evaluate_outcomes("recommend_outcome_shadow")
       · 出场数学复用同一 _short_exit_sim，**不重写**
```

**唯一变量是排序键** —— 两组同日、同池、同过滤链（融合门槛/T1 闸门/大盘门控/观察线/gap guard/板块/ST）。

⚠ 前置：**必须重启 `start.bat`** 才会开始写入 `chip_conc`（历史行仍为 NULL，不回填、不动）。

## 3. 查进度

```bash
# 影子组累积情况（起止日期 / 条数 / 已出终态数）
python -c "import sqlite3;c=sqlite3.connect('core/quant.db');
print(c.execute('SELECT MIN(scan_date),MAX(scan_date),COUNT(*),SUM(evaluated_at IS NOT NULL) FROM recommend_outcome_shadow').fetchone())"

# chip_conc 是否已开始写入（0 = 还没重启或还没新信号）
python -c "import sqlite3;c=sqlite3.connect('core/quant.db');
print(c.execute(\"SELECT COUNT(*) FROM stock_signal WHERE COALESCE(horizon,'short')='short' AND chip_conc IS NOT NULL\").fetchone())"
```

## 4. 什么时候可以下结论

| 门槛 | 值 | 理由 |
|---|---|---|
| 交易日 | **≥ 20** | 按日配对检验需要足够配对，消日效应 |
| 每组已终态笔数 | **各 ≥ 40** | 少于则 Welch / 配对 t 无功效 |
| 同时看 | **MDE** | 报告⑤会给出"当前样本能识别的最小效应"。真实 Δ 若小于 MDE ⇒ **检不出来**，不是"没效果" |
| 单周压力 | 剔除最好/最差一周后 Δ 仍在 | 铁律 3 |

工具到门槛前会明确打印「**证据不足 —— 不得据此判定有效或无效**」。**不要把它读成"没效果"**。

## 5. 读报告的顺序（`python tools/eval_chip_shadow.py --refresh`）

1. **【① 重叠度】** —— 先看这个！重叠 100% = 排序没改变任何选票 = **实验没做**（不是结论）。重叠高时 Δ 的噪声会被放大。
2. **【② 逐笔口径】** —— 均值/中位/胜率/止损率/PF。`—` 表示样本未终态，不是 0。
3. **【③ 按日配对 t（主判据）】** —— 同日同池 ⇒ 配对消掉日效应，功效远高于非配对。**这一项才是主判据**。
4. **【④ 差集分解】** —— 纯影子（新进）vs 纯基线（被剔）。这才是"改排序动了哪几只"的真实表现。
5. **【⑤ 功效分析】** —— MDE + 还需多少交易日。
6. **【⑦ 单周压力】** —— 剔除最好/最差一周。

输出同时写入 `logs/_chip_shadow_eval.txt`。

## 6. 如果 Δ 站得住 → 上线清单（步骤 ④）

1. **历史回填 `stock_signal.chip_conc`** —— ⚠ **必须传 `as_of`**，否则取最新行情 = **未来函数**（证据：000651 最新 38.95 sell / as_of 08-20 41.42 add）。
2. **同步 `H.portfolio()`** 的硬编码 `ext ASC + fusion DESC` → 换成 `_short_order_keys(..., chip_first=True, ...)`，否则新旧结论**不可比**。
3. 置 `short_chip_sort_prioritize = 1`，重启 `start.bat`。
4. 检查三处出口是否同源（今日推荐 / 出场跟踪 / 复盘入库共用 `short_order_clause()`）。
5. **市值收紧（3000亿→400亿）另批单独评估**，不要与排序同批上。

## 7. 禁忌

- ❌ **不要中途开 `short_chip_sort_prioritize=1`** —— 基线会变，前向样本被污染，实验从头再来。
- ❌ **不要用历史回测数字替代前向结论** —— 那正是本次实验要规避的东西。
- ❌ **不要在样本不足时下方向性结论** —— 包括"看起来更好"。
- ⚠ 注意：`recommend_outcome` 是 **60 天滚动窗口**，`min(scan_date)` 会随之前移，属设计而非误删；前向实验的样本以 **`recommend_outcome_shadow`** 为准。

## 8. 自检

```bash
python tests/_verify_chip_shadow.py --write   # 55 PASS（含对齐性精确证明）
python tests/_probe_chip_shadow_overlap.py --days 90   # 重叠度体检
```

---
*本报告仅供研究参考，不构成个人投资建议*
