# 短线推荐系统优化修改方案

> 文档性质：可执行方案（非诊断报告）。每一项都给出**改动文件 + 函数 + 行号 + 具体改法 + 验证指标**。
> 配套诊断脚本：`tools/diag_short_reco.py`（多 cohort OC 口径，纯读取，可复用验证）。
> 诊断结论数据见文末附录。

---

## 0. 执行状态（2026-08-09 更新）

| 编号 | 状态 | 说明 |
|---|---|---|
| P0-1.1 | ✅ 已落地 | 参数改为 **止损 -5% / 止盈 +8% / 持仓 1 天**（原方案 3 天；取 1 天因诊断 edge 在 T+1 附近，-3.5% 止损贴"易被洗出"阈值故取 -5%）；`HORIZON_MAX_HOLD["short"]=1`；增量重算 108.8s，落库止损/止盈已验证 -5%/+8% |
| P0-1.2 | ✅ 已落地（仅硬否决） | `EXTENSION_FILTER` + `extension_filter_series` 加入过滤链（chase 之后）；重算后最大偏离 +38.3%→+11.7%、>8% 占比 26.9%→18.4%。**软降权（RSI×0.6）未实现**——RSI>70 仅占候选 1.4%，影响面可忽略；且 ×0.6 会造成 daily_price（回测口径）与 stock_signal（推荐口径）分裂 |
| P1-2.1 | ✅ 已转正（2026-08-09） | 平滑回踩 v2（非文档二值化——二值化会锐减候选；平滑版候选量减半更温和）：`strategy/strategies.py::strategy_bottom_fishing_v2`，`SHORT_ENGINE="pure_bottom_v2"` 已为线上默认。`recalc_all_scores` 全量重算后 diag 实盘历史验证与回测吻合：T+1 OC 胜率 **55.4%**/+0.158%（回测预测 55.5%/+0.211%）、T+3 -0.588%（回测 -0.585%）、最新日候选 MA20 偏离均值 +3.11%、>8% 占比 1.6%（v1 时代 26.9%）。回滚：切回 `pure_bottom` + `recalc_all_scores` |
| P1-2.2 | ❌ 砍掉 | 与 2.1/3.1 同源（都治"买高位"）但最间接，且只改 stock_signal 会造成回测口径分裂 |
| P1-2.3 | ✅ 已落地 | cold/cool 时 short 组 limit 减半、cold 强制 wait（investor.py 分组处）；接口验证通过 |
| P2-3.1 | ❌ 回测证伪 | `tools/_eval_adj_sort.py` 39 cohort：adj 排序 T+1 OC 49.0%/+0.243% **劣于** fusion 排序 53.5%/+0.639%——fusion 排序本身有选择力，惩罚中间区间损失 alpha；另原方案"daily_price 已有 ma20 列"为事实错误（表结构无该列） |
| P2-3.2 | ✅ 已落地 | gap guard 改为：跳空 > 止盈价（≈+8%）→ `sell`（错过不追）；平开/小涨保持 `buy`；止盈缺失回退旧 2.5% 阈值 |

实施差异：阶段二只做 2.3；2.1 保留为回测实验（`tools/_eval_pullback.py` 可复用灰度验证）；阶段三 3.1 证伪不实施。

---

## 0. 目标与边界

**目标**：把短线推荐从"接反弹高位、持亏"转为"买回踩、快进快出、低扩展度入场"。

**硬边界**（不改）：
- 引擎 `SHORT_ENGINE="pure_bottom"` + `PURE_BOTTOM_WEIGHTS=[0,0,0,1,0]` + `FUSION_MODE="max"` 不动——双口径回测证明这是唯一赚钱组合，替换风险高。
- 所有可调参数走 `TUNABLE_PARAMS` 覆盖层（`strategy_param_override` 表），支持 DB 即时覆盖 + `invalidate_param_cache()` 回滚；新增过滤用 `enabled` 开关一键降级。

**优先级约定**：P0=配置/UI 层低风险改动，立即生效；P1=需回测验证的打分/算法改动；P2=排序/语义优化。

---

## 1. 改动总览

| 优先级 | 编号 | 改动 | 文件 / 函数 | 风险 |
|---|---|---|---|---|
| **P0** | 1.1 | 执行期对齐：短线改为快进快出，止盈/止损收紧、持仓上限 10→3 天 | `strategy_params.py`·`exit_advisor.py`·`investor.py` | 低 |
| **P0** | 1.2 | 扩展/超买否决：价>MA20×1.12 硬否决，RSI>72 软降权 | `strategy_params.py`·`rec_filters.py`·`sync.py` | 低 |
| **P0** | 1.3 | **融合分/排序分重定义（核心）**：抄底打分从"奖励已反弹"改为"低扩展度+刚启动"，并放宽趋势闸门让买回踩票进库 | `strategies.py`·`rec_filters.py`·`sync.py` | 中（需回测） |
| **P1** | 2.1 | 新鲜度衰减：反弹/上行成熟天数对融合分打折 | `sync.py::_build_signal_records` | 中 |
| **P1** | 2.2 | 稳健 regime 门控：cold 时短线推荐数量减半 | `investor.py::today_recommendations` | 低 |
| **P2** | 3.1 | 分离触发与排序：落库按信号触发，推荐按"扩展度调整分"排序（低扩展度优先） | `investor.py::today_recommendations` | 低 |
| **P2** | 3.2 | 重做 gap guard 语义：跳空过大才放弃，平开按原计划买 | `investor.py::_build_item` | 低 |

---

## 1.5 关键发现（2026-08-09）：融合分排序无效 / 反向

用户质疑"策略融合把后期会涨的票排后面了"——**被数据证实，且比'排后面'更彻底**。

**验证**（`tools/diag_fusion_rank.py`，落库短线票按 fusion_score 分三档，3351 cohort，n≈14万，T+1 开盘买 OC 口径）：

| 档位 | T+1 | T+3 | T+5 |
|---|---|---|---|
| 高融合分 | 46.7% / +0.063% | 45.4% / -0.039% | 44.8% / -0.167% |
| 中 | 47.6% / +0.064% | 46.1% / +0.035% | 45.8% / -0.026% |
| 低融合分 | 48.1% / +0.060% | 47.5% / +0.075% | 47.0% / +0.051% |

→ **融合分越高，后续越差**（T+3/T+5 低分档显著优于高分档，T+5 仅低分档仍为正）。当前 Top8 按融合分排序，选出的恰是"已反弹最猛"、边际空间最小的票。

**两层病灶**：
1. **融合分定义错位**：`fusion_score = bottom_score 派生`，`bottom_score` 的 `score_rebound=(rebound_ratio/0.05).clip(0,1)` 奖励"已反弹5%"+放量 → 融合分高 = 已涨得多 = 扩展度高 = 未来空间小，与"选未来会涨"语义相反。
2. **趋势闸门在落库前直接排除低位票**：`trend_gate_series`（`rec_filters.py:62`）要求 `close>MA20 且 MA20 向上`。还在底部、MA20 未拐头的票**根本不进 stock_signal**——不是排后面，是消失。真正"后期会涨"的低位启动票连候选资格都没有。

**结论**：融合分/排序分重定义从 P1（治本可选）**提升为 P0-1.3 核心必做**；且不能只在读取端调排序（原 P2-3.1），要从融合/落库端就引入"低扩展度+刚启动"信号，并放宽趋势闸门让买回踩票能进库。详见下节。

---

## 2. 详细修改项

### 【P0-1.1】执行期对齐 —— 短线改快进快出

**根因**：诊断证明 edge 只在隔夜（T+1 OC 均值 +0.08%），持到 T+3/T+5 转负（-3.86%@T+5）。但当前参数把短线当 5-10 日波段推：`HORIZON_MAX_HOLD["short"]=10`、止盈 +20%、止损 -6%，等于主动走进负收益区。

**改动 A — 参数默认值**（`config/strategy_params.py` 的 `TUNABLE_PARAMS`，约 247-254 行）：

```python
"short_stop_loss": {
    "default": -0.035, "type": float,          # 原 -0.06，收紧到 -3.5%
    "min": -0.12, "max": -0.02, "label": "短线止损比例",
},
"short_take_profit": {
    "default": 0.08, "type": float,            # 原 0.20，快进快出 +8%
    "min": 0.04, "max": 0.40, "label": "短线止盈比例",
},
"short_max_hold_days": {                        # 新增
    "default": 3, "type": int,
    "min": 1, "max": 10, "label": "短线最大持仓天数",
},
```
> 注意：若 `strategy_param_override` 表已写入旧值，需同步更新或清空该表，否则 DB 覆盖优先于代码默认值。

**改动 B — 持仓上限**（`strategy/exit_advisor.py:199`）：

```python
HORIZON_MAX_HOLD: dict = {"short": 3, "mid": 60, "long": None}   # short 10→3
```

**改动 C — 信号灯语义**（`routes/investor.py::156 _calc_signal`）：当 `horizon=="short"` 且 `level=="buy"` 时：
- `reasons.append("短线动量反转：T+1/T+3 了结，不恋战")`
- 新增：若 `latest` 相对 `entry` 涨幅 ≥ `short_take_profit` → 返回 `level="sell"`（止盈离场）而非继续持有，避免"买了就一直拿"。
- 在 `today_recommendations` 调用 `_calc_signal` 处（约 649 行）传入 `horizon=d.get("horizon")` 参数（当前签名无 horizon，需补）。

**验证指标**：用 `tools/diag_short_reco.py` 跑"模拟 T+3 了结"口径，预期 T+3 OC 均值从 **-1.41%** 提升至接近 0/转正；`/exit_advice` 短线持仓上限从 10 天变为 3 天。

---

### 【P0-1.2】扩展/超买否决

**根因**：最新候选 26.9% 高于 MA20 超 8%、最大 +38.3%；低位组（<MA20 2%）T+1 OC 52.2%/+0.28% 优于高位组 48.3%/+0.04%。缺扩展度否决是主缺口。

**改动 A — 新增配置**（`config/strategy_params.py`，紧跟 `CHASE_FILTER` 之后，约 162 行后）：

```python
EXTENSION_FILTER = {
    "enabled": True,
    "max_pct_above_ma20": 0.12,   # 价 > MA20×1.12 → 硬否决（易均值回归尾部）
    "max_rsi14": 72.0,            # RSI(14) > 72 → 软降权（fusion ×0.6，保留动量尾）
}
```

**改动 B — 新增过滤器**（`strategy/rec_filters.py`，在 `chase_filter_series` 后新增）：

```python
def extension_filter_series(df, cfg=None):
    """扩展度否决：低价位/低 RSI 优先。True=可推荐。"""
    cfg = cfg or EXTENSION_FILTER
    if not cfg.get("enabled", True):
        return pd.Series(True, index=df.index)
    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    above = (close / ma20 - 1.0).fillna(99)          # 偏离度
    rsi = _rsi14(df)                                   # 需补一个 RSI(14) 辅助函数
    ok = above < float(cfg.get("max_pct_above_ma20", 0.12))
    return ok.reindex(df.index).fillna(False).astype(bool)

def extension_soft_penalty(df, cfg=None) -> pd.Series:
    """RSI>阈值时返回折扣系数（1.0 或 0.6），供融合分乘用。"""
    ...
```

**改动 C — 融合前调用**（`core/sync.py::_build_signal_records`，约 1269-1299 行融合分计算处）：
- 融合前：对未通过 `extension_filter_series` 的交易日，`fuse` 结果置 0（硬否决）。
- 融合后：若命中 `extension_soft_penalty`（RSI>72），`fusion_score *= 0.6`。

**验证指标**：diag harness 加"剔除 >MA20×1.12 候选"对照组，预期整体 T+1 OC 均值从 **+0.08%** 提升（高位组被砍）。

---

### 【P0-1.3】融合分 / 排序分重定义（核心，必须回测）★ 本轮升级重点

**根因（见 1.5 关键发现）**：`fusion_score = bottom_score 派生`，而 `bottom_score` 的 `score_rebound=(rebound_ratio/0.05).clip(0,1)` 奖励"已反弹5%"+放量 → 融合分高 = 已涨得多 = 扩展度高 = 未来空间小。且趋势闸门（`rec_filters.py:62`，`close>MA20 且 MA20 向上`）在落库前直接排除"底部未拐头"的票。两者叠加：当前 Top8 选出的恰是"已反弹最猛"的票，而真正"后期会涨"的低位启动票既在融合分上吃亏，又被闸门排除。diag_fusion_rank 证明融合分排序轻度反向（高分档 T+5 -0.167% vs 低分档 +0.051%）。

**这是本轮最核心、必须做的改动**——它直接回应"策略融合是否需要调整"的质疑。包含三个手段：

**手段 A — 抄底打分重定时（买回踩不买反弹）**（`strategy/strategies.py::strategy_bottom_fishing`，203-205 行）：

```python
# 现状（信号滞后于反弹，融合分奖励高位）：
rebound_ratio = ((d["close"] - low_10d) / low_10d.clip(lower=1e-9)).clip(lower=0)
score_rebound = (rebound_ratio / 0.05).clip(lower=0, upper=1.0)

# 拟改为（买回踩不买反弹）：上行趋势中刚回踩至支撑
ma20 = d["close"].rolling(20).mean()
in_uptrend    = ma20 >= ma20.shift(5)                       # 趋势向上
near_support  = ((d["close"] - low_10d) / low_10d.clip(lower=1e-9)).between(0.01, 0.04)
score_rebound = (in_uptrend & near_support).astype(float)  # 仅比10日低高1~4%才满分
```

**手段 B — 放宽趋势闸门**（让买回踩票进库）：`rec_filters.py:62` 当前 `gate = (close > ma) & (ma >= ma.shift(slope_lb))` 要求 MA20 已明显向上。改为允许"MA20 走平/微向上 + 价格回踩至 MA20 附近（close ∈ [ma×0.98, ma×1.03]）"即通过，捕捉"刚启动、还没大涨"的票。新参数 `SHORT_TREND_GATE.allow_pullback=True` + `pullback_band=0.03`。

**手段 C — 落库即带"扩展度/未来空间"维度**：在 `_build_signal_records` 落库时，除 `fusion_score` 外新增派生字段 `rank_score = fusion_score × 低扩展度系数`（价/MA20 越接近 1 系数越高），让低扩展度票即使融合分略低也能在排序端进位（与 P2-3.1 衔接，从源头解决"排后面"）。

**⚠ 上线前必须回测**：用 `tools/eval_short_engine.py` 或 diag harness 对照"已反弹"版 vs "买回踩+放宽闸门"版的 OC 多 cohort（≥30 日）胜率/均值，确认 T+1 不劣化且 T+3/T+5 提升，才替换。改动可用新函数名（如 `strategy_bottom_fishing_v2`）灰度，由 `SHORT_ENGINE` 开关切换；闸门放宽用 `SHORT_TREND_GATE.allow_pullback` 开关一键回滚。

---

### 【P1-2.2】新鲜度衰减

**做法**（`core/sync.py::_build_signal_records`）：记录融合分时计算"反弹/上行成熟天数"（close 距 `low_10d` 触底后的交易日数），>3 日对 `fusion_score` 乘衰减系数 `0.9^(days-3)`，抑制"推荐已走完的票"。需在落库字段新增或复用 `trigger_list` 备注。

**验证**：diag harness 对比"带衰减"前后 Top8 的反弹成熟天数分布。

---

### 【P1-2.3】稳健 regime 门控

**现状**：`_compute_market_regime`（`investor.py:63`）已是多日 composite（非单日），但诊断显示 34 cohort 方差仍大，最差 cohort T+1 胜率仅 ~40%。

**改动**（`investor.py::today_recommendations`，约 716 行分组处）：当 `market_regime in ("cold","cool")` 时，`short` 组 `limit` 减半（如 8→4），避免最差 cohort 满仓推荐；`regime=="cold"` 时整组 `level` 强制 `wait`。

**验证**：对照不同 regime 日的推荐命中率。

---

### 【P2-3.1】扩展度调整排序

**做法**（`investor.py::today_recommendations`，约 466 行 `ORDER BY fusion_score DESC`）：
改为按 `extension_adj_score = fusion_score × (1 - clamp((price/ma20-1) - 0.08, 0, 0.3)/0.3 × 0.4)` 排序。
`daily_price` 已有 `ma20` 列，可 `LEFT JOIN daily_price` 取最新 `ma20` 与 `close` 在 SQL/Python 侧算 adj_score 后重排。`limit*3` 候选池保留，先 adj 再截前 limit。

**验证**：对比改动前后 Top8 的平均 MA20 偏离（预期下降）。

---

### 【P2-3.2】重做 gap guard 语义

**现状**（`investor.py:661-671`）：T+1 相对信号日收盘涨 >2.5% → 降级 `wait`。但隔夜跳空恰是唯一正 edge，好日子里它砍掉该赚的钱。

**改动**（`_build_item` 内 gap guard 段）：
- 跳空 > `short_take_profit`（如 +8%）→ `level="sell"`（已错过，不追，等回踩）；
- 平开或小涨（<2.5%）→ 保持 `buy`，挂 `entry`（信号价）或回踩支撑，不再 `wait`；
- 即把"涨了就 wait（错过 edge）"改为"跳空过大才放弃，平开按原计划买"。

**验证**：diag harness 对比"原 wait 语义" vs "新语义"的 T+1 OC 收益。

---

## 3. 实施顺序与回滚

| 阶段 | 内容 | 回滚方式 |
|---|---|---|
| 阶段一 | P0-1.1 + P0-1.2（配置+过滤+UI） | 改 `TUNABLE_PARAMS` 默认值 / 清 `strategy_param_override`；`EXTENSION_FILTER.enabled=False` 一键降级；`HORIZON_MAX_HOLD` 改回 10 |
| 阶段二 | P1-2.1（回测通过后，灰度 `v2` 引擎）+ P1-2.2 + P1-2.3 | `SHORT_ENGINE` 开关切回 `pure_bottom`；regime 门控 `enabled` 关 |
| 阶段三 | P2-3.1 + P2-3.2 | 排序改回 `fusion_score`；gap guard 改回原阈值 |

**统一验证 harness**：任何优化决策都跑 `tools/diag_short_reco.py` 多 cohort OC 口径复验，避免单日好行情误导（单日 cohort 曾现 80.6% 胜率，34 日聚合后拉平到 50.2%）。

---

## 4. 风险与开放问题

1. **回测口径冲突**：组合口径（持有10日）下纯抄底年化 +10.81% 唯一赚钱；但隔日 OC 口径下持有越久越亏。P0-1.1 是把线上执行从"组合口径假设"对齐到"真实 OC 口径"——逻辑自洽，但需确认不破坏既有的组合口径绩效叙事。
2. **行业字段恒为空**：`stock_info.industry` 未填充，行业配额/行业中性化类优化暂不可行。
3. **NEXT_DAY_MOMENTUM 并行线**：`隔日动量`策略（龙虎榜净买）已是 OC 口径、止盈 +6.5%/止损 -4%，与 P0-1.1 思路一致，可作为"快进快出"范本参考，不参与本方案改动。

---

## 附录：诊断关键数据（2026-08-08 跑 `tools/diag_short_reco.py`）

**A. 推荐时点位置（最新日 2026-08-07，212 候选）**
- 价相对 MA20 偏离均值 **+6.3%**，>8% 占 26.9%（最大 +38.3%）
- 相对 10 日最低反弹均值 **+9.4%**，>5% 占 **73.6%**
- RSI(14) 均值 56.0，>70 仅 1.4%

**B. 推荐后真实表现（聚合 34 历史日，n=10166，T+1=次日开盘买）**

| 周期 | OC 胜率/均值 | CC 胜率/均值 |
|---|---|---|
| T+1 | 50.2% / +0.08% | 44.5% / -0.82% |
| T+3 | 44.0% / -1.41% | 40.6% / -2.30% |
| T+5 | 40.9% / -3.00% | 38.5% / -3.86% |

**C. 分位验证**：高位组（>MA20 8%）T+1 OC 48.3%/+0.04% vs 低位组（<MA20 2%）52.2%/+0.28% → 低扩展度入场更优，支撑 P0-1.2 与 P1-2.1。
