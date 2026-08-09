# AIQuant 项目长期记忆

> ⚠️ **记忆校准声明（2026-08-06 重写）**：本文件描述**主工作树 `dev_0.0.1`** 的真实状态。
> 项目另有一个未合并的 git worktree 分支 `worktree-feature+aiquant-9-improvements`（路径 `.claude/worktrees/feature+aiquant-9-improvements`），其中包含 LLM、`deployment/`、三省六部 `governance/`+`ministries/`、`risk_config.yaml`、AI模型(RandomForest)、WebSocket 行情等**拓展功能，尚未合并进主树**。切勿把这些 worktree 内容当作主树现状。
> 本记忆曾严重失实（把 worktree 状态当主树），已整体重写。

## 项目概述
AIQuant —— A股量化投资系统，基于 Flask 后端 + 前端仪表板（dashboard.html）。
当前 git 分支：**`dev_0.0.1`**。主树无显式项目版本号（仅 `config/strategies/default.yaml` 有 `version: "1.0" 策略配置版本`）。

## 用户偏好
- 操作系统：Windows
- 路径：`E:\小项目\Project\AIQuant`
- 启动方式：Bat 脚本（`start.bat` 前台 / `install.bat` 装依赖；主树**无** `start-bg.bat`）
- 颜色惯例：A股红涨绿跌（涨红跌绿）

## 主树真实模块结构（基于 2026-08-06 扫描）
```
app.py                  Flask 入口，注册 7 个 Blueprint
routes/                 system, sync, scoring, screen, backtest, optimizer, investor
core/                   data_fetcher, data_cleaner, db, sync, repository,
                        em_kline/em_realtime/em_guard(东方财富行情), outcome_tracker, task_queue
strategy/               strategies(5策略), scorer, factor_lib, indicators, rec_filters,
                        rule_scanner, rules_store, optimizer, exit_advisor, mid_long,
                        next_day_momentum, adaptive_weights, config, intent/
risk/                   ⚠️ 主树仅空 __pycache__（风控实现在 worktree，未合并）
config/                 strategy_params.py, thresholds.py, settings.py, personal_config.py,
                        llm_config.yaml, strategies/(default.yaml)
scheduler/              runner.py(daily_sync_by_date 调度主路径)
backtest/               回测引擎（本地因子计算，不读 stock_signal 表）
qlib_engine/            Qlib 初始化封装
utils/                  logger, timing, api 等
tools/ reports/ static/ docs/ tests/ data_cache/ logs/
```
**说明**：以上为文件/目录层面的真实存在。各模块的功能成熟度以源码为准，本记忆不夸大"已完成"。

## 短线推荐引擎关键事实（主树，2026-08-06 核实）
- `SHORT_ENGINE="pure_bottom"`（config/strategy_params.py）：每日短线推荐 **100% 由 `strategy_bottom_fishing`（抄底型）驱动**；`PURE_BOTTOM_WEIGHTS=[0,0,0,1,0]`，其余 4 策略融合分贡献为 0，`FUSION_MODE="max"` 下融合分 = `BOTTOM_SCORE × 5`。
- **"推荐已涨过的票/挂高位"根因**：`strategy_bottom_fishing.score_rebound` 奖励"较10日最低反弹5%"→信号在反弹后最强；叠加 `SHORT_TREND_GATE`（MA20 向上）偏向上行趋势票；全链路**无超买/扩展度否决**（`CHASE_FILTER` 仅挡连板/3日+25%/涨停打开）。结果系统性推荐"回踩后已反弹、处上行趋势"的票，用户 T+1 开盘买在反弹高位。
- 改进方向（docs/short-reco-anti-chase-methods.md）：A 扩展/超买硬否决 + C T+1 跳空守卫（纯加法，P0）；B 抄底触发重定时为"买回踩不买反弹"（治本，需回测）；D 新鲜度衰减；E 复用市场状态联动；F 用自有 `eval_fusion_mode` 验证。

## 每日推荐数据流（关键事实，2026-08-06 纠正）
> 以下为寇豆码纠正后确认的真实链路，后续动推荐系统务必以此为准。

- **调度主路径不刷新推荐信号**：18:00 定时任务 `scheduler/runner.py` → `daily_sync_by_date(target="all")` → `_recompute_strategy_scores_for_updated`（`core/sync.py`）。该函数**只增量重算 fusion_score（按受影响股），不写 stock_signal 表**。
- **stock_signal 仅由手动触发**：全量重算 `recalc_all_scores`（`core/sync.py`，逐股×全历史逐交易日写 stock_signal，手动触发约 5 分钟）只在 `routes/sync.py` 的 `/recalc_all_scores` 接口触发；前端 dashboard 在"推荐日期非今日"时自动 POST /recalc 兜底。**今日推荐的时效实际依赖前端/手动兜底，而非调度内保证。**
- **回测不依赖 stock_signal 表**：backtest engine 的 `stock_signals` 是本地因子计算产物（非读库），qlib_engine 零引用；该表仅前端展示 + `tools/analyze_confirm_gap.py` 读取。→ 改造"只写最新 scan_date 候选"安全，唯一索引 (scan_date, code, horizon) 天然保留历史。
- **行业字段恒为空**：`stock_info.industry` 列已加但未填充，全项目无填充代码。行业配额类优化需先接行业数据源。
- **market_regime 单日噪音**：`routes/investor.py` 两处重复，均用 `AVG(pct_change)` 取 `MAX(trade_date)` 单日均值；`is_trading_day`（`core/sync.py`）与 `core/repository/price_repo.py` 各有一处无缓存的 akshare `tool_trade_date_hist_sina()` 调用。
- **optimizer 是 suggest-only 半自动闭环**：寻优评估消费 `daily_price` 的 OC/CC 重放，非 `recommend_outcome`；outcome 仅用于样本量守护与归因。`PURE_BOTTOM_WEIGHTS` 全压抄底有双窗回测依据（胜率44.35%/PF1.066），**按近期 accuracy 微调易过拟合**。
- **recommend_outcome 无 status 列**：含 hit_stop/hit_tp/exit_reason/exit_date/exit_return。"已推/已持仓"判定应查 `personal_position(status='holding')` 或 `exit_reason`/`exit_date` 时间窗。
- **质量过滤**：`strategy/rec_filters.py` 含 ST/退市剔除 + 流动性 min_amt20 + 市值区间 [min_mktcap, max_mktcap]（默认约 [30亿,800亿]）；无波动率上限/换手率下限。
- **候选池为 limit*3**：多取 3 倍候选供"避免"级剔除后替补，非严格前 8。

## 主树真实 API 端点（2026-08-06 扫描，非 worktree）
- 页面：`/`、`/dashboard`（dashboard.html，3714 行）、`/reports/<path:filename>`
- 基础：`GET /api/health`
- 数据同步(sync)：`/recalc`、`/recalc_all_scores`、`/today`、`/today/dates`、`/latest`、`/latest_date`
- 评分(scoring)：`/stock/<code>`、`/factor_trend` 等
- 选股/规则扫描(screen)：`/rules`、`/rule_signals`、`/scan_rules`、`/save_as_rule`、`/conditions`、`/pool`、`/history`、`/apply`、`/rollback`、`/guard-clear`、`/guard-status`、`/alerts`、`/dismiss`、`/explain`、`/explain/<metric>`
- 回测(backtest)：`/backtest/*`（具体见 routes/backtest.py）
- 优化(optimizer)：`/optimizer/*`（见 routes/optimizer.py）
- 个人投研(investor)：`/watchlist`、`/watchlist/<code>`、`/positions`、`/positions/<int:pid>`、`/outcome_list`、`/outcome_summary`、`/exit_advice`、`/market-overview`、`/realtime`、`/kline/<code>`、`/parse_intent`、`/recommendations/history`
- 任务队列：`/task/<task_id>`、`/status/<task_id>`、`/<task_id>/{cancel,export,result,status,trades}`
- 调度/系统：`/scheduler`、`/status`、`/auto-status`、`/progress`、`/run`
> ⚠️ 以下**主树不存在**（仅在 worktree）：`/api/llm/*`、`/api/deployment/*`、`/api/risk/*`、`/api/governance/*`、`/api/data/fetch*`、`/api/ai/*`、`/api/broker/*`、`/ws/market`。

## 关键配置（主树真实路径）
- 策略参数：`config/strategy_params.py`、`config/strategies/default.yaml`
- 阈值：`config/thresholds.py`
- LLM 配置：`config/llm_config.yaml`（文件存在，但 LLM 路由/功能在 worktree）
- 数据库：`core/quant.db`（SQLite）
- ⚠️ `config/risk_config.yaml` **主树不存在**（仅在 worktree）

## 常用命令
```bash
start.bat          # 前台启动
install.bat        # 安装依赖
```
> 主树无 `start-bg.bat`（该脚本在 worktree）。

## 注意事项
- WebSocket：主树**未使用** flask-sock（实时行情推送功能在 worktree）。
- 模拟交易/实盘：主树有 `personal_position` 基础持仓表；统一交易下单、Broker 抽象层在 worktree，未合并。
- AI 模型(RandomForest 等)：主树**无** sklearn 模型代码（在 worktree）。
- 风控系统（11条规则+YAML）：主树**无**实现（risk/ 为空，配置在 worktree）。
- 三省六部架构：主树**无**（governance/ministries 在 worktree）。

## 待办 / 开放问题
- 短线推荐"挂高位"问题（见上方「短线推荐引擎关键事实」），改进方案文档：`docs/short-reco-anti-chase-methods.md`。
- 推荐系统代码改动已交由其他同学处理（用户 2026-08-06 反馈）；本 Agent 角色为策略/方法诊断与方案对齐。
- 记忆曾把 worktree 状态误当主树，已整体重写；后续写入记忆前须先确认目标文件在主树还是 worktree。

---
*最后更新: 2026-08-06（整体重写校准主树/ worktree 偏差）*
