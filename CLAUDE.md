# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

AIQuant 是一套 A 股量化选股与个人评分系统，涵盖数据同步、策略打分、条件选股、回测和可视化仪表板。技术栈：**Python 3.11+、Flask、SQLite、pandas、AkShare/baostock/东方财富接口、scikit-learn、可选 Qlib**。

## Development commands

```bash
# Install dependencies
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # + pytest, pytest-timeout

# Run the system
python app.py                     # Flask API + dashboard (port 5000)
start.bat                         # Windows 一键启动（检查依赖/杀旧进程/起服务）

# Tests
python -m pytest tests/ -q        # 全部测试（网络基准类默认 skip）
python tests/test_em_speed.py     # 手动跑东财接口测速
```

## Architecture

单进程 Flask 应用，`app.py` 注册 6 个 Blueprint，前端是单文件 `dashboard.html`。

### Core modules

| Dir | Purpose |
|-----|---------|
| `core/` | SQLite 数据层：`db.py`（~12 表，get_conn 上下文管理器）、`sync.py`（行情同步主流程）、`data_fetcher.py`（baostock/AkShare 多源）、`data_cleaner.py`（L1-L6 脏数据过滤）、`em_realtime.py`/`em_kline.py`（东财接口）、`em_guard.py`（缓存+频控+配额三道防线）、`task_queue.py` |
| `core/repository/` | 11 个领域 Repository（stock/price/signal/position/trade/sync/margin/north/futures/lhb/mgmt），db.py 中旧函数正逐步迁移至此 |
| `strategy/` | `scorer.py`（回测/因子流式行情加载工具，仅供 backtest 复用）、`factor_lib.py`、`indicators.py`、`rules_store.py`（含 derive_horizon）、`strategies.py`（短线 5 信号融合）、`mid_long.py`（中线综合/长线趋势扫描）、`strategy.py`（中线综合评分库，被 mid_long 调用）、`intent/parser.py`（自然语言选股） |
| `backtest/` | `engine.py`（可视化回测引擎，BacktestParams 含 risk_free_rate）、`rule_engine.py`（策略规则一键回测+沪深300基准）、`service.py`（任务封装，rule_id 分发 + fitness 回写）、`conditions.py`（条件构建）、`nl_parser.py`（自然语言策略→条件+参数，LLM 主路径+本地正则兑底） |
| `routes/` | Flask Blueprint：`system` / `sync` / `scoring` / `screen` / `backtest` / `investor` |
| `scheduler/` | `runner.py` 每日 18:00 盘后自动同步+重算分，`state.py` 共享状态 |
| `qlib_engine/` | 可选 Qlib 集成；`app.py` 启动时 try/except 初始化，失败自动降级不影响主流程 |
| `ai/` | `features.py` 特征工程 + `predictor.py`（RandomForest，模型存 `ai/models/`） |
| `config/` | `settings.py`（同步/回测/挖掘参数）、`strategy_params.py`、`thresholds.py`、`personal_config.py` |
| `utils/` | `api.py`（ok/fail 响应封装）、`serialization.py`（NaN/枚举安全 JSON）、`llm_client.py`（OpenAI 兼容 LLM 调用，env 覆盖 yaml）、`cache.py`、`timing.py`、`finance_data.py` |

### Data flow

```
东财/AkShare/baostock → core/sync.py（+em_guard 三道防线、data_cleaner 六级过滤）
                      → SQLite core/quant.db（daily_price 111万+ 行，WAL 模式）
                      → strategy 融合打分（同步管线写 daily_price.fusion_score）/ stock_signal 推荐
                      → routes/* Flask API → dashboard.html
```

### 三周期（horizon）推荐与回测闭环

全系统统一的周期维度（`stock_signal.horizon` / `strategy_rules.horizon` 列，init_db 幂等迁移+回填）：

| horizon | 持仓期 | 策略来源 |
|---|---|---|
| short 短期 | 1~10 交易日 | `strategies.py` 5 信号融合（recalc_all_scores 逐日写库） |
| mid 中期 | 10~60 交易日 | `mid_long.scan_mid_term` → `strategy.py` 综合评分≥65 且当日 BUY_SIGNAL（ATR 止损止盈） |
| long 长期 | 60+ 交易日 | `mid_long.scan_long_term`：MA60/MA120 趋势 + 低波 + 回撤打分（≥2/3 命中） |

- 中/长线只评最新交易日写一条信号；`stock_signal` 唯一索引为 `(scan_date, code, horizon)`。
- `/api/investor/today` 返回 `{groups: {short, mid, long}}`（每组 fusion_score DESC 取 N，`items`=short 组别名兼容旧前端，`?horizon=` 单组过滤）；long 组在 `stock_info.pe_ttm` 存在时剔除 ≤0 或 >100；建仓计划金额读 `config/personal_config.py` 的 `POSITION_PLAN_ACCOUNT/MAX_PCT`。
- 规则一键回测：`POST /api/backtest/run {rule_id}` → `rule_engine.run_rule_backtest`（factor_lib 向量化条件评估 → 次日开盘买入 → holding_max/止损/止盈出场 → 基准对比）→ 完成后 `service._writeback_rule_fitness` 回写 strategy_rules（库存百分数，fitness 用小数公式 `annual*1.5+win+min(sharpe,3)*0.5-|mdd|`）。
- `GET /api/backtest/rules` 返回 is_active=1 规则供前端下拉；回测报告含沪深300 基准对比行。
- 自然语言回测：`POST /api/backtest/parse_intent {text}` → `nl_parser.parse_backtest_intent`（LLM 按 conditions catalog 输出 JSON → 白名单校验/补默认；无 `LLM_API_KEY` 或调用失败时降级本地正则提取止损/止盈/持股/区间/资金），前端一键填充条件与参数表单。

## Key architectural notes

- **db.py 函数迁移中**：`upsert_daily_price`/`get_daily_price` 等已标 deprecated，新代码优先用 `core/repository/` 对应模块；deprecated 函数仍可工作。
- **写库路径**：行情写入统一走 `core/sync.py::_batch_write_daily_price`（含 data_cleaner 行级校验 + 成交量手→股换算），不要绕过。
- **em_guard 三道防线**：所有东财接口调用必须经 `cached_fetch`（TTL 缓存 → 窗口频控 → 每日配额），超限返回 stale 数据而非失败。
- **JSON 安全**：Flask jsonify 不序列化自定义枚举与 NaN，统一用 `utils/serialization.py` 与 `utils/api.py` 的 ok/fail；枚举显式 `.value`。
- **测试约定**：`tests/` 内文件用 `sys.path.insert(0, dirname(dirname(__file__)))` 引入项目根；需要真实库的测试用 `pytest.mark.skipif` 按 quant.db 是否存在跳过；网络基准（test_em_speed）模块级 skip，仅手动运行。
- **Windows 环境**：批处理脚本含中文时需注意 CMD 代码页；PowerShell 不支持 `&&`，用 `;`。

## File naming conventions

- 配置文件 `config/llm_config.yaml` 已 gitignore — 勿提交密钥
- `data_cache/`、`reports/`、`.sync_cache/`、`.cache/`、`.uploads/` 为 gitignore 的运行时目录
- `tests/_*.py` 下划线前缀为调试脚本，pytest 不收集

## 已废弃（勿再引用）

- `docs/ARCHITECTURE.md`：描述旧 Streamlit 架构，已过时，结构以本文档为准
- `docs/compose/plans/2026-06-19-aiquant-fullstack-upgrade.md`：FastAPI+PG+Vue 升级计划，2026-07 决策废弃，维持 Flask+SQLite 现状
- "三省六部" agents/ministries 架构：已随重构移除（原 risk/ 包为损坏残骸，已删除）
