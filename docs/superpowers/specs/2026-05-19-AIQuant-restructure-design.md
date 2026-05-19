# AIQuant 重构设计：个人股票评分系统

**日期**: 2026-05-19
**目标**: 将 AIQuant 从复杂的多 Agent 量化平台重构为聚焦的个人股票评分系统

## 核心理念

适合个人投资者的轻量量化工具，三步流程：

1. **策略挖掘**（手动触发）→ 从因子库自动生成有效策略规则
2. **每日打分**（收盘后运行）→ 活跃策略对全市场股票打分排序
3. **回测验证**（按需运行）→ 验证策略历史表现，查看逐笔交易明细

## 目录结构

```
AIQuant/
├── app.py                    # Flask 入口，注册路由
├── dashboard.html            # 单页仪表盘（三 Tab）
├── config/                   # 配置（保留现有）
│   ├── settings.py
│   ├── strategy_params.py
│   └── thresholds.py
├── core/                     # 核心层（保留+精简）
│   ├── db.py                 # SQLite 操作（保留）
│   ├── sync.py               # 数据同步编排（保留）
│   ├── data_fetcher.py       # baostock/akshare（保留）
│   └── quant.db
├── strategy/                 # 策略层（重组）
│   ├── factor_lib.py         # 80+因子定义（保留）
│   ├── miner.py              # NEW: 策略挖掘（穷举+IC过滤+遗传）
│   ├── scorer.py             # NEW: 每日打分引擎
│   └── rules_store.py        # NEW: 策略规则 CRUD
├── backtest/                 # 回测层（重组）
│   ├── engine.py             # NEW: 统一回测引擎（封装 Qlib）
│   ├── trade_store.py        # NEW: 逐笔交易明细存储
│   └── reporter.py           # NEW: 回测报告生成
├── routes/                   # API 路由（精简）
│   ├── scoring.py            # NEW: 打分排名 API
│   ├── strategy.py           # 策略管理 API
│   └── backtest.py           # 回测 API
├── qlib_engine/              # Qlib 集成层（保留）
│   ├── strategy_adapter.py   # Qlib 回测适配
│   ├── data_bridge.py        # SQLite → Qlib 数据桥接
│   └── model_runner.py       # ML 模型预测
└── tests/                    # 测试
```

### 删除项

| 删除 | 原因 |
|------|------|
| `agents/` (全部) | Agent 流水线过于复杂，逻辑并入 Service/Core |
| `ministries/` (全部) | 六部抽象层不再需要 |
| `services/` (全部) | 业务逻辑并入 strategy/ 和 backtest/ |
| `backtest/archive/` (全部) | 历史实验脚本，清理 |
| `backtest/engine.py` (旧版) | Backtrader 引擎，Qlib 替代 |
| `backtest/backtest_ui.py` | Streamlit UI，不再使用 |
| `strategy/strategies.py` | 5 个硬编码策略，由自动挖掘策略替代 |
| `strategy/strategy.py` | 0-100 技术综合评分，由新 scorer 替代 |
| `strategy/rule_miner.py` | 重构为 miner.py |
| `strategy/genetic_evolver.py` | 并入 miner.py |
| `strategy/lgbm_ranker.py` | 可选保留，后续集成 |
| `strategy/dynamic_selector.py` | 可选保留，后续集成 |
| `llm/` | 与评分主线无关 |

## 打分流水线（每日）

```
收盘后触发 (手动或定时)
  → 数据同步: core/sync.py 全量同步日线
  → 因子计算: strategy/factor_lib.py 逐股计算 ~80 因子 → factor_daily 表
  → 策略打分: strategy/scorer.py 加载活跃规则 → 逐股匹配 → stock_score 表
  → 排序展示: dashboard.html 按 score DESC 渲染
```

### stock_score 表

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| trade_date | TEXT | 交易日期 |
| code | TEXT | 股票代码 |
| name | TEXT | 股票名称 |
| score | REAL | 综合得分 |
| rule_id | TEXT | 命中策略规则 ID |
| rule_name | TEXT | 策略名称 |
| factors_json | TEXT | 各项因子值 JSON |
| created_at | TEXT | 计算时间 |

### 保留的现有表

| 表 | 用途 |
|---|---|
| `daily_price` | 日线 OHLCV 数据（K 线图数据源） |
| `stock_info` | 股票元数据 |
| `index_daily` | 指数日线数据 |
| `sync_log` | 数据同步审计 |
| `factor_daily` | 每日因子值（新需求，如不存在则创建） |

### Scorer 类

- 加载 `strategy_rules` 表中 `is_active=1` 的规则
- 对每只股票逐条规则计算：因子条件满足 → 加权得分
- 同一股票命中多规则时取最高分
- 结果写入 `stock_score` 表，按日期可追溯

## 策略挖掘流程（手动触发）

```
手动触发
  → 1. 因子 IC 过滤: 对所有因子计算 IC_RANK（过去 60 日因子值与未来 5 日收益相关性）
     过滤 |IC| < 阈值的因子 → 候选因子池
  → 2. 规则模板穷举:
     模板1: 单因子阈值 (例: momentum_10 > 0.8)
     模板2: 双因子组合 (例: momentum_10 > 0.7 AND volume_ratio > 0.6)
     模板3: 交叉信号 (例: MACD_DIF CROSS MACD_DEA)
     生成候选规则集（数千条）
  → 3. 回测验证: 每条候选规则 → Qlib 回测
     返回: 年化收益/夏普/最大回撤/胜率/交易次数
  → 4. 规则评分: score = 0.3×收益 + 0.3×胜率 + 0.25×夏普 - 0.15×|回撤|
  → 5. 存入 strategy_rules 表
```

### strategy_rules 表（复用 + 增强）

| 列 | 类型 | 说明 |
|---|---|---|
| rule_id | TEXT PK | 唯一 ID |
| rule_name | TEXT | 规则名称 |
| rule_type | TEXT | T1/T2/T3 |
| conditions_json | TEXT | 条件表达式 JSON |
| fitness | REAL | 综合得分 |
| annual_return | REAL | 年化收益 |
| win_rate | REAL | 胜率 |
| sharpe_ratio | REAL | 夏普比率 |
| max_drawdown | REAL | 最大回撤 |
| total_trades | INTEGER | 交易次数 |
| is_active | INTEGER | 是否启用（用户手动切换） |
| created_at | TEXT | 生成时间 |
| last_backtest_at | TEXT | 最后回测时间 |

### 关键设计点

- 回测期间每笔交易明细存入 `backtest_trades` 表
- 用户可在管理页看到每条规则的汇总指标，点击展开逐笔交易
- `is_active` 由用户手动控制，不自动启用/禁用
- 策略挖掘时的批量回测复用同一套引擎和存储

## 回测系统

### 回测引擎

`backtest/engine.py` 封装 Qlib `SimulatorExecutor`：

- 输入: 策略规则 + 回测周期（起止日期）
- 输出: 汇总指标 + 逐笔交易列表
- 汇总指标 → `backtest_results` 表
- 逐笔交易 → `backtest_trades` 表

### 回测范围

- **全市场回测**：选策略 → 对所有股票应用 → 汇总所有交易
- **单股回测**：选策略 + 股票 → 仅该股历史交易

### backtest_results 表

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| rule_id | TEXT | 策略规则 ID |
| start_date | TEXT | 回测起始日 |
| end_date | TEXT | 回测结束日 |
| annual_return | REAL | 年化收益率 |
| cumulative_return | REAL | 累计收益率 |
| win_rate | REAL | 胜率 |
| sharpe_ratio | REAL | 夏普比率 |
| max_drawdown | REAL | 最大回撤 |
| total_trades | INTEGER | 总交易次数 |
| win_trades | INTEGER | 盈利次数 |
| created_at | TEXT | 回测时间 |

### backtest_trades 表（核心）

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| result_id | INTEGER FK | 关联 backtest_results |
| code | TEXT | 股票代码 |
| name | TEXT | 股票名称 |
| entry_date | TEXT | 买入日期 |
| entry_price | REAL | 买入价格 |
| exit_date | TEXT | 卖出日期 |
| exit_price | REAL | 卖出价格 |
| holding_days | INTEGER | 持仓天数 |
| pnl_pct | REAL | 单笔收益率(%) |
| exit_reason | TEXT | 卖出原因(stop_loss/take_profit/expire) |

### K 线联动

点击逐笔交易中的股票代码 → 弹出 K 线侧面板：

- 使用 ECharts candlestick 图表
- 买入点标记（绿色箭头/圆点）
- 卖出点标记（红色箭头/圆点）
- 标注: 买入日期/价格、卖出日期/价格、持仓天数、收益率、卖出原因

## Dashboard 布局

单页 HTML (`dashboard.html`)，三 Tab 无页面跳转：

### Tab 1: 每日打分

- 日期选择器 + 刷新按钮
- 排名表格: 排名 / 代码 / 名称 / 得分 / 命中策略
- 点击股票行 → 弹出 K 线图侧面板（含当日因子值明细）

### Tab 2: 策略管理

- "挖掘新策略"按钮 + 上次挖掘时间
- 策略表格: 启用开关 / 策略名称 / 年化收益 / 胜率 / 夏普 / 交易次数
- 点击策略行 → 展开该策略回测的逐笔交易明细
- 启用/禁用开关控制策略是否参与每日打分

### Tab 3: 回测中心

- 策略下拉 + 起止日期选择 + "开始回测"按钮
- 汇总指标行: 年化收益 / 胜率 / 夏普 / 最大回撤 / 交易次数
- 逐笔交易表格: 代码 / 买入日 / 买入价 / 卖出日 / 卖出价 / 收益率
- 点击股票代码 → K 线侧面板（含买卖点标记）
- 历史回测记录列表

### 技术选型

- 纯 HTML + JS + CSS，无前端框架
- K 线图: ECharts（candlestick + markPoint 标注买卖点）
- 数据表格: 原生实现，支持排序
- 通过 Fetch API 调用 Flask REST API

## API 设计

### 打分相关

```
GET  /api/scoring/daily?date=2025-01-15    # 获取某日打分排名
POST /api/scoring/run                       # 触发当日打分计算
GET  /api/scoring/stock/{code}?date=...     # 获取单股因子明细
```

### 策略相关

```
GET    /api/strategy/rules                  # 获取所有策略规则列表
PUT    /api/strategy/rules/{id}/toggle      # 启用/禁用策略
POST   /api/strategy/mine                   # 触发策略挖掘
GET    /api/strategy/mine/status            # 查询挖掘进度
GET    /api/strategy/rules/{id}/trades      # 获取某策略的历史交易明细
```

### 回测相关

```
POST /api/backtest/run                      # 触发回测
GET  /api/backtest/results                  # 历史回测结果列表
GET  /api/backtest/results/{id}             # 回测汇总结果
GET  /api/backtest/results/{id}/trades      # 回测逐笔交易
GET  /api/backtest/kline/{code}?start=&end= # K 线数据（含买卖点标注）
```

## 错误处理

- API 响应统一格式: `{success: bool, data: any, error: string|null}`
- 长时间运行操作（挖掘、回测）返回 `202 Accepted` + 轮询状态端点
- NaN/Inf 值在序列化前替换为 null

## 测试策略

- `strategy/scorer.py`: 单元测试 — 给定因子值，验证得分计算正确
- `strategy/miner.py`: 集成测试 — 已知数据集上验证规则生成 + IC 过滤
- `backtest/engine.py`: 集成测试 — 已知策略规则，验证回测输出格式和交易明细完整性
- `routes/`: API 测试 — 验证请求/响应格式 + 数据完整性
