# AIQuant 项目长期记忆

> 主工作树 `dev_0.0.1`（另有未合并 worktree：LLM/deployment/governance/风控/AI模型/WebSocket）。
> 侧车（改对应模块前**必须**读）：`reference/surge-observation.md` · `reference/long-selection.md` · `reference/lhb-institution.md` · `docs/chip-peak-short-strategy.md`

## 项目与用户
- A股量化：Flask + SQLite(`core/quant.db` ~3.9G)；前端单文件 `dashboard.html` + `static/css/dashboard.css`，无构建。
- `E:\小项目\Project\AIQuant`；Windows；改后端必须重启 `start.bat`；改前端只硬刷新。
- 用户"寇豆码"，成都；中文、表格/编号列表、根因+代码改动、精简 UI；A股**红涨绿跌**（`--positive:#ff5252` / `--negative:#00e676`）。

## 铁律（改前必读）
1. 单笔收益率不能相加 → 平均每笔，或按仓位累计 `Σ(单笔%×仓位%)`。
2. 回测样本不按"最近 N 天"截断（`final==1` 已保证局面完整）。
3. 说"某过滤/加权/排序键提升收益"前，**分年 + 剔除单年**都要成立。
4. 加权/过滤前先问：变量是否真的驱动结果？**必须做对照组**。
5. 行号会漂移 → 改前重新 grep。`ret` 在回测缓存/`sim_one` 是**百分数**，复利前 /100。
6. 子群主判据是**桶内增量 Δ**，不是绝对收益。
7. **组合口径（每日 top-N）才是最终判据**；过滤只在池子边缘动刀。
8. `daily_price` 混两个价源：`turnover>0` 行 `low/high/close` 是前复权，`amount/volume` 是原始值。
9. 移动止盈不变量：`trailing_pct` 必须 < 启动线浮盈比例，否则退化成「止亏」。
10. **时点正确性**：绝不用「当前最新价」回溯筛选历史样本。
11. 同一参数多链路复用时先查语义（`mid_trailing_pct` → `LIVE_MID_TRAILING_PCT`）。
12. 选排序键必须用「**带出场纪律**」的分年表，不能用裸持有表。
13. 连续值排序键不能靠字典序组多键（同分几乎不存在 → 退化成第一键）。
14. **改选股前先测「票池 alpha」**；条件存 **bitmask** 做 leave-one-out，并保留不满足条件的样本当基线。
15. 连续值百分位加权**必须预计算成列**（分母=当日票池），由 `sync.recompute_long_rank_key()` 回填。
16. ⚠ **新增「票池过滤列」上线前必须回填历史**（chip_conc / vol_ratio / long_mask 三次同坑）：历史全 NULL ⇒ 筛掉所有历史票。
17. ⚠ **多窗口口径列必须先判窗口**：同源不同「上榜原因」的金额窗口不同（单日 vs 连续3日），聚合前先分类。
18. ⚠ **跨表统计前必须与线上函数逐字段对账取值来源**（2026-09-24 教训：用 `daily_price.pct_change` 代替线上 `lhb_row["pct_change"]` 判涨停 ⇒ 样本 601→2348、结论整体反转）。
19. `daily_price.pct_change` 是**百分数**（9.28 = +9.28%）。ST 股以 `nd_oc=0 & nd_cc≈+5%` 一字板形态污染 OC 统计（`dp.name` 来自 stock_info **当前**名，摘帽后过滤失效）⇒ 按 `pct_change ∈ [4.6,5.4]` 剔除。
20. ⚠ **过滤条件必须在 UNION 的每个分支上一致**（2026-09-24：反转首日入库的 `stock_signal` 分支加了板块过滤、`first_reversal_hist` 分支漏加 ⇒ 跟踪组混入 **49% 不可交易的创业板**，把胜率 47.1% 抬到 52.1%）。同理「卡片口径 vs 跟踪口径」必须同源。
21. ⚠ **`MAIN_BOARD_ONLY` 是硬可交易性约束**（`personal_config.py:48`「小资金，未开通科创/创业板权限」），不是偏好 ⇒ 任何**统计/跟踪/展示**都要套 `main_board_filter()`；上证 30x/68x 整段排除。
22. 单文件前端 `dashboard.html` **无构建** ⇒ 改完 JS 必须过 `node tools/_check_dashboard_js.mjs`（同进程 `vm.Script`；本沙箱 **node→node spawn 会 EBUSY**，不能用 `node --check`）。
23. ⚠ **改完后端必须重启 `start.bat`，否则当晚 19:00 调度用「进程内存里的旧代码」重算复盘表**。2026-09-24 事故：进程 09:40 启动时 `sync.py` 已有反转首日接线、`outcome_tracker.py` 还没有 ⇒ 19:00 `insert_new_outcomes(days_back=60)` **先删 `scan_date>=今天-60` 再按 `stock_signal` 回填**，缺了反转首日分支 ⇒ **静默净删 424 行**（951→485→61），成绩单无人察觉。⇒ 凡是「先删窗口再重建」的函数，**任何再插入分支的缺失都会变成静默净删除**；加了 `_rev_before/_rev_after` 告警，但护栏挡不住旧代码，**只有重启**。诊断入口：`evaluated_at` 分布 + `logs/aiquant.log` 的 `Running job _job_sync` 时间戳。
24. ⚠ **前端交互逻辑不能用「语法通过」当验收**：`tools/_smoke_reversal_history.mjs` 是**迷你 DOM 冒烟测试**范式（把 dashboard.html 里那段真实 JS 抽进 node `vm`，用只含 `id/style/innerHTML/checked/value` 的假 DOM 承接）。⚠ 假 DOM **必须解析 id 标签的内联 `style="..."`**，否则 `el.style.display` 恒 `undefined`、断言假失败/假通过。⚠ 测试里换数据别用 `toggle*()`（可能已是展开态 ⇒ 变成收起拿到旧缓存）。

## ⚑ 开放待办
- **筹码排序上线前置（步骤④）**：① 历史 `chip_conc` 回填（必须传 `as_of`）② 同步 `H.portfolio()` 硬编码 `ext ASC+fusion DESC` ③ 再开 `short_chip_sort_prioritize=1`。
- **待决策**：`short_ext_sort_desc` 升序→降序（样本内 +1.15%/样本外 +1.90%）。⚠ 与 ext≥5.5% 互为替代；ATR 三向交互未测。
- **待决策**：观察线阈值 `short_observe_bottom_min_ext=0.055` 是否降到 0（short 出不满主因，收益上无所谓）。
- **long 待提升**：胜率 51.1%（线上）/57.0%（回测），距 60% 有差距。
- **仍未开放**：盘中止损替代收盘判定（左尾 −21.33%）。
- **待评估**：`stock_lhb_detail` 混了 3 日累计额，surge 硬过滤正在消费它（详见 lhb-institution.md §6）。
- `stock_info.industry` 恒空；行业配额需先接行业数据源。
- **命名误导**：`total_shares`/`circ_shares` 都是流通股本 ⇒ `QUALITY_FILTER` 的"市值"实为流通市值。
- ⚠ **已推翻**：~~2026-08/09 短线逆风~~ —— 实为普涨（全市场 +12.44%/胜率 76.1%），推荐票跑输 9.5~15.9pp ⇒ **选股 alpha 为负**。

## ⚑ 模块结论（细节见侧车或历史日志）
**long 选股（已重设计上线）**：票池 `(long_mask & 7)=7` + `long_rank_key = 0.75×pctile(vol60)+0.25×pctile(dd250)` 升序。实盘 −3.59%/9.4% → **+0.47%/51.1%**，开关 `long_lowvol_sort_enabled`。📄 long-selection.md §6~§8。

**筹码峰**：`conc` 是**风险因子不是收益因子**；当过滤器 ❌ / 当第一排序键 ✅（全期 +0.32%→+1.05%、止损 37.1%→23.1%）。影子表 `recommend_outcome_shadow`，开关 `short_chip_shadow_enabled`。

**龙虎榜机构席位（2026-09-22 新增）**：表 `stock_lhb_jg_detail`、`sync_lhb_jg()`、API `/api/investor/lhb_jg_track`、前端「机构席位建仓跟踪」栏目。🚫 **机构买卖方向无效**（1日 净买 +0.72% vs 净卖 +0.66%，t=+0.53；同「近20日上榜≥4次」时净卖 +1.19% > 净买 +0.94%）⇒ **只做成本位跟踪，不做信号**。回填 24019 行/544 日。

**隔日动量线（龙虎榜，2026-09-24 诊断 600641 追高事件）**：根因 = 该线天然滞后（上榜原因文本含"累计涨幅偏离 20%"即官方标注已连涨）+ 系统**无任何线做反转首日识别**（`pure_bottom_v2` 的 `score_rebound` 奖励"相对10日低点反弹 0~4%"，反转首日必 >4% ⇒ 归零；又被 `short_observe_bottom_min_ext=0.055` 砍一刀）。⚠ **五组防追高过滤实测全部反向**（`ext≥12%` Δ−0.15pp / `ret3≥25%` Δ−0.43pp / 触板打开 Δ−0.78pp / 连涨≥5 / 累计偏离型，n=182 OC，分年 2024/25/26 一致）⇒ **禁止给该线加 CHASE/EXTENSION 过滤**。基线 +0.98%/胜率 55.5%（三年稳定）。真实风险在执行价：`stop_loss` 按信号日收盘固定 ⇒ 跳空 >2.5% 时盈亏比跌破 1.0；`T1_GAP_GUARD.gap_limit` 现取止盈线(~6.5%) ⇒ +3~+5% 高开恰在"不拦"区。⚠ 勿为"抓首日"降 `short_observe_bottom_min_ext`（3 年回测+单月依赖压测定下，且它管的是融合线非龙虎榜线）。📄 docs/short-lhb-chase-diagnosis.md

**次日强势观察（已上线隔日了结）**：`max_hold_days` 5→**1** + `judge_entry_day=True` + 开盘破止损放弃。全族 n=1695：胜率 36.9%→**45.6%**（分年 +4.2/+8.3/+10.1pp）、均值 −0.19%→**+0.09%**。⚠ 入场端未解决（LHB+大阳候选次日集体低开）。回归 72 PASS。

**反转首日观察（新建上线 2026-09-24 + 已纳入推荐复盘，全栈完成）**：承接 600641 追高事件 —— 独立「首破 MA20 × 大阳≥5% × 量比≥1.5 × 非涨停」线，只标**入场位最低那天**，**定位观察池非买入推荐**。模块 `strategy/first_reversal.py`；配置 `FIRST_REVERSAL`（`enabled=True`、`kdj_k_max=None`、`track_enabled=True`、`track_top_n=0`）；接线 `core/sync.py`；API `/reversal_picks`；前端首页面板；验收 `tools/_verify_first_reversal.py`（A~F + D3 数据源/单位 + D4 板块/隔离）。权威口径 n=**15841**、日均 29.1、OC **+0.328%**、胜率 **49.5%**、超额 **+0.174pp(t=+2.80)**，分年 +0.23/+0.14/+0.18 三年一致，中位偏离 MA20 **+3.8%**；600641 **仅 2026-09-16 命中 1 次**。⚠ 胜率未达 52% 门槛、扣成本仅 +0.07pp、**出场纪律为负贡献** ⇒ **不占 Top-N 名额**。**KDJ 只做提示标签**（K 分档胜率单调：<20 52.7%→>80 44.0%，但收益维度 t=+0.08、2026 反向 ⇒ 铁律 3 不过，禁做硬过滤）；MACD DIF 上穿 0 轴无 alpha（t=+0.33）。
　↳ **复盘组（独立成组）**：`insert_new_outcomes` 末尾单独 INSERT（源 = `stock_signal` ∪ 回填表 `first_reversal_hist`，实时优先），`_evaluate_short` 对其**豁免回踩入场**；所有聚合函数默认 `strategy != '反转首日'` ⇒ 表头/明细零污染（`/recommendations/history` 仍 55/~33%/−1.3%）。回填 `tools/backfill_first_reversal.py`（1424 候选→951 行）。**主板过滤后 490 行**（含实时）**/ 90 天窗 胜率 46.4% / 均 +0.62% / PF 1.42 / T+1 44.3%**。
　↳ ⚠ **可交易性关键发现**：收益高度集中于**创业板**（466 条 **58.9%/+2.38%/PF 1.99**）vs 主板（485 条 47.1%/+0.79%）——账户未开通该权限 ⇒ 本线对本账户价值需打折（铁律 21）。
　↳ `/reversal_picks` 单位陷阱：`stock_signal.pct_above_ma20` 是**分数**、`first_reversal_hist.ext_pct` 是**百分数** ⇒ SQL 统一 `*100`；`pct_day` 用 `close/prev_close`（非 `pct_change`）。无实时信号时面板回落到最近历史回填日（响应 `source` 字段）。
　↳ **历史表现（2026-09-24 新增）**：API `/reversal_history?days=60&limit=600`（days 5~180、limit 20~2000）+ 前端「📜 历史表现」（窗口 30/60/90/180 + 「只看已结算」复选框，**默认收起懒加载**）。⚠ 本接口**故意不复用**「次日强势观察」的自建回放：本线已进 `recommend_outcome` 就该直接读复盘表，再写一套回放＝第二套出场数学（铁律 18）；实现上把**同一份明细**喂给 `get_merged_summary(items=…)`（新增 `items` 形参）⇒ 列表与成绩单**不可能分叉**（实测 429=429）。出场中文标签 + `tone`(positive/negative/neutral/holding) 由后端给，前端只配色；**配色用 `--positive` 红 / `--negative` 绿**（未沿用 `recommendations/history` 前端把「已止盈」涂 `var(--green)` 的反向旧写法）。`ext_pct/vol_ratio/kdj_k` 由 `first_reversal_hist` ∪ `stock_signal` 补，实时行的量比/KDJ 无列可读 ⇒ 用正则从 `trigger_list` 文本兜底（填充率 429/429）。60 天窗实测 **429 条 / 已结算 367 / 胜率 44.6% / 均 +0.33% / PF 1.39 / 最佳 +48.24% / 最差 −14.44%**；出场分布 移止 141 / 止损 160 / 到期 66 / 持仓中 62。⚠ 「只看已结算」是为解「结算滞后 5~10 交易日 ⇒ 降序时第一屏全是『--/持仓中』」而加，纯前端过滤且**不改整窗成绩单**。

**短线推荐**：`SHORT_ENGINE="pure_bottom_v2"`，`strategy` 恒 '短线融合'。过滤链 = `fusion≥short_conf_gate` + T1 + 大盘走弱闸门 + `pct_above_ma20≥0.055`。18:00 调度只增量重算 fusion_score、**不写 stock_signal**；`recommend_outcome` 是 60 天滚动窗。⚠ 过滤链**已证不是收益主因**（全链 +0.969% vs 无过滤 +0.400%，逐个剔除不显著）⇒ 提胜率要靠入场择时。

**出场/择时**：✅ ATR 自适应止损（k=2.5/下限5%/上限15%：全期 +0.32%→+0.63%、止损率 37%→8~9%；三处出口共用 `stock_signal.stop_loss`，**勿分别改**）。✅ mid 移动止盈 `mid_trailing_pct` 0.10→**0.05**（胜率 22.7%→58.6%）。✅ 展示口径改「按仓位累计」。✅ long 止损 `long_ma_stop_mult` 0.90。❌ 否决：连续止损熔断 / 高波动不出单 / ATR 反算仓位 → 维持等权。

**个股深度**：buy 档裸持有 −0.75%/44.9%，加回踩+止损+移动止盈 +1.38%/61.5% ⇒ **收益来自出场纪律不是选股**；ATR 止损宽度上限 cap=8%；历史回补**必须传 `as_of`**；并行必须多进程（8 线程慢 4.2x）。

**今日推荐数量规律**：mid 为空 = 当天无 `horizon='mid'` 行（产出节奏，非 bug）；long 曾为空 = `long_mask` 未回填（铁律 16）；short 最稀缺（日均 1.65 只，被弱市闸门 + 观察线阈值砍两刀）。

## 踩坑
- 该 bash 无 coreutils（`ls`/`grep`/`tail`）→ 用 Grep/Glob/Read 或 Python `os`；反引号会被当命令替换 ⇒ 写文档用 Write/Edit。
- 沙箱代理 `HTTP_PROXY=127.0.0.1:49513` 是坏的 → 请求前 `os.environ.pop('HTTP_PROXY')` 或 `Session().trust_env=False`。
- 本地验证：hermes venv 有 flask/pandas 无 qlib ⇒ `from app import app` 必炸；用 `Flask(__name__)+register_blueprint(investor_bp)`+`test_client`。
- 本地 `daily_price` **不覆盖科创板(688)/北交所(920)**（688 仅 1 只，92x 为 0）。
- `clist/hist` 旧接口 404 已被两级降级吸收（东财→腾讯→akshare），**勿再修**；改 CSS 需同步 `?v=YYYYMMDD`。
- ⚠ **`daily_price.pct_change` 有 63~65% 的行恒为 0（未写入）⇒ 不可用于涨跌幅/涨停判定**；`close` 才是权威连续序列。一律用 `strategy.indicators.calc_true_ret()`（`close/close.shift(1)`），并用 `audit_daily_price()` 自检。曾据此误判 `close` 混价源并做 `cumprod` 重建（结论全废）。
- ⚠ 本沙箱 **node 进程内 spawn node 会 EBUSY** ⇒ JS 语法检查只能同进程 `vm.Script`（`tools/_check_dashboard_js.mjs`）。
- ⚠ node 里取 ROOT 不要用 `new URL(import.meta.url).pathname`（会把**中文路径 URL 编码**成 `%E5%B0%8F…`）⇒ 用 `fileURLToPath`。
- 重建 `recommend_outcome` 后必须做**逐行 diff（排除被改的那条线）** 证明其余线未被误动：`tools/rebuild_short_tracking.py` 会备份 `recommend_outcome_bak_<ts>`。
- 复现「调度链是否丢数据」的**正控/负控**手法：正控 = 用线上同参数（`days_back=60`）跑一遍比行数；负控 = 临时改一个开关（如 `FIRST_REVERSAL['track_top_n']=1`）把再插入分支打坏，确认告警**真的会响**，再改回并重跑验证自动补回（源表未动 ⇒ 必可补回）。`core/outcome_tracker.py` 内的 `_rev_before/_rev_after` 告警即由此验证。
- 面板加可折叠/懒加载区块时，状态必须**收口到单一注入口**（如 `_hydrateRevHist()`）：面板整体 `innerHTML = …` 重绘会冲掉展开态 DOM，否则出现「按钮写『收起』但内容是空的」。回归入口：`tools/_verify_first_reversal.py`（A~F + D3/D4/D5）已把 `tools/_check_dashboard_js.mjs` 与 `tools/_smoke_reversal_history.mjs` 一并串起来。

---
*最后更新: 2026-09-24*
