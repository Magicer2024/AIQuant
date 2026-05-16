# Qlib Integration Design

> 日期：2026-05-16 | 状态：待实施
> 目标：将 Microsoft Qlib 作为 AIQuant 的量化引擎，保留 Flask UI + Agent 编排层

---

## 1. Architecture Overview

三层架构，Qlib 替代自研引擎层：

```
┌─────────────────────────────────────────────────────────┐
│  UI Layer (AIQuant - 保留)                               │
│  dashboard.html / quant.html / agent-dashboard.html      │
│  Flask API (~33 routes)                                  │
├─────────────────────────────────────────────────────────┤
│  Service Layer (AIQuant - 适配)                          │
│  agents/* → Qlib workflow 阶段的薄包装                    │
│  services/* → 调用 Qlib API, 不写回测/因子逻辑           │
│  risk/ → 保留, 基于账户状态的规则检查                     │
├─────────────────────────────────────────────────────────┤
│  Engine Layer (QLIB - 新增)                              │
│  ┌──────────┬──────────┬──────────┬──────────┐          │
│  │  Data    │  Model   │ Backtest │ Workflow │          │
│  │  Handler │  Zoo     │ Executor │ Recorder │          │
│  │  Alpha158│  LGBM    │ Exchange │ MLflow   │          │
│  │          │  GRU...  │ Account  │          │          │
│  └──────────┴──────────┴──────────┴──────────┘          │
│  Qlib binary data: ~/.qlib/qlib_data/cn_data/           │
└─────────────────────────────────────────────────────────┘
```

核心规则：**AIQuant 代码不直接操作原始行情数据**。所有因子计算、模型预测、回测模拟走 Qlib。

---

## 2. Component Mapping

### 2.1 核心替代

| 当前 AIQuant | 替代方案 |
|---|---|
| `strategy/factor_lib.py` (72 因子) | Qlib Alpha158 + Expression Engine |
| `strategy/indicators.py` | Qlib 表达式引擎 |
| `strategy/strategies.py` (5 策略) | 规则替代 |
| `backtest/backtest.py` (Backtester) | Qlib SimulatorExecutor |
| `backtest/engine.py` (Backtrader) | 同上 |
| `backtest/backtest_v4.py` | 同上 |
| `backtest/custom_strategy_backtest.py` | 同上 |
| `backtest/strategy_screen_backtest.py` | 同上 |
| `backtest/unified_engine.py` | 同上 |
| `backtest/indicator_engine.py` | Qlib 表达式引擎 |
| `backtest/condition_builder.py` | Qlib 规则 |
| `backtest/optimize_params.py` | Qlib 参数搜索 |
| `backtest/param_optimizer.py` | 同上 |
| `backtest/strategy_interface.py` | 不再需要抽象层 |
| `ai/predictor.py` | Qlib Model Zoo |
| `ai/features.py` | 同上 |

### 2.2 保留并适配

| 当前 AIQuant | 适配方式 |
|---|---|
| `strategy/rule_miner.py` | IC 计算改用 Qlib, 模板枚举保留 |
| `strategy/genetic_evolver.py` | 适应度回测改为 Qlib Executor |
| `strategy/dynamic_selector.py` | 双窗口回测改为 Qlib Executor |
| `strategy/lgbm_ranker.py` | 保留, Qlib 预测作为元特征输入 |
| `agents/signal_agent.py` | 融合层: 规则筛 + Qlib 模型排序 |
| `agents/data_agent.py` | 数据校验 + 触发 Qlib 同步 |
| `agents/backtest_agent.py` | 调用 Qlib SimulatorExecutor |
| `core/sync.py` | 输出改为 Qlib 二进制增量 |
| `core/db.py` | 移除 daily_price/index_daily 相关 |
| `services/strategy_lab_service.py` | P1-P4 底层调用 Qlib |
| `services/backtest_service.py` | 调用 Qlib Executor |
| `services/signal_service.py` | 融合层逻辑 |
| `app.py` | 启动时 `qlib.init()` |

### 2.3 完整保留不变

`routes/`(28 files), `services/`(position, account, stock, optimizer), `agents/`(risk, report, orchestrator, base), `strategy/`(lgbm_ranker, strategy, config, intent), `backtest/backtest_ui.py`, `backtest/archive/`, `core/`(task_queue, audit, repository/*), `config/`, `ministries/`, `governance/`, `llm/`, `deployment/`, `live/`, `scheduler/`, `dashboard.html`, `quant.html`, `agent-dashboard.html`

---

## 3. Data Layer

### 3.1 迁移策略: 一次性直接迁移

```
SQLite daily_price  ──→  Python 迁移脚本         ──→  ~/.qlib/qlib_data/cn_data/
SQLite index_daily   ──→  (qlib_engine/migration.py)  features/<code>/open.bin
SQLite stock_info    ──→                                          /high.bin
                                                                  /low.bin
                                                                  /close.bin
                                                                  /volume.bin
                                                             calendars/day.txt
                                                             instruments/all.txt
```

迁移脚本使用 Qlib 的 `DumpDataAll` API，直接从 SQLite DataFrame 写入二进制格式，不经过 CSV 中间步骤。

### 3.2 日常增量同步

迁移完成后，`core/sync.py` 不再写 `daily_price` 表：
- AkShare/baostock 拉取增量 → 直接写入 Qlib 二进制增量
- `sync_log` 表保留追踪同步状态
- `daily_price` / `index_daily` 表删除

### 3.3 数据存储拆分

| 数据类型 | 存储 |
|---------|------|
| OHLCV + 因子值 | Qlib binary (`~/.qlib/qlib_data/cn_data/`) |
| 股票元信息 (stock_info) | SQLite |
| 策略规则 (strategy_rules) | SQLite |
| 策略信号 (strategy_signals) | SQLite |
| 活跃策略 (active_strategies) | SQLite |
| 风控/审计 (risk_*, audit_log, blacklist) | SQLite |
| 持仓/交易 (positions, trades, account_snapshots) | SQLite |
| 融资融券/北向/龙虎榜/管理层 (stock_*) | SQLite |

---

## 4. Model Layer

两层模型架构，解决不同问题：

### 第 1 层: Qlib Model Zoo (股票打分)

```
Alpha158 因子 (158维)
       │
       ▼
┌─────────────────────┐
│  Qlib Model Zoo      │
│  - LGBModel          │  初始接入 LGBM + GRU
│  - GRU               │  预留接口后续按需加
│  (可选: ALSTM, GATs)  │
│                      │
│  统一接口:            │
│  fit(dataset)        │
│  predict() → Series  │
└─────────────────────┘
       │
       ▼
每只股票的预测收益
```

### 第 2 层: AIQuant LGBM Ranker (规则元特征排序，保留)

```
Qlib 预测分数 ─────┐
规则表现历史 ──────┤
市场状态 ──────────┼──→ LGBM Ranker ──→ 规则×股票的置信度 (0-100)
个股状态 ──────────┤
板块拥挤度 ────────┘
```

Qlib 预测作为 Ranker 的第 8 类元特征输入，让 Ranker 不仅知道"哪条规则好"，还知道"哪些股票被模型看好"。

---

## 5. Strategy & Backtest Layer — 融合机制

### 5.1 两种范式的融合

Qlib 选"什么股票"，AIQuant 选"什么逻辑"。融合后各司其职：

```
                    市场状态识别
                         │
                         ▼
            ┌────────────────────────┐
            │  P4 动态策略选择        │
            │  输出: 活跃规则集 (5-10) │
            └────────────────────────┘
                         │
                         ▼
            ┌────────────────────────┐
            │  活跃规则触发信号        │
            │  全市场扫描 → 候选股票池 │
            └────────────────────────┘
                         │
                         ▼
            ┌────────────────────────┐
            │  Qlib 模型打分          │
            │  对候选池每只股票预测    │
            └────────────────────────┘
                         │
                         ▼
            ┌────────────────────────┐
            │  融合排序              │
            │  规则得分 × Qlib 预测分  │
            │  → 最终 Top-K 股票      │
            └────────────────────────┘
```

### 5.2 Qlib 回测引擎统一

P1/P2/P4 各自调用回测，不再各自维护 Backtrader 实例。统一通过适配层：

```python
# qlib_engine/strategy_adapter.py
def backtest_rules(rules, stocks, start_date, end_date):
    """多条规则 × 多只股票 → 每条规则的绩效"""
    for rule in rules:
        strategy = RuleSignalStrategy(rule)
        executor = SimulatorExecutor(...)
        result = executor.run(strategy, stocks, start_date, end_date)
        yield rule, PortfolioMetrics(result)
```

Agent 流水线走 Qlib 标准 workflow：模型预测 → TopkDropoutStrategy → Executor。

---

## 6. Agent Pipeline Remapping

| Agent | 变化 | 行数 |
|-------|------|------|
| DataAgent | 数据校验 + 触发 Qlib 同步, 不再抓取数据 | ~50 |
| SignalAgent | 融合层: 规则筛 + Qlib 预测 → 最终信号 | ~80 |
| BacktestAgent | 调用 Qlib SimulatorExecutor | ~60 |
| RiskAgent | 不变 (基于账户状态的规则检查) | 不变 |
| ReportAgent | 不变 (读取结果生成报表) | 不变 |

SignalAgent 核心逻辑：

```
SignalAgent.run(ctx):
  1. 活跃规则 = 从 SQLite 读取 P4 选择结果
  2. 候选股票 = 活跃规则在全市场触发信号
  3. Qlib预测 = Qlib模型.predict(候选池)
  4. 最终信号 = 规则得分 × Qlib预测分 → Top-K
  5. 写入 stock_signal 表
```

---

## 7. File Impact Summary

### 新增: `qlib_engine/` (4 files, ~500 lines)

```
qlib_engine/
├── __init__.py           # qlib.init() 封装
├── migration.py          # 一次性迁移工具
├── data_bridge.py        # 增量同步 AkShare → Qlib binary
└── strategy_adapter.py   # 规则 → Qlib Strategy 适配
```

### 删除 (16 files, ~5000 lines)

`backtest/backtest.py`, `backtest/engine.py`, `backtest/backtest_v4.py`, `backtest/custom_strategy_backtest.py`, `backtest/strategy_screen_backtest.py`, `backtest/unified_engine.py`, `backtest/indicator_engine.py`, `backtest/condition_builder.py`, `backtest/optimize_params.py`, `backtest/param_optimizer.py`, `backtest/strategy_interface.py`, `strategy/factor_lib.py`, `strategy/indicators.py`, `strategy/strategies.py`, `ai/predictor.py`, `ai/features.py`

### 调整 (18 files, ~400 lines net reduction)

`core/sync.py`, `core/db.py`, `core/data_fetcher.py`, `strategy/rule_miner.py`, `strategy/genetic_evolver.py`, `strategy/dynamic_selector.py`, `services/strategy_lab_service.py`, `services/backtest_service.py`, `services/signal_service.py`, `services/data_fetcher.py`, `agents/signal_agent.py`, `agents/data_agent.py`, `agents/backtest_agent.py`, `routes/backtest.py`, `routes/backtest_new.py`, `routes/scoring.py`, `routes/ai.py`, `app.py`

### 保持不变 (~120+ files)

`routes/` (28), `services/` (4), `agents/` (4), `strategy/` (4), `backtest/backtest_ui.py`, `backtest/archive/`, `core/` (其余), `config/`, `ministries/`, `governance/`, `llm/`, `deployment/`, `live/`, `scheduler/`, `*.html`

**净效果: 删 ~5000 行, 增 ~500 行。**

---

## 8. Integration Sequence

按依赖关系分 4 个阶段：

| 阶段 | 内容 | 依赖 |
|------|------|------|
| Phase 1 | 数据迁移: `migration.py` + `data_bridge.py` | 无 |
| Phase 2 | 模型接入: Qlib Model Zoo + 适配 Ranker 输入 | Phase 1 |
| Phase 3 | 回测替换: `strategy_adapter.py` + P1/P2/P4 适配 | Phase 2 |
| Phase 4 | Agent + 融合层: SignalAgent 融合逻辑 + Flask routes | Phase 3 |

---

## 9. Risk & Rollback

- **每个 Phase 独立可验证**: Phase 1 迁移后即可用 Qlib 数据加载验证。Phase 2 训练后可对比新旧模型预测。Phase 3 可对比新旧回测结果。
- **回退方案**: Phase 1 迁移完成后立即备份 Qlib binary。同时 `daily_price` 表在 Phase 4 确认稳定前不删除，随时可回退到旧引擎。
