# 策略回测系统架构文档

> 版本：v0.0.2 | 更新：2026-04-18
> 核心功能：A股量化选股 + 全市场回测 + 可视化条件构建

---

## 目录

1. [项目概览](#1-项目概览)
2. [目录结构](#2-目录结构)
3. [技术架构图](#3-技术架构图)
4. [核心模块详解](#4-核心模块详解)
   - [4.1 指标计算引擎 (`indicator_engine.py`)](#41-指标计算引擎-indicator_enginepy)
   - [4.2 条件构建器 (`condition_builder.py`)](#42-条件构建器-condition_builderpy)
   - [4.3 自定义策略回测 (`custom_strategy_backtest.py`)](#43-自定义策略回测-custom_strategy_backtestpy)
   - [4.4 策略筛选回测 (`strategy_screen_backtest.py`)](#44-策略筛选回测-strategy_screen_backtestpy)
   - [4.5 回测 UI (`backtest_ui.py`)](#45-回测-ui-backtest_uipy)
   - [4.6 超跌反弹 v4 (`backtest_v4.py`)](#46-超跌反弹-v4-backtest_v4py)
5. [三种选股模式](#5-三种选股模式)
6. [数据流](#6-数据流)
7. [可视化条件构建器使用指南](#7-可视化条件构建器使用指南)
8. [策略评分体系](#8-策略评分体系)
9. [回测参数说明](#9-回测参数说明)
10. [扩展指南](#10-扩展指南)

---

## 1. 项目概览

**定位**：A股量化交易系统的策略研究与回测模块

**核心能力**：

- 全市场 3800+ 只股票历史数据管理
- 5策略融合评分体系（放量突破/均线粘合/量价背离/抄底/主力建仓）
- 3种选股方式：预设模板 / 可视化条件构建 / 自定义 Python 代码
- 全市场批量回测，支持参数优化
- 超跌反弹 v4 专项回测（年化 +20.43%，胜率 45.1%）

**技术栈**：Python 3.11 + Streamlit + SQLite + Plotly + ECharts

---

## 2. 目录结构

```
jiaoyi/
├── docs/                          # 文档
│   └── ARCHITECTURE.md            # 本文档
│
├── backtest/                      # 回测核心（重点）
│   ├── __init__.py
│   ├── backtest.py               # 基础回测引擎（单股）
│   ├── backtest_v4.py            # 超跌反弹 v4 引擎
│   ├── backtest_ui.py            # 回测结果 UI 渲染
│   ├── indicator_engine.py       # 🆕 批量指标计算引擎
│   ├── condition_builder.py      # 🆕 可视化条件构建器
│   ├── strategy_screen_backtest.py # 预设策略 + 批量筛选回测
│   ├── custom_strategy_backtest.py  # 自定义策略回测引擎
│   └── batch/
│       └── batch_backtest.py     # 批量回测（旧版，已保留）
│
├── strategy/                      # 策略信号计算
│   ├── __init__.py
│   ├── strategies.py             # 5策略打分函数
│   └── strategy_score.py         # 融合分计算
│
├── core/                          # 核心基础设施
│   ├── db.py                     # 数据库操作（SQLite）
│   ├── sync.py                   # 数据同步（baostock）
│   ├── data_fetcher.py           # 数据获取封装
│   └── stock_info.db             # stock_info 表
│   └── quant.db                  # daily_price / stock_signal 表
│
├── app_pages/                    # Streamlit 页面组件
│   ├── dashboard.py              # 仪表板 + K线图
│   ├── signal_history.py         # 历史推荐记录
│   ├── position_management.py     # 持仓管理
│   └── backtest.py              # 回测入口
│
├── utils/                        # 工具函数
│   └── finance_data.py           # 财务数据工具
│
├── strategy_backtest_app.py     # 策略回测 Streamlit 主入口（端口8503）
├── backtest_app.py              # 独立回测应用（老版本入口）
├── quant.py                     # 策略扫描批处理脚本
├── streamlit_app.py             # 主仪表板 Streamlit（端口8501）
└── requirements.txt             # 依赖列表
```

---

## 3. 技术架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         用户交互层                                   │
│                                                                      │
│   ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │
│   │ 预设策略模板  │  │ 可视化条件构建器  │  │  自定义 Python 代码  │  │
│   │  (preset)   │  │   (custom)       │  │      (code)          │  │
│   └──────┬───────┘  └────────┬─────────┘  └──────────┬───────────┘  │
└──────────┼───────────────────┼───────────────────────┼──────────────┘
           │                   │                       │
           ▼                   ▼                       │
   ┌──────────────────────────────────────────────┐    │
   │            回测引擎层                          │    │
   │                                                │    │
   │  ┌─────────────────┐  ┌──────────────────────┐ │    │
   │  │StrategyScreen   │  │CustomStrategy        │ │    │
   │  │Backtest         │◄─┤Backtest              │─┘    │
   │  │(预设+筛选)      │  │(自定义/条件构建器)    │      │
   │  └────────┬────────┘  └──────────┬───────────┘      │
   │           │                      │                  │
   │           ▼                      ▼                  │
   │  ┌────────────────────────────────────────────┐     │
   │  │         BacktestEngine（共享撮合逻辑）       │     │
   │  │  仓位管理 / 止盈止损 / 资金曲线 / 统计指标   │     │
   │  └──────────────────────┬─────────────────────┘     │
   │                         │                            │
   │  ┌──────────────────────▼─────────────────────┐     │
   │  │      指标计算引擎 IndicatorEngine           │     │
   │  │  (MA/RSI/KDJ/MACD/布林带/成交量/...)       │     │
   │  └──────────────────────┬─────────────────────┘     │
   │                         │                            │
   │  ┌──────────────────────▼─────────────────────┐     │
   │  │         条件构建器 ConditionBuilder         │     │
   │  │  (单点值/持续N日/出现过N日/统计值/AND-OR)   │     │
   │  └────────────────────────────────────────────┘     │
   └──────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         数据层                                       │
│                                                                      │
│   ┌─────────────────────┐        ┌──────────────────────────────┐   │
│   │   quant.db          │        │     stock_info.db            │   │
│   │   (1.5GB)           │        │                              │   │
│   │  daily_price  3800股 │        │   stock_info (4000+股票)     │   │
│   │  index_daily   5指数 │        │                              │   │
│   │  stock_signal  推荐   │        └──────────────────────────────┘   │
│   └─────────────────────┘                                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. 核心模块详解

### 4.1 指标计算引擎 (`indicator_engine.py`)

**职责**：一次性计算所有技术指标，供策略函数复用，避免重复计算。

**核心类**：

```
IndicatorRegistry          指标注册表（单例）
  ├── _indicators: dict   指标字典
  ├── calc(key, df)      计算单个指标
  ├── keys()              所有指标名
  └── by_category()       按分类返回

compute_indicators(df)    批量计算所有指标
eval_condition(series, cond_type, comparator, threshold, period, stat_method)
                           计算条件布尔序列
```

**已注册指标（50+个）**：

| 分类 | 指标 |
|------|------|
| 价格 | close, open, high, low |
| 均线 | MA5/10/20/30/60/120, EMA5/10 |
| 动量 | RSI(6/12/14), KDJ_K/D/J, MACD/DIF/DEA, CCI(14), 威廉%R(14) |
| 量能 | volume, amount, vol_ma5/10/20, 量比, 换手率, OBV |
| 波动 | 当日振幅, ATR(14), ret_3d/5d/10d/20d/60d |
| 布林 | 布林上轨, 布林中轨, 布林下轨 |
| 策略分 | fusion_score, vol_score, ma_score, diverge_score, bottom_score, whale_score |
| 排名 | volume_rank_pct, amount_rank_pct |

**使用方式**：

```python
from backtest.indicator_engine import compute_indicators, _REGISTRY

# 批量计算所有指标
df = compute_indicators(df)

# 计算单个指标
reg = _REGISTRY
ma5_series = reg.calc("MA5", df)

# 在指标序列上计算条件
from indicator_engine import eval_condition
cond = eval_condition(ma5_series, cond_type="all_n", comparator="gt",
                     threshold=10.0, period=5)
```

---

### 4.2 条件构建器 (`condition_builder.py`)

**职责**：将可视化配置转换为策略函数，支持 4 种条件类型和条件组组合。

**数据结构**：

```
SingleCondition          单个条件
  ├── cond_type           CondType.POINT/ALL_N/ANY_N/STAT_N
  ├── indicator           指标 key（MA5/RSI14/成交量/...）
  ├── comparator          比较符（>/≥/</≤/上穿/下穿/区间）
  ├── threshold_value     固定阈值
  ├── threshold_is_indicator  阈值为另一个指标（MA5 > MA10）
  ├── threshold_indicator  阈值指标
  ├── range_low/high      区间上下界
  ├── period              N日窗口
  ├── stat_method          统计方式（均值/最大/最小/求和/标准差）
  └── evaluate(df)        计算布尔序列

ConditionGroup           条件组
  ├── group_id            组编号（A/B/C...）
  ├── conditions: list    条件列表
  ├── group_op             组内逻辑（and/or）
  └── evaluate(df)        组内条件组合

build_strategy_function(groups, warmup_period=20)
                         生成 strategy_function 代码字符串
```

**4 种条件类型**：

| 类型 | 说明 | 示例 |
|------|------|------|
| `point` | 单点值比较 | `收盘价 > MA5` |
| `all_n` | 持续 N 日 | `近5日 MA5 > MA10`（均线多头排列） |
| `any_n` | 出现过 N 日 | `近20日 RSI > 80`（超买预警） |
| `stat_n` | 统计值 | `近10日成交量的均值 > 500万` |

**使用方式**：

```python
from backtest.condition_builder import (
    ConditionGroup, SingleCondition, CondType,
    Comparator, build_strategy_function
)

# 构建条件
groups = [
    ConditionGroup(
        group_id=0,
        conditions=[
            SingleCondition(cond_type=CondType.ALL_N, period=5,
                          indicator="MA5", comparator=Comparator.GT,
                          threshold_is_indicator=True, threshold_indicator="MA10"),
            SingleCondition(cond_type=CondType.ANY_N, period=20,
                          indicator="RSI14", comparator=Comparator.GT,
                          threshold_value=80),
        ],
        group_op="and"
    )
]

# 生成策略函数
code = build_strategy_function(groups)
fn, err = compile_strategy(code)
engine = CustomStrategyBacktest(fn, params)
result = engine.run()
```

---

### 4.3 自定义策略回测 (`custom_strategy_backtest.py`)

**职责**：对自定义策略函数（或条件构建器生成代码）进行全市场回测。

**核心类**：

```
CustomBacktestParams     回测参数
  ├── start_date / end_date
  ├── capital              初始资金
  ├── max_positions        最大同时持股数
  ├── position_pct         每仓仓位比例
  ├── max_hold_days        最大持仓天数
  ├── extra_stop_loss      额外止损线
  ├── extra_take_profit    额外止盈线
  └── ...

CustomStrategyBacktest    回测引擎
  ├── run()               执行回测
  ├── _summarize()        汇总统计
  └── _static_filter()    静态过滤（ST/科创板/创业板）
```

**strategy_function 约定**：

```python
def strategy_function(df, stock_code=None, **params) -> pd.DataFrame:
    """
    接收：单只股票 OHLCV 历史数据（含指标列）
    返回：含 'signal' 列的 DataFrame
          signal = 1 → 买入信号
          signal = -1 → 卖出信号
          signal = 0  → 持有/无操作
    """
    df = df.copy()
    df["signal"] = ...  # 计算信号
    return df[["signal"]]
```

**可用 df 列**：

```
open, high, low, close, volume, amount, pct_change, turnover
+ compute_indicators() 计算的所有指标
+ fusion_score, vol_score, ma_score, diverge_score, bottom_score, whale_score
```

---

### 4.4 策略筛选回测 (`strategy_screen_backtest.py`)

**职责**：基于预存策略评分数据，对全市场按日筛选候选股并模拟交易。

**核心概念**：

```
每日筛选流程：
  1. 扫描全市场股票
  2. 按 sort_by 字段排序候选股
  3. 取 top N 只买入
  4. 已有持仓检查卖出条件
  5. 更新资金曲线

sort_by 可选字段（对应数据库列）：
  volume, amount, vol_ratio_5d, vol_sum_5d, fusion_score,
  pct_change, turnover, ret_5d, vol_rank, amount_rank ...
```

**预设策略模板**（在 `backtest_ui.py` 中定义）：

| 模板名 | 条件 |
|--------|------|
| 5日线突破 | MA5>MA10 + 近2日振幅<5% + 当日振幅<8% + 换手率1~6% |
| 均线多头+放量 | 收盘>MA5/10/20 + 量能>1.5倍均量 |
| 超跌反弹 | 近5日跌幅>8% + 收盘>最低 + 换手率>1% |
| 量价背离 | 近2日价格变动<3% + 近5日缩量 |
| 融合信号高分 | 融合分 ≥ 15 |

---

### 4.5 回测 UI (`backtest_ui.py`)

**职责**：Streamlit UI 渲染，包括预设 tab、条件构建器 tab、代码 tab，以及结果展示。

**主要函数**：

```
render_preset_tab()           预设策略选择 Tab
render_custom_tab()           🆕 可视化条件构建器 Tab
render_code_tab()             自定义代码 Tab
render_backtest_result()      回测结果指标卡 + 资金曲线
render_trades_table()         交易记录表格（含盈亏着色）
render_position_detail()      每日持仓明细
```

---

### 4.6 超跌反弹 v4 (`backtest_v4.py`)

**职责**：专项回测超跌反弹策略（跟踪止盈 + 大盘择时）。

**核心参数**：

```python
run_oversold_v4(
    start_date, end_date,
    init_capital,              # 初始资金
    stop_loss=-0.06,          # 止损 6%
    trailing_pct=0.10,        # 跟踪止盈回撤 10%
    single_pos_ratio=0.40,   # 单股仓位上限 40%
    score_threshold=1.8,     # 信号分阈值
    use_market_timing=True,  # 大盘择时（上证MA5>MA20）
)
```

**回测结果（2025-04 ~ 2026-04）**：

| 指标 | 值 |
|------|------|
| 年化收益率 | +20.43% |
| 胜率 | 45.1% |
| 最大回撤 | -11.9% |
| 交易笔数/年 | ~112 |

---

## 5. 三种选股模式

```
┌─────────────────────────────────────────────────────────────────┐
│                     🎯 策略选股回测                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐ ┌────────────────────────┐ ┌────────────────┐  │
│  │🎯 预设策略模板│ │🛠️ 可视化条件组合      │ │💻 自定义策略  │  │
│  │              │ │                        │ │   函数         │  │
│  │ 直接选择     │ │ 选指标+比较符+阈值      │ │                │  │
│  │ 5种预设模板  │ │ 自由组合 AND/OR         │ │ 编写Python     │  │
│  │              │ │ 支持持续/出现/统计条件  │ │ 代码           │  │
│  │ 零配置       │ │ 实时预览条件描述        │ │ 完全灵活       │  │
│  │ 开箱即用     │ │ 生成代码可查看         │ │ 需要编程基础   │  │
│  └──────┬───────┘ └───────────┬────────────┘ └───────┬────────┘  │
│         │                      │                      │           │
│         └──────────────────────┼──────────────────────┘           │
│                                ▼                                │
│                    ┌──────────────────────────┐                  │
│                    │   CustomStrategyBacktest │                  │
│                    │        回测引擎          │                  │
│                    └──────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 6. 数据流

```
数据同步（core/sync.py）
    │
    ▼
quant.db / stock_info.db
    │
    ├── daily_price ────► 回测引擎 ◄── CustomStrategyBacktest
    │                        │              │
    │                        ▼              ▼
    │                 指标计算引擎    条件构建器
    │                 (indicator_engine)  (condition_builder)
    │                        │              │
    │                        └──────┬───────┘
    │                               ▼
    │                    strategy_function(df)
    │                               │
    │                               ▼
    │                    signal_map: {code: df(signal列)}
    │                               │
    │                               ▼
    │                    按时间撮合（买入/卖出/资金曲线）
    │                               │
    │                               ▼
    │                    回测结果（指标卡+资金曲线+交易记录）
    │
    └── stock_signal ──► 融合推荐面板（dashboard）
```

---

## 7. 可视化条件构建器使用指南

### 7.1 界面布局

```
🛠️ 可视化条件组合
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[➕ 添加条件组]  [➖ 删除最后组]  [🔄 重置]

┌─ 条件组 A ──────────────────────────────────────────────┐
│ AND 同时满足  │                                    [删除]│
│ ──────────────────────────────────────────────────────── │
│ 类型: [单点值比较 ▼]   指标: [[均线] MA5 ▼]             │
│ 比较符: [> ▼]           阈值: [10.00     ]              │
│ 💡 收盘价 > 10.00                              [🗑️]  │
│ ──────────────────────────────────────────────────────── │
│ 类型: [持续N日    ▼]   指标: [[均线] MA5 ▼]             │
│ 比较符: [> ▼]           N日: [5   ]                     │
│ 比较符: [> ▼]           阈值: [MA10  ☑阈值为指标]        │
│ 💡 近5日 MA5 持续 > MA10                            [🗑️] │
│ ──────────────────────────────────────────────────────── │
│ ➕ 添加条件                                                │
└──────────────────────────────────────────────────────────┘
```

### 7.2 条件类型详解

#### 单点值比较（POINT）
直接比较当期指标值与阈值。

```
示例：RSI(14) > 70
     收盘价 > MA20
     量比 < 0.8
     MACD 上穿 0轴
```

#### 持续 N 日（ALL_N）
要求窗口内每一期都满足条件。用于趋势确认。

```
示例：近5日 MA5 > MA10          ← 均线多头排列
     近10日 RSI >= 30           ← 没有超卖
     近3日 成交量 > 100万        ← 连续放量
```

#### 出现过 N 日（ANY_N）
窗口内至少有一期满足即可。用于反转/突破信号。

```
示例：近20日 出现过 RSI > 80    ← 超买预警
     近10日 出现过 收盘 > MA20   ← 曾经突破
     近5日  出现过 量比 > 3      ← 突然放巨量
```

#### 统计值（STAT_N）
对窗口内数据做统计，再与阈值比较。

```
统计方式：均值 / 最大值 / 最小值 / 求和 / 标准差

示例：近10日 成交量的均值 > 500万     ← 持续活跃
     近20日 最高价的最小值 > 50       ← 底部抬高
     近5日  涨跌幅的标准差 < 3%       ← 波动收敛
```

### 7.3 典型策略配置

**均线多头排列 + 放量**：

```
条件组 A（AND）
  ├─ 近5日  MA5    持续 >  MA10
  ├─ 近5日  MA10   持续 >  MA20
  └─ 近5日  量比   出现过 >  1.5
```

**超跌反弹**：

```
条件组 A（AND）
  ├─ 近20日 ret_20d  持续 <=  -0.12    ← 跌幅超12%
  ├─ 收盘价  >  MA5                   ← 站上均线
  └─ 近3日   量比   出现过 >  1.2     ← 有资金进场

条件组 B（AND）                          ← 可选
  ├─ RSI(14)  在区间  30 ~ 60
```

**威廉%R 超卖反转**：

```
条件组 A（AND）
  ├─ 近10日 威廉%R  出现过 <=  -90     ← 严重超卖
  └─ 收盘价  >  MA5                   ← 价格企稳
```

---

## 8. 策略评分体系

### 8.1 5 策略独立评分

每只股票每日计算 5 个策略评分，各 0~10 分：

| 策略 | 英文名 | 权重参考 | 说明 |
|------|--------|----------|------|
| 放量突破 | VOL | 0.30 | 量比 + 多头排列 + 3日涨幅 |
| 均线粘合 | MA | 0.15 | 均线粘合度 + 向上发散 |
| 量价背离 | DIVERGE | 0.20 | 底背离 + 反弹强度 |
| 抄底型 | BOTTOM | 0.20 | 下跌深度 + 反弹幅度 |
| 主力建仓 | WHALE | 0.15 | 低位程度 + 异常放量 |

### 8.2 融合分计算

```
融合分 = (VOL_SCORE × w1 + MA_SCORE × w2 + DIVERGE × w3 + BOTTOM × w4 + WHALE × w5)
         / (w1+w2+w3+w4+w5) × 5
范围：0~50，阈值 ≥ 20 触发买入信号
```

### 8.3 评分持久化

- `daily_price` 表新增列：`vol_score`, `ma_score`, `diverge_score`, `bottom_score`, `whale_score`, `fusion_score`
- `quant.py` 的 `sync_job()` 同步完成后自动计算并存储
- `stock_signal` 表记录每日推荐结果

---

## 9. 回测参数说明

| 参数 | 说明 | 建议值 |
|------|------|--------|
| `start_date / end_date` | 回测区间 | 至少 6 个月 |
| `capital` | 初始资金 | 10 万 |
| `max_positions` | 最大同时持股数 | 1~5 |
| `position_pct` | 每仓资金比例 | 0.2（20%） |
| `buy_num_per_day` | 每日最多买几只 | 1~3 |
| `take_profit` | 额外止盈线 | 5~15% |
| `stop_loss` | 额外止损线 | -5%~-10% |
| `hold_days` | 最大持仓天数 | 2~10 |
| `trailing_stop_pct` | 移动止盈回撤 | 0~10% |
| `buy_at_open` | 开盘买入（T+1）vs 收盘买入 | True |
| `sort_by` | 候选股排序字段 | fusion_score |
| `exclude_st/kcb/cyb` | 排除 ST/科创板/创业板 | 视策略而定 |

---

## 10. 扩展指南

### 10.1 添加新指标

在 `indicator_engine.py` 的 `IndicatorRegistry._register_defaults()` 中添加：

```python
c("MY_IND", "我的指标", lambda df: ..., {})
```

### 10.2 添加新条件类型

在 `condition_builder.py` 中扩展 `CondType` 枚举和 `eval_condition` 函数。

### 10.3 添加预设策略模板

在 `backtest_ui.py` 的 `PRESET_OPTIONS` 列表中添加，然后在 `strategy_screen_backtest.py` 中实现对应的筛选函数。

### 10.4 新增回测模式

参考 `CustomStrategyBacktest` 的结构，在 `backtest/` 目录下创建新的回测引擎类。

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.0.1 | 2026-03 | 初始版本，5策略体系 + 预设/代码模式 |
| v0.0.2 | 2026-04-18 | 新增可视化条件构建器 + 指标计算引擎 |
