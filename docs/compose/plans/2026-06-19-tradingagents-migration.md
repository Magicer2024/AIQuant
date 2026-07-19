# AIQuant 完整移植 TradingAgents 多智能体架构

> **状态：已废弃（2026-07 决策）**。本计划（移植 TradingAgents 多智能体 LLM 架构）未执行，项目中无 agents/ 相关代码，决策维持现有规则策略体系。文档仅作历史参考保留。

## 改造目标

将 AIQuant 从传统的因子挖掘+规则回测系统改造为基于 LLM 的多智能体交易框架，完整移植 TradingAgents 的核心架构。

## TradingAgents 核心架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        分析师团队 (Analyst Team)                  │
├─────────────┬─────────────┬─────────────┬─────────────────────┤
│ 基本面分析师 │ 情绪分析师   │ 新闻分析师   │ 技术分析师           │
│ Fundamentals│ Sentiment   │ News        │ Technical           │
├─────────────┴─────────────┴─────────────┴─────────────────────┤
│                        研究员团队 (Researcher Team)              │
├─────────────────────────┬─────────────────────────────────────┤
│       看多研究员         │            看空研究员                │
│       Bull Researcher   │            Bear Researcher          │
├─────────────────────────┴─────────────────────────────────────┤
│                    交易员代理 (Trader Agent)                    │
├───────────────────────────────────────────────────────────────┤
│                    风险管理 (Risk Management)                   │
├───────────────────────────────────────────────────────────────┤
│               投资组合经理 (Portfolio Manager)                  │
└───────────────────────────────────────────────────────────────┘
```

## 数据源混合模式

| 数据类型 | 数据源 | 用途 |
|---------|--------|------|
| A 股行情 | FinShare/BaoStock/AkShare | 基本面+技术分析 |
| 公司财报 | AkShare/东方财富 | 基本面分析 |
| 新闻资讯 | 财联社/新浪财经 | 新闻分析 |
| 社交媒体 | 东方财富股吧/雪球 | 情绪分析 |
| 宏观数据 | 国家统计局/央行 | 宏观分析 |

## 阶段划分

### 阶段 1：核心引擎移植 (预计 2-3 周)
**目标**：移植 TradingAgents 的多智能体核心框架

#### 1.1 创建 LLM 集成层
- 创建 `llm/` 目录
- 实现 DeepSeek API 调用封装
- 支持 OpenAI 兼容接口格式

#### 1.2 移植分析师团队
- `agents/analysts/fundamentals.py` - 基本面分析师
- `agents/analysts/sentiment.py` - 情绪分析师
- `agents/analysts/news.py` - 新闻分析师
- `agents/analysts/technical.py` - 技术分析师

#### 1.3 移植研究员团队
- `agents/researchers/bull.py` - 看多研究员
- `agents/researchers/bear.py` - 看空研究员
- `agents/researchers/debate.py` - 辩论机制

#### 1.4 移植交易决策层
- `agents/trader.py` - 交易员代理
- `agents/risk_manager.py` - 风险管理
- `agents/portfolio_manager.py` - 投资组合经理

#### 1.5 创建 LangGraph 工作流
- `graph/trading_graph.py` - 核心工作流图
- `graph/state.py` - 状态管理
- `graph/nodes.py` - 节点定义

### 阶段 2：数据层改造 (预计 1-2 周)
**目标**：为多智能体提供数据支持

#### 2.1 扩展数据获取
- `data/market.py` - 行情数据（现有）
- `data/fundamentals.py` - 财务数据
- `data/news.py` - 新闻数据
- `data/social.py` - 社交媒体数据
- `data/macro.py` - 宏观数据

#### 2.2 数据缓存层
- `data/cache.py` - Redis/SQLite 缓存
- `data/processors.py` - 数据预处理

### 阶段 3：API 层改造 (预计 1 周)
**目标**：暴露多智能体分析接口

#### 3.1 新增 API 端点
- `POST /api/analyze` - 触发分析
- `GET /api/analyze/{task_id}` - 查询进度
- `GET /api/analyze/{task_id}/result` - 获取结果
- `GET /api/decisions` - 历史决策

#### 3.2 WebSocket 实时推送
- `ws/analyze` - 分析过程实时推送

### 阶段 4：前端改造 (预计 1-2 周)
**目标**：展示多智能体分析过程和结果

#### 4.1 分析过程可视化
- 分析师报告卡片
- 研究员辩论过程
- 决策流程图

#### 4.2 决策结果展示
- 买卖信号
- 风险评估
- 历史表现

### 阶段 5：配置和部署 (预计 3-5 天)
**目标**：完善配置和部署

#### 5.1 配置管理
- `config/llm_config.yaml` - LLM 配置
- `config/agent_config.yaml` - 智能体配置
- `config/data_config.yaml` - 数据源配置

#### 5.2 部署优化
- 更新 `docker-compose.yml`
- 更新 `start.bat`

## 技术栈

| 组件 | 技术选择 |
|------|---------|
| LLM 框架 | LangChain + LangGraph |
| LLM 提供商 | DeepSeek (OpenAI 兼容) |
| 后端 | FastAPI (已有) |
| 前端 | Vue 3 (已有) |
| 数据库 | SQLite/PostgreSQL (已有) |
| 缓存 | SQLite (简化) / Redis (可选) |

## 文件结构变化

```
AIQuant/
├── llm/                          # 新增：LLM 集成层
│   ├── __init__.py
│   ├── client.py                 # DeepSeek API 封装
│   ├── prompts.py                # Prompt 模板
│   └── utils.py                  # 工具函数
├── agents/                       # 新增：智能体系统
│   ├── __init__.py
│   ├── analysts/                 # 分析师团队
│   │   ├── __init__.py
│   │   ├── fundamentals.py       # 基本面分析师
│   │   ├── sentiment.py          # 情绪分析师
│   │   ├── news.py               # 新闻分析师
│   │   └── technical.py          # 技术分析师
│   ├── researchers/              # 研究员团队
│   │   ├── __init__.py
│   │   ├── bull.py               # 看多研究员
│   │   ├── bear.py               # 看空研究员
│   │   └── debate.py             # 辩论机制
│   ├── trader.py                 # 交易员代理
│   ├── risk_manager.py           # 风险管理
│   └── portfolio_manager.py      # 投资组合经理
├── graph/                        # 新增：LangGraph 工作流
│   ├── __init__.py
│   ├── trading_graph.py          # 核心工作流
│   ├── state.py                  # 状态定义
│   └── nodes.py                  # 节点实现
├── data/                         # 重构：数据层
│   ├── __init__.py
│   ├── market.py                 # 行情数据
│   ├── fundamentals.py           # 财务数据
│   ├── news.py                   # 新闻数据
│   ├── social.py                 # 社交数据
│   ├── cache.py                  # 缓存管理
│   └── processors.py             # 数据处理
├── strategy/                     # 保留：现有策略系统（渐进废弃）
├── backtest/                     # 保留：回测系统
├── core/                         # 保留：基础设施
├── routes/                       # 重构：API 路由
│   └── analyze.py                # 新增：分析 API
├── api/                          # 重构：FastAPI 路由
│   └── analyze.py                # 新增：分析 API
├── frontend/src/                 # 重构：前端
│   ├── views/
│   │   └── Analyze.vue           # 新增：分析页面
│   └── components/
│       ├── AgentCard.vue         # 智能体卡片
│       ├── DebateView.vue        # 辩论过程
│       └── DecisionFlow.vue      # 决策流程
├── main.py                       # 更新：启动入口
└── requirements.txt              # 更新：新增依赖
```

## 关键依赖

```
# 新增依赖
langchain>=0.3.0
langchain-core>=0.3.0
langgraph>=0.2.0
openai>=1.0.0
httpx>=0.27.0
aiohttp>=3.9.0
```

## 验证计划

### 单元测试
- 每个分析师 Agent 的独立测试
- LangGraph 工作流测试
- LLM 调用测试

### 集成测试
- 完整分析流程测试
- API 端点测试

### 端到端测试
- 选择一只股票，运行完整分析
- 验证决策输出格式
