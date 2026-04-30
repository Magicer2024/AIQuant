# A股量化系统 —— 架构改进建议

> 分析时间：2026-04-30
> 基于 AkShare 数据栈和当前代码库现状

---

## 一、当前架构全景

```
启动仪表板.bat → python app.py → 仪表板.html
                     ↓
              ┌──────┴──────┐
         Streamlit      Flask API (39路由)
         (双轨并行)       ↓
                     core/ + strategy/ + backtest/ + scripts/
```

### 代码规模统计

| 模块 | 文件数 | 代码行数 | 主要问题 |
|------|--------|----------|----------|
| `core/db.py` | 1 | ~1900 | 12个表的CRUD全塞一个文件 |
| `backtest/` | 17 .py | ~9000 | 9个回测引擎变体，高度重复 |
| `scripts/` | 30+ .py | ~6000 | diag/下20+一次性脚本未清理 |
| `app.py` | 1 | ~800 | 路由+业务逻辑+调度全混 |
| `strategy/` | 4 .py | ~1500 | 相对健康，但配置分散 |
| `app_pages/` | 6 .py | ~800 | Streamlit前端，与Vue并行 |

---

## 二、五大核心问题

### 问题1：数据库模块严重膨胀（db.py）

**现状**：一个文件管12个表，每个表都有 `upsert_*` / `get_*` / `get_latest_*` / `_clean` / `_fmt`，重复模式超过80%。

**后果**：
- 新增一张表要复制粘贴150行代码
- 事务边界不清晰（部分用 `get_conn()` 上下文，部分手动 commit）
- `_safe_add_column` 等迁移逻辑和运行时查询混在一起

**目标**：按表拆分为 Repository 类，抽取通用 CRUD 基类。

---

### 问题2：回测引擎碎片化

**backtest/ 目录现状**：

| 文件 | 用途 | 行数 | 是否核心 |
|------|------|------|----------|
| `backtest.py` | 单股回测引擎（T+1） | 516 | 核心 |
| `strategy_screen_backtest.py` | 批量策略选股回测 | 779 | 核心 |
| `custom_strategy_backtest.py` | 自定义策略函数回测 | 662 | 核心 |
| `backtest_v4.py` | 超跌反弹v4策略 | 623 | 策略特化 |
| `backtest_v3.py` | 超跌反弹v3策略 | 675 | 历史遗留 |
| `backtest_bt.py` | Backtrader封装 | 851 | 实验性 |
| `backtest_quant.py` | 量化框架实验 | 595 | 实验性 |
| `backtest_oversold.py` | 超跌策略实验 | 534 | 实验性 |
| `independent_backtest.py` | 独立回测引擎 | 356 | 重复造轮子 |

**核心矛盾**：
- `backtest.py`、`strategy_screen_backtest.py`、`custom_strategy_backtest.py` 三者的**资金曲线计算、手续费、滑点、止损止盈逻辑几乎相同**，但各自实现了一遍。
- `backtest_v3/v4` 本质上是 "策略"，却放在了 `backtest/` 目录下，和 "引擎" 混在一起。

**目标**：
- 保留 **1个通用回测引擎**（负责资金曲线、手续费、滑点、持仓管理）
- 所有策略（放量突破、超跌反弹v4、条件扫描等）都实现为 **可插拔的策略接口**
- 删除或归档历史遗留的实验性引擎

---

### 问题3：API层职责过重（app.py）

**现状**：
- 39个路由定义
- 数据同步的后台线程调度（`_run_sync_blocking`）
- 策略扫描的后台线程调度（`_run_scan_blocking`）
- 定时任务调度器管理
- 各种数据转换辅助函数（`safe_float`、`df_to_json_safe`）

**后果**：任何业务改动都要改 app.py，冲突概率高，测试困难。

**目标**：
- `app.py` 只保留路由注册（薄层）
- 业务逻辑下沉到 `services/`（如 `SyncService`、`ScanService`、`BacktestService`）
- 后台调度独立到 `scheduler/` 模块

---

### 问题4：scripts/diag/ 成为垃圾堆

**现状**：`scripts/diag/` 下有 20+ 个 `check_*.py`、`debug_*.py` 脚本，都是一次性诊断用途。

```
check_dates.py       (24行)
check_dist.py        (22行)
check_fusion.py      (69行)
check_latest.py      (22行)
check_market.py      (25行)
check_scores.py      (35行)
check_scores2.py     (32行)
check_schema.py      (8行)
check_sync.py        (25行)
check_tables.py      (11行)
debug_420.py         (34行)
debug_equity_drop.py (187行)
debug_full_backtest.py (252行)
debug_ui_params.py   (251行)
...
```

**问题**：
- 这些脚本大量复制了 `core/db.py` 的连接逻辑
- 大部分已经过时（如 debug_equity_drop 对应的 bug 早已修复）
- 新来的人不知道这些脚本是干什么的，不敢删

**目标**：清理归档，只保留有持续价值的脚本（如 `full_scan_csv.py`）。

---

### 问题5：前端双轨并行

**现状**：
- 主前端：`仪表板.html`（Vue 3 + ECharts 5），功能完整，体验好
- 副前端：`streamlit_app.py` + `app_pages/`（6个页面），用于快速原型

**问题**：维护两套前端的 API 适配逻辑，Streamlit 的缓存机制和 Flask 后端的数据流经常打架（见 MEMORY.md 中 2026-04-15 的旧缓存 bug）。

**建议决策**：
- 如果 `仪表板.html` 已经覆盖所有核心场景，**废弃 Streamlit**
- 如果还需要 Streamlit 做快速实验，把它独立成一个子项目或分支

---

## 三、分阶段改进路线图

### 第一阶段：清理与合并（1-2天，零风险）

1. **清理 scripts/diag/**
   - 创建 `scripts/archive/` 目录
   - 把所有 `check_*.py`、`debug_*.py` 移入归档（除 `full_scan_csv.py`、`condition_scan.py` 外）
   - 保留 `scripts/batch/batch_backtest.py`（持续使用）

2. **清理回测引擎**
   - 把 `backtest_v3.py`、`backtest_bt.py`、`backtest_quant.py`、`backtest_oversold.py` 移入 `backtest/archive/`
   - 保留核心 trio：`backtest.py`、`strategy_screen_backtest.py`、`custom_strategy_backtest.py`
   - 标记 `independent_backtest.py` 为待合并（功能已被 `strategy_screen_backtest.py` 覆盖）

3. **前端二选一**
   - 废弃 `streamlit_app.py` + `app_pages/`（或单独分支保留）
   - 后续所有 UI 需求走 `仪表板.html`

**预期收益**：文件数从 80+ 降到 50+，心理负担大幅降低。

---

### 第二阶段：分层重构（3-5天，低风险）

#### 2.1 拆分 db.py → Repository 模式

```
core/
  db.py              # 只保留：连接管理、init_db、通用基类
  repository/
    __init__.py
    base.py          # BaseRepository（通用 upsert/get/latest）
    stock_repo.py    # StockRepository
    price_repo.py    # PriceRepository
    signal_repo.py   # SignalRepository
    position_repo.py # PositionRepository
    trade_repo.py    # TradeRepository
    margin_repo.py   # MarginRepository
    north_repo.py    # NorthMoneyRepository
```

**BaseRepository 设计示例**：

```python
class BaseRepository:
    def __init__(self, table: str, pk_cols: list[str]):
        self.table = table
        self.pk_cols = pk_cols

    def upsert(self, records: list[dict], col_types: dict):
        # 通用 UPSERT 逻辑，消灭重复
        ...

    def get_latest(self, filter_col: str = None, filter_val = None) -> str | None:
        # 通用 MAX(date) 查询
        ...
```

每个具体 Repository 只需声明表结构和字段映射，不必重写 `upsert` 逻辑。

#### 2.2 拆分 app.py → routes + services

```
app.py                  # Flask 应用实例 + 蓝图注册
routes/
  __init__.py
  stock.py              # /api/stock/*
  backtest.py           # /api/backtest/*
  position.py           # /api/position/*
  signal.py             # /api/signals/*
  system.py             # /api/system/*
  optimizer.py          # /api/optimizer/*
services/
  __init__.py
  sync_service.py       # 数据同步业务逻辑
  scan_service.py       # 策略扫描业务逻辑
  backtest_service.py   # 回测参数组装与调用
scheduler/
  __init__.py
  jobs.py               # schedule 任务定义
  runner.py             # 后台线程启动/停止管理
```

#### 2.3 统一回测引擎接口

定义策略接口：

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class SignalOutput:
    buy: bool
    sell: bool
    score: float
    metadata: dict

class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """返回含 BUY_SIGNAL / SELL_SIGNAL / SCORE 的 DataFrame"""
        ...
```

所有策略（放量突破、超跌反弹v4、均线粘合等）都实现这个接口。
回测引擎只接收 `Strategy` 实例列表，不关心具体是什么策略。

---

### 第三阶段：工程化提升（持续进行）

#### 3.1 统一配置中心

```
config/
  __init__.py
  settings.py           # 基础配置（DB路径、API端口）
  strategy_params.py    # 各策略默认参数
  thresholds.py         # 扫描阈值、回测默认参数
```

当前分散在各处的配置：
- `FUSION_THRESHOLD=20.0`（在 `strategy/strategies.py` 和 `quant.py` 中都有）
- `backtest.py` 中的 `commission_rate=0.0003`、`slippage_rate=0.001`
- `backtest_v4.py` 中的止损-6%、跟踪止盈10%
- `app.py` 中的定时任务时间（07:00、09:00）

全部收敛到 `config/`。

#### 3.2 补充核心测试

```
tests/
  test_db_repository.py
  test_backtest_engine.py    # 重点：验证T+1、资金曲线、手续费计算
  test_strategy_signals.py   # 各策略信号生成正确性
  test_condition_builder.py  # 条件扫描解析
```

当前 `tests/` 只有5个文件，且从 MEMORY.md 看曾多次出现 "调用方未同步更新" 的 bug（如 equity_curve 返回类型变更）。测试覆盖率提升后，这类问题可以在本地发现。

#### 3.3 类型注解全覆盖

当前只有部分函数有类型注解（如 `db.py` 中的 `get_stock_name(code: str) -> str`），大量核心函数缺失。建议：
- 新代码必须带类型注解
- 老代码在重构时顺手补齐

---

## 四、收益预估

| 指标 | 当前 | 改进后 | 收益 |
|------|------|--------|------|
| 核心 Python 文件数 | ~80 | ~50 | 维护负担 -40% |
| db.py 行数 | ~1900 | ~200（基类+连接）+ 分散到8个repo | 单文件复杂度 -90% |
| 回测引擎数 | 9个 | 1个通用 + 策略插件 | 重复代码 -70% |
| app.py 路由+逻辑 | 混在一块 | 路由/服务/调度分离 | 改动冲突概率大幅下降 |
| 一次性脚本 | 20+ | 归档清理 | 代码库整洁度提升 |

---

## 五、建议的下一步行动

如果你认同这个方向，建议按以下顺序执行（每个步骤都是独立的，可随时暂停）：

1. **今天**：清理 `scripts/diag/`（移入 archive，零风险）
2. **明天**：清理 `backtest/` 历史遗留文件（移入 archive，零风险）
3. **本周内**：决定是否废弃 Streamlit（决策点）
4. **下周**：拆分 `db.py` → `core/repository/`（低风险，有测试即可验证）
5. **后续**：逐步拆分 `app.py` 路由

需要我帮你执行其中任何一步吗？
