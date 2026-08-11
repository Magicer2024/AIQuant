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
| `core/` | SQLite 数据层：`db.py`（~12 表，get_conn 上下文管理器）、`sync.py`（行情同步主流程）、`data_fetcher.py`（baostock/AkShare 多源）、`data_cleaner.py`（L1-L6 脏数据过滤）、`em_realtime.py`/`em_kline.py`（东财接口，`fetch_klines_by_date` 降级链：东财直连 → 腾讯 qt.gtimg.cn 批量（500 只/次，全 A ≈10s）→ AkShare）、`em_guard.py`（缓存+频控+配额三道防线）、`task_queue.py` |
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
- **每日盘后增量重算**：`daily_sync_by_date` 同步后调 `core/sync.py::recalc_incremental_signals`，只重写最新交易日的 short/mid/long/动量信号（历史日信号保留，供回测读取）；全量 `recalc_all_scores`（约 5 分钟，逐历史日写库）仅在数据回填/修正后手动触发（CLI `python core/sync.py full`）。两路径共用 `_build_signal_records`（scan_date 参数区分逐日/仅当日）。增量路径读 scan_date 前 700 自然日窗口（停牌密集股窗口 <260 行时回退全历史，保证长线 min_rows=250 口径一致），并复用 `daily_price` 分数列（`reuse_scores=True`，纯抄底权重下 weighted_avg 与 max 数学等价）。前端兜底 `ensureTodayRecs` 用 `/investor/today` 的 `latest_trade_date` 判断推荐是否最新，非最新才调 `POST /api/sync/recalc_incremental`（异步、秒级~分钟级），替代原 `/sync/recalc` 全量 5 分钟。
- **市场冷热（market_regime）**：`routes/investor.py::_compute_market_regime` 用多日宽度 composite（近 5 日涨跌家数比 + 全市场站上 MA20 宽度 + 沪深300/中证500 5 日斜率，10 分钟缓存），替代原单日 `AVG(pct_change)`（避免"一天定生死"）；数据不足自动回退单日均值口径。
- **交易日历缓存**：`core/trade_calendar.py` 缓存 akshare `tool_trade_date_hist_sina` 全量交易日（12h TTL，失败 60s 后重试、按工作日近似降级）；`core/sync.py::is_trading_day` 与 `price_repo.get_last_trade_date` 共用，不再每次打网络接口。
- `/api/investor/today` 返回 `{groups: {short, mid, long}}`（每组 fusion_score DESC 取 N，`items`=short 组别名兼容旧前端，`?horizon=` 单组过滤）；long 组在 `stock_info.pe_ttm` 存在时剔除 ≤0 或 >100；建仓计划金额读 `config/personal_config.py` 的 `POSITION_PLAN_ACCOUNT/MAX_PCT`。
- 规则一键回测：`POST /api/backtest/run {rule_id}` → `rule_engine.run_rule_backtest`（factor_lib 向量化条件评估 → 次日开盘买入 → holding_max/止损/止盈出场 → 基准对比）→ 完成后 `service._writeback_rule_fitness` 回写 strategy_rules（库存百分数，fitness 用小数公式 `annual*1.5+win+min(sharpe,3)*0.5-|mdd|`）。
- `GET /api/backtest/rules` 返回 is_active=1 规则供前端下拉；回测报告含沪深300 基准对比行。
- 自然语言回测：`POST /api/backtest/parse_intent {text}` → `nl_parser.parse_backtest_intent`（LLM 按 conditions catalog 输出 JSON → 白名单校验/补默认；无 `LLM_API_KEY` 或调用失败时降级本地正则提取止损/止盈/持股/区间/资金），前端一键填充条件与参数表单。

## Key architectural notes

- **db.py 函数迁移中**：`upsert_daily_price`/`get_daily_price` 等已标 deprecated，新代码优先用 `core/repository/` 对应模块；deprecated 函数仍可工作。
- **写库路径**：行情写入统一走 `core/sync.py::_batch_write_daily_price`（含 data_cleaner 行级校验 + 成交量手→股换算），不要绕过。
- **追高否决过滤（chase filter）**：短线写库前（`recalc_all_scores` 纯抄底分支）除趋势闸门/质量过滤外，还须过 `strategy/rec_filters.py::chase_filter_series`（config `CHASE_FILTER`）：连续涨停≥3 当日、近 5 日内出现 3 连板冷却期、当日涨停打开（盘中触板未封住）、近 3 日涨幅≥25%，任一命中即不写 stock_signal。背景：000815 三连板启动期被趋势闸门挡掉、涨停打开放量日反而高分进推荐；持仓口径回测被过滤信号胜率 27.9%/均值-2.6%/止损率 64%（远差于保留信号），过滤后信号池均值由负转正。改阈值在 config/strategy_params.py，勿把连板阈值调 >3。
- **扩展度否决过滤（extension filter）**：同一过滤链还需过 `rec_filters.py::extension_filter_series`（config `EXTENSION_FILTER`，2026-08 P0-1.2）：价相对 MA20 偏离 >12% 硬否决（易均值回归尾部）。背景：diag 实测候选均值偏离 MA20 +6.3%、>8% 占 26.9%，高位组 T+1 OC 48.3%/+0.04% 劣于低位组 52.2%/+0.28%；落地后（增量重算）最大偏离 +38.3%→+11.7%、>8% 占比→18.4%。软降权（RSI 惩罚）与"扩展度调整排序"（adj_score）经回测证伪未实现/已砍——fusion_score 排序本身有选择力，只砍极端高位、不惩罚中间区间。
- **短线快进快出（2026-08 P0-1.1）**：`TUNABLE_PARAMS` 止损默认 -5%、止盈 +8%、`exit_advisor.HORIZON_MAX_HOLD["short"]=3`（2026-08-09 由 1→3：v2 转正后 edge 在 T+3/T+5，回测 OC T+1 +0.10%→T+3 +0.181%→T+5 +0.226%；8-04/8-05 实盘验证 T1 负但 T2/T3 回正，1 天了结恰卖在回踩确认期最低点）；`routes/investor.py` gap guard 语义为"跳空 > 止盈价才 sell（错过不追），平开/小涨保持 buy"，`T1_GAP_GUARD.max_gap_pct`（2.5%）仅作止盈缺失时回退；`today_recommendations` 在 `market_regime in ("cold","cool")` 时 short 组推荐减半、cold 时整组强制 wait（P1-2.3）。回测对比：买回踩 v2 平滑版（tools/_eval_pullback.py）T+1 OC 胜率 51.5%→55.5%、均值 +0.104%→+0.211%，候选量减半——**2026-08-09 已转正**：`SHORT_ENGINE="pure_bottom_v2"` 为线上默认，`recalc_all_scores` 全量重算（4371 只、57251 条历史信号）；转正后 diag 实盘历史验证与回测吻合：T+1 OC 胜率 55.4%/+0.158%（回测预测 55.5%/+0.211%）、最新日候选 MA20 偏离均值 +3.11%、>8% 占比 1.6%。回滚：切回 "pure_bottom" + 重算。
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
