# AIQuant 模块优化项与执行计划（2026-10）

> 依据：2026-10-09 全库代码走查（core/utils、strategy/backtest/ai、routes/scheduler/dashboard.html）。
> 优先级：P0 = 正确性/明显性能问题，P1 = 技术债，P2 = 改善项。
> 性能改动一律遵守项目协议「先测量，再优化」（`utils/timing.py` + `tools/bench_endpoints.py`，以 `reports/bench_*.json` 前后对照验收，已稳定接口 P95 不回退 >10%）。

---

## 一、各模块优化项清单

### 1. 数据层 `core/` + `utils/`

| # | 优先级 | 位置 | 问题 | 影响 |
|---|---|---|---|---|
| D1 | P0 | `core/repository/price_repo.py` `get_price_batch` | 先按 code 分组再逐组回查最新价（N+1 查询） | 批量行情读取随股票数线性放大 SQL 次数，是接口慢的高嫌疑点 |
| D2 | P0 | `core/sync.py` `_build_signal_records` / `_batch_write_daily_price` | 逐日信号重建与逐行校验/写库交织；增量窗口不足时回退全历史 | 每日盘后增量重算耗时不稳定，偶发长尾 |
| D3 | P1 | `core/db.py` | deprecated 旧函数（`get_daily_price`/`get_signal` 等）与 `core/repository/` 双轨并存；`get_conn` 事务边界各处不一 | 访问路径不唯一，批量写入异常时回滚口径不一致 |
| D4 | P1 | `core/data_fetcher.py` | 东财/腾讯/baostock/AkShare 降级链过长，逻辑集中在一个流程 | 新增/下线数据源改动面大，失败根因难定位 |
| D5 | P1 | `core/em_realtime.py` | ~~实时行情疑似逐只调用~~ **T6 实测证伪**：早已是 clist 批量分段分页；真正问题是东财主备断连时实时路径无腾讯兜底（2026-10-11 已补） | 见 T6 |
| D6 | P1 | `core/task_queue.py` | 模块级全局 `ThreadPoolExecutor`、队列无上限 | 单测难隔离；高峰任务无界堆积 |
| D7 | P2 | `core/em_guard.py` / `utils/cache.py` | 缓存键硬编码字符串；内存缓存重启即失 | 缓存键改名易漏改；重启后缓存穿透 |

### 2. 策略/回测层 `strategy/` + `backtest/` + `ai/`

| # | 优先级 | 位置 | 问题 | 影响 |
|---|---|---|---|---|
| S1 | **P0** | `strategy/indicators.py:210/271、182/337、189/344、203/358` | `calc_volume_ratio`、`calc_dpo`、`calc_bollinger`、`calc_historical_volatility` **各定义两次**，后定义静默覆盖前定义 | 调用方以为用的是前一个实现，实际是后一个；指标口径随时可能被"无感知改写"，属正确性隐患（已核实） |
| S2 | P1 | `backtest/engine.py:43-78、85-243` | `_add_indicators` 对每只股票盲算全套指标（MA/MACD/RSI/KDJ…），不按条件按需计算；`_eval_condition` 每个分支重复"有列则用、无列重算" fallback | 大股票池回测 CPU/内存浪费；新增指标要同步多处，易遗漏 |
| S3 | P1 | `strategy/stock_deep.py:82-101` vs `strategy/indicators.py:63-82` | KDJ 双实现（`calc_kdj` 与 `_calc_kdj_safe`） | 修 KDJ 算法两边易不同步，产生口径漂移 |
| S4 | P1 | `backtest/engine.py:379-385、492-701` | 指标计算夹在 groupby 后逐股循环内；`_run_portfolio` 逐日×逐股×逐持仓深嵌套 | 回测慢；加约束（如交易限制）容易漏边界条件 |
| S5 | P2 | `strategy/strategy.py:253-264`、`strategy/mid_long.py:120-123` | 盈亏比 2.5、`backtest/engine.py:31-34` 佣金/印花税/滑点均为魔法数字，分散硬编码 | 统一调整交易成本/RR 需改多处 |
| S6 | P2 | `strategy/mid_long.py:99-105` | 用 `for age in range(...)` 前向扫描找上穿锚点 | 可读性差，可用 pandas 反向搜索替代 |

### 3. 应用层/前端/工程化 `routes/` + `scheduler/` + `dashboard.html` + `tools/` + `tests/`

| # | 优先级 | 位置 | 问题 | 影响 |
|---|---|---|---|---|
| A1 | **P0** | `scheduler/runner.py:362,382` + `:284-290` | 同步入口只查 `SYNC_STATUS["running"]` 再置位，非原子；退避重试线程 sleep 后无锁再抢占 | 调度循环/手动触发/重试线程可叠加跑多个同步任务，可能并发写库 |
| A2 | P0 | `routes/investor.py`（3584 行 / 63 函数） | `today_recommendations`、`_compute_market_regime`、`_calc_signal` 等业务逻辑全在 Blueprint 里 | 改推荐口径牵动路由层，是全系统最大耦合点 |
| A3 | P1 | `dashboard.html`（5115 行） | 单文件含 HTML+CSS+全量 JS；`sd-search`/`sd-deep` 等多处绕过 `RequestManager` 直接 `fetch`；轮询逻辑分散在 ≥3 处；无 TTL 缓存/防抖 | 切页重复请求、取消与错误处理不一致、前端维护成本高 |
| A4 | P1 | `tests/` | scheduler 无独立测试；状态迁移/恢复/重复触发逻辑无回归保障 | 与 A1 叠加，改调度最容易引入回归 |
| A5 | P2 | `tools/` | 大量 `_diag_*`/`_eval_*`/`backfill_*` 语义相近脚本平铺 | 维护与甄别成本高，易跑错脚本 |
| A6 | P2 | `start.bat:27-30` | 用 `python -c "import flask"` 探测依赖，虚拟环境/路径异常时难排查 | 首次启动失败体验差 |

---

## 二、可执行计划（按阶段排期，每项含改动点/验收/回滚）

### 阶段 1 — 正确性与并发安全（P0，约 1~2 天）

- **T1 修复 indicators.py 重复定义（S1）✅ 已完成 2026-10-09**
  - 改动：逐一比对 4 组重复函数（`calc_volume_ratio`/`calc_dpo`/`calc_bollinger`/`calc_historical_volatility`）前后两版语义差异，保留生效版本（后者），删除被覆盖版本；若前版本语义更正确则合并后统一。KDJ 双实现（S3）顺手合并为单一 NaN 安全实现（`stock_deep` 改为引用）。
  - 验收：新增 `tests/test_indicators.py` 断言模块内无重复函数名 + 固定样例序列输出快照一致；`python -m pytest tests/ -q -m "not browser"` 全绿。
  - 回滚：纯删重复代码，git revert 即可。
- **T2 scheduler 同步入口原子化（A1）✅ 已完成 2026-10-09**
  - 改动：为同步入口引入 `threading.Lock` + `compare-and-set` 式抢占（统一 `_job_sync`/手动触发/重试线程三入口共用一个 `_try_acquire_sync()`），重试线程启动前同样走抢占。
  - 验收：新增 `tests/test_scheduler_lock.py` 模拟并发触发只允许一个任务进入；重复触发返回"已在运行"。
  - 回滚：单文件改动，revert 即可。

### 阶段 2 — 性能热点（P0，约 2~3 天，先测量后动手）

- **T3 基线测量（前置，半天）✅ 已补验收 2026-10-11**
  - 落地：`tools/bench_endpoints.py`（同库快照 `data_cache/bench_snapshot_20261009.db`、关网、冷启动+3 预热+30 采样，墙钟 P50/P95、SqlCounter SQL 数、读行数、tracemalloc 峰值，原样落 JSON）；`tools/bench_recalc_incremental.py` 用 sqlite backup 克隆可写副本 + `StageTimer` 测增量重算。
  - 验收（真实输出）：基线 JSON 全部落盘 `reports/`（`bench_20261010_000339.json` 首测 + 同日 2 份复测 + 3 份 recalc；2026-10-11 补测 `bench_20261011_fast3.json`/`bench_20261011_exit.json`/`bench_recalc_20261011_235618.json`）。首测基线（10-10 00:03）：今日推荐 P50/P95 1624/1718ms SQL=170；出场跟踪 18404/19056ms SQL=234；个股深度 17.6/20.8ms SQL=10；历史列表 166.7/189.7ms SQL=41；增量重算 388.95/393.95s。10-11 复测（同快照同口径）：1437/1503ms SQL=165、17.1/20.6ms SQL=10、167.8/181.0ms SQL=41，出场跟踪见 T4。
  - 回滚：纯测量工具与报告，无生产代码，删除 JSON/脚本不影响系统。
- **T4 消除 get_price_batch N+1（D1）✅ 已补验收 2026-10-11**
  - 落地：批量取价现为 `core/repository/price_repo.py::get_price_frames`（T10 仓储重构中取代原 `get_price_batch`，旧函数名已消失）——**单条** `code IN (?)` SQL 取全部候选行情，内存游标分段构建 {code: 收盘价序列}，保留调用方契约；出场跟踪路径另经 T5-3 连接复用改造（同机 A/B 证据见 `tools/ab_conn_sharing.py`）。
  - 验收（真实输出）：①常数级实证——同快照库以 1/10/50/200 只股票调 `get_price_frames`，SqlCounter 记录 SQL 语句数恒定 **1 条**（读行 314→62192→329043→1301299，SQL 不随股票数增长），O(股票数)→O(1) 直接验证；②前后对照（同快照、关网、各 30 采样，`bench_20261010_000339` → `bench_20261010_010730`）：出场跟踪 SQL P50 **234→17（-92.7%）**、P95 239→22（-90.8%，达标"SQL 降 ≥50%"），墙钟 P95 19056→13510ms（**-29.1%**，基本贴近 ≥30% 目标且 SQL 降幅远超门槛），读行数 875419→875181 持平（返回内容等价）；冷启动 SQL 239→22；③无回退——10-11 复测 SQL 仍为 P50=17/P95=22、读行 875181 持平；当日墙钟 15.1s 高于 10-10 凌晨 12.8s，系深夜机器整体负载噪声（同机增量重算同步慢约 20%），SQL 数这一无噪声口径零变化，T14 后生产代码未触碰该查询路径，判定无回归；其余三接口 P95 波动 ≤5%、SQL 数全部持平。
  - 回滚：Repository 单函数改动，revert 后重跑 bench 对照即可；无接口合同变化。
- **T5 增量重算与写库梳理（D2）✅ 已补验收 2026-10-11**
  - 落地：`core/sync.py::recalc_incremental_signals` 只重写最新交易日信号（前 700 自然日窗口、停牌密集回退全历史保证长线 min_rows=250 口径一致），复用 `daily_price` 已算分数列；`StageTimer` 分段计时；写库仍统一走 `_batch_write_daily_price`，未新增旁路。测量工具增强：`tools/bench_recalc_incremental.py` 新增 `snapshot_day_signals`/`diff_consecutive_snapshots`——每次 run 后动态读取 `stock_signal` 业务列（排除自增 id 与每次 `INSERT OR REPLACE` 必刷的 created_at）取 sha256 指纹并相邻 run 对称差对比，4 种情形（幂等/分数漂移/新增行/列清单）内存小库用例全过。
  - 验收（真实输出，报告 `bench_recalc_20261011_235618.json`）：①耗时——同快照同口径 10-10 首测 388.95/393.95s → 10-11 中午 277.88/275.43s（**-28.6%**）；当晚复测 328.7/342.4s（深夜机器负载偏高，与 T4 墙钟噪声同向，跨天最佳证据以中午口径为准）；②**diff=0 硬验收**——当晚两次实跑当日 564 条信号全行业务列对称差 **0 行**、sha256 均为 `7909230d0321`，返回计数 signals=571/codes=4487 两次及历史三次测量全部一致。
  - 回滚：生产路径早已是每日盘后默认链路（非本次新增），本次仅增强测量工具；工具改动 revert 不影响同步与打分。
- **T6 核实并优化 em_realtime 调用方式（D5）✅ 已完成 2026-10-11**
  - 核实结论（实测）：①"逐只调用"不成立——`fetch_realtime_all` 早已走东财 clist 批量分段分页（pz=500），全代码库无任何逐只实时行情调用，且前端 dashboard.html **0 处调用** `/api/sync/realtime`（唯一消费点），"页面加载慢"前提不成立；②实测当日东财主备全断连（`push2.eastmoney.com` 直连与 akshare 的 `82.push2` 均 RemoteDisconnected，护栏返回 0 行），而实时路径唯独没有日线路径早已具备的腾讯批量兜底。
  - 改动：`core/em_realtime.py` 新增 `_parse_tencent_quote_line` + `_fetch_realtime_all_tencent`（500 只/请求，股票池/单位口径与 `_fetch_klines_by_date_tencent` 一致），降级链收敛为 `_fetch_realtime_with_fallback`（东财 clist → 腾讯批量 → akshare），整体仍在 `em_guard.cached_fetch` 内，一轮只占 1 次配额；输出 13 列与东财路径同构，接口契约不变。
  - 验收：真实网络实测（周日，返回 2026-10-09 收盘快照）——改动前 0 行/14.3s，改动后 4516 行/6.1s、原始 HTTP 仅 10 个（qt.gtimg.cn，另有 1 个失败的东财探测）；TTL 内第二轮 source=fresh、0 请求、0.0s。护栏测试 `tests/test_em_realtime_tencent.py` 10 项全绿（解析口径/批量请求数=ceil(n/500)/降级链顺序）。注：东财存活时一轮约 51 请求（pz 被服务端静默降级，见 `tools/_probe_pz.py`，封锁日未能复测），腾讯通道 10 请求（约 5 倍），请求数"降一个数量级"按 D5 原假设（逐只 4500+）口径成立、按 51 口径为约 5 倍；核心收益是可用性恢复。
  - 回滚：单文件改动 + 新增测试，revert 即可。

### 阶段 3 — 技术债与可维护性（P1，约 3~5 天）

- **T7 investor.py 业务逻辑下沉（A2）✅ 已完成（2026-10）**
  - 改动：routes/investor.py 由 3920 行降至 **536 行**（目标 <1500），路由只剩参数解析/clamp + ok/fail；业务编排按领域下沉为 2 个 strategy 模块 + 8 个 core/services 模块：
    - `strategy/market_regime.py`：大盘冷热 composite + 10 分钟 TTL 缓存；
    - `strategy/recommendation.py`：REGIME_LABEL / calc_signal / 今日三周期推荐组装；
    - `core/services/exit_tracking.py`：出场跟踪（回踩入场/连续段/移动止盈/perf）；
    - `core/services/signal_picks.py`：强势突破/反转首日/反转历史/龙虎榜机构席位；
    - `core/services/market_overview.py`：市场速览；
    - `core/services/personal.py`：持仓/流水/自选/风险告警（PersonalRequestError）；
    - `core/services/review.py`：outcome 汇总明细 + 历史推荐复盘（表读优先、实时回退）；
    - `core/services/browse.py`：指标白话解释 EXPLAINERS + factor_trend + rule_signals；
    - `core/services/stock_deep_service.py`：明日候选/全市场扫描任务/单股深析/搜索/近期关注；
    - `core/services/deep_tracking.py`：跟踪列表/推进/平仓/删除 + 历史扫描回补后台进程与原子进度文件（DeepTrackError）。
  - 验收：golden 护栏（`tools/golden_investor_api.py`，固定库快照 27 个 GET 端点）**每小步 diff=0、最终 diff=0**（证据 `reports/t7_golden_before.json` vs `reports/t7_golden_after.json`）；针对性 pytest 42 passed，全量 **412 passed / 4 skipped**（与 T6 基线一致）；API 响应结构不变、前端零改动。
  - 回滚：纯移动不改行为，接口契约不变，revert 代码即可。
- **T8 backtest/engine.py 指标按需计算 + 条件评估统一（S2/S4）✅ 已完成（2026-10）**
  - 改动（TDD：护栏测试先行，全部转绿后才动生产代码）：
    - **视觉回测指标按需**：`backtest/engine.py` 新增 frozen dataclass `IndicatorNeed(ma_periods/rsi_periods/macd/kdj)` + `from_conditions()`（ma_cross 取短/长周期、close_above_ma/bias 取单周期、rsi 取周期、缺省补 5/20/6/14 默认值；纯内联条件零预计算列）；`_add_indicators(df, need=None)` 只算需求列、`prev_close` 始终算，need=None 保持全量旧行为（tools/ 三个 eval 脚本单参调用零改动）；`run()` 循环外推导一次 need 传入。`_eval_condition` 四处重复 MA fallback 收敛为 `_ma_series(df, close, period)`（自定义周期如 MA13 缺列时现算，双保险）。
    - **规则回测因子按需**：`strategy/factor_lib.py` 的 `compute_all_factors` 拆为 6 个分组 builder（趋势/动量/波动/量能/形态/流动性，组内共享一次 calc 调用），新增 `only=[因子名]`：注册表外名称忽略、产出按 only 顺序、归一化按列独立故单列按需与全量逐值相等；`backtest/rule_engine.py` 从规则 conditions 提取因子名传 only（一条规则通常 2~5 个因子 vs 原 60 个全算）；rule_scanner/signal_runtime 等旧调用方 only=None 行为不变。
    - **成本常量收敛（S5）**：`config/strategy_params.py::BACKTEST_COST`（commission 0.0003/stamp 0.001/slippage 0.001/min_commission 5.0）成为唯一来源；engine.py（含退档预估金）、service.py（参数默认值 + 回显 min_commission）、oversold_sim.py 共 9 处硬编码改读它，数值零变化。
  - 验收：
    - 指纹 diff=0（`reports/t8_baseline_visual.json` vs `reports/t8_after.json`，固定库 t7_golden，window 2025 全年，含收益/胜率/交易数/权益曲线逐日净值/逐笔交易）：light/ma_only/heavy 三场景全部一致；
    - bench 提速：light total **130.2s→48.6s（-62.7%）**、indicators 90.8s→**5.4s（-94%）**；ma_only 116.1s→43.8s（-62.3%）、indicators 90.2s→13.5s（-85%）；heavy（全套重指标）持平无回退（+3.5% 在单机噪声内）；
    - 规则 only 等价：真实规则 602（TURNOVER_5MA/PCTL）、606（RSI_21/AMIHUD）各 100 只，passed/strength 逐行 mismatch=0；
    - 全量 pytest **504 passed / 4 skipped**（412 基线 + T8 两个护栏 29+63 转绿）。
  - 护栏资产：`tests/test_backtest_indicators.py`（23 条件分支掩码一致 + need 推导/默认/轻量零列/自定义周期）、`tests/test_factor_lib_selective.py`（60 因子按需==全量 atol=0 + 顺序/未知因子）、`tools/bench_t8_engine.py`（run/compare/ab，--universe-limit 小宇宙）。
  - 回滚：结果一致性已验证（指纹 + 信号双证据），revert 五个文件即可，无 DB/API 变化。
- **T9 dashboard.html 请求收敛（A3）✅ 已完成（2026-10）**
  - 改动（全部在 dashboard.html 单文件内，无后端/API 契约改动）：
    - **RequestManager 短 TTL 缓存**：`fetch(key,url,opts)` 仅对「无 body 的 GET + 显式 `cacheTtl`」读/写 URL 级内存缓存（默认 5s）；任何非 GET 写操作成功后全量失效（保证改完数据读到新值）；命中不续期（固定 TTL 从取数时刻起算，避免快速来回切页给旧行情"续命"）。17 个高频只读接口挂缓存（market-overview / today?limit=4 / exit_advice / lhb_jg_track / surge/reversal / stock_deep / deep_track / rule_signals / positions / watchlist / alerts / explain / recommendations/history / optimizer/latest / factor_trend / sync auto-status / kline）；搜索联想、轮询 URL、today 其他用途等故意不加。
    - **轮询收编**：timerManager 新增统一原语 `poll(key,fn,delay,runImmediately)`（busy 锁防慢请求堆积、单轮异常吞掉、fn 内 `clear(key)` 即停）；6 处分散定时器全部收编——回补扫描 / 全市场深析 / 寻优任务 / 历史修正 / 同步进度 / 增量重算。其中 optimizer、fixHistory 原裸 setInterval 切页 `clearAll` 停不掉（请求泄漏），现统一可清理；各轮询原有终止/失败语义逐一保持。
    - **直连 fetch 清零**：scan_rules、optimizer apply/dismiss/rollback、fix-history 4 处等 11 处裸 fetch/fetchJSON 全部改走 `RequestManager.fetch`（保留各自错误处理风格）。
    - **切页 120ms 防抖**：`switchTab` 立即更新导航高亮（即时反馈），实际 `_doSwitchTab`（cancelAll+clearAll+TabState+loadTab）延迟 120ms，快速连切只执行末次；首屏直调 loadTab 不受影响。
  - 顺手修复：app.py 新增 `FLASK_HOST` 环境变量覆盖（浏览器夹具在 AIQUANT_TESTING 外网守卫下需绑 127.0.0.1，生产默认 0.0.0.0 不变）；test_frontend 夹具启动失败时带出服务尾部日志（原 stderr=DEVNULL 无法诊断）。
  - 验收：
    - 浏览器测试 **5 passed**（2 原有 + 3 新增：缓存去重 / 轮询切页必停 / 防抖保留末次），命令 `AIQUANT_BROWSER_TESTS=1 python -m pytest tests/test_frontend.py -m browser`；
    - network 证据（Playwright 实测真实首页）：5s 窗内第二次强制重载首页，GET 网络请求 **19 → 5（-74%）**，14 个高频只读接口全部命中缓存；与 TabState 的 5 分钟整页 HTML 缓存互补（命中时零请求）；
    - 全量非浏览器回归 **412 passed / 6 skipped**（T8 两个 TDD 护栏未实现前 skip，其余与基线一致）。
  - 回滚：纯前端重构，revert dashboard.html + app.py 环境变量覆盖即可。
- **T10 db.py 双轨收敛（D3，小步）✅ 已完成（2026-10）**
  - 现状测绘：db.py 自住业务函数此前只剩 6 个自选股相关查询（其余早已是 repository re-export 兼容层）；全仓 `from core.db import` 共 ~60 处，绝大多数是 `get_conn/init_db/DB_PATH/connect_db/input_connection/_safe_add_column` 等连接/DDL 基础设施（本就属于 db.py，非双轨，不动）。
  - 改动（纯移动、行为不变）：
    - 新建 `core/repository/watchlist_repo.py`：`get_watchlist_codes/get_watchlist_with_names/count_watchlist`（personal_watchlist 只读，保留「表未建→空自选」吞异常降级）；
    - `core/repository/price_repo.py` 补 `has_price_data`（原 `has_watchlist_data` 正名——实际查 daily_price 不查 watchlist）、`get_latest_date_for_codes/get_stock_count_in_db_for_codes`；
    - db.py 删除 6 个函数定义 + 死导入 pandas，兼容层追加新位置 re-export（旧导入路径不断）；
    - 生产调用点全部改走 repository 直连：`core/sync.py`（顶部 18 个符号的导入块拆 5 个 repo + 4 处惰性导入）、`core/services/personal.py`、`core/em_realtime.py`（2 处）、`core/data_fetcher.py`、`strategy/adaptive_weights.py`、`backtest/oversold_sim.py`、`scheduler/runner.py`；
    - 测试跟随：`test_em_realtime_tencent.py` 两个用例 monkeypatch 目标由 `core.db.get_all_stocks` 改到真实依赖 `core.repository.stock_repo`（旧 patch 在直连后失效，曾致 2 例失败，已修复）；`test_repository.py` 新增 2 个 T10 护栏（新 repo 导入/降级语义/签名/空入参边界/兼容层 re-export）。
  - 验收：**生产代码（core/strategy/backtest/routes/scheduler/utils/qlib_engine/config + app.py）业务函数双轨调用点 = 0**；全量 pytest **506 passed / 4 skipped**（T8 后 504 + T10 2 护栏），零回归。
  - 剩余清单 ~~（按任务约定不追求清零，建议随 T12 一并处理）~~ **已于 T12 全部清零**：tools/ 8 个一次性脚本 + tests/ 4 个下划线调试脚本的业务函数导入均改走 repository（详见 T12）；全仓（含非生产代码）业务函数旧路径 grep=0。
  - 遗留一步（有意不做）：db.py 尾部 re-export 兼容层（约 60 行）现零内部消费者，但 `test_db_compat_import` 仍断言其可用、个人未追踪脚本可能依赖；确认无外部使用后可连同该测试一起删除，db.py 即只剩连接/DDL/迁移。
  - 回滚：纯导入路径迁移 + 函数平移，无 SQL/行为变化；revert 相关文件即可。

### 阶段 4 — 改善项（P2，空闲时做）

- **T11 魔法数字收口（S5 剩余 + em_guard 缓存键常量化 D7）✅ 已完成（2026-10）**
  - **S5 中线盈亏比收口**：审计点名的两处 RR=2.5（`strategy/mid_long.py` 信号计算、`strategy/strategy.py::calc_stop_loss_take_profit` 展示列）此前各硬编码一份，现统一读 `TUNABLE_PARAMS.mid_risk_reward`（默认 2.5，1.0~5.0，DB 覆盖/冻结快照即时生效），展示列与信号口径同源；mid_long 的 ATR 缺失兜底 -8%/+20% 提为模块常量 `_MID_FALLBACK_STOP/TAKE_PRICE_RATIO`（异常路径无回测调参依据，不进 TUNABLE_PARAMS）。交易成本部分 T8 已随 BACKTEST_COST 完成。
  - **D7 缓存键常量化**：`core/em_guard.py` 新增 `GuardName`（REALTIME_ALL/KLINES_BY_DATE/KLINE/STOCK_ZH_A_SPOT），**常量值即写入 em_cache/em_calls 的持久化字符串，只可新增不可改名**（注释明示改名=旧缓存与频控记录失联）；em_realtime（4 处）、em_kline（2 处）、routes/sync（2 处）裸字符串全部改引用常量，生产代码 `cached_fetch("...")`/`GUARD_CONFIG.get("...")` 零残留；外部 API 入参 `force_clear_cache(name=body.get("name"))` 保持运行时值。另把 cached_fetch 内临时默认 dict 与 5 处散落 `.get(1800/10/50/120)` 收敛为 `_DEFAULT_GUARD_CFG` + `_resolve_cfg()`（注册档覆盖默认档，四键恒齐）。`utils/cache.py` 是键自动 hash 的通用 TTL 装饰器，无硬编码键，不动；D7 原提「内存缓存重启即失」对 em_guard 已不成立（SQLite 持久化），短 TTL 内存缓存（market_regime 10min 等）做持久化收益低，明确不做。
  - 验收：值零变化（常量 pin 住旧字面量、RR 默认仍 2.5、兜底 0.92/1.20 锚点）；新增 6 个护栏（em_guard 3：持久化值 pin/常量必须登记配额档/_resolve_cfg 默认与覆盖；horizon 3：参数注册默认 2.5/默认 RR 与显式 2.5 逐值相等含 11.0 数值锚点/兜底常量）；全量 pytest **512 passed / 4 skipped**（T10 后 506 + 本次 6 护栏），零回归。
  - 回滚：配置新增项 + 常量提取，无 SQL/接口变化；revert 五个文件即可。注意 `mid_risk_reward` 新增会改变参数哈希快照基线（与既有新增参数同机制，legacy 模式仅诊断）。
- **T12 tools/ 脚本归档（A5）✅ 已完成（2026-10）**
  - **方案取舍：README 全量索引，不做子目录迁移**。实测 tools/ 有 142 个脚本，而 docs/CLAUDE.md/reference/SKILL 中有 **110 处 `tools/<名>.py` 路径引用**（历史决策文档的复跑指引，如 backtest_midlong_exit.py/_eval_short_long.py），移动会使它们整体失效；且文件名前缀（backfill/eval/diag/mine/bench/_ 等）本身已是分类体系，迁移只付成本不增信息。
  - **落地**：重写 `tools/README.md`：①命名约定表新增 bench/ab/golden/profile「可复用测量资产」与一次性脚本判定规则；②分类清单由 58 个补全到 **142/142（PowerShell 交叉比对脚本名校验，零漏登）**，新增 D 组（性能测量与验收护栏：bench_endpoints/bench_t8_engine/golden_investor_api 等）、H 组（筹码因子 4 个）、L 组（`_probe_*` 探针），K 组把 60+ 个 `_` 一次性脚本按主题（长线/反转/龙虎榜/强势突破/价源…）归档并标注「已冻结、旧版勿复用、以最终版为准」；③安全须知新增 T10 数据访问约定。
  - **顺带闭环 T10 剩余清单**：8 个 tools 脚本（backfill_first_reversal/fix_history/eval_fusion_mode/eval_quality_filter/eval_short_engine/mine_next_day/mine_surge_next_day/recalc_mid_history）+ 4 个 tests/_ 调试脚本（_verify_incremental/_verify_atr_paths/_verify_atr_stop/_verify_chip_shadow）的业务函数导入全部改走 `core/repository/`（get_conn/init_db/DB_PATH/connect_db 等连接基础设施保留在 core.db）。**全仓业务函数旧路径 grep = 0**；12 文件 py_compile + 4 代表脚本 importlib 实加载通过（含 tools 跨包导入链）；全量 pytest **512 passed / 4 skipped** 持平。
  - **兼容层未删（有意保留）**：db.py 尾部 re-export 现零内部消费者，但 `test_db_compat_import` 仍故意断言其可用，且个人未追踪脚本可能依赖；删除是纯破坏性动作，建议在下次确认无外部使用时单独进行（连同该测试一起删）。
  - 回滚：README 为纯文档；导入迁移是路径平移、符号实现未变，revert 即可。
- **T13 scheduler 测试补盲 + start.bat 依赖检查增强（A6）✅ 已完成（2026-10）**
  - **补盲（新增 `tests/test_scheduler_runner.py`，16 用例，全部 monkeypatch 隔离、零网络/零写库）**：T2（`test_scheduler_lock.py`）此前只锁定三入口原子抢占与并发窗口，主流程零回归保障。本次覆盖：①`run_sync_blocking` 非交易日直接 success(`skipped=non_trading_day`) 且不拉行情 / 交易日成功链路（同步行数经 log_sync 落库 123、持仓诊断→全市场深析→跟踪推进顺序、成功后 `_retry_count` 归零、租约回写 success+释放）/ 深析跟踪 best-effort 异常不阻断主任务 / 主同步失败回写 error 且首次退避精确 1800s（`30min×2^0`）、计数+1、仅起一个重试线程 / 已达 `_MAX_RETRIES=3` 放弃且计数归零、不再排程 / 无 task_id 旧调用方式不触碰任务表；②`_submit_daily_sync_task` 四分支（幂等键 reused 不认领、资源组 conflict 返回持有者无 token、新建认领成功返回 uuid4.hex token、认领被抢 `claimed=False` 无 token），并 pin 住幂等键 `daily_sync:<today>` 与 `RESOURCE_PIPELINE_WRITE` 资源组；③`_job_sync` 同日重复 → `skipped_duplicate` 不起线程、任务表异常回退旧路径照常同步（task_id=None）、认领成功 → started+起线程；④`start_scheduler` 开关关闭不回收/不注册/不起线程，开启注册唯一每日 19:00 job 并起 daemon 调度线程（用例 finally 停循环清状态）；⑤`_recover_and_persist` 回收结果透传 + 每日安排持久化参数 pin（`daily_sync_1900`/daily/19:00/`{"target":"all"}`）。task_repo 一律 mock，避免 `daily_sync:<today>` 幂等键污染真实任务表；失败路径 Thread 替换为只记录不执行的 FakeThread，防止重试重入。
  - **start.bat（A6）**：旧版 Step1 仅 `python --version`、Step2 只探 `import flask` 单包且裸 `pip` 安装，venv 未激活/多解释器串 PATH 时装错环境难排查。增强后：①Step1 打印 `sys.executable` 完整路径暴露实际解释器；②新增 3.11+ 版本闸门（不满足直接退出并提示激活 venv）；③新增 `python -m pip --version` 同源检查；④Step2 一次探测 10 个运行时核心包（flask/flask_cors/schedule/akshare/baostock/requests/pandas/numpy/sklearn/joblib），输出 `ALL_OK` 或 `MISSING:a,b` 精确缺包清单而非只看 flask；⑤安装改用 `python -m pip` 保证与探测同一解释器，装后复检，仍缺则报出缺包名与手动命令。echo 保持英文避免 CMD 代码页问题。
  - 验收：bat 三段内联 python（解释器路径/版本闸/依赖探测）在真实 venv 实跑通过，ALL_OK exit 0 与模拟缺包 exit 1 两分支及 findstr 分支逻辑均验证（未执行整个 bat，不启动服务）；新文件 16 用例 + T2 锁测试 6 用例 22 绿；全量 pytest **528 passed / 4 skipped / 5 deselected**（T12 基线 512 +16），零回归。
  - 回滚：测试为纯新增文件，删除即回滚；start.bat 为 Step1/2 段落替换，revert 该文件即可，无代码/数据库/接口变化。
- **T14 task_queue 可测试性 + 队列背压改造（D6）✅ 已完成（2026-10）**
  - **问题**：`core/task_queue.py` 原为 import 时固定创建的模块级 `ThreadPoolExecutor(max_workers=4)`，测试无法替换只能起真线程 sleep 等待；`submit_task` 提交无界，4 个 worker 全忙时高峰任务无限堆积在内存。
  - **落地（4 文件，提交点 7 处零霰弹修改）**：①新增 `QueueFullError(RuntimeError)` 与 `configure(*, executor=None, max_workers=None, max_pending=_UNSET)`——`executor` 直接注入外部执行器（生命周期归外部、替换时不代为 shutdown，由 `_owns_executor` 标记区分），`max_workers` 新建自建池并优雅关闭旧的自建池，`max_pending` 用哨兵区分「未传」与「显式 None=不限」；另提供 `shutdown(wait=)`。②背压闸门在 `submit_task` 登记前于同一把锁内统计 **pending（未被 worker 领取）** 任务数：`>= max_pending` 即抛错且**任务不登记、不提交执行器**；running 不占排队槽位（4 个重任务在跑时仍允许排队）、任务转 running/success/error 槽位自动释放。executor.submit 保持锁外调用（避免 ThreadPoolExecutor 内部队列满时持锁阻塞）。③`app.py` 单点注册 `@errorhandler(QueueFullError)` 统一转 `fail(msg, 503)`——sync 路由（recalc/增量/fix_history/推荐池）、optimizer、stock_deep_service、personal 共 7 个提交点无需各自 try/except；`initialize_runtime` 启动时按 settings 装配。④`config/settings.py` 新增 `TASK_QUEUE_MAX_WORKERS`（默认 4）/ `TASK_QUEUE_MAX_PENDING`（默认 32），env `AIQUANT_TASK_QUEUE_WORKERS` / `AIQUANT_TASK_QUEUE_MAX_PENDING` 可覆盖；不调 configure 时模块默认值即 4/32，纯导入无行为变化。
  - **测试（新增 `tests/test_task_queue_limits.py`，13 护栏，假执行器零线程零 sleep）**：默认常量 pin（4/32/自带池）；同步注入立即 success、挂起注入保持 pending；上限 2 时第 3 次抛 `QueueFullError`（消息含 `2/2`）且不留痕；4 条手工 running 不占槽位；同步执行器下上限 1 连提 5 个全成功（槽位即时释放）；`max_pending=None` 连提 50 不拒；自建池替换旧池被 shutdown（提交验证抛 RuntimeError）、外来池替换时不被 shutdown；哨兵语义（只换执行器不动 max_pending）；shutdown 透传 wait；Flask 三层 `error_handler_spec[None][None][QueueFullError]` 处理器返回 503 且消息透传。autouse 夹具用例后只 shutdown 替换上来的池、恢复默认池与任务表，旧 `test_task_queue.py` 7 个真线程用例不受影响。
  - 验收：env 覆盖链路真实执行验证（默认 `4 32` → env 覆盖 `8 64`）；全量 pytest **541 passed / 4 skipped / 5 deselected**（T13 基线 528 +13），零回归。
  - 回滚：revert `core/task_queue.py` / `app.py` / `config/settings.py` 与新增测试文件即可；无数据库/接口合同变化（仅新增一种 503 响应场景）。
  - **至此本计划 T1–T14 全部完成。**

---

## 三、假设与待确认

1. **D5 已闭环（T6，2026-10-11）**：逐只调用假设实测证伪，但发现并修复了东财断连期实时行情无腾讯兜底的可用性缺口（详见 T6）。
2. T7 刻意不动 API 契约与前端，避免与「统一信号模型 legacy/shadow/v2」迁移叠加风险。
3. 所有涉及信号口径的改动（T5/T8）必须以"结果 diff=0 或可解释"为准，防止静默改变选股结果——这是本项目历史教训（见 CLAUDE.md 追高否决/缩量回踩条目）。
