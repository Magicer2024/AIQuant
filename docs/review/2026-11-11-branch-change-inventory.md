# 2026-11-11 分支修改项盘点与修改记录（dev_0.0.1）

> **结论第一句**：分支与远端已同步在 `c1e7b7d`；工作区 67 项在途改动是一次「归档清理」的半成品——44 个删除项在 `archive/` 下都有**内容无损**的副本，但两边都还没提交，**只提交删除会让这些文件从仓库里消失**。本次只提交本记录与 `.gitignore` 一行，未动任何在途改动。

## ① 分支状态（实测，非自报）

| 项 | 值 |
|---|---|
| 本地 HEAD | `c1e7b7d` |
| `origin/dev_0.0.1` | `c1e7b7d`（**一致**；此前挂着的 `bd294df` + `c1e7b7d` 推送已完成） |
| 未推送提交数 | **0** |
| dev 领先 main | **21** 个提交 |
| main tip | `ae229e6` |

## ② 工作区在途改动：67 项（23 修改 + 44 删除）

### 2.1 删除 44 项 —— 全部是归档搬迁，不是功能删除

| 目录 | 数量 | 内容 |
|---|---|---|
| `tools/` | 23 | `_diag_*` 20 个、`_probe_*` 2 个、`_backtest_short_combo.py` 1 个（一次性诊断脚本） |
| `core/repository/` | 7 | futures / margin / mgmt / north / position / rule / trade 七个 repo |
| `docs/` | 6 | ARCHITECTURE、architecture_review、compose/plans 2、superpowers plans+specs 2 |
| `config/` | 2 | `strategies/default.yaml`、`thresholds.py` |
| `qlib_engine/` | 2 | `migration.py`、`model_runner.py` |
| `strategy/` | 2 | `config.py`、`intent/parser.py` |
| 根目录 | 1 | `scan_dirty_data.py` |
| `utils/` | 1 | `finance_data.py` |

### 2.2 修改 23 项

`CLAUDE.md`、`README.md`、`backtest/service.py`、`config/__init__.py`、`config/settings.py`、`config/strategy_params.py`、`constraints.txt`、`core/db.py`、`core/outcome_tracker.py`、`core/pipeline.py`、`requirements.txt`、`routes/backtest.py`、`routes/sync.py`、`scheduler/runner.py`、`start.bat`、`strategy/first_reversal.py`、`strategy/strategies.py`、`tests/test_discipline.py`、`tests/test_repository.py`、`tests/test_scheduler_runner.py`、`tools/README.md`、`tools/backfill_first_reversal.py`、`tools/track_trailing_rollout.py`

形态上是「删掉旧模块 + 清理对它们的引用」，但**引用清理是否完整未经验证**（`core/db.py`、`strategy/strategies.py`、`tests/test_repository.py` 都动过）。

## ③ 归档副本核验（管理A 独立复核）

- 44 个删除项，**44/44** 在 `archive/` 下找到同名副本（部分有两份：`archive/2026-11-06-*` 与 `archive/legacy/2026-11-06/*`）。
- 原始 sha256 比对：**44/44 不一致** → 追查后确认是 **CRLF 换行差异**（Windows 落盘 CRLF，git blob 为 LF）。
- 归一化（去 `\r`、去 BOM）后重算：**44/44 内容完全一致**。抽样三例 `config/thresholds.py`、`core/repository/trade_repo.py`、`docs/ARCHITECTURE.md` 归一化哈希逐位相同。
- **结论**：副本无损，成对提交安全。但**必须在同一个 commit 里同时包含 `archive/` 副本与原位置删除**；只提交删除 = 文件从仓库消失，只提交副本 = 仓库里出现两份。

## ④ 未跟踪文件：919 项分类

| 类别 | 数量 | 处置建议 |
|---|---|---|
| `archive/` | 154 | 与 44 个删除**成对提交**（见 ③） |
| `data_snapshots/` | 690 | **不入库**。gz 数据快照，属运行产物 |
| 其它 | 75 | `docs/review/` 执行日志与审计、`docs/reference/config-single-source.md`、`docs/roadmap-trustworthy-daily-loop.md`、`research/reversal_v2_20261103/*` —— 属团队留档，建议单独一个 docs commit |

**本次修的隐患**：`.gitignore` 原本忽略 `*.db`、`data_cache/`、`reports/`、`logs/`，但**没有忽略 `data_snapshots/`**。任何一次 `git add .` 都会把 690 个 gz 快照带进仓库。本次补一行 `data_snapshots/`。

## ⑤ 本次提交内容（只有这两项）

1. `docs/review/2026-11-11-branch-change-inventory.md`（本记录）
2. `.gitignore`：新增 `data_snapshots/`

**未提交**：67 项在途改动、919 项未跟踪（除本记录外）。**未推送**。**未碰 main**。**未 stash**。

## ⑥ 待决项（需 owner 确认，管理A 不代做）

| 项 | 建议动作 | 为什么不由管理A 直接做 |
|---|---|---|
| 归档成对提交 | 一个 commit 同时 `add archive/`（154）+ 44 个删除 | 涉及删除入库，不可逆；且这是他人**在途**的清理操作，其完整性需操作者确认 |
| 23 个修改 | 逐文件看 diff，确认引用清理完整、测试可跑 | 可能含未完成重构，提交即固化 |
| 75 个 docs/research 留档 | 单独 docs commit | 属他人产出，需其确认已定稿 |

## ⑦ 本轮两项判定留档

**判定 A（2026-11-11，Yu Fang 亲自决策）**：股票池范围冻结为**沪深主板（60/00）+ 创业板（30）**；科创板（68）与北交所（8x/4x）**不在 universe 内**。已写入周报自动任务 `tr_4aeda10493f63701e8860977` 的铁律与第 2 行固定注解。改池属 Yu Fang 审批线，自动任务无此权限。理由：现有 7,027 笔 buy 全部来自该池，改池会打掉三条研究线结论的历史可比性。

**行业补数（2026-11-11）**：`stock_info.industry` 回填 **4519/4523 = 99.91%**，证监会（CSRC）口径 83 个行业 + `industry_src` 来源列（CSRC 4519 / CSRC_EMPTY 4）。整库备份 `_backup/quant_20261111_2200.db` + 写前快照 `stock_info_pre_20261111_2200.csv`。复核：`pragma quick_check = ok`、`stock_deep_signal` 87,341 行未动、`daily_price` 14,918,164 行 max=2026-11-11。首份分布：buy 7,027 笔覆盖 80 个行业，单行业最高 C39 13.3%（未触 25% 集中度线）。**东财行业板块第二维（`stock_industry_em`）在数据侧手上，尚未交付。**

## ⑧ 红线复核

未推 main、未改生产参数、未改推荐口径、未 stash、未提交他人在途改动；全部库访问 `mode=ro`；不承诺收益、不给行动指令。
