# tools/ 工具脚本说明

本目录集中存放 AIQuant 的**运维、回填、重建工具**与**离线研究脚本**。脚本之间互不依赖、可独立运行，
统一约定从项目根目录以 `python tools/<脚本>.py [参数]` 调用（多进程工具须独立进程运行，见下方安全须知）。

> **2026-10 T12 归档**：本索引覆盖目录下全部 142 个脚本；**不做子目录迁移**——docs/CLAUDE.md/reference/SKILL
> 中共有 110 处 `tools/<名>.py` 路径引用，移动会使历史决策文档的复跑指引全部失效，而文件名前缀本身已是分类体系。
> **新增脚本请同步在第三节相应分组登记一行**。

---

## 一、命名约定（一眼判断用途）

| 前缀 | 类别 | 是否写库 | 说明 |
|---|---|---|---|
| `backfill_*` | 历史数据回填 | ✅ 写库（生产） | 补建历史缺失的扫描日 / 信号，落 `stock_signal` 或 `stock_deep_signal` |
| `rebuild_*` / `recalc_*` | 数据重建 / 重算 | ✅ 写库（生产） | 用新口径重跑并覆盖既有数据，通常带自动备份 |
| `fix_*` | 数据修复 | ✅ 写库（生产） | 修历史量价单位、缺失等脏数据 |
| `gen_*` | 报告生成 | ⚠️ 产文件 | 生成 HTML 报告到本地 / `reports/` |
| `bench_*` / `ab_*` / `golden_*` / `profile_*` | 性能测量与验收护栏 | ❌ 只读 | 2026-10 优化计划沉淀的**可复用测量资产**（见 D 组），改动前后对照以此为准 |
| `eval_*` / `diag_*` / `mine_*` | 离线评估 / 诊断 / 挖掘 | ❌ 只读 | 复刻线上口径做回测对比，不改任何数据 |
| `backtest_*` / `walkforward_*` | 回测 / 样本外验证 | ❌ 只读 | 策略或参数网格回测 |
| `analyze_*` / `calibrate_*` / `verify_*` / `today_*` | 分析 / 校准 / 预验证 / 复盘 | ❌ 只读（today 产 HTML） | 单点假设验证、T+0 体检 |
| `_` 前缀（`_diag_*` / `_eval_*` / `_probe_*` 等） | **一次性实验/探针** | 多数只读 | 探索过程稿，结论已沉淀进生产或 docs 后脚本即冻结；仅保留复现能力，**勿当工具复用**（K/L 组） |

> **写库 vs 只读**：凡属「评估 / 回测 / 诊断 / 挖掘」类均不写库，可放心反复运行；`backfill`/`rebuild`/`fix` 会改动 SQLite 本地运行数据，运行前务必确认备份。
> **一次性判定**：`_` 前缀中，少数是历史决策的「可复跑证据脚本」（如 `_eval_short_long.py`、`_eval_pullback.py`，CLAUDE.md 有引用），其余 v2~v7、jg2 这类带版本号的一律视为已被后续版本取代的过程稿。

---

## 二、安全须知

1. **`quant.db`（~3.9G）不入库**（被 `.gitignore` 排除），但本地运行数据真实存在，写库工具会改它。
2. **`rebuild_deep_track.py --apply` 自动备份**：执行前先 `CREATE TABLE deep_track_bak_<时间戳> AS SELECT * FROM deep_track`，可逆；确认无误后可 `DROP` 备份表。
3. **Windows 多进程必须独立脚本**：`analyze_stock` 为纯 Python 计算，受 GIL 限制，线程池反而更慢；多进程工具（`backfill_deep_scan` / `backfill_signal_features`）通过 `subprocess.Popen` / `ProcessPoolExecutor` 独立拉起，**禁止在 Flask 请求或线程内直接起进程池**（会重复导入主模块、重启 app）。
4. **未来函数红线**：任何回补 / 重算历史信号的脚本都必须传 `as_of`（扫描日）截断行情，否则取到的是最新价、构成前视偏差（look-ahead bias）。
5. 只读脚本多数会把结果写入 `reports/*.json`，便于复核，不影响线上。
6. **数据访问约定（T10 起）**：脚本统一从 `core/repository/` 导入业务数据访问函数，不要新增 `from core.db import <业务函数>`（db.py 仅保留兼容 re-export 层，待全部脚本迁移后可删）。

---

## 三、分类清单（全量 142 个）

### A. 数据回填（写库·生产）
| 脚本 | 作用 |
|---|---|
| `backfill_deep_scan.py` | 多进程回补历史扫描日，同步补建个股深度跟踪单；修复未来函数（`load_history` 加 `as_of` 截断）。参数：`--days/--start/--end/--procs/--dry-run/--progress-file` |
| `backfill_signal_features.py` | 回填 `stock_deep_signal` 排序特征列（`score/base_score/frac20/risk_pct/atr_pct`），历史行缺失会导致排序静默退化 |
| `backfill_signal_extension.py` | 回填 `stock_signal.pct_above_ma20` 扩展度字段（2026-08 短线 S4 口径） |
| `backfill_lhb.py` | 龙虎榜明细回填（akshare 东方财富，按日聚合） |
| `backfill_lhb_jg.py` | 龙虎榜**机构专用席位**买卖统计回填 → `stock_lhb_jg_detail`；与 backfill_lhb 的差异：保留原始多行不聚合（金额统计窗口随上榜原因变化） |
| `backfill_lhb_momentum.py` | 历史龙虎榜动量信号回填（`stock_signal`，含大跌/跌停过滤同口径） |
| `backfill_first_reversal.py` | 回填「反转首日」历史信号 → `first_reversal_hist`（推荐复盘独立成组） |
| `backfill_pullback_dip.py` | 历史「缩量回踩」信号回填（⚠ 该策略 2026-08 已停用，仅留历史复现能力） |
| `_run_deep_scan.py` | 手动触发一次全市场深析扫描（独立进程，确保用最新逻辑），落 `stock_deep_signal` |
| `_backfill_long_pool_cols.py` | 一次性：回填 long 信号 `long_mask/vol60/dd250/long_rank_key` |
| `_backfill_long_volratio.py` | 一次性：回填 long 信号 `vol_ratio`（波动收敛度排序键） |

### B. 数据修复 / 重建 / 重算（写库·生产）
| 脚本 | 作用 |
|---|---|
| `rebuild_deep_track.py` | 用当前排序键（`strategy/stock_deep.candidate_order_by`）重建 `deep_track`；`--apply` 前自动备份，`--dry-run`（默认）仅对比 |
| `rebuild_short_tracking.py` | 清空并重建「短线跟踪」记录（recommend_outcome） |
| `recalc_mid_history.py` | 中 / 长线信号历史重算（重建 `[EXIT_TRACK_START_DATE, 最新交易日]` 区间） |
| `fix_history.py` | 全量历史量价数据修正 |
| `fix_volume_unit.py` | 修复 `daily_price` 量价单位不统一（手 / 股 + 成交额混用） |
| `fix_circ_shares.py` | 股本取证 + `stock_info` 流通/总股本列修备（筹码因子口径前置修复） |

### C. 报告生成（⚠ 产文件）
| 脚本 | 作用 |
|---|---|
| `gen_stock_analysis.py` | 生成个股决策仪表盘 HTML 报告（星辰专家格式），供个股深度面板消费 |
| `today_review.py` | 今日/指定日推荐 **T+0 质量体检** HTML（区别于 outcome_tracker 的 T+1 事后归因：不需要未来行情） |
| `_gen_reversal_preview.py` | 一次性：生成「反转首日观察」面板静态预览（reports/*.html+.png） |
| `_gen_triple_cmp_html.py` | 一次性：读 reports/_triple_cmp_data.json 生成分层对比 HTML |
| `_make_jg_preview.py` | 一次性：生成「机构席位建仓跟踪」栏目离线预览页 |

### D. 性能测量与验收护栏（只读·可复用资产）
| 脚本 | 作用 |
|---|---|
| `bench_endpoints.py` | **方案 F2 接口性能固定协议**：同库快照/关网/冷启动+预热/≥30 采样，P50-P95+SQL 数+读行+内存，结果落 `reports/bench_<ts>.json` |
| `bench_recalc_incremental.py` | 增量重算耗时测量（T3 基线 / T5 验收复测）；每次 run 后对当日 stock_signal 全行业务列取 sha256 指纹并相邻 run 对称差对比（排除 id/created_at），T5 同日重算 diff=0 的硬证据 |
| `bench_t8_engine.py` | T8 回测引擎按需指标：`run/compare/ab` 三场景指纹对照（`--db/--rule-ids/--universe-limit`），FINGERPRINT DIFF=0 为验收铁律 |
| `ab_conn_sharing.py` | T5-3：增量重算连接复用同机 A/B 对照 |
| `golden_investor_api.py` | T7 护栏：investor 蓝图 GET 接口响应快照冻结/比对 |
| `profile_build_signal.py` | cProfile 下钻 `_build_signal_records` 内部热点 |

### E. 短线策略评估 / 回测（只读研究）
| 脚本 | 作用 |
|---|---|
| `eval_short_engine.py` | 短线引擎回测对比（OLD 纯抄底 vs 现网 pure_bottom_v2） |
| `eval_short_fusion_cap.py` | 短线选股融合分上限/排序方向全窗评估 |
| `eval_short_return_boost.py` | 短线收益率提升对比实验 |
| `eval_short_sort_replay.py` | 短线排序变体「真实复盘口径」重放对比 |
| `eval_short_top3.py` | 短线 Top3 化 + 选股侧调参网格（近 6 月回测） |
| `eval_short_top3b.py` | Top3 实验补充（g22 低扩展 Top4 基准） |
| `eval_short_top3c.py` | 大盘状态（regime）过滤实验 |
| `eval_short_t1_filter.py` | Top3 精选位 T1 辅助过滤挖掘 |
| `eval_short_t1_filter2.py` | 实装语义复核（过滤在 Top3 截取之前，有替补） |
| `eval_short_t1_filter3.py` | 对账窗（2026-07-20 起）同口径复核 |
| `eval_short_t1_filter4.py` | regime 自动切换实装语义全窗复核 |
| `eval_short_observe_rule.py` | 「抄底融合线」收窄排除条件评估 |
| `eval_short_regime_filter.py` | 「高波动 regime 直接不出单」回测 |
| `eval_short_risk_rules.py` | 短线风控：①ATR 自适应止损 ②连续止损熔断 |
| `eval_fusion_mode.py` | 融合方式对比（max / weighted_avg 等） |
| `eval_quality_filter.py` | 质量过滤边界敏感性扫描 |
| `eval_atr_position_sizing.py` | ATR 反算仓位（风险平价）：等权 vs w∝1/止损宽度 |
| `eval_bottom_reentry.py` | 抄底「买回踩+重燃」vs 现网「买反弹」回放 |
| `eval_lhb_surge_boost.py` | 大阳突破 × 龙虎榜净买交集子集验证 |
| `calibrate_short_topn.py` | 短线推荐 top-N 数量校准回测（0~4 个） |
| `diag_fusion_rank.py` | 验证「融合分排序」是否反向（结论：最高分=扩展度最高，无选择力） |
| `diag_short_reco.py` | 短线推荐系统诊断（推荐时点位置分布等） |
| `diag_price_data_quality.py` | 行情体检：`volume/amount/turnover` 自洽性 |
| `backtest_dynamic_count.py` | 推荐数量动态调整回测（每天 8→4，差日子出 0） |
| `backtest_engine_compare.py` | v1（已反弹）vs v2（买回踩）vs v2+放宽闸门全链路回测 |
| `backtest_enhance.py` | v2 之上入场质量增强回测 |
| `backtest_exit.py` | 不同出场规则真实收益模拟（v2 Top8） |
| `backtest_short_live_1y.py` | 短线近一年真实复盘口径回测 |
| `backtest_short_stress.py` | 对近一年回测结论的反向压力测试 |
| `walkforward_short_ext.py` | 扩展度「死区」稳健性/混杂隔离（样本外走步） |

### F. 中线 / 长线评估（只读研究）
| 脚本 | 作用 |
|---|---|
| `mine_mid_entry.py` | 中线买入端改进方向挖掘（go/no-go 证据） |
| `mine_mid_highwin.py` | 中线高胜率风格选型挖掘 |
| `mine_pullback_quality.py` | 「缩量回踩低吸」假设只读挖掘（结论：真实复盘窗失效，已停用） |
| `verify_mid_three_filter.py` | 三过滤组合 × 线上出场口径预验证 |
| `backtest_mid_exit_grid.py` | 中线移动止盈参数网格回测（向量化） |
| `backtest_midlong_exit.py` | **中/长线止损参数回测依据**（mid_atr_stop_mult 2.0→2.5、long 0.99→0.95，entry=T+1 open） |

### G. 隔日动量 / 龙虎榜（只读研究，写库工具见 A 组）
| 脚本 | 作用 |
|---|---|
| `mine_next_day.py` | 隔日动量信号挖掘（nd_oc 口径受 T+1 限制，见 CLAUDE.md） |
| `mine_surge_next_day.py` | 「次日大涨/涨停」候选策略挖掘 |
| `_eval_lhb_momentum.py` | 龙虎榜隔日动量 2024-07~2026-08 全期表现验证（含大跌过滤子集证据） |

### H. 筹码峰因子（只读研究）
| 脚本 | 作用 |
|---|---|
| `eval_chip_factor.py` | 筹码峰（持仓成本分布）因子对短线选股是否有效 |
| `eval_chip_mktcap.py` | 市值分层：筹码峰策略在小市值上是否更有效 |
| `eval_chip_shadow.py` | 影子对照：筹码优先排序 vs 线上基线（前向记录） |
| `eval_chip_turnover_bias.py` | 换手率口径缺陷影响：`circ_shares == total_shares` 证伪检验 |

### I. 个股深度（只读研究 + 排序核心）
| 脚本 | 作用 |
|---|---|
| `eval_topk_pick.py` | **top10 排序键优化核心**：复刻入场/止损/移动止盈/到期，新键均值 +0.07%→+1.56%、PF 1.02→1.48 |
| `eval_deep_atr_stop.py` | deep_track 出场侧「止损宽度规则」头对头回测 |
| `analyze_confirm_gap.py` | 确认缺口（confirmation gap）分析 |

### J. 推荐复盘 / 对账（只读）
| 脚本 | 作用 |
|---|---|
| `_eval_short_long.py` | **短线链路决策证据（可复跑）**：2024-07~2026-08 拆解，去缩量回踩+宽松闸门 +106.6%/回撤 -12.48% |
| `_eval_pullback.py` | 买回踩 v1 vs v2 平滑权重版 T+1 OC 对照（55.5%/+0.211% 转正证据） |

### K. 一次性诊断与实验稿（`_` 前缀，已冻结，仅留复现）
> 结论均已沉淀进生产代码或 docs；带 v2~v7 版本号的以最终版为准，旧版勿复用。

| 主题 | 脚本 |
|---|---|
| 长线选股重设计 | `_diag_long_pool.py`（票池 alpha + 打分项 LOO 归因）、`_diag_long_pool_2026.py`、`_diag_long_mask.py`（16 种组合归因）、`_diag_long_rescore.py`（历史重放+排序键挖掘）、`_diag_long_key_eval.py`、`_diag_long_exit_sim.py`（带真实出场的最终判据）、`_diag_long_redesign.py`（候选方案对比） |
| 中线出场 | `_diag_midlong_exit_grid.py`（实际推荐票出场参数网格） |
| 短线过滤链 | `_diag_short_filter_chain.py`、`_diag_short_filter_big.py`（大样本 LOO）、`_diag_short_filter_live.py`（实战口径）、`_diag_short_ob_rr.py`（观察线与盈亏比门）、`_diag_short_baseline.py`（实盘复盘基线） |
| 反转首日线 | `_diag_reversal_first_day.py`（最小实验）、`_diag_reversal_v2.py`、`_diag_reversal_v3.py`、`_diag_reversal_v4.py`、`_diag_reversal_v5.py`、`_diag_reversal_v6_clean.py`、`_diag_reversal_v7.py`（v7 干净口径定稿）、`_verify_first_reversal.py`（上线验收：模块 vs 向量化对账+600641 核对+路由回归） |
| 龙虎榜/动量 | `_diag_lhb_chase_filter.py`、`_diag_lhb_chase_filter_v2.py`、`_diag_lhb_v3.py`、`_diag_lhb_v4.py`、`_diag_lhb_v5.py`（防追高过滤五代评估，v5 剔 ST 一字板定稿）、`_diag_lhb_gap.py`（T+1 跳空 vs OC 决定性实验）、`_diag_lhb_freq.py`（频繁上榜线）、`_diag_lhb_recon.py`（复刻 mine_next_day 对账）、`_diag_lhb_jg.py`、`_diag_lhb_jg2.py`（机构席位信号及二次验证） |
| 强势突破观察 | `_diag_surge_live.py`（线上成绩单）、`_diag_surge_perf.py`（归因）、`_diag_surge_replay.py`（历史重放+LHB 硬过滤）、`_diag_surge_tune.py`（参数网格） |
| 三屏/个股 | `_diag_triple_screen.py`、`_diag_triple_screen2.py`、`_diag_triple_screen3.py`、`_cmp_triple_vs_existing.py` |
| 出场跟踪 | `_diag_exit_track_exitrule.py`（出场纪律对比）、`_diag_exit_track_quota.py`（名额内替补）、`_diag_exit_track_guard.py`（gap guard 最新价筛选影响） |
| 价源/数据 | `_diag_pricesource.py`、`_diag_pricesource2.py`（close 列混价源检验）、`_diag_which_is_truth.py`（close vs pct_change 权威性，隔夜跳空判据）、`_diag_data_completeness.py`（行覆盖/日期间隔）、`_diag_600641_timing.py`（个股追高时点定位） |
| KDJ | `_diag_kdj_hypothesis.py`、`_diag_kdj_veto.py`（K>80 硬过滤配对检验 v9） |
| today 接口 | `_diag_today_empty.py`、`_diag_today_short_layers.py`（各组为空的过滤链逐层计数） |
| 个股深度 | `_eval_deep_buypoints.py`（**买点低吸改造证据**：frac20 守卫，T+3 61.3→67.3%）、`_eval_adj_sort.py`（fusion vs 扩展度排序）、`_eval_perstock_threshold.py`（本股分位阈值，已否决）、`_eval_deep_exit_reach.py`（止盈可达性仅 4.7%）、`_eval_deep_exit_arms.py`（卖点规则多臂头对头） |
| 方案对账 | `_reconcile_plan_a.py`、`_reconcile_short.py`、`_report_engine_compare.py`、`_eval_v2_topn_portfolio.py` |

### L. 临时探针（`_probe_*`，接口/数据口径实测，跑完即弃）
| 脚本 | 作用 |
|---|---|
| `_probe_pz.py` | T6：东财 clist 单页 pz 参数敏感性实测 |
| `_probe_realtime_reqcount.py` | T6：全 A 实时一轮实际 HTTP 请求数核实（10 请求） |
| `_probe_chip.py` | 筹码分布模块正确性探针 |
| `_probe_lhb_detail_caliber.py` | stock_lhb_detail 口径污染面评估 |
| `_probe_lhb_jg.py` / `_probe_lhb_jg2.py` | akshare 机构类接口可用性/列结构；机构买卖金额单日 vs 多日窗口口径 |

---

## 四、常用命令

```bash
# 1) 回补最近 N 天扫描日 + 补建跟踪单（独立进程，多进程加速）
python tools/backfill_deep_scan.py --days 60 --procs 8

# 2) 回填候选排序特征列（历史行必跑，否则新排序静默退化）
python tools/backfill_signal_features.py --procs 8

# 3) 离线评估排序键优劣（输出 reports/eval_topk_pick.json）
python tools/eval_topk_pick.py --procs 8 --out reports/eval_topk_pick.json

# 4) 用新排序键重建 deep_track（先 dry-run 看对比，再 --apply）
python tools/rebuild_deep_track.py                 # 默认 dry-run
python tools/rebuild_deep_track.py --apply         # 落地，自动备份到 deep_track_bak_*

# 5) 性能/正确性回归（2026-10 优化计划护栏）
python tools/bench_endpoints.py --db <snapshot.db>          # 接口 P50-P95 对照
python tools/bench_t8_engine.py compare --db <snapshot.db>  # 回测指纹 diff，须 = 0
python tools/golden_investor_api.py                        # investor 接口快照比对
```

> 排序键定义在 `strategy/stock_deep.candidate_order_by()`，门槛 `top_rank_min_score` 在 `config/strategy_params.py`；
> 跟踪建单（`sync_from_scan`）与面板展示（`get_market_signal_latest`）共用同一键，避免两侧看到不一致。
