# AIQuant 项目长期记忆

## 项目概述
AIQuant —— A股量化投资系统，基于Flask后端 + 前端仪表板。

## 用户偏好
- 操作系统：Windows
- 路径：`E:\小项目\Project\AIQuant`
- 启动方式：Bat脚本（已修复CMD兼容性问题）
- 颜色惯例：A股红涨绿跌

## 项目架构：三省六部制
```
governance/              —— 三省架构
  crown_prince/          —— 太子院（决策层）
  chancellery/           —— 中书省（策略层）
  secretariat/           —— 尚书省（执行层）
ministries/              —— 六部职能
  rites/                 —— 礼部（数据服务 + 行情监控）
  war/                   —— 兵部（交易执行）
  libu/                  —— 吏部/礼部报表（报告生成）
```

## 已完成模块

### Phase 1: 核心能力补齐 ✅
| 模块 | 说明 |
|------|------|
| 风控系统 | 10条规则 + YAML配置化 + 热加载 + 黑名单 + 豁免 |
| 策略配置化 | 6个策略参数外置 + 热加载 |
| 审计日志 | `@audit_log`装饰器 + 查询统计API |

### Phase 2: 架构升级 ✅
| 模块 | 说明 |
|------|------|
| 三省六部架构 | 太子院/中书省/尚书省 + 六部框架 |
| 多数据源 | Local + Akshare + Tushare，主备自动切换 |
| 回测框架 | Backtrader 封装，支持参数优化 |

### Phase 3+: 中低优先级 ✅
| 模块 | 说明 |
|------|------|
| 实时行情监控 | WebSocket推送，支持订阅/取消 |
| 多因子评分 | 趋势/动量/波动率/成交量/质量，0-100分 |
| 模拟交易 | 订单管理 + 持仓跟踪 + 账户概览 |
| 报告系统 | HTML持仓/交易/风控/日报 |

### Phase 3: 高级特性 ✅
| 模块 | 说明 |
|------|------|
| 实盘监控 | 持仓扫描、止损止盈告警、Webhook推送 |
| 策略意图解析 | 自然语言→结构化策略配置 |
| 任务队列 | 异步任务执行、状态跟踪、结果回调 |

### 深度拓展 ✅
| 模块 | 说明 |
|------|------|
| AI模型接入 | sklearn RandomForest，33维特征，涨跌预测 |
| 实盘交易接口 | 抽象层 + SimulationBroker + EasyTrader预留 |
| 统一仪表板 | dashboard.html，暗色主题，响应式，30秒轮询 |

### 超级拓展 ✅ (2026-05-02)
| 模块 | 说明 |
|------|------|
| LLM API接入 | `llm/` 模块，OpenAI兼容格式，支持GPT-4o/DeepSeek/Kimi/通义千问 |
| LLM策略顾问 | `llm/advisor.py`：策略分析、市场情绪、意图增强解析、交易理由生成 |
| 策略实盘部署 | `deployment/` 模块：注册/启动/暂停/停止/注销，独立线程运行，日志追踪 |
| 可视化升级 | ECharts集成：资产曲线、持仓饼图、盈亏柱状图、风险仪表盘、回撤图、特征重要性 |
| LLM智脑页面 | dashboard新增LLM板块：意图解析、通用对话、模型配置 |
| 策略部署页面 | dashboard新增部署板块：注册策略、一键启停、运行状态监控 |
| AI数据拉取 | dashboard新增数据管理面板：手动拉取akshare数据到本地数据库 |
| AI训练修复 | 训练/预测自动fallback到akshare在线拉取，无需本地数据库有数据 |

## 关键配置
- 风控配置：`config/risk_config.yaml`
- 策略配置：`config/strategies/default.yaml`
- 数据库：`core/quant.db`（SQLite）

## 常用命令
```bash
# 启动服务
start.bat          # 前台启动
start-bg.bat       # 后台启动

# 安装依赖
install.bat
```

## API 关键端点
- `GET /api/health` —— 健康检查
- `WS /ws/market` —— 实时行情WebSocket
- `POST /api/scoring/evaluate` —— 股票评分
- `POST /api/trade/order` —— 创建订单（模拟）
- `GET /api/reports/daily` —— 每日报告
- `GET /api/live/status` —— 实盘监控状态
- `POST /api/intent/parse` —— 策略意图解析（本地正则）
- `POST /api/tasks/submit` —— 提交异步任务
- `POST /api/ai/train` —— 训练AI模型
- `POST /api/ai/predict` —— AI涨跌预测
- `POST /api/broker/order` —— 统一交易下单
- `GET /api/broker/account` —— 查询账户资金
- `POST /api/llm/chat` —— LLM通用对话
- `POST /api/llm/stream` —— LLM流式对话(SSE)
- `POST /api/llm/strategy` —— LLM策略分析
- `POST /api/llm/intent` —— LLM增强意图解析
- `GET /api/llm/health` —— LLM连接检测
- `POST /api/deployment/register` —— 注册策略
- `POST /api/deployment/deploy` —— 部署启动策略
- `POST /api/deployment/stop` —— 停止策略
- `GET /api/deployment/status` —— 查询部署状态
- `POST /api/data/fetch` —— 拉取单只股票数据（akshare→本地DB）
- `POST /api/data/fetch_batch` —— 批量拉取数据
- `GET /api/data/fetch/status` —— 本地数据库数据概况
- `/dashboard` —— 统一仪表板 v2.5（新首页）

## 注意事项
- Bat脚本不能包含 `chcp 65001` 和 `2>&1` 组合（CMD解析错误）
- 使用 `if %errorlevel% neq 0` 替代 `if errorlevel 1`
- WebSocket使用 flask-sock 库（非 flask-socketio）
- 模拟交易默认开启，实盘需额外配置
- AI模型首次使用需先调用 `/api/ai/train` 训练
- 实盘交易接口默认模拟模式，切换实盘需配置 EasyTrader
- **LLM使用**：在 `config/llm_config.yaml` 中配置 api_key，或通过环境变量 `LLM_API_KEY` 设置
- **LLM预设模型**：gpt-4o / gpt-4o-mini / deepseek-chat / deepseek-reasoner / moonshot-v1 / qwen-plus
- **策略部署**：注册后通过 `/api/deployment/deploy` 启动，策略在独立线程中定时运行

## 项目版本
- 当前版本：**v2.5.1**
- 总路由数：150+
- Blueprints：29 个

---
*最后更新: 2026-05-02*
