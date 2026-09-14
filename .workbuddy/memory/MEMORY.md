# AIQuant 项目长期记忆

> 描述**主工作树 `dev_0.0.1`**。另有未合并 worktree（LLM/deployment/governance/风控/AI模型/WebSocket），勿当主树现状。主树**无** sklearn / LLM / 风控(`risk/` 空) / 三省六部 / WebSocket。

## 项目与用户
- A股量化：Flask + SQLite(`core/quant.db` ~3.9G) + akshare；前端单文件 `dashboard.html` + `static/css/dashboard.css`，无构建步骤。
- `E:\小项目\Project\AIQuant`；Windows；`start.bat` 启动、`install.bat` 装依赖。
- 用户"寇豆码"，成都；中文、表格/编号列表、根因+代码改动说明、精简 UI；A股**红涨绿跌**。

## 铁律（跨模块，改动前必读）
1. **单笔收益率不能相加**：`sum(单笔%)` 无资金含义（37 笔累出 -111.7%）→ 用**平均每笔**或**等权复利**。已清理 3 处。
2. **回测样本不要按"最近 N 天"截断**：`final==1` 已保证局面完整；曾误加 `scan_date<=today-30d` → 有偏抽样剔掉最差月。
3. **单月依赖压力测试**：说"某过滤/加权提升收益"前，逐个剔除单月看是否仍成立（2024-10 曾占 ext≥5.5% 样本 38.2%）。
4. **加权/过滤类改进先问：被用来加权的变量是否真的驱动结果？**（见「否决清单」③'）
5. **行号会漂移** → 每次改前重新 grep，勿信记忆里的行号。`ret` 在回测缓存/`sim_one` 里是**百分数**，做复利前必须 /100。

## 短线推荐引擎
- `SHORT_ENGINE="pure_bottom_v2"`（`config/strategy_params.py`；一键回退值 `"pure_bottom"`）：短线 100% 由 `strategy_bottom_fishing_v2` 驱动，`stock_signal.strategy` 恒为 **'短线融合'**。
- 观察线排除 = 阈值门：`short_observe_bottom_sql()` = `(非短线融合) OR (pct_above_ma20 >= short_observe_bottom_min_ext)`，**阈值 0.055 已生效**；`min_ext=0` 退化旧行为。三处出口共用（今日推荐/出场跟踪/复盘入库）。⚠ 2026-08-26 曾因 `short_observe_bottom=1` 零产出。
- 18:00 调度只增量重算 fusion_score，**不写 stock_signal** → 今日推荐时效不由调度保证。
- `rec_filters.py`：ST/退市 + 流动性 + 市值[30亿,800亿]；无波动率上限/换手率下限。候选池 = limit×3。`recommend_outcome` 无 status 列；optimizer 是 suggest-only。
- "推荐已涨过的票"根因：`score_rebound` 奖励"较10日最低反弹5%"→信号在反弹后最强；叠加 MA20 向上趋势门；全链路无超买/扩展度否决 → `docs/short-reco-anti-chase-methods.md`。

## 短线退出/择时侧
缓存 `logs/_short_risk_{prices,sim}.pkl` → 重跑报告秒级。样本 26,746 信号 → 26,262 终态 / 267 交易日 / 4,510 只 / 2023-01-17~2026-09-08。
工具：`tools/eval_short_risk_rules.py`、`eval_short_regime_filter.py`、`eval_atr_position_sizing.py`。口径：复用 `build_population`；出场直接 import 线上 `_short_exit_sim` 只换入参，**不重写数学**。

**✅ 已落地：ATR 自适应止损**（2026-09-14）k=2.5 / 下限5% / 上限15% —— 📄 `reports/short_atr_stop_rollout_2026-09-14.md`
- 组合级 top3：全期 +0.32%→**+0.63%**、同窗口 -0.84%→**+1.68%**、样本内 -0.20%→-0.04%、样本外 +0.91%→+1.40%；止损率 37.1%→**8.3%**、胜率 44.7%→**50.5%**；分半年 7/8 优；k=3 无增益。与 ext≥5.5% 叠加有效（全期 +1.56%、样本外 +1.92%、剔 2024-10 后 +1.26% 无单月依赖），但最近 2 月窗口 +1.68%→+1.01% → **ext 非全天候**。
- ⚠ 代价 **左尾变肥**：最差单笔 -12.90%→**-21.33%**、P05 -7.20%→-10.25%（和远气体 002971 十日 -41%；收盘判定在连续跌停时穿透止损位 6.3pp；全域 <-15% 仅 84 笔/0.33%）。
- 落地：`core/sync.py::_build_signal_records` 加 `_atr_stop_sc()` + 2 处止损调用；4 参数 `short_atr_stop_{enabled,k,floor,cap}`；`short_stop_loss` 降级为「ATR 关闭/缺失时的回退值」。三处出口共用 `stock_signal.stop_loss` → **勿分别改**。`calc_atr`=SMA(14)（与 Wilder 相关 0.965、宽度差 0.335pp → 复用）；rolling 回看 → 无未来函数。
- **锚点口径（已实测无害）**：回测锚 `exec_entry`，线上只能锚信号日收盘=买点；gap 中位 **0.036%** / P95 1.15%（n=25,806）→ 线上完整复现回测。`simulate()` 有 `stop_anchor` 参数。
- 实际止损宽度：中位 5.00%→**8.45%**（全期）/ 8.54%（2026）/**9.26%**（同窗口）；触顶15% 仅 3.8%、触底5% 7.3% → 这就是止损率 37%→8~9% 的机制。
- 回归 `tests/_verify_atr_stop.py`、`_verify_atr_paths.py`（三路径 960 次比对）均 PASS。

### 否决清单（勿重复尝试；详细证据见 reports/）
| 假设 | 结论 | 决定性证据 |
|---|---|---|
| ② 连续止损熔断 | ❌ | 止损率几乎不动（37.1%→34.5%）→ 无识别坏时段能力；改善全来自交易变少(-34%)=降仓位；叠 ATR 后 8 个半年 7 个变差 |
| ③ 高波动 regime 直接不出单 | ❌ | **对 2026H2 伤痛的过拟合**。全期方向相反（屏蔽日均值**更高**：波动≥90分位 +3.42% vs +0.62%；指数20日回撤≤-7% +4.22% vs +0.65%）；分半年「波动≥70分位」仅 2/6（2024H2 +7.12pp 砍掉最赚日子）；唯一正向项仅占 3.7% 笔、剔 2026-08 后 +0.01pp；叠 ATR 增量仅 +0.18pp 📄 `reports/short_regime_filter_2026-09-14.md` |
| ③' ATR 反算仓位（w∝1/ATR%） | ❌ | 短线线全期 +0.036%（t=+0.72 不显著）、**样本外反向 -0.017%（t=-0.21）**、分半年 4:4，代价是单票权重 P95 **50%→64%**；深度 top4/top20 同窗 **-0.271%/t=-2.14**、**-0.299%/t=-2.79** → **维持等权** |

**③/③' 机制（可迁移的方法论）**：① 止损几乎不绑定（到期 60.3% / 移止 30.0% / **止损仅 9.7%**）→ 止损宽度 ≠ 真实风险敞口；② `corr(ATR%,收益) = -0.012` → 波动**不预测收益方向**，配平不改期望；③ 日内仅 3~4 只 → 权重分散 2.3~3.0×，集中度上升抵消配平收益；④ 深度 top4 的 ATR 本就同质（Q12 键含高波动惩罚）→ 无信息可加；⑤ 抄底收益来自**跌深→反弹大**，2026H2 病根是**波动高但反弹不来（阴跌）**、信号日不可预判 → **想控回撤就降仓位/减 top_n**。

**regime 指标帧（可复用）**：`logs/_short_regime_frame.pkl`（`--rebuild-regime` 重建）。主指标用 **`daily_price` 全A等权口径**（`index_daily` 仅 2024-01 起）：`mkt_ret/disp/atr_med/vol20/dd20`；分位 = 滚动500日且只用当日及之前，前 120 日不判定。

## 个股深度（stock_deep / deep_track）
- **buy 档裸持有选股能力为负，加纪律后转正**：T+10 裸持有 -0.75%/44.9%；加回踩入场+止损+移动止盈 **+1.38%/61.5%**（与 deep_track 实测 +1.32%/61.13% 吻合 → 模拟器可信）→ **收益主要来自出场纪律，不是选股**。add 档 +0.82%/54.5%，**不要改成 add 优先**。裸持有衰减拐点：T+5~T+6 峰值、T+9 转负、T+15 -2.33%。
- **`daily_top_n`=4**（2026-09-05 从 10 改）+ **Q12 排序键**（`stock_deep.candidate_order_by`）：`2.2*(1-frac20) + 1.0*(pct_above_ma60<0) - 1.5*(volume_ratio>=2) - 0.6*(max(0,atr_pct-4)/4)`，按 level 优先 buy。均值 **2.15%**/胜率 **67.6%**/PF **2.18**（旧 N 键 +1.04%/60.8%/1.33）。机制：买"未启动、位置低"的。过拟合探针（frac20 权重 1.2→2.2→3.2→4.5 → 1.52→1.72→1.70→1.65，**2.2 见顶**）来源 `tools/eval_topk_pick.py`。**三档降级**：新列缺失自动回退旧键，绝不崩；旧三档键已废弃。
- **`stock_deep_signal` 增 5 列** `pct_above_ma60/regime/pattern/obv_grad_pct/volume_ratio`（旧库自动 ALTER）。**历史行需重扫填充**，否则 NULL → 降级纯旧键。
- **历史回补必须传 `as_of`**，否则取最新行情 = **未来函数**（证据 000651：最新 38.95 sell / as_of 08-20 41.42 add）。
- **分档 `_classify`**：buy≥5 / add≥2 / hold≥-2 / sell<-2。add 门槛极低 → 每天 1000~1800 只，**buy 仅 40~90 只**。
- **并行必须多进程**：8 线程实测**慢 4.2 倍**；8 进程 3.3x，12 进程饱和（12 核）。**Windows spawn**：不能在 Flask 请求/线程里起进程池（会重复导入主模块重启 app）→ `tools/backfill_deep_scan.py` + `subprocess.Popen` + JSON 进度轮询。约 45 秒/扫描日。
- **✅ 2026-09-14 落地止损宽度上限 cap=8%**：`deep_atr_stop_{enabled=1,cap=0.08}`；`strategy/stock_deep.py::_deep_stop_cap()` = `stop = max(stop, round(entry×(1−cap),2))`，接在 **2 处**止损出口（`_signal_plan_at` → 落 `stock_deep_signal.stop_loss`；`_current_signal`，含 `max(stop,swing_low)` 兜底）。**只动止损不动止盈**（先用未夹逼止损推 tp 再夹逼；风报比按夹逼后重算）。方向**与短线线相反**：原无夹逼（宽度中位 10.2%/最大 52%）→ **收窄有效**。buy 池 n=2635 **+0.376%/t=5.38**；all 池 n=35517 **+0.139%/t=8.74**（最差 -35.50%→**-19.00%**、P5 -14.10%→-10.28%）；top4 不显著（宽度中位本就 9.06%、仅 10.2% 笔被改）。buy/all 各 **2/3 月**最优且最差月改善最大 → 无单月依赖；**只加下限(floor5)零效果** → 起作用的是「上限」。
  ⚠ **代价（削左尾也削右尾）**：胜率 48.2%→46.2%(buy)、54.5%→53.1%(all)，中位同降，止损率 15.5%→30.2%(buy)。看重胜率退 cap12/cap15 或 enabled=0。
  ⚠ **生效**：需重启 `start.bat`；**历史行不自动更新**（最新日 571 行中 63.4% 待收紧）→ 重扫或等下次每日扫描；已建跟踪单不回溯。回归 `tests/_verify_deep_stop_cap.py` 16 项 PASS（用「开关前/后两跑对比」当基线）。📄 `reports/deep_atr_stop_cap_rollout_2026-09-14.md`
- 接口 `main_n`+`reserve_n`（双层切片）；前端 4 主推（`is-main`）+ 6 储备（默认收起，`localStorage.aiq_sd_more`）。
- deep_track：回补上限 = `stock_deep_signal` 的 DISTINCT scan_date 数。入场=T+1 起 5 日内 low 触及买点成交、价=min(买点,开盘)；出场=T+1 不判 / 收盘≤止损 / 已启动且收盘≤最高×(1-3%) / 满 max_hold 收盘平。重建跟踪单无需重扫（`sync_from_scan` 只读两表，清空后逐日重跑 + `update_open_tracks`，分钟级）。

## 手动持仓诊断（personal_position）
- `routes/investor.py::_diagnose_position` + `scheduler/runner.py::_evaluate_holdings`；`df` 查询**无 LIMIT** → 末行 = 最新交易日。
- **✅ 2026-09-14 落地 ATR 动态止损**：`strategy/exit_advisor.py::atr_dynamic_stop_pct(df)` = `−clamp(k×ATR14(最新日)%, 5%, 15%)`，止损价 = 成本×(1+pct)，每次刷新重算（与推荐线同参数，但 ATR 取**最新交易日**而非信号日）。
- **⚠ 坑（已修）**：动态止损**不能用「持仓期最低价」判定** —— 波动率下行→止损线上移→"当初没破线、现在还浮盈"被误判已止损。`evaluate_exit()` 新增 `stop_is_dynamic`，动态口径**只按当前收盘价**判定；固定口径保持原行为不动。
- 输出 `stop_eff`/`stop_source`（ATR 不可用回退 `short_stop_loss`）；前端「止损 X.XX（ATR动态 k=2.5× · 5%~15%）」，来源 ATR 时标 `(ATR)`。回归 `tests/_verify_dynamic_atr_stop.py` 11 项 PASS（含假阳性构造复现）。

## 数据源降级 / surge_picks / 前端踩坑
📄 **细节全部在 `.workbuddy/memory/reference/frontend-and-data.md`**（改前端、排查数据源、写离线自测前必读）。要点：
- **`clist/hist` 旧接口 404** 已被两级降级吸收（东财 except → `_fetch_klines_by_date_tencent` qt.gtimg.cn 500只/批 ≈10s 全A → akshare）。4419 只无影响。**勿再修**。
- 5 指数串行易 RemoteDisconnected → `sync_indices_by_date` 内已加 `time.sleep(0.4)`。
- **surge_picks**（`strategy='强势突破'`）：不占今日推荐名额、不进出场跟踪；入场=次日**开盘价**，出场同 deep_tracker，次日开盘已越止盈→`skipped`；历史块需 `grid-column:1/-1`。
- **⚠ 改 CSS 必须同步改 `dashboard.html` 里的 `?v=YYYYMMDD`**；**改前端只需硬刷新，改后端必须重启 `start.bat`**。
- **⚠ 5000 端口常有用户自己的服务常驻**（后起者拿不到流量 → 新路由像 404）→ 自测另起 5001，测完清理。
- **⚠ `request.args.get(k, default, type=int)` 的 default 必须也是 int**；**⚠ `PRAGMA table_info` 用索引 `r[1]`**。
- 离线自测：正则抽内联 script → `node --check` → node stub 灌真实 API 数据 → 断言 HTML（项目未装 playwright）；或用 `app.test_client()`。测试蓝图 `register_blueprint(inv.investor_bp)` **不要再加 url_prefix**。

## 待办 / 开放问题
- **待决策**：`short_ext_sort_desc` 升序→**降序**（降序样本内 +1.15%/样本外 **+1.90%** vs 升序 -0.20%/+0.91%，四组合一律降序更优；机制 = 升序挑最贴线的 = 挑到最弱）。⚠ 降序与 ext≥5.5% **互为替代** → **二选一，勿同时上**；且与 ATR 止损的**三向交互未测**。
- **待决策**：`DEEP_TRACK['max_hold_days']` 10→7（T+7 +1.40%/周转快 21% vs T+10 +1.38%/61.5%，用户暂不改）。
- 是否用新排序键**重建历史 610 单**（用户数据未动，仅代码+回填生效于未来建单）。
- 提选股质量：降动量类权重、加位置类权重（frac20 低分位），需重新回测。
- `stock_info.industry` 恒空，无填充代码；行业配额优化需先接行业数据源。
- **仍未开放**：盘中止损替代收盘判定（左尾 -21.33% 的根因：收盘判定 + 连续跌停穿透）。
- **观察**：2026-08/09 短线逆风（ext≥5.5% 子集 08 -0.50% / 09 -4.21%）—— 阈值能提期望，救不了逆风月。`mid` 分组当日可为空（非 bug，是产出节奏）。

---
*最后更新: 2026-09-14（① 手动持仓 ATR 动态止损已落地；② 个股深度 ATR 止损宽度上限 cap=8% 已落地；③' ATR 反算仓位 否决；阈值 5.5% 已满足）*
