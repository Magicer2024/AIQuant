# 短线推荐「每日最多 4 / 最少 0」改造实施手册

> 目标：把当前"每天固定 8 个短线推荐"改为"每天最多 4 个、最少 0 个"，
> 在不牺牲盈利的前提下满足用户对短线数量的要求，并为冲"周 2% / 月 10% / 年 50%"打底。
> 本文档含**根因 + 回测证据 + 逐文件改法 + 验证步骤**，可直接交给执行同学落地。

---

## 0. 验收标准（Definition of Done）

1. `/api/investor/today` 在 **normal** 大盘下 short 组返回 **≤4** 条；**cool** 下 ≤2；**cold** 下 **0**。
2. 弱市 / 信号稀疏日，short 组可返回 **0**（即 `count=0, groups.short=[]`），不再强制凑数。
3. 改动后重跑 `tools/backtest_dynamic_count.py`，**S4 组合年化 ≥ +18%（回撤 ≤ -40%）**，且 short 组取数口径与回测一致（不可出现"推荐/回测分裂"）。
4. 不改变 mid / long 组的既有行为（本次只改短线）。

---

## 1. 根因（为什么"固定 8 个"必须改）

| 现象 | 根因 | 证据 |
|---|---|---|
| 固定 8 个 → 系统亏损 | 8 个相关头寸在同一波下跌中同时触发 -6% 止损，**几何收益被拖垮** | 回测 S0 Top8 年化 **-16.5%**、最大回撤 **-84.8%** |
| 砍到 4 个立刻转正 | 头寸数下降 → 同时止损概率下降 → 几何收益恢复 | S1 Top4 年化 **+10.5%**、回撤 **-46.8%** |
| "挂高位"体感 | 按融合分排序，**当日前 4 名单笔均值 -0.031%（最差），第 5-8 名反 +0.298%（最好）**；融合分最高 = 已涨最多 = 扩展度最高 = 未来空间最小 | `rank_buckets`：1-4 桶 mean -0.031% vs 5-8 桶 +0.298% |

**结论**：① 数量上限必须砍到 4（你的直觉正确）；② 不能"仍按融合分取前 4"——会拿到最差一批，必须**换选择标准**（高融合 ∩ 低扩展度）。

---

## 2. 回测证据（2020+ 全市场，v2 引擎 + E3 RSI 甜区 + 止损 -6% / 止盈 +10% / 持仓 3 日，满仓等权）

| 场景 | 取法 | 年化 CAGR | 最大回撤 | 周≥2%占比 | 月≥10%占比 | 笔数 |
|---|---|---|---|---|---|---|
| S0 基线 | Top8（融合分，现状） | **-16.5%** | -84.8% | 28.8% | 8.1% | 10585 |
| S1 截断 | Top4（融合分） | +10.5% | -46.8% | 32.1% | 12.2% | 5866 |
| S2 截断+置信 | Top4 + fusion≥22 | +14.1% | -48.2% | 31.1% | 14.9% | 4538 |
| S3 低扩展无门控 | Top4（按扩展度） | +2.9% | -78.8% | 33.3% | 10.8% | 5866 |
| **S4 最优** | **Top4（fusion≥22 ∩ 低扩展度）** | **+18.6%** | **-39.5%** | **36.2%** | **18.9%** | 4538 |

> 50% 年目标需每个 3 日周期均值 **+0.484%**，实测最优篮子仅 **+0.100%**（缺口约 5 倍）。
> 仅数量+选择调整到不了 50%，需后续接入第二条 alpha 线（龙虎榜动量），见第 6 节"超出范围"。

---

## 3. 改动总览

| 编号 | 改动 | 文件 | 优先级 | 对应回测场景 |
|---|---|---|---|---|
| A | `limit` 默认 8→4；clamp 允许 0；regime 天花板（cold→0 / cool→2 / normal→4） | `routes/investor.py` | P0 | S1 |
| B | 置信度门控 `fusion_score >= CONF_GATE`（默认 22），弱日自然出 0 | `config/strategy_params.py` + `routes/investor.py` | P0 | S2 |
| C | 短线换选择标准：先门控，再按 `pct_above_ma20 ASC`（低扩展度=未来空间最大）取前 N | `core/db.py`(DDL) + `core/sync.py`(落库) + `routes/investor.py`(排序) | P1 | S4 |
| D | `exit_advice` 跟踪口径 `rn<=8` → `rn<=4`，与推荐对齐 | `routes/investor.py` | P1 | — |
| E | 组合层风控（连亏暂停 / 单票上限），保护几何收益 | `config/thresholds.py` | P2 | （兜底） |

---

## 4. 逐文件改法

### A. 数量上限 + regime 天花板 —— `routes/investor.py`

**A1. 第 404-405 行**：默认 8→4，clamp 允许 0（当前 `max(1,...)` 强制至少 1，会破坏"最少 0"）。

```python
# 改前
limit = request.args.get("limit", default=8, type=int)
limit = max(1, min(limit, 30))
# 改后
limit = request.args.get("limit", default=4, type=int)
limit = max(0, min(limit, 8))     # 允许 0：cold 大盘由 regime 设为 0
SHORT_CAP = limit                  # short 组上限（mid/long 仍用原 limit 逻辑）
```

**A2. 第 718-736 行（分组截断块）**：把"cold/cool 仅减半"改为"天花板 0/2/4"，且 cold 整组出 0。

```python
# 改前（节选）
groups = {}
for hz in horizons:
    items = [_build_item(dict(r)) for r in rows_by_horizon[hz]]
    items = [it for it in items if it["signal"]["level"] != "avoid"]
    if hz == "short" and market_regime in ("cold", "cool"):
        items = items[:max(1, limit // 2)]
        if market_regime == "cold":
            for it in items:
                if it["signal"]["level"] == "buy":
                    it["signal"]["level"] = "wait" ...
    groups[hz] = items[:limit]

# 改后
# regime 对 short 组的上限天花板（满足"最多4/最少0"）
_REGIME_CAP = {"cold": 0, "cool": 2, "normal": SHORT_CAP, "hot": SHORT_CAP}
groups = {}
for hz in horizons:
    items = [_build_item(dict(r)) for r in rows_by_horizon[hz]]
    items = [it for it in items if it["signal"]["level"] != "avoid"]
    if hz == "short":
        cap = _REGIME_CAP.get(market_regime, SHORT_CAP)
        if cap == 0:
            items = []                                  # cold：整组出 0
        else:
            items = items[:cap]                          # cool→≤2, normal→≤4
        groups[hz] = items
    else:
        groups[hz] = items[:limit]                       # mid/long 保持原逻辑
```

> 注意：`cold` 时直接 `items=[]`，不再走"buy→wait"降级（降级无意义，本来就 0）。

---

### B. 置信度门控 —— `config/strategy_params.py` + `routes/investor.py`

**B1. `config/strategy_params.py` 的 `TUNABLE_PARAMS`（第 264 行附近）新增一条**，走已有覆盖层（DB `strategy_param_override`），不写死：

```python
"short_conf_gate": {
    "default": 22.0, "type": float,
    "min": 0.0, "max": 50.0, "label": "短线置信门控(融合分下限)",
},
```

**B2. `routes/investor.py` 取数 SQL（第 446-476 行）**：WHERE 增加 `fusion_score >= ?`，并在 short 组改用"低扩展度排序"（见 C）。门控对所有 horizon 生效即可（信号稀疏/弱日自然出 0）。

```python
gate = get_param("short_conf_gate")   # 默认 22
rows_by_horizon[hz] = conn.execute(
    f"""
    SELECT ...  -- 字段不变
    FROM stock_signal s
    LEFT JOIN latest_price lp ON lp.code = s.code
    LEFT JOIN stock_info i ON i.code = s.code
    WHERE s.scan_date = ?
      AND COALESCE(s.horizon, 'short') = ?
      AND (s.buy_price IS NOT NULL OR s.fusion_score IS NOT NULL)
      AND s.fusion_score >= ?                      -- ★ 新增置信门控
      AND s.name NOT LIKE '%ST%'
      AND s.name NOT LIKE '%退%'
      {board_filter}
      {pe_filter}
    ORDER BY {order_clause}                          -- ★ 见 C3：short 改用扩展度排序
    LIMIT ?
    """,
    # 多取 3 倍候选：avoid 级剔除后由替补顶上（cap*3）
    (scan_date, hz, gate, cap * 3 if cap > 0 else 0),
).fetchall()
```

---

### C. 短线换选择标准（高融合 ∩ 低扩展度）—— 核心改动

> 反直觉结论：融合分最高的恰是"已涨最多、扩展度最高、未来空间最小"的票。
> 最优取法 = **先门控 fusion≥22，再按"距 MA20 偏离最小"取前 N**（S4，年化 +18.6%）。

**C1. DDL：给 `stock_signal` 加扩展度列 —— `core/db.py` 第 111-132 行建表处或迁移脚本**

```sql
ALTER TABLE stock_signal ADD COLUMN pct_above_ma20 REAL;  -- 价/MA20 - 1，<=0 表示在 MA20 下方
```
> 建议在 `core/db.py` 的建表/迁移段补一行 `ALTER TABLE`（已存在的库自动跳过），或单独跑一次迁移工具。

**C2. 落库时计算 `pct_above_ma20` —— `core/sync.py` 的 `recalc_all_scores` 逐股循环**

在构造每条 stock_signal 行（调用 `save_scan_signals` 之前）按该股 `daily_price` 序列算：

```python
import pandas as pd
close = df_g["close"].astype(float)               # 该股按 trade_date 升序的收盘价序列
ma20 = close.rolling(20).mean()
scan_close = float(df_g.loc[scan_date, "close"])
scan_ma20 = float(ma20.loc[scan_date])
pct_above_ma20 = (scan_close / scan_ma20 - 1.0) if scan_ma20 > 0 else 0.0
row["pct_above_ma20"] = round(pct_above_ma20, 4)
```
并把该字段透传进 `save_scan_signals` 的 records dict 与 INSERT 列（见 `core/repository/signal_repo.py` 第 67-100 行，加一列 `pct_above_ma20`）。

**C3. 排序子句（第 446-476 行 SQL 的 `ORDER BY`）按 horizon 分支**

```python
if hz == "short":
    # 先门控(WHERE 已做)，再按扩展度升序：距 MA20 越近/越低 → 未来空间越大 → 优先
    order_clause = "COALESCE(s.pct_above_ma20, 0) ASC"
else:
    order_clause = "COALESCE(s.fusion_score, 0) DESC"   # mid/long 保持融合分排序
```

> 实现替代方案（可选）：`tools/_eval_adj_sort.py` 已实现 `fusion × (1 - extension_penalty)` 的调权排序，可作为"软排序"替代硬门控+硬排序；但回测 S4 用的是"硬门控+硬排序"口径，建议先按本文落地以便与回测对齐复验。

**C4. 一次性回填历史行**：DDL 后历史 `stock_signal` 的 `pct_above_ma20` 为 NULL。写一个小工具 `tools/backfill_signal_extension.py`，对 `scan_date >= '2026-01-01'` 的行按 C2 算法回填（或等下次 `recalc_all_scores` 全量重算自动覆盖）。回填前 `ORDER BY COALESCE(pct_above_ma20,0) ASC` 会把 NULL 排最前——务必先回填再上线。

---

### D. 跟踪口径对齐 —— `routes/investor.py` 第 807 行

`exit_advice` 当前硬码 `rn <= 8`，与"短线最多 4"不一致，导致跟踪列表多出未推荐的票。

```python
# 改前
WHERE rn <= 8
# 改后（与 short 组上限对齐；可用 SHORT_CAP 变量）
WHERE rn <= 4
```
> 该端点按 `scan_date >= EXIT_TRACK_START_DATE` 实时计算，改阈值即生效，无需迁移。

---

### E. 组合层风控（兜底，P2）—— `config/thresholds.py`

<50% 胜率 + 负偏下，集中持仓提升几何收益但放大回撤（S4 回撤 -39.5%）。启用既有参数（如已存在 `consecutive_loss_limit`）：

- `consecutive_loss_limit = 3`：连亏 3 次暂停下一笔（需出入场跟踪 `recommend_outcome` 支持）。
- 单票仓位上限 ~25%（与"少而精"协同）。
- 本项不改变推荐数量，仅保护账户曲线，建议与 A/B/C 同期或随后上线。

---

## 5. 验证步骤（落地后必跑）

1. **回测复验**：`python tools/backtest_dynamic_count.py`
   - 确认 S4 年化 ≥ +18%、回撤 ≤ -40%；
   - 把脚本里 `TOPN_PROP=4`、`FUSION_GATE=22` 与新代码参数对齐（或直接读 `get_param`）。
2. **真实推荐诊断**：`python tools/diag_short_reco.py`
   - 检查今日 short 组是否 ≤4、是否出现"融合分前 4 恰是高位票"的反模式已消失；
   - 检查 `pct_above_ma20` 列已填充、排序确实按扩展度升序。
3. **接口联调**：`GET /api/investor/today` 与 `?date=最近一个 cold 日`，确认 cold 返回 `count=0`。
4. **回归**：mid/long 组条数与排序不受影响（A 仅改 short，B/D 对全 horizon 但只收紧）。

---

## 6. 超出本次范围（冲 50% 必须，但不在"数量调整"内）

仅 A+B+C（≈S4）≈ **+18.6% 年化**，离 50% 仍有约 5 倍单笔 edge 缺口。要够 50%：

- **第二条独立 alpha 线**：接入 `NEXT_DAY_MOMENTUM`（龙虎榜净买占比，OC 口径 +0.95%/笔，见 `config/strategy_params.py` 第 215 行，已 `enabled`，但当前未并入 short 组取数）。
- **复合分**：RSI 甜区中部 + 量能健康叠加打分，替代单一融合分。
- **适度杠杆 / 移动止损**：吃 T+3/T+5 漂移（见 `short_max_hold_days` 已由 1→3）。
- 以上均需 E 的风控兜住更大回撤。

---

## 7. 关键参数速查（TUNABLE_PARAMS，可 DB 覆盖）

| 参数 key | 默认值 | 含义 |
|---|---|---|
| `short_conf_gate` ★新增 | 22.0 | 短线置信门控（融合分下限），弱日自然出 0 |
| `short_max_hold_days` | 3 | 持仓天数（v2 edge 在 T+3/T+5） |
| `short_stop_loss` | -0.06 | 止损 |
| `short_take_profit` | 0.10 | 止盈 |
| `SHORT_CAP`（代码常量） | 4 | short 组数量上限（normal regime） |

---

## 8. 风险提示

- **C4 回填遗漏**会导致 NULL 排最前、推荐失真——上线前务必确认 `pct_above_ma20` 全填充。
- **回测/推荐口径分裂**：任何打分/过滤改动必须与 `tools/backtest_dynamic_count.py` 用同一套口径（尤其 RSI 用 `rolling(14)` 简单均值，勿改用 `indicators.calc_rsi` 的 ewm 口径）。
- **A 的 clamp 改 `max(0,...)`** 后，`limit=0` 的极端请求会返回空，属预期行为，前端需兼容 `count=0`。
