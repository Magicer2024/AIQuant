# tools/ 工具脚本说明

本目录集中存放 AIQuant 的**运维、回填、重建工具**与**离线研究脚本**。脚本之间互不依赖、可独立运行，
统一约定从项目根目录以 `python tools/<脚本>.py [参数]` 调用（多进程工具须独立进程运行，见下方安全须知）。

---

## 一、命名约定（一眼判断用途）

| 前缀 | 类别 | 是否写库 | 说明 |
|---|---|---|---|
| `backfill_*` | 历史数据回填 | ✅ 写库（生产） | 补建历史缺失的扫描日 / 信号，落 `stock_signal` 或 `stock_deep_signal` |
| `rebuild_*` / `recalc_*` | 数据重建 / 重算 | ✅ 写库（生产） | 用新口径重跑并覆盖既有数据，通常带自动备份 |
| `fix_*` | 数据修复 | ✅ 写库（生产） | 修历史量价单位、缺失等脏数据 |
| `gen_*` | 报告生成 | ⚠️ 产文件 | 生成 HTML 报告到本地 / `reports/` |
| `eval_*` / `diag_*` / `mine_*` | 离线评估 / 诊断 / 挖掘 | ❌ 只读 | 复刻线上口径做回测对比，不改任何数据 |
| `backtest_*` / `walkforward_*` | 回测 / 样本外验证 | ❌ 只读 | 策略或参数网格回测 |
| `analyze_*` / `calibrate_*` / `verify_*` | 分析 / 校准 / 预验证 | ❌ 只读 | 单点假设验证 |
| `_` 前缀（如 `_eval_*` / `_diag_*`） | 一次性实验脚本 | ❌ 只读 | 探索性、不代表最终方案，结果不入主流程 |

> **写库 vs 只读**：凡属「评估 / 回测 / 诊断 / 挖掘」类（无 `backfill`/`rebuild`/`fix`/`gen` 前缀且非 `_` 实验稿的也多为只读）均不写库，可放心反复运行；`backfill`/`rebuild`/`fix` 会改动 SQLite 本地运行数据，运行前务必确认备份。

---

## 二、安全须知

1. **`quant.db`（~3.9G）不入库**（被 `.gitignore` 排除），但本地运行数据真实存在，写库工具会改它。
2. **`rebuild_deep_track.py --apply` 自动备份**：执行前先 `CREATE TABLE deep_track_bak_<时间戳> AS SELECT * FROM deep_track`，可逆；确认无误后可 `DROP` 备份表。
3. **Windows 多进程必须独立脚本**：`analyze_stock` 为纯 Python 计算，受 GIL 限制，线程池反而更慢；多进程工具（`backfill_deep_scan` / `backfill_signal_features`）通过 `subprocess.Popen` / `ProcessPoolExecutor` 独立拉起，**禁止在 Flask 请求或线程内直接起进程池**（会重复导入主模块、重启 app）。
4. **未来函数红线**：任何回补 / 重算历史信号的脚本都必须传 `as_of`（扫描日）截断行情，否则取到的是最新价、构成前视偏差（look-ahead bias）。
5. 只读脚本多数会把结果写入 `reports/*.json`，便于复核，不影响线上。

---

## 三、分类清单

### A. 数据回填（写库·生产）
| 脚本 | 作用 |
|---|---|
| `backfill_deep_scan.py` | 多进程回补历史扫描日，同步补建个股深度跟踪单；修复未来函数（`load_history` 加 `as_of` 截断）。参数：`--days/--start/--end/--procs/--dry-run/--progress-file` |
| `backfill_signal_features.py` | 回填 `stock_deep_signal` 排序特征列（`score/base_score/frac20/risk_pct/atr_pct`），历史行缺失会导致排序静默退化 |
| `backfill_lhb.py` | 龙虎榜明细回填（akshare 东方财富） |
| `backfill_lhb_momentum.py` | 历史龙虎榜动量信号回填（`stock_signal`） |
| `backfill_pullback_dip.py` | 历史「缩量回踩」信号回填（`stock_signal`） |
| `backfill_signal_extension.py` | 回填 `stock_signal.pct_above_ma` 扩展度字段 |
| `_run_deep_scan.py` | 手动触发一次全市场深析扫描（独立进程，确保用最新逻辑），落 `stock_deep_signal` |

### B. 数据修复 / 重建 / 重算（写库·生产）
| 脚本 | 作用 |
|---|---|
| `rebuild_deep_track.py` | 用当前排序键（见 `strategy/stock_deep.candidate_order_by`）重建 `deep_track` 跟踪单；`--apply` 前自动备份，`--dry-run`（默认）仅对比不写库 |
| `recalc_mid_history.py` | 中 / 长线信号历史重算（重建 `[EXIT_TRACK_START_DATE, 最新交易日]` 区间） |
| `fix_history.py` | 全量历史量价数据修正 |
| `fix_volume_unit.py` | 修复 `daily_price` 量价单位不统一（手 / 股 + 成交额混用） |

### C. 报告生成
| 脚本 | 作用 |
|---|---|
| `gen_stock_analysis.py` | 生成个股决策仪表盘 HTML 报告（星辰专家格式），供个股深度面板消费 |

### D. 个股深度候选排序（多为只读研究）
| 脚本 | 作用 |
|---|---|
| `eval_topk_pick.py` | **本次 top10 优化核心**：离线评估「每日 buy 池怎么挑 top10」的多种排序键，复刻真实入场 / 止损 / 移动止盈 / 到期规则。结论：新键均值 +0.07%→+1.56%、PF 1.02→1.48、胜率 60.2%→62.9% |
| `analyze_confirm_gap.py` | 只读研究：确认缺口（confirmation gap）分析 |
| `_eval_deep_buypoints.py` | 只读诊断：个股深度买点是否落在局部高点 |
| `_eval_adj_sort.py` | 只读对比：fusion_score 排序 vs 扩展度调整排序 |
| `_eval_perstock_threshold.py` | 只读 A/B：`_classify` 阈值改成「本股历史分数分位」（无未来函数 + 全局阈值对照臂）。**结论：等密度下 T+5 胜率 67.9%→66.4%，已被否决**，详见 `docs/stock-deep-perstock-calibration-report.md` |
| `_eval_deep_exit_reach.py` | 只读测量：个股深度计划价的止盈可达性（生产同口径逐笔模拟）。**结论：84.7% 交易由 10 天到期了结，止盈仅 4.7% 触达**，触达率随本股波幅 1.0%→8.6% 单调 |

### E. 短线策略评估 / 回测（只读研究）
| 脚本 | 作用 |
|---|---|
| `eval_short_engine.py` | 短线引擎回测对比（OLD 纯抄底 vs 现网） |
| `eval_short_fusion_cap.py` | 短线选股排序方向全窗评估 |
| `eval_short_return_boost.py` | 短线收益率提升对比实验 |
| `eval_short_sort_replay.py` | 短线排序变体「真实复盘口径」重放对比 |
| `eval_short_top3.py` | 短线 Top3 化 + 选股侧调参网格（近 6 月回测） |
| `eval_short_top3b.py` | Top3 实验补充（g22 低扩展 Top4 基准） |
| `eval_short_top3c.py` | 大盘状态（regime）过滤实验 |
| `eval_short_t1_filter.py` | Top3 精选位 T1 辅助过滤挖掘 |
| `eval_short_t1_filter2.py` | 实装语义复核（过滤发生在 Top3 截取之前，有替补） |
| `eval_short_t1_filter3.py` | 对账窗（2026-07-20 起）同口径复核 |
| `eval_short_t1_filter4.py` | regime 自动切换实装语义全窗复核 |
| `eval_fusion_mode.py` | 融合方式对比（只读） |
| `eval_quality_filter.py` | 质量过滤边界敏感性扫描 |
| `calibrate_short_topn.py` | 短线推荐 top-N 数量校准回测（0~4 个） |
| `eval_bottom_reentry.py` | 方法 B 离线回放：抄底「买回踩 + 重燃」vs 现网「买反弹」 |
| `eval_lhb_surge_boost.py` | 大阳突破 × 龙虎榜净买交集子集验证 |
| `diag_fusion_rank.py` | 验证「融合分排序」是否反向 |
| `diag_short_reco.py` | 短线推荐系统诊断（推荐时点位置分布等） |
| `backtest_dynamic_count.py` | 短线推荐数量动态调整回测（每天 8→4，差日子出 0） |
| `backtest_engine_compare.py` | v1（已反弹）vs v2（买回踩）vs v2+放宽闸门 全链路回测 |
| `backtest_enhance.py` | v2 之上的入场质量增强回测 |
| `backtest_exit.py` | 不同出场规则下真实收益模拟（v2 Top8） |
| `backtest_short_live_1y.py` | 短线近一年真实复盘口径回测 |
| `backtest_short_stress.py` | 对近一年回测结论的反向压力测试 |
| `walkforward_short_ext.py` | 扩展度「死区」稳健性 / 混杂隔离（样本外走步） |

### F. 中线 / 长线评估（只读研究）
| 脚本 | 作用 |
|---|---|
| `mine_mid_entry.py` | 中线买入端改进方向挖掘（go/no-go 证据，不改线上） |
| `mine_mid_highwin.py` | 中线高胜率风格选型挖掘 |
| `mine_pullback_quality.py` | 「缩量回踩低吸」假设只读挖掘 |
| `verify_mid_three_filter.py` | 三过滤组合 × 线上出场口径预验证 |
| `backtest_mid_exit_grid.py` | 中线移动止盈参数网格回测（向量化） |
| `backtest_midlong_exit.py` | 中 / 长线出场参数回测（验证止损放宽是否有正收益） |

### G. 隔日 / 动量 / 龙虎榜信号挖掘（只读研究）
| 脚本 | 作用 |
|---|---|
| `mine_next_day.py` | 隔日动量信号挖掘 |
| `mine_surge_next_day.py` | 「次日大涨 / 涨停」候选策略挖掘 |
| `_eval_lhb_momentum.py` | 龙虎榜隔日动量信号全期（2024-07~2026-08）表现验证 |

### H. 推荐复盘 / 对账 / 对比（只读，多为一次性）
| 脚本 | 作用 |
|---|---|
| `_diag_short_baseline.py` | 短线推荐实盘复盘基线（recommend_outcome 口径规范） |
| `_reconcile_plan_a.py` | 方案 A（移动止盈 / 止损 -5% / 启动 +8% / 回撤 3% / 持 10）落地后对账 |
| `_reconcile_short.py` | 用真实短线推荐验证回测模拟器口径一致性 |
| `_report_engine_compare.py` | 推荐策略对比报告：旧 v1 vs 最新 v2 的 T1~T5 胜率 |
| `_eval_pullback.py` | P1-2.1 回测对比：已反弹版 vs 买回踩 v2 平滑权重版 |
| `_eval_short_long.py` | 短线策略长周期检验 v2（含改动前后对照） |
| `_eval_v2_topn_portfolio.py` | v2 引擎原始候选 top-N 组合表现实验 |

---

## 四、本次优化相关常用命令

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
```

> 排序键定义在 `strategy/stock_deep.candidate_order_by()`，门槛 `top_rank_min_score` 在 `config/strategy_params.py`；
> 跟踪建单（`sync_from_scan`）与面板展示（`get_market_signal_latest`）共用同一键，避免两侧看到不一致。
