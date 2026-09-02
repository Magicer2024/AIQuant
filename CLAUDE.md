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
| `strategy/` | `scorer.py`（回测/因子流式行情加载工具，仅供 backtest 复用）、`factor_lib.py`、`indicators.py`、`rules_store.py`（含 derive_horizon）、`strategies.py`（短线 5 信号融合）、`mid_long.py`（中线综合/长线趋势扫描）、`strategy.py`（中线综合评分库，被 mid_long 调用）、`intent/parser.py`（自然语言选股）、`stock_deep.py`（个股深度分析：量价关系 + 涨跌节奏 + 买卖建议，供首页「个股深度」面板；含 NaN 安全 KDJ 与 `run_full_market_scan`/`get_market_signal_latest` 全市场深析扫描，结果落库 `stock_deep_signal` 表） |
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
| mid 中期 | 10~60 交易日 | `mid_long.scan_mid_term` → `strategy.py` 综合评分≥65 且当日 BUY_SIGNAL（ATR 止损止盈，止损倍数 `mid_atr_stop_mult` 可调，默认 2.5） |
| long 长期 | 60+ 交易日 | `mid_long.scan_long_term`：MA60/MA120 趋势 + 低波 + 回撤打分（≥2/3 命中；止损 MA120×`long_ma_stop_mult` 可调，默认 0.95，止盈 +50% 为兜底、实盘走移动止盈） |

- 中/长线只评最新交易日写一条信号；`stock_signal` 唯一索引为 `(scan_date, code, horizon)`。
- **每日盘后增量重算**：`daily_sync_by_date` 同步后调 `core/sync.py::recalc_incremental_signals`，只重写最新交易日的 short/mid/long/动量信号（历史日信号保留，供回测读取）；全量 `recalc_all_scores`（约 5 分钟，逐历史日写库）仅在数据回填/修正后手动触发（CLI `python core/sync.py full`）。两路径共用 `_build_signal_records`（scan_date 参数区分逐日/仅当日）。增量路径读 scan_date 前 700 自然日窗口（停牌密集股窗口 <260 行时回退全历史，保证长线 min_rows=250 口径一致），并复用 `daily_price` 分数列（`reuse_scores=True`，纯抄底权重下 weighted_avg 与 max 数学等价）。前端兜底 `ensureTodayRecs` 用 `/investor/today` 的 `latest_trade_date` 判断推荐是否最新，非最新才调 `POST /api/sync/recalc_incremental`（异步、秒级~分钟级），替代原 `/sync/recalc` 全量 5 分钟。
- **市场冷热（market_regime）**：`routes/investor.py::_compute_market_regime` 用多日宽度 composite（近 5 日涨跌家数比 + 全市场站上 MA20 宽度 + 沪深300/中证500 5 日斜率，10 分钟缓存），替代原单日 `AVG(pct_change)`（避免"一天定生死"）；数据不足自动回退单日均值口径。
- **交易日历缓存**：`core/trade_calendar.py` 缓存 akshare `tool_trade_date_hist_sina` 全量交易日（12h TTL，失败 60s 后重试、按工作日近似降级）；`core/sync.py::is_trading_day` 与 `price_repo.get_last_trade_date` 共用，不再每次打网络接口。
- `/api/investor/today` 返回 `{groups: {short, mid, long}}`（short 组：**龙虎榜动量信号优先占名额**（strategy=隔日动量，fusion≥30 天然过门控），剩余名额由低扩展度抄底补足——`fusion≥short_conf_gate`(默认22) 门控 + `pct_above_ma20` 升序，regime 天花板 cold→0 / cool→2 / normal→4（`_REGIME_CAP`）；mid/long 组：fusion_score DESC 取 N（2026-08 起受 `MID_LONG_REGIME_CAP` 约束：cold→0 / cool→mid 2、long 1，展示层生效）；`items`=short 组别名兼容旧前端，`?horizon=` 单组过滤）；long 组在 `stock_info.pe_ttm` 存在时剔除 ≤0 或 >100；建仓计划金额读 `config/personal_config.py` 的 `POSITION_PLAN_ACCOUNT/MAX_PCT`。
- 规则一键回测：`POST /api/backtest/run {rule_id}` → `rule_engine.run_rule_backtest`（factor_lib 向量化条件评估 → 次日开盘买入 → holding_max/止损/止盈出场 → 基准对比）→ 完成后 `service._writeback_rule_fitness` 回写 strategy_rules（库存百分数，fitness 用小数公式 `annual*1.5+win+min(sharpe,3)*0.5-|mdd|`）。
- `GET /api/backtest/rules` 返回 is_active=1 规则供前端下拉；回测报告含沪深300 基准对比行。
- 自然语言回测：`POST /api/backtest/parse_intent {text}` → `nl_parser.parse_backtest_intent`（LLM 按 conditions catalog 输出 JSON → 白名单校验/补默认；无 `LLM_API_KEY` 或调用失败时降级本地正则提取止损/止盈/持股/区间/资金），前端一键填充条件与参数表单。

## Key architectural notes

- **db.py 函数迁移中**：`upsert_daily_price`/`get_daily_price` 等已标 deprecated，新代码优先用 `core/repository/` 对应模块；deprecated 函数仍可工作。
- **写库路径**：行情写入统一走 `core/sync.py::_batch_write_daily_price`（含 data_cleaner 行级校验 + 成交量手→股换算），不要绕过。
- **追高否决过滤（chase filter）**：短线写库前（`recalc_all_scores` 纯抄底分支）除趋势闸门/质量过滤外，还须过 `strategy/rec_filters.py::chase_filter_series`（config `CHASE_FILTER`）：连续涨停≥3 当日、近 5 日内出现 3 连板冷却期、当日涨停打开（盘中触板未封住）、近 3 日涨幅≥25%，任一命中即不写 stock_signal。背景：000815 三连板启动期被趋势闸门挡掉、涨停打开放量日反而高分进推荐；持仓口径回测被过滤信号胜率 27.9%/均值-2.6%/止损率 64%（远差于保留信号），过滤后信号池均值由负转正。改阈值在 config/strategy_params.py，勿把连板阈值调 >3。
- **扩展度否决过滤（extension filter）**：同一过滤链还需过 `rec_filters.py::extension_filter_series`（config `EXTENSION_FILTER`，2026-08 P0-1.2）：价相对 MA20 偏离 >12% 硬否决（易均值回归尾部）。背景：diag 实测候选均值偏离 MA20 +6.3%、>8% 占 26.9%，高位组 T+1 OC 48.3%/+0.04% 劣于低位组 52.2%/+0.28%；落地后（增量重算）最大偏离 +38.3%→+11.7%、>8% 占比→18.4%。⚠ 2026-08 短线推荐改造（docs/short-reco-dynamic-count-plan.md）后：**融合分排序被证伪无选择力**（2020+ 全市场回测 rank 1-4 桶 -0.031% vs 5-8 桶 +0.298%，最高分=已涨最多=扩展度最高=未来空间最小），短线推荐改为「fusion≥`short_conf_gate`(默认22) 门控 + 按 `pct_above_ma20`(价/MA20-1) 升序取前 4」的 S4 口径（`stock_signal` 新增 `pct_above_ma20` 列，落库与回填见 `core/sync.py::_build_signal_records` / `tools/backfill_signal_extension.py`）。
- **短线快进快出（2026-08 P0-1.1）**：`TUNABLE_PARAMS` 止损默认 -5%、止盈 +8%、`exit_advisor.HORIZON_MAX_HOLD["short"]=3`（2026-08-09 由 1→3：v2 转正后 edge 在 T+3/T+5，回测 OC T+1 +0.10%→T+3 +0.181%→T+5 +0.226%；8-04/8-05 实盘验证 T1 负但 T2/T3 回正，1 天了结恰卖在回踩确认期最低点）；`routes/investor.py` gap guard 语义为"跳空 > 止盈价才 sell（错过不追），平开/小涨保持 buy"，`T1_GAP_GUARD.max_gap_pct`（2.5%）仅作止盈缺失时回退；`today_recommendations` 在 `market_regime in ("cold","cool")` 时 short 组推荐减半、cold 时整组强制 wait（P1-2.3）。回测对比：买回踩 v2 平滑版（tools/_eval_pullback.py）T+1 OC 胜率 51.5%→55.5%、均值 +0.104%→+0.211%，候选量减半——**2026-08-09 已转正**：`SHORT_ENGINE="pure_bottom_v2"` 为线上默认，`recalc_all_scores` 全量重算（4371 只、57251 条历史信号）；转正后 diag 实盘历史验证与回测吻合：T+1 OC 胜率 55.4%/+0.158%（回测预测 55.5%/+0.211%）、最新日候选 MA20 偏离均值 +3.11%、>8% 占比 1.6%。回滚：切回 "pure_bottom" + 重算。
- **个股深度买点回归「低吸」（2026-08 P0-1.3）**：`strategy/stock_deep.py` 的买卖建议买点原偏「追涨确认」——`_signal_at` 奖励站上MA20/MACD金叉/放量上涨，天然在涨上来后才触发，导致买点扎堆于阶段高点（用户反馈"买点买在局部高点"）。diag（tools/_eval_deep_buypoints.py，240 只抽样 14705 个 buy/add 信号）：买点近20日区间位置 frac20 中位 0.82、≥0.75 占 **60.5%**、≥0.9 或创20日新高组 T+3 胜率仅 ~56%，而 frac20<0.5 组 T+3 胜率 ~70%；**区间位置 frac20 是比 MA20 扩展度更干净的高位信号**（剔 ext>8 反而变差——高扩展其实有短期动量）。落地：`_signal_at` 新增「区间位置守卫」（frac20≥0.9 或创20日新高 −3、≥0.75 −1、<0.5 +1），`_rhythm_adjust` 新增 `rng` 参数——价格已在顶部时抵消"上升初期/中段"顺势加分。before/after（同口径 OC）：买点 14705→11030（−25%），T+1 胜率 58.0%→**61.1%**、T+3 61.3%→**67.3%**、T+5 62.3%→**69.8%**；frac20≥0.75 占比 60.5%→**24.2%**、扩展度均值 +4.52%→+1.17%。不写库，前端字段结构不变（仅 reason/risk 新增区间文案）；`stock_deep_signal` 表随下次全市场扫描（每日盘后）自动用新逻辑重算。
- **隔日动量大跌/跌停过滤（2026-08）**：`NEXT_DAY_MOMENTUM.max_down_pct`（默认 -7%，设 0/None 关闭）——龙虎榜净买占比≥10% 但**当日跌幅 ≤ -7%** 的信号为明确负期望子集（历史 T+1 -1.0%/胜率47%、T+2 -3.6%/34%、T+5 -5.8%/25%），不产生隔日动量信号；`exclude_limit_up` 原只排涨停不排跌停，麦迪科技 603990 2026-08-13 跌停日（-9.99%）净买 10.6% 即因此误入今日短线推荐。落点在 `strategy/next_day_momentum.py`（`scan_next_day_momentum` 双向检查）+ `tools/backfill_lhb_momentum.py`（候选 SQL 同口径）；违规历史信号已从 stock_signal/recommend_outcome 清理（11 条）。
- **中/长线评估口径修复 + 出场参数可调（2026-08）**：
  - **复盘口径**：`core/outcome_tracker.py::evaluate_outcomes` 按 horizon 分派——mid/long 改走 `strategy/exit_advisor.py::evaluate_exit_by_prices`（逐日模拟移动止盈 + 周期持仓上限），窗口拉长（mid ~70 交易日 / long ~130 交易日），long `max_hold=None` 不设强制出场上限、未出场 `exit_return` 保持 NULL 由后续交易日继续评估；short 保持原 15 天/触及止损止盈/T+10 逻辑不变。背景：旧逻辑把 60+ 天长线按 10 天强制了结、+50% 止盈在窗口内不可达 → 复盘假象「0 止盈、长线 36% 胜率/-0.58%」；修复后与出场跟踪同口径（中线止盈 0→9、止损 11→5，长线止损 25→14，均按收盘价判定）。
  - **止损参数可调**：`TUNABLE_PARAMS` 新增 `mid_atr_stop_mult`（默认 **2.5**，旧硬编码 2.0）与 `long_ma_stop_mult`（默认 **0.95**，旧 0.99），生效于 `strategy/mid_long.py`（scan_mid_term 用 ATR×倍数算止损/止盈、RR 固定 2.5；scan_long_term 用 MA120×倍数算止损）。回测依据 `tools/backtest_midlong_exit.py`（向量化全历史，entry=T+1 open）：放宽止损降误扫——中线 2.0→2.5 止损率 63%→58%、胜率 34.8%→36.6%、均值 +2.30%→+2.97%、PF 2.50→2.38（hold=60）；长线 0.99→0.95 止损率 52%→44%、胜率 44.8%→49.3%、均值 +3.97%→+4.65%、PF 1.95→1.67（hold=120，移动止盈 +20%启动/15%回撤）。代价是 PF 略降（单笔亏损变大）。改动后需 `recalc_incremental_signals`（每日盘后自动）刷新 stock_signal 止损价；回滚：DB 覆盖或改回旧默认 + 重算。
  - **regime 天花板**：`MID_LONG_REGIME_CAP`（cold→0 / cool→mid 2、long 1 / neutral+ 不限），`today_recommendations` 展示层生效（与短线 `_REGIME_CAP` 同源），不影响写库与复盘。
- **短线推荐链路（2026-08-26 调整，用户反馈「出场跟踪短线累计收益 -37.77%」）**：拆解（`tools/_eval_short_long.py`，2024-07~2026-08 复现 + 改动前后对照，只读不改库）后落地三件事：
  - **停用「缩量回踩」**：`PULLBACK_DIP.enabled=False` + 三处出口（今日推荐/出场跟踪/复盘入库）排除 `strategy != '缩量回踩'`。它是 -37.77% 的绝对主源（实时窗 56 段 / -53.75% / 均值 -0.96% / 胜率 41%）；2 年回测去掉它后组合收益 +25.18%→+113.12%、回撤 -28.33%→-15.19%（无闸门口径）。原「train/test 双正」在真实复盘窗被证伪（挖掘说明亦自陈「近 6 月 -0.28%」），典型 OOS 失效。回滚：置 True 并恢复 `short_order_clause` 优先级表。
  - **大盘走弱闸门**：`core/outcome_tracker.py::short_market_gate_sql`（弱市日只保留正期望的「隔日动量」占名额，抄底/回踩类不推；三处出口共用，与 `short_t1_filter_sql` 同构）+ `_short_weak_dates`。**2026-08-26 v2 放宽**（初版过紧：ANY 指数略破 MA20 即判弱，2 年窗口 73% 交易日被标弱，用 ~25% 收益换 2.6pp 回撤）；现口径 = `仅 regime==cold 或 指数收盘<MA20×0.98 或 5日动量<-1.5%`（弱市日 386→304/525）。2 年回测：+84.47%→+106.60%、回撤 -12.61%→-12.48%。
  - **隔日动量「快进快出」实验（#3）已回退**：原拟为隔日动量单独设「次日收盘了结」，但 **A 股 T+1 下「买 T+1 开→T+1 收」不可执行**（买入当天禁卖，最早 T+2），其 `76.5%/+2.14%` 的 T+1 OC "edge" 是同日内往返假象（`mine_next_day.py` 的 `nd_oc` 口径受此限制）；可执行的最快出口（T+2 收）仅 +0.53%/47% 胜率（vs 通用持仓 +0.14%/PF1.05），且 n=17 样本小、在 8 月顶部一单（600227）把 +6.62 未实现浮盈锁成 -3.05。2 年平均略优但短期噪声大，**用户决定回退**，隔日动量回归通用移动止盈/持 10 天；现值 `get_max_hold`（`strategy/exit_advisor.py`）无 strategy 感知。复跑该实验见 `tools/_eval_short_long.py` 的 `t2close` 列与「隔日动量出口方式对比」。
  - **净效果**：实时出场跟踪短线累计收益 -37.77% → **-5.5%**（胜率 47.1%）；2 年长周期（现生产口径 = 去缩量回踩 + 宽松闸门）**+106.60%、最大回撤 -12.48%**。
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
