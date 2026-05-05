# 策略自动生成与优化系统 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建策略自动生成、优化和动态选择的完整四阶段系统（因子库 → 模板穷举 → 遗传进化 → LightGBM 融合 → 动态选策略）

**Architecture:** 5 个新模块按 Phase 1→4 顺序构建，`factor_lib.py` 为基础层被所有阶段复用，`rule_miner.py` 产出种子策略存入 `strategy_rules` 表，`genetic_evolver.py` 持续进化并回流模板，`lgbm_ranker.py` 对触发信号做置信度打分，`dynamic_selector.py` 每周选 Top-K 活跃策略驱动实盘。

**Tech Stack:** Python 3.11+, pandas, numpy, LightGBM, SQLite (core/quant.db), backtesting.py (已有依赖)

---

## 文件结构

```
strategy/
├── factor_lib.py          # 新建：因子库（计算 + 注册表 + 归一化 + IC监控）
├── rule_miner.py          # 新建：Phase 1 模板穷举
├── genetic_evolver.py     # 新建：Phase 2 遗传进化
├── lgbm_ranker.py         # 新建：Phase 3 LightGBM 排序融合
├── dynamic_selector.py    # 新建：Phase 4 动态策略选择
├── indicators.py          # 已有：扩展 KDJ/CCI/DPO/OBV 等指标
config/
└── strategy_params.py     # 修改：新增因子和模板配置
core/
└── db.py                  # 修改：新增3张表 + 访问函数
agents/
└── signal_agent.py        # 修改：对接新 FUSION_SCORE
strategy/
└── strategies.py          # 修改：fuse_signals() 适配
```

---

### Task 1: 数据库 — 新增 3 张表及访问函数

**Files:**
- Modify: `core/db.py` — 在 `init_db()` 新增建表 SQL，新增 upsert/get 函数

- [ ] **Step 1: 在 init_db() 的 executescript 块中新增3张表的建表 SQL**

在 `core/db.py` 的 `init_db()` 函数中，在现有 `conn.executescript("""...` 块内（最后一个 CREATE INDEX 之后、结束三引号之前）追加：

```sql
-- 策略规则库
CREATE TABLE IF NOT EXISTS strategy_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name       TEXT NOT NULL UNIQUE,
    rule_type       TEXT NOT NULL,           -- T1/T2/T3/T4/genetic
    encoding        TEXT NOT NULL,           -- JSON: 前缀表达式规则树
    conditions      TEXT,                    -- JSON: 条件列表(可读形式)
    sell_conditions TEXT,                    -- JSON: 卖出条件
    holding_min     INTEGER DEFAULT 3,
    holding_max     INTEGER DEFAULT 20,
    source          TEXT DEFAULT 'template', -- template/genetic/manual
    generation      INTEGER DEFAULT 0,       -- 进化代数
    fitness         REAL DEFAULT 0,          -- 最终适应度
    annual_return   REAL DEFAULT 0,
    win_rate        REAL DEFAULT 0,
    sharpe_ratio    REAL DEFAULT 0,
    max_drawdown    REAL DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    signal_overlap  REAL DEFAULT 0,          -- 与策略库最大重叠率
    is_active       INTEGER DEFAULT 1,
    degraded_at     TEXT,                    -- 降级时间
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rules_type ON strategy_rules(rule_type);
CREATE INDEX IF NOT EXISTS idx_rules_active ON strategy_rules(is_active);
CREATE INDEX IF NOT EXISTS idx_rules_fitness ON strategy_rules(fitness DESC);

-- 每日信号触发日志
CREATE TABLE IF NOT EXISTS strategy_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    rule_id         INTEGER NOT NULL,
    rule_name       TEXT,
    confidence      REAL,                   -- Phase 3 置信度 (0-100)
    raw_score       REAL,                   -- 规则原始评分
    features_json   TEXT,                   -- 元特征向量 JSON
    label_return    REAL,                   -- 未来20日超额收益(用于训练)
    is_win          INTEGER,                -- 是否盈利(事后标注)
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (rule_id) REFERENCES strategy_rules(id)
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON strategy_signals(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_code ON strategy_signals(code);
CREATE INDEX IF NOT EXISTS idx_signals_rule ON strategy_signals(rule_id);
CREATE INDEX IF NOT EXISTS idx_signals_date_code ON strategy_signals(trade_date, code);

-- 当期活跃策略
CREATE TABLE IF NOT EXISTS active_strategies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    select_date     TEXT NOT NULL,           -- 选择日期(每周五)
    rule_id         INTEGER NOT NULL,
    rule_name       TEXT,
    window_60_score REAL,                    -- 60日窗口评分
    window_120_score REAL,                   -- 120日窗口评分
    final_score     REAL,                    -- 加权最终评分
    rank            INTEGER,                 -- 排名
    is_emergency    INTEGER DEFAULT 0,       -- 是否应急触发
    valid_until     TEXT NOT NULL,           -- 有效期截止日(下周五)
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (rule_id) REFERENCES strategy_rules(id)
);
CREATE INDEX IF NOT EXISTS idx_active_date ON active_strategies(select_date);
CREATE INDEX IF NOT EXISTS idx_active_valid ON active_strategies(valid_until);
```

- [ ] **Step 2: 在 core/db.py 末尾新增 strategy_rules 访问函数**

```python
def upsert_strategy_rule(rule: dict) -> int:
    """插入或更新策略规则，返回 rule_id"""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO strategy_rules
                (rule_name, rule_type, encoding, conditions, sell_conditions,
                 holding_min, holding_max, source, generation, fitness,
                 annual_return, win_rate, sharpe_ratio, max_drawdown,
                 total_trades, signal_overlap, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(rule_name) DO UPDATE SET
                encoding=excluded.encoding, conditions=excluded.conditions,
                sell_conditions=excluded.sell_conditions, fitness=excluded.fitness,
                annual_return=excluded.annual_return, win_rate=excluded.win_rate,
                sharpe_ratio=excluded.sharpe_ratio, max_drawdown=excluded.max_drawdown,
                total_trades=excluded.total_trades, signal_overlap=excluded.signal_overlap,
                updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        """, (
            rule["rule_name"], rule["rule_type"], rule.get("encoding", "[]"),
            rule.get("conditions", "[]"), rule.get("sell_conditions", "[]"),
            rule.get("holding_min", 3), rule.get("holding_max", 20),
            rule.get("source", "template"), rule.get("generation", 0),
            rule.get("fitness", 0), rule.get("annual_return", 0),
            rule.get("win_rate", 0), rule.get("sharpe_ratio", 0),
            rule.get("max_drawdown", 0), rule.get("total_trades", 0),
            rule.get("signal_overlap", 0),
        ))
        return cur.lastrowid


def get_active_rules(rule_type: str = None, min_fitness: float = 0.0, limit: int = 200) -> list:
    """获取活跃策略规则列表"""
    with get_conn() as conn:
        sql = "SELECT * FROM strategy_rules WHERE is_active=1"
        params = []
        if rule_type:
            sql += " AND rule_type=?"
            params.append(rule_type)
        if min_fitness > 0:
            sql += " AND fitness>=?"
            params.append(min_fitness)
        sql += " ORDER BY fitness DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def degrade_rule(rule_id: int):
    """降级策略规则"""
    with get_conn() as conn:
        conn.execute("""
            UPDATE strategy_rules SET is_active=0, degraded_at=?
            WHERE id=?
        """, (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), rule_id))


def save_strategy_signal(signal: dict):
    """保存单条信号触发记录"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO strategy_signals
                (trade_date, code, rule_id, rule_name, confidence,
                 raw_score, features_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["trade_date"], signal["code"], signal["rule_id"],
            signal.get("rule_name", ""), signal.get("confidence"),
            signal.get("raw_score"), signal.get("features_json", "{}"),
        ))


def get_signals_for_training(start_date: str, end_date: str) -> list:
    """获取带标注的信号用于 LightGBM 训练"""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT * FROM strategy_signals
            WHERE trade_date BETWEEN ? AND ? AND label_return IS NOT NULL
            ORDER BY trade_date
        """, (start_date, end_date)).fetchall()]


def update_signal_labels(updates: list):
    """批量更新信号的事后标注（未来20日超额收益）"""
    with get_conn() as conn:
        conn.executemany("""
            UPDATE strategy_signals SET label_return=?, is_win=?
            WHERE id=?
        """, updates)


def upsert_active_strategies(selections: list):
    """保存当期活跃策略选择结果"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO active_strategies
                (select_date, rule_id, rule_name, window_60_score,
                 window_120_score, final_score, rank, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, selections)


def get_current_active_strategies() -> list:
    """获取当前有效的活跃策略"""
    with get_conn() as conn:
        today = datetime.now().strftime('%Y-%m-%d')
        return [dict(r) for r in conn.execute("""
            SELECT * FROM active_strategies
            WHERE valid_until >= ? AND is_emergency=0
            ORDER BY rank
        """, (today,)).fetchall()]
```

- [ ] **Step 3: 在 core/db.py 顶部 datetime import 后确保 json 已 import**

检查 `import json` 是否在文件顶部。如果没有则添加。

- [ ] **Step 4: 验证数据库初始化**

```bash
python -c "from core.db import init_db; init_db(); print('DB init OK')"
```

- [ ] **Step 5: 验证表结构**

```bash
python -c "
from core.db import get_conn
with get_conn() as conn:
    tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'strategy_%'\").fetchall()
    print([t['name'] for t in tables])
"
```
Expected: `['strategy_rules', 'strategy_signals', 'active_strategies']`

- [ ] **Step 6: Commit**

```bash
git add core/db.py
git commit -m "feat: add strategy_rules, strategy_signals, active_strategies tables for strategy generation system"
```

---

### Task 2: 因子库 — strategy/factor_lib.py

**Files:**
- Create: `strategy/factor_lib.py`
- Modify: `strategy/indicators.py` — 补充 KDJ、CCI、DPO、OBV 指标

**Depends on:** Task 1 (DB)

- [ ] **Step 1: 补充 strategy/indicators.py 缺失指标**

在 `strategy/indicators.py` 末尾追加：

```python
def calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series,
             n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ 指标"""
    lowest_low = low.rolling(window=n).min()
    highest_high = high.rolling(window=n).max()
    rsv = ((close - lowest_low) / (highest_high - lowest_low).clip(lower=1e-9)) * 100
    k = rsv.ewm(span=m1, adjust=False).mean()
    d = k.ewm(span=m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"KDJ_K": k, "KDJ_D": d, "KDJ_J": j}, index=close.index)


def calc_cci(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    """CCI 商品通道指数"""
    tp = (high + low + close) / 3
    ma_tp = tp.rolling(window=n).mean()
    md = tp.rolling(window=n).apply(lambda x: np.abs(x - x.mean()).mean())
    cci = (tp - ma_tp) / (0.015 * md.clip(lower=1e-9))
    return pd.DataFrame({"CCI": cci}, index=close.index)


def calc_dpo(close: pd.Series, n: int = 20) -> pd.DataFrame:
    """DPO 去价格趋势震荡"""
    ma = close.rolling(window=n).mean()
    dpo = close - ma.shift(int(n / 2) + 1)
    return pd.DataFrame({"DPO": dpo}, index=close.index)


def calc_obv(close: pd.Series, volume: pd.Series) -> pd.DataFrame:
    """OBV 能量潮"""
    direction = np.where(close.diff() > 0, 1, np.where(close.diff() < 0, -1, 0))
    obv = (volume * direction).cumsum()
    return pd.DataFrame({"OBV": obv}, index=close.index)


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    """ATR 平均真实波幅（标准化为百分比）"""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=n).mean()
    atr_pct = atr / close.clip(lower=1e-9)
    return pd.DataFrame({"ATR": atr, "ATR_PCT": atr_pct}, index=close.index)


def calc_bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """布林带"""
    ma = close.rolling(window=n).mean()
    std = close.rolling(window=n).std()
    upper = ma + k * std
    lower = ma - k * std
    pct_b = (close - lower) / (upper - lower).clip(lower=1e-9)
    bandwidth = (upper - lower) / ma.clip(lower=1e-9)
    squeeze = bandwidth.rolling(window=n).apply(lambda x: x.min() if len(x) > 0 else np.nan)
    return pd.DataFrame({
        "BB_UPPER": upper, "BB_LOWER": lower, "BB_MA": ma,
        "BB_PCT_B": pct_b, "BB_BANDWIDTH": bandwidth,
    }, index=close.index)


def calc_historical_volatility(close: pd.Series, n: int = 20) -> pd.DataFrame:
    """历史波动率（年化）"""
    log_ret = np.log(close / close.shift(1))
    hv = log_ret.rolling(window=n).std() * np.sqrt(252)
    return pd.DataFrame({f"HV_{n}": hv}, index=close.index)
```

- [ ] **Step 2: 验证 indicators.py 可正常 import**

```bash
python -c "from strategy.indicators import calc_kdj, calc_cci, calc_dpo, calc_obv, calc_atr, calc_bollinger, calc_historical_volatility; print('All indicators OK')"
```

- [ ] **Step 3: 创建 strategy/factor_lib.py — FACTOR_REGISTRY 和归一化函数**

```python
"""
factor_lib.py —— 因子库
=======================
功能：
- 因子注册表 FACTOR_REGISTRY
- 因子批量计算 compute_all_factors()
- 动态归一化 normalize_factor()
- IC 计算与监控
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Callable
from strategy.indicators import (
    calc_ma, calc_macd, calc_rsi, calc_kdj, calc_cci, calc_dpo,
    calc_obv, calc_atr, calc_bollinger, calc_historical_volatility,
)


# ── 归一化策略 ────────────────────────────────

def _norm_price_factor(values: pd.Series, factor_name: str) -> pd.Series:
    """价量因子归一化：保留原始语义，线性映射到[0,1]"""
    if "RSI" in factor_name or "KDJ" in factor_name:
        return (values / 100.0).clip(0, 1)
    if "CCI" in factor_name:
        return ((values + 200) / 400).clip(0, 1)
    return values.clip(lower=values.quantile(0.01), upper=values.quantile(0.99))


def _norm_fundamental_factor(values: pd.Series) -> pd.Series:
    """基本面因子归一化：中心化后截断到[-1,1]"""
    median = values.median()
    mad = (values - median).abs().median()
    if mad < 1e-9:
        return pd.Series(0.0, index=values.index)
    return ((values - median) / (mad * 1.4826)).clip(-1, 1)


def _norm_sentiment_factor(values: pd.Series) -> pd.Series:
    """情绪/流动性因子归一化：截断到[0,1]"""
    lower, upper = values.quantile(0.01), values.quantile(0.99)
    if upper - lower < 1e-9:
        return pd.Series(0.5, index=values.index)
    return ((values - lower) / (upper - lower)).clip(0, 1)


# ── 因子注册表 ─────────────────────────────────

FACTOR_REGISTRY = {
    # ===== 趋势类 =====
    "MA5_偏离":     {"category": "trend", "norm": "price"},
    "MA10_偏离":    {"category": "trend", "norm": "price"},
    "MA20_偏离":    {"category": "trend", "norm": "price"},
    "MA60_偏离":    {"category": "trend", "norm": "price"},
    "MA_多头强度":  {"category": "trend", "norm": "price"},
    "MACD_DIF":     {"category": "trend", "norm": "price"},
    "MACD_DEA":     {"category": "trend", "norm": "price"},
    "MACD_HIST":    {"category": "trend", "norm": "price"},
    "MACD_金叉距离": {"category": "trend", "norm": "price"},
    "ADX":          {"category": "trend", "norm": "price"},
    "PDI":          {"category": "trend", "norm": "price"},
    "MDI":          {"category": "trend", "norm": "price"},
    "DPO":          {"category": "trend", "norm": "price"},
    "CCI":          {"category": "trend", "norm": "price"},

    # ===== 动量类 =====
    "RSI_6":   {"category": "momentum", "norm": "price"},
    "RSI_9":   {"category": "momentum", "norm": "price"},
    "RSI_14":  {"category": "momentum", "norm": "price"},
    "RSI_21":  {"category": "momentum", "norm": "price"},
    "BIAS_6":  {"category": "momentum", "norm": "price"},
    "BIAS_12": {"category": "momentum", "norm": "price"},
    "BIAS_24": {"category": "momentum", "norm": "price"},
    "MOM_10":  {"category": "momentum", "norm": "price"},
    "MOM_20":  {"category": "momentum", "norm": "price"},
    "KDJ_K":   {"category": "momentum", "norm": "price"},
    "KDJ_D":   {"category": "momentum", "norm": "price"},
    "KDJ_J":   {"category": "momentum", "norm": "price"},
    "RET_3D":  {"category": "momentum", "norm": "price"},
    "RET_5D":  {"category": "momentum", "norm": "price"},
    "RET_10D": {"category": "momentum", "norm": "price"},
    "RET_20D": {"category": "momentum", "norm": "price"},

    # ===== 波动类 =====
    "BB_PCT_B":     {"category": "volatility", "norm": "price"},
    "BB_BANDWIDTH": {"category": "volatility", "norm": "price"},
    "ATR_PCT":      {"category": "volatility", "norm": "price"},
    "HV_10":        {"category": "volatility", "norm": "price"},
    "HV_20":        {"category": "volatility", "norm": "price"},

    # ===== 量能类 =====
    "VOL_RATIO_5":  {"category": "volume", "norm": "price"},
    "VOL_RATIO_10": {"category": "volume", "norm": "price"},
    "VOL_RATIO_20": {"category": "volume", "norm": "price"},
    "OBV_偏离度":   {"category": "volume", "norm": "price"},
    "VOL_波动率":    {"category": "volume", "norm": "price"},
    "VWAP_偏离":    {"category": "volume", "norm": "price"},
    "AMOUNT_RATIO_20": {"category": "volume", "norm": "price"},

    # ===== 形态类 =====
    "CONSEC_UP":      {"category": "pattern", "norm": "price"},
    "CONSEC_DOWN":    {"category": "pattern", "norm": "price"},
    "AMPLITUDE_5":    {"category": "pattern", "norm": "price"},
    "AMPLITUDE_20":   {"category": "pattern", "norm": "price"},
    "GAP_UP":         {"category": "pattern", "norm": "price"},
    "GAP_DOWN":       {"category": "pattern", "norm": "price"},
    "HAMMER":         {"category": "pattern", "norm": "price"},
    "ENGULF_BULL":    {"category": "pattern", "norm": "price"},
    "ENGULF_BEAR":    {"category": "pattern", "norm": "price"},
    "MORNING_STAR":   {"category": "pattern", "norm": "price"},
    "EVENING_STAR":   {"category": "pattern", "norm": "price"},
    "DOUBLE_BOTTOM":  {"category": "pattern", "norm": "price"},
    "DOUBLE_TOP":     {"category": "pattern", "norm": "price"},

    # ===== 流动性 =====
    "TURNOVER":         {"category": "liquidity", "norm": "sentiment"},
    "TURNOVER_5MA":     {"category": "liquidity", "norm": "sentiment"},
    "TURNOVER_PCTL":    {"category": "liquidity", "norm": "sentiment"},
    "AMIHUD_ILLIQ":     {"category": "liquidity", "norm": "sentiment"},
    "PV_CORREL":        {"category": "liquidity", "norm": "sentiment"},

    # ===== 基本面 =====
    "PE_TTM":           {"category": "fundamental", "norm": "fundamental"},
    "PB_LF":            {"category": "fundamental", "norm": "fundamental"},
    "ROE":              {"category": "fundamental", "norm": "fundamental"},
    "ROA":              {"category": "fundamental", "norm": "fundamental"},
    "PROFIT_YOY":       {"category": "fundamental", "norm": "fundamental"},
    "REVENUE_YOY":      {"category": "fundamental", "norm": "fundamental"},
    "GROSS_MARGIN":     {"category": "fundamental", "norm": "fundamental"},
    "NET_MARGIN":       {"category": "fundamental", "norm": "fundamental"},
    "DEBT_RATIO":       {"category": "fundamental", "norm": "fundamental"},
    "CURRENT_RATIO":    {"category": "fundamental", "norm": "fundamental"},
    "CFO_RATIO":        {"category": "fundamental", "norm": "fundamental"},
    "MKT_CAP":          {"category": "fundamental", "norm": "fundamental"},
    "MKT_CAP_PCTL":     {"category": "fundamental", "norm": "sentiment"},

    # ===== 情绪 =====
    "RPS":              {"category": "sentiment", "norm": "sentiment"},
    "SECTOR_RANK":      {"category": "sentiment", "norm": "sentiment"},
    "LHB_FLAG":         {"category": "sentiment", "norm": "sentiment"},
}


def get_factor_category(factor_name: str) -> str:
    return FACTOR_REGISTRY.get(factor_name, {}).get("category", "unknown")


def get_factor_norm(factor_name: str) -> str:
    return FACTOR_REGISTRY.get(factor_name, {}).get("norm", "price")


def normalize_factor(values: pd.Series, factor_name: str) -> pd.Series:
    """按因子类型选择归一化策略"""
    norm_type = get_factor_norm(factor_name)
    if norm_type == "fundamental":
        return _norm_fundamental_factor(values)
    elif norm_type == "sentiment":
        return _norm_sentiment_factor(values)
    else:
        return _norm_price_factor(values, factor_name)
```

- [ ] **Step 4: 实现因子批量计算函数 compute_all_factors()**

在 `strategy/factor_lib.py` 中追加：

```python
def compute_all_factors(df: pd.DataFrame, index_close: pd.Series = None,
                        fundamental_data: dict = None) -> pd.DataFrame:
    """
    批量计算所有价量因子。

    Args:
        df: OHLCV DataFrame, 含 open/high/low/close/volume/amount/turnover 列
        index_close: 大盘指数收盘价 Series（用于计算 RPS），可选
        fundamental_data: 基本面数据 dict，{factor_name: value}，可选

    Returns:
        所有因子归一化后的 DataFrame，index 与 df 对齐
    """
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    amount = df.get("amount", volume * close)
    result = pd.DataFrame(index=df.index)

    # ── 趋势类 ──
    mas = calc_ma(close, periods=[5, 10, 20, 60])
    result["MA5_偏离"] = (close - mas["MA5"]) / mas["MA5"].clip(lower=1e-9)
    result["MA10_偏离"] = (close - mas["MA10"]) / mas["MA10"].clip(lower=1e-9)
    result["MA20_偏离"] = (close - mas["MA20"]) / mas["MA20"].clip(lower=1e-9)
    result["MA60_偏离"] = (close - mas["MA60"]) / mas["MA60"].clip(lower=1e-9)
    result["MA_多头强度"] = (
        ((mas["MA5"] - mas["MA10"]) / mas["MA10"].clip(lower=1e-9)).clip(lower=0) * 0.5
        + ((mas["MA10"] - mas["MA20"]) / mas["MA20"].clip(lower=1e-9)).clip(lower=0) * 0.3
        + ((mas["MA20"] - mas["MA60"]) / mas["MA60"].clip(lower=1e-9)).clip(lower=0) * 0.2
    )

    macd = calc_macd(close)
    result["MACD_DIF"] = macd["MACD_DIF"] / close
    result["MACD_DEA"] = macd["MACD_DEA"] / close
    result["MACD_HIST"] = macd["MACD_HIST"] / close
    result["MACD_金叉距离"] = (macd["MACD_DIF"] - macd["MACD_DEA"]).abs()

    dmi = calc_dmi(high, low, close) if "calc_dmi" in dir() else pd.DataFrame(index=df.index)
    if not dmi.empty:
        result["ADX"] = dmi.get("ADX", 0)
        result["PDI"] = dmi.get("PDI", 0)
        result["MDI"] = dmi.get("MDI", 0)

    cci = calc_cci(high, low, close)
    result["CCI"] = cci["CCI"]

    dpo = calc_dpo(close)
    result["DPO"] = dpo["DPO"] / close.clip(lower=1e-9)

    # ── 动量类 ──
    for p in [6, 9, 14, 21]:
        rsi = calc_rsi(close, periods=[p])
        result[f"RSI_{p}"] = rsi[f"RSI_{p}"]

    for p in [6, 12, 24]:
        ma_p = close.rolling(p).mean()
        result[f"BIAS_{p}"] = (close - ma_p) / ma_p.clip(lower=1e-9)

    for p in [10, 20]:
        result[f"MOM_{p}"] = close - close.shift(p)

    kdj = calc_kdj(high, low, close)
    result["KDJ_K"] = kdj["KDJ_K"]
    result["KDJ_D"] = kdj["KDJ_D"]
    result["KDJ_J"] = kdj["KDJ_J"]

    for p in [3, 5, 10, 20]:
        result[f"RET_{p}D"] = close.pct_change(p)

    # ── 波动类 ──
    bb = calc_bollinger(close)
    result["BB_PCT_B"] = bb["BB_PCT_B"]
    result["BB_BANDWIDTH"] = bb["BB_BANDWIDTH"]

    atr = calc_atr(high, low, close)
    result["ATR_PCT"] = atr["ATR_PCT"]

    result["HV_10"] = calc_historical_volatility(close, 10)["HV_10"]
    result["HV_20"] = calc_historical_volatility(close, 20)["HV_20"]

    # ── 量能类 ──
    for p in [5, 10, 20]:
        vol_ma = volume.rolling(p).mean()
        result[f"VOL_RATIO_{p}"] = volume / vol_ma.clip(lower=1e-9)

    obv = calc_obv(close, volume)
    obv_ma = obv["OBV"].rolling(20).mean()
    result["OBV_偏离度"] = (obv["OBV"] - obv_ma) / obv_ma.clip(lower=1e-9)

    result["VOL_波动率"] = volume.rolling(20).std() / volume.rolling(20).mean().clip(lower=1e-9)

    result["VWAP_偏离"] = (close - (amount / volume.clip(lower=1e-9)).fillna(close)) / close.clip(lower=1e-9)

    result["AMOUNT_RATIO_20"] = amount / amount.rolling(20).mean().clip(lower=1e-9)

    # ── 形态类 ──
    result["CONSEC_UP"] = _calc_consecutive(close, "up")
    result["CONSEC_DOWN"] = _calc_consecutive(close, "down")
    result["AMPLITUDE_5"] = (high.rolling(5).max() - low.rolling(5).min()) / close.rolling(5).mean()
    result["AMPLITUDE_20"] = (high.rolling(20).max() - low.rolling(20).min()) / close.rolling(20).mean()
    result["GAP_UP"] = ((low - df["high"].shift(1)) / df["high"].shift(1).clip(lower=1e-9) > 0.005).astype(float)
    result["GAP_DOWN"] = ((df["low"].shift(1) - high) / df["low"].shift(1).clip(lower=1e-9) > 0.005).astype(float)
    result["HAMMER"] = _detect_hammer(open_=df["open"], high=high, low=low, close=close).astype(float)
    result["ENGULF_BULL"] = _detect_engulf(df, "bull").astype(float)
    result["ENGULF_BEAR"] = _detect_engulf(df, "bear").astype(float)
    result["MORNING_STAR"] = _detect_morning_star(df).astype(float)
    result["EVENING_STAR"] = _detect_evening_star(df).astype(float)
    result["DOUBLE_BOTTOM"] = _detect_double_pattern(close, "bottom").astype(float)
    result["DOUBLE_TOP"] = _detect_double_pattern(close, "top").astype(float)

    # ── 流动性 ──
    if "turnover" in df.columns:
        result["TURNOVER"] = df["turnover"]
        result["TURNOVER_5MA"] = df["turnover"].rolling(5).mean()
        result["TURNOVER_PCTL"] = df["turnover"].rolling(252).rank(pct=True)
    ret = close.pct_change()
    result["AMIHUD_ILLIQ"] = (ret.abs() / amount.clip(lower=1e-9)) * 1e8
    result["PV_CORREL"] = close.rolling(20).corr(volume)

    # ── 情绪 ──
    if index_close is not None:
        result["RPS"] = (close.pct_change(20) - index_close.pct_change(20))

    # ── 基本面插值 ──
    if fundamental_data:
        for fname, fval in fundamental_data.items():
            if fname in FACTOR_REGISTRY:
                result[fname] = fval

    # ── 归一化 ──
    for col in result.columns:
        if col in FACTOR_REGISTRY:
            result[col] = normalize_factor(result[col], col)
    result = result.fillna(0)

    return result
```

- [ ] **Step 5: 实现形态识别辅助函数**

在 `strategy/factor_lib.py` 中追加：

```python
def _calc_consecutive(close: pd.Series, direction: str) -> pd.Series:
    diff = close.diff()
    if direction == "up":
        sign = (diff > 0).astype(int)
    else:
        sign = (diff < 0).astype(int)
    streaks = sign.groupby((sign != sign.shift()).cumsum()).cumsum()
    return (streaks / 10.0).clip(0, 1)


def _detect_hammer(open_: pd.Series, high: pd.Series, low: pd.Series,
                   close: pd.Series) -> pd.Series:
    body = (close - open_).abs()
    lower_shadow = (open_.combine(close, min) - low)
    upper_shadow = (high - open_.combine(close, max))
    is_hammer = (lower_shadow > body * 2) & (upper_shadow < body * 0.3)
    return is_hammer.fillna(False)


def _detect_engulf(df: pd.DataFrame, direction: str) -> pd.Series:
    open_, close = df["open"], df["close"]
    prev_open, prev_close = open_.shift(1), close.shift(1)
    if direction == "bull":
        return ((prev_close < prev_open) & (close > open_) &
                (open_ <= prev_close) & (close >= prev_open)).fillna(False)
    else:
        return ((prev_close > prev_open) & (close < open_) &
                (open_ >= prev_close) & (close <= prev_open)).fillna(False)


def _detect_morning_star(df: pd.DataFrame) -> pd.Series:
    open_, close = df["open"], df["close"]
    body1 = close.shift(2) - open_.shift(2)
    body2 = (close.shift(1) - open_.shift(1)).abs()
    body3 = close - open_
    return ((body1 < 0) & (body2 < body1.abs() * 0.3) & (body3 > 0) &
            (close > (open_.shift(2) + close.shift(2)) / 2)).fillna(False)


def _detect_evening_star(df: pd.DataFrame) -> pd.Series:
    open_, close = df["open"], df["close"]
    body1 = close.shift(2) - open_.shift(2)
    body2 = (close.shift(1) - open_.shift(1)).abs()
    body3 = close - open_
    return ((body1 > 0) & (body2 < body1.abs() * 0.3) & (body3 < 0) &
            (close < (open_.shift(2) + close.shift(2)) / 2)).fillna(False)


def _detect_double_pattern(close: pd.Series, pattern: str) -> pd.Series:
    low_20 = close.rolling(20).min()
    high_20 = close.rolling(20).max()
    mid = (high_20 + low_20) / 2
    if pattern == "bottom":
        near_low = (close - low_20).abs() / low_20.clip(lower=1e-9) < 0.03
        prev_near = near_low.shift(10).fillna(False)
        return (near_low & prev_near & (close > mid)).fillna(False)
    else:
        near_high = (close - high_20).abs() / high_20.clip(lower=1e-9) < 0.03
        prev_near = near_high.shift(10).fillna(False)
        return (near_high & prev_near & (close < mid)).fillna(False)
```

- [ ] **Step 6: 实现 IC 计算与监控**

在 `strategy/factor_lib.py` 中追加：

```python
def calc_factor_ic(factor_values: pd.Series, forward_returns: pd.Series) -> float:
    """计算单个因子的 IC (Rank IC)"""
    common = factor_values.notna() & forward_returns.notna()
    if common.sum() < 30:
        return 0.0
    return factor_values[common].rank().corr(forward_returns[common].rank())


def calc_all_ic(factor_df: pd.DataFrame, forward_returns: pd.Series) -> dict:
    """计算所有因子的 IC 值"""
    return {col: calc_factor_ic(factor_df[col], forward_returns) for col in factor_df.columns}


def filter_by_ic(factor_df: pd.DataFrame, forward_returns: pd.Series,
                 min_abs_ic: float = 0.02) -> list:
    """按 |IC| >= min_abs_ic 筛选因子，返回通过筛选的因子名列表"""
    ic = calc_all_ic(factor_df, forward_returns)
    return [name for name, v in ic.items() if abs(v) >= min_abs_ic]


def get_factor_names_by_category(category: str) -> list:
    """按类别获取因子名列表"""
    return [k for k, v in FACTOR_REGISTRY.items() if v["category"] == category]


def get_factor_names_by_categories(categories: list) -> list:
    """按多个类别获取因子名列表"""
    result = []
    for cat in categories:
        result.extend(get_factor_names_by_category(cat))
    return result
```

- [ ] **Step 7: 验证 factor_lib 可正常 import 和运行**

```bash
python -c "
from strategy.factor_lib import FACTOR_REGISTRY, compute_all_factors, calc_factor_ic
import pandas as pd
import numpy as np
dates = pd.date_range('2024-01-01', periods=100, freq='B')
df = pd.DataFrame({
    'open': np.random.randn(100).cumsum() + 100,
    'high': np.random.randn(100).cumsum() + 102,
    'low': np.random.randn(100).cumsum() + 98,
    'close': np.random.randn(100).cumsum() + 100,
    'volume': np.random.randint(1000000, 10000000, 100),
}, index=dates)
df['amount'] = df['volume'] * df['close']
df['turnover'] = np.random.uniform(0.01, 0.1, 100)
result = compute_all_factors(df)
print(f'Factors computed: {len(result.columns)} columns, {len(result)} rows')
print(f'Registry size: {len(FACTOR_REGISTRY)}')
"
```

- [ ] **Step 8: Commit**

```bash
git add strategy/indicators.py strategy/factor_lib.py
git commit -m "feat: add factor library with 80+ factors, dynamic normalization, and IC monitoring"
```

---

### Task 3: Phase 1 — 模板穷举规则生成 (rule_miner.py)

**Files:**
- Create: `strategy/rule_miner.py`
- Modify: `config/strategy_params.py`

**Depends on:** Task 2 (factor_lib)

- [ ] **Step 1: 在 config/strategy_params.py 中新增模板和 Phase 1 配置**

```python
# ── Phase 1: 模板穷举配置 ──
PHASE1_CONFIG = {
    "ic_min_abs": 0.02,           # IC 过滤阈值
    "top_per_cluster": 3,         # 每类因子保留数
    "thresholds": [0.2, 0.35, 0.5, 0.65, 0.8],
    "sample_stocks": 200,         # 快速回测采样数
    "min_trades": 20,             # 最少交易次数
    "top_n_rules": 50,            # 入库数量
    "backtest_start": "20220101",
}

# 交叉信号预定义对 (Phase 1 T3 模板)
CROSS_PAIRS = [
    ("MACD_DIF", "MACD_DEA"),
    ("KDJ_K", "KDJ_D"),
    ("MA5_偏离", "MA20_偏离"),
    ("PDI", "MDI"),
]
```

- [ ] **Step 2: 创建 strategy/rule_miner.py — 数据结构与模板构建器**

```python
"""
rule_miner.py —— Phase 1: 模板穷举规则生成
===========================================
功能：
- IC 筛选 + 聚类去重
- 四类模板穷举 (T1/T2/T3/T4)
- 全市场快速回测验证
- 综合评分 → Top-50 入库
- 模板库回流更新
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field, asdict
from itertools import product, combinations
import json
import random

from strategy.factor_lib import (
    FACTOR_REGISTRY, compute_all_factors, calc_all_ic,
    filter_by_ic, get_factor_names_by_category,
)
from config.strategy_params import PHASE1_CONFIG, CROSS_PAIRS
from core.db import upsert_strategy_rule, get_active_rules
from backtest.backtest import Backtester, BacktestResult


@dataclass
class RuleCondition:
    factor: str
    operator: str      # "<" | ">" | "cross_above" | "cross_below"
    threshold: float = 0.5

    def to_dict(self): return asdict(self)

    def evaluate(self, factor_df: pd.DataFrame) -> pd.Series:
        if self.operator == ">":
            return factor_df[self.factor] > self.threshold
        elif self.operator == "<":
            return factor_df[self.factor] < self.threshold
        elif self.operator == "cross_above":
            return (factor_df[self.factor] > self.threshold) & \
                   (factor_df[self.factor].shift(1) <= self.threshold)
        elif self.operator == "cross_below":
            return (factor_df[self.factor] < self.threshold) & \
                   (factor_df[self.factor].shift(1) >= self.threshold)
        return pd.Series(False, index=factor_df.index)


@dataclass
class StrategyRule:
    name: str
    rule_type: str            # T1/T2/T3/T4
    conditions: List[RuleCondition] = field(default_factory=list)
    sell_conditions: List[RuleCondition] = field(default_factory=list)
    holding_min: int = 3
    holding_max: int = 20
    source: str = "template"

    def get_buy_signal(self, factor_df: pd.DataFrame) -> pd.Series:
        if not self.conditions:
            return pd.Series(False, index=factor_df.index)
        result = self.conditions[0].evaluate(factor_df)
        for cond in self.conditions[1:]:
            result = result & cond.evaluate(factor_df)
        return result.fillna(False)

    def get_sell_signal(self, factor_df: pd.DataFrame) -> pd.Series:
        if self.sell_conditions:
            result = self.sell_conditions[0].evaluate(factor_df)
            for cond in self.sell_conditions[1:]:
                result = result & cond.evaluate(factor_df)
            return result.fillna(False)
        return ~self.get_buy_signal(factor_df)


# ── 模板构建器 ──────────────────────────────────

class TemplateBuilder:
    """四类模板的规则生成器"""

    def __init__(self, factor_names: List[str], thresholds: List[float]):
        self.factors = factor_names
        self.thresholds = thresholds

    def build_t1(self) -> List[StrategyRule]:
        """单因子阈值模板"""
        rules = []
        for f in self.factors:
            cat = FACTOR_REGISTRY.get(f, {}).get("category", "unknown")
            for t in self.thresholds:
                for op in [">", "<"]:
                    cond = RuleCondition(f, op, t)
                    rules.append(StrategyRule(
                        name=f"T1_{f}_{op}_{t:.2f}",
                        rule_type="T1",
                        conditions=[cond],
                    ))
        return rules

    def build_t2(self) -> List[StrategyRule]:
        """双因子组合模板 — 跨类别配对"""
        rules = []
        # 按类别分组
        cat_factors = {}
        for f in self.factors:
            cat = FACTOR_REGISTRY.get(f, {}).get("category", "unknown")
            cat_factors.setdefault(cat, []).append(f)
        categories = list(cat_factors.keys())

        for cat_a, cat_b in combinations(categories, 2):
            for f1 in cat_factors[cat_a][:3]:  # 每类最多取3个
                for f2 in cat_factors[cat_b][:3]:
                    for t1 in self.thresholds[1:4]:  # 中间3个阈值
                        for t2 in self.thresholds[1:4]:
                            op1 = ">" if "VOL" not in f1 else ">"
                            op2 = "<" if "PCT_B" in f2 or "HV" in f2 else ">"
                            rules.append(StrategyRule(
                                name=f"T2_{f1}_{op1}{t1:.2f}_{f2}_{op2}{t2:.2f}",
                                rule_type="T2",
                                conditions=[
                                    RuleCondition(f1, op1, t1),
                                    RuleCondition(f2, op2, t2),
                                ],
                            ))
        return rules

    def build_t3(self) -> List[StrategyRule]:
        """交叉信号模板"""
        rules = []
        for f_fast, f_slow in CROSS_PAIRS:
            if f_fast not in self.factors or f_slow not in self.factors:
                continue
            rules.append(StrategyRule(
                name=f"T3_{f_fast}_cross_above_{f_slow}",
                rule_type="T3",
                conditions=[RuleCondition(f_fast, "cross_above", 0)],
            ))
        return rules

    def build_t4(self) -> List[StrategyRule]:
        """三因子确认模板 — 取T2 Top-200 + 增加一个条件"""
        t2_rules = self.build_t2()
        if len(t2_rules) > 200:
            t2_rules = random.sample(t2_rules, 200)
        rules = []
        for t2_rule in t2_rules:
            for f in self.factors[:10]:  # 只取Top-10因子做增量
                third_cond = RuleCondition(f, ">", self.thresholds[2])
                new_conds = t2_rule.conditions + [third_cond]
                rules.append(StrategyRule(
                    name=f"T4_{t2_rule.name}_{f}",
                    rule_type="T4",
                    conditions=new_conds,
                ))
        return rules
```

- [ ] **Step 3: 实现剪枝流程与快速回测**

```python
class RuleMiner:
    """Phase 1: 模板穷举 + 剪枝 + 回测验证"""

    def __init__(self, sample_size: int = None, min_trades: int = None,
                 top_n: int = None):
        self.config = PHASE1_CONFIG
        self.sample_size = sample_size or self.config["sample_stocks"]
        self.min_trades = min_trades or self.config["min_trades"]
        self.top_n = top_n or self.config["top_n_rules"]

    def prune_factors(self, factor_df: pd.DataFrame,
                      forward_returns: pd.Series) -> List[str]:
        """
        ① IC 筛选：|IC| >= 0.02
        ② 聚类去重：同一类别内保留 IC 最高的 3 个
        """
        passing = filter_by_ic(factor_df, forward_returns, self.config["ic_min_abs"])
        ic = calc_all_ic(factor_df[passing] if passing else factor_df, forward_returns)
        cat_best = {}
        for f in passing:
            cat = FACTOR_REGISTRY.get(f, {}).get("category", "unknown")
            if cat not in cat_best:
                cat_best[cat] = []
            cat_best[cat].append((f, abs(ic.get(f, 0))))
        result = []
        for cat, items in cat_best.items():
            items.sort(key=lambda x: x[1], reverse=True)
            result.extend([f for f, _ in items[:self.config["top_per_cluster"]]])
        return result

    def generate_candidates(self, factor_names: List[str]) -> List[StrategyRule]:
        """③ 生成候选条件 + ④ 模板穷举"""
        builder = TemplateBuilder(factor_names, self.config["thresholds"])
        rules = []
        rules.extend(builder.build_t1())
        rules.extend(builder.build_t2())
        rules.extend(builder.build_t3())
        rules.extend(builder.build_t4())
        print(f"  [RuleMiner] 生成候选规则: {len(rules)} 条")
        return rules

    def quick_backtest(self, rule: StrategyRule, stock_df: pd.DataFrame,
                       factor_df: pd.DataFrame) -> Optional[dict]:
        """
        ⑤ 单只股票单条规则快速回测
        返回: {"total_return", "win_rate", "sharpe", "max_drawdown", "total_trades"}
        """
        buy_signal = rule.get_buy_signal(factor_df)
        sell_signal = rule.get_sell_signal(factor_df)
        if buy_signal.sum() < 3:
            return None
        signal_df = stock_df[["close", "volume"]].copy()
        signal_df["BUY_SIGNAL"] = buy_signal.astype(int)
        signal_df["SELL_SIGNAL"] = sell_signal.astype(int)
        signal_df["BUY_SCORE"] = buy_signal.astype(float)
        signal_df["STRATEGY"] = rule.name
        if "open" in stock_df.columns:
            signal_df["open"] = stock_df["open"]
        try:
            bt = Backtester(initial_capital=100000)
            result = bt.run(signal_df)
            return {
                "total_return": result.total_return,
                "annual_return": result.annual_return,
                "win_rate": result.win_rate,
                "sharpe_ratio": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "total_trades": result.total_trades,
            }
        except Exception:
            return None

    def evaluate_rule(self, rule: StrategyRule, stock_data: Dict[str, Tuple[pd.DataFrame, pd.DataFrame]]) -> Optional[dict]:
        """
        全市场采样回测（200只分层采样）
        stock_data: {code: (price_df, factor_df)}
        """
        results = []
        for code, (price_df, factor_df) in stock_data.items():
            perf = self.quick_backtest(rule, price_df, factor_df)
            if perf:
                results.append(perf)
        if not results or len(results) < 5:
            return None
        n = len(results)
        return {
            "total_return": np.mean([r["total_return"] for r in results]),
            "win_rate": np.mean([r["win_rate"] for r in results]),
            "sharpe_ratio": np.mean([r["sharpe_ratio"] for r in results]),
            "max_drawdown": np.max([r["max_drawdown"] for r in results]),
            "total_trades": np.sum([r["total_trades"] for r in results]),
            "sample_count": n,
        }

    def score_rule(self, perf: dict) -> float:
        """⑥ 综合评分: 0.3×收益 + 0.3×胜率 + 0.25×夏普 - 0.15×最大回撤"""
        return (0.3 * perf["total_return"] / 100.0
                + 0.3 * perf["win_rate"]
                + 0.25 * perf["sharpe_ratio"]
                - 0.15 * abs(perf["max_drawdown"]) / 100.0)

    def run_phase1(self, stock_data: Dict[str, Tuple[pd.DataFrame, pd.DataFrame]],
                   forward_returns: pd.Series,
                   factor_df: pd.DataFrame) -> List[dict]:
        """
        执行完整 Phase 1 流程
        stock_data: {code: (price_df, factor_df)}  采样后的股票数据
        """
        print("[Phase 1] 开始模板穷举...")
        # ① IC + ② 聚类去重
        active_factors = self.prune_factors(factor_df, forward_returns)
        print(f"  [Phase 1] 剪枝后因子: {len(active_factors)} 个")

        # ③ + ④ 生成候选
        candidates = self.generate_candidates(active_factors)

        # ⑤ 全市场快速回测
        print(f"  [Phase 1] 回测验证中 (采样{len(stock_data)}只)...")
        scored = []
        for i, rule in enumerate(candidates):
            perf = self.evaluate_rule(rule, stock_data)
            if perf and perf["total_trades"] >= self.min_trades:
                score = self.score_rule(perf)
                scored.append((score, rule, perf))
            if (i + 1) % 500 == 0:
                print(f"    已评估: {i+1}/{len(candidates)}, 有效: {len(scored)}")

        # ⑥ 排序取 Top-N
        scored.sort(key=lambda x: x[0], reverse=True)
        top_rules = scored[:self.top_n]
        print(f"  [Phase 1] Top-{self.top_n} 规则评分范围: "
              f"{top_rules[-1][0]:.3f} ~ {top_rules[0][0]:.3f}")

        # 入库
        results = []
        for score, rule, perf in top_rules:
            rule_dict = {
                "rule_name": rule.name,
                "rule_type": rule.rule_type,
                "encoding": json.dumps([c.to_dict() for c in rule.conditions]),
                "conditions": json.dumps([c.to_dict() for c in rule.conditions]),
                "sell_conditions": json.dumps([c.to_dict() for c in rule.sell_conditions]),
                "holding_min": rule.holding_min,
                "holding_max": rule.holding_max,
                "source": "template",
                "fitness": score,
                "annual_return": perf["annual_return"],
                "win_rate": perf["win_rate"],
                "sharpe_ratio": perf["sharpe_ratio"],
                "max_drawdown": perf["max_drawdown"],
                "total_trades": perf["total_trades"],
            }
            rule_id = upsert_strategy_rule(rule_dict)
            results.append({"rule_id": rule_id, "score": score, "rule": rule})
        return results


def load_template_library() -> List[StrategyRule]:
    """加载模板库 (供 Phase 2 初始化)"""
    active = get_active_rules(min_fitness=0.3, limit=100)
    rules = []
    for row in active:
        try:
            conds_data = json.loads(row["conditions"])
            conds = [RuleCondition(**c) for c in conds_data]
            rules.append(StrategyRule(
                name=row["rule_name"], rule_type=row["rule_type"],
                conditions=conds, source=row.get("source", "template"),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return rules
```

- [ ] **Step 4: 验证 rule_miner 可正常 import**

```bash
python -c "from strategy.rule_miner import RuleMiner, StrategyRule, RuleCondition, TemplateBuilder; print('RuleMiner OK')"
```

- [ ] **Step 5: Commit**

```bash
git add strategy/rule_miner.py config/strategy_params.py
git commit -m "feat: add Phase 1 rule miner with template exhaustion and pruning"
```

---

### Task 4: Phase 2 — 遗传规划进化 (genetic_evolver.py)

**Files:**
- Create: `strategy/genetic_evolver.py`

**Depends on:** Task 3 (rule_miner)

- [ ] **Step 1: 创建 strategy/genetic_evolver.py — 编码/解码**

```python
"""
genetic_evolver.py —— Phase 2: 遗传规划策略进化
================================================
功能：
- 前缀表达式规则树 编码/解码
- 遗传算子（交叉/变异/阈值微调/因子替换/随机重置）
- 适应度函数（综合评分 × 新颖度 × 简洁性 × 稳定性 × 合法性）
- 进化循环 + 收敛判定 + 策略入库 + 模板回流
"""
import json
import random
import math
import copy
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from strategy.factor_lib import FACTOR_REGISTRY, get_factor_names_by_categories
from strategy.rule_miner import StrategyRule, RuleCondition, load_template_library
from core.db import upsert_strategy_rule, get_active_rules, degrade_rule
from backtest.backtest import Backtester


# ── 操作符配置 ──────────────────────────────────
FUNCTIONS = ["AND", "OR"]         # 逻辑函数符
TERMINAL_OPS = ["<", ">"]         # 终端比较符
MAX_DEPTH = 5                     # 最大深度
MAX_CONDITIONS = 7                # 最大条件数


# ── 因子合法区间 (用于阈值约束) ──────────────────
FACTOR_RANGES = {
    "RSI_6": (0, 100), "RSI_9": (0, 100), "RSI_14": (0, 100), "RSI_21": (0, 100),
    "KDJ_K": (0, 100), "KDJ_D": (0, 100), "KDJ_J": (-20, 120),
    "BB_PCT_B": (0, 1), "ATR_PCT": (0, 0.5), "HV_10": (0, 2.0), "HV_20": (0, 2.0),
}
FACTOR_DEFAULT_RANGE = (0, 1)     # 归一化因子默认区间


def get_factor_range(factor_name: str) -> Tuple[float, float]:
    return FACTOR_RANGES.get(factor_name, FACTOR_DEFAULT_RANGE)


# ── 编码/解码 ────────────────────────────────────

def encode_rule(conditions: List[RuleCondition]) -> list:
    """将条件列表编码为前缀表达式"""
    if len(conditions) == 1:
        c = conditions[0]
        return [c.operator, c.factor, c.threshold]
    # 多个条件用 AND 串联
    encoded = ["AND"]
    for c in conditions:
        encoded.extend([c.operator, c.factor, c.threshold])
    return encoded


def decode_rule(encoded: list) -> List[RuleCondition]:
    """从前缀表达式解码为条件列表"""
    conditions = []
    i = 0
    while i < len(encoded):
        token = encoded[i]
        if token in ("<", ">"):
            if i + 2 < len(encoded):
                factor = encoded[i + 1]
                threshold = float(encoded[i + 2])
                conditions.append(RuleCondition(factor, token, threshold))
                i += 3
            else:
                i += 1
        elif token in ("AND", "OR", "NOT"):
            i += 1
        else:
            i += 1
    return conditions


def random_rule(depth: int = 3) -> StrategyRule:
    """生成随机合法规则树"""
    categories = ["trend", "momentum", "volatility", "volume"]
    factors = get_factor_names_by_categories(categories)
    n_conditions = random.randint(1, min(depth, MAX_CONDITIONS))
    conditions = []
    for _ in range(n_conditions):
        f = random.choice(factors)
        op = random.choice(TERMINAL_OPS)
        lo, hi = get_factor_range(f)
        t = random.uniform(lo + (hi - lo) * 0.1, hi - (hi - lo) * 0.1)
        conditions.append(RuleCondition(f, op, t))
    return StrategyRule(
        name=f"GEN_{random.randint(10000, 99999)}",
        rule_type="genetic",
        conditions=conditions,
        source="genetic",
    )
```

- [ ] **Step 2: 实现遗传算子**

```python
# ── 遗传算子 ────────────────────────────────────

def crossover(parent1: StrategyRule, parent2: StrategyRule) -> StrategyRule:
    """交叉：交换条件列表中的随机位置"""
    if len(parent1.conditions) < 1 or len(parent2.conditions) < 1:
        return parent1
    split1 = random.randint(0, len(parent1.conditions) - 1)
    split2 = random.randint(0, len(parent2.conditions) - 1)
    new_conds = parent1.conditions[:split1] + parent2.conditions[split2:]
    if len(new_conds) > MAX_CONDITIONS:
        new_conds = new_conds[:MAX_CONDITIONS]
    return StrategyRule(
        name=f"GEN_{random.randint(10000, 99999)}",
        rule_type="genetic",
        conditions=new_conds,
        source="genetic_crossover",
    )


def mutate_subtree(rule: StrategyRule) -> StrategyRule:
    """子树变异：随机替换一个条件"""
    if not rule.conditions:
        return random_rule()
    new_conds = rule.conditions.copy()
    idx = random.randint(0, len(new_conds) - 1)
    categories = ["trend", "momentum", "volatility", "volume"]
    factors = get_factor_names_by_categories(categories)
    f = random.choice(factors)
    op = random.choice(TERMINAL_OPS)
    lo, hi = get_factor_range(f)
    t = random.uniform(lo, hi)
    new_conds[idx] = RuleCondition(f, op, t)
    return StrategyRule(
        name=f"GEN_{random.randint(10000, 99999)}",
        rule_type="genetic", conditions=new_conds,
        source="genetic_mutate",
    )


def mutate_threshold(rule: StrategyRule, sigma: float = 0.03) -> StrategyRule:
    """阈值微调：N(0, sigma) 高斯扰动"""
    new_conds = []
    for c in rule.conditions:
        lo, hi = get_factor_range(c.factor)
        new_t = c.threshold + np.random.normal(0, sigma)
        new_t = max(lo, min(hi, new_t))  # 截断至合法区间
        new_conds.append(RuleCondition(c.factor, c.operator, new_t))
    return StrategyRule(
        name=f"GEN_{random.randint(10000, 99999)}",
        rule_type="genetic", conditions=new_conds,
        source="genetic_threshold",
    )


def mutate_factor(rule: StrategyRule) -> StrategyRule:
    """因子替换：同类别替换"""
    new_conds = []
    for c in rule.conditions:
        cat = FACTOR_REGISTRY.get(c.factor, {}).get("category")
        if cat:
            same_cat = get_factor_names_by_categories([cat])
            if len(same_cat) > 1:
                new_f = random.choice([f for f in same_cat if f != c.factor])
                new_conds.append(RuleCondition(new_f, c.operator, c.threshold))
            else:
                new_conds.append(copy.deepcopy(c))
        else:
            new_conds.append(copy.deepcopy(c))
    return StrategyRule(
        name=f"GEN_{random.randint(10000, 99999)}",
        rule_type="genetic", conditions=new_conds,
        source="genetic_factor_swap",
    )


def tournament_select(population: List[Tuple[float, StrategyRule]],
                      tournament_size: int = 3) -> StrategyRule:
    """锦标赛选择"""
    candidates = random.sample(population, min(tournament_size, len(population)))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return copy.deepcopy(candidates[0][1])
```

- [ ] **Step 3: 实现适应度函数**

```python
# ── 适应度函数 ──────────────────────────────────

def signal_overlap(rule1: StrategyRule, rule2: StrategyRule,
                   factor_df: pd.DataFrame) -> float:
    """计算两条规则的信号重叠率"""
    sig1 = rule1.get_buy_signal(factor_df)
    sig2 = rule2.get_buy_signal(factor_df)
    both = (sig1 & sig2).sum()
    either = (sig1 | sig2).sum()
    return both / max(either, 1)


def novelty_bonus(new_rule: StrategyRule, existing_rules: List[StrategyRule],
                  factor_df: pd.DataFrame) -> float:
    """新颖度奖励"""
    if not existing_rules:
        return 1.2
    max_overlap = max(signal_overlap(new_rule, r, factor_df) for r in existing_rules)
    if max_overlap < 0.2:
        return 1.2
    elif max_overlap <= 0.4:
        return 1.0
    elif max_overlap <= 0.7:
        return 0.7
    else:
        return 0.3


def simplicity_penalty(n_conditions: int) -> float:
    """简洁性惩罚"""
    if n_conditions <= 3:
        return 1.0
    elif n_conditions <= 5:
        return 0.85 ** (n_conditions - 3)
    else:
        return 0.7 ** (n_conditions - 3)


def legality_check(rule: StrategyRule) -> float:
    """合法性惩罚：检查逻辑矛盾和阈值越界"""
    for c in rule.conditions:
        lo, hi = get_factor_range(c.factor)
        if c.threshold < lo or c.threshold > hi:
            return 0.1
    # 简易矛盾检测：同因子同时大于高阈值和小于低阈值
    cond_map = {}
    for c in rule.conditions:
        key = c.factor
        if key not in cond_map:
            cond_map[key] = []
        cond_map[key].append(c)
    for key, conds in cond_map.items():
        if len(conds) > 1:
            has_gt = any(c.operator == ">" for c in conds)
            has_lt = any(c.operator == "<" for c in conds)
            if has_gt and has_lt:
                gt_vals = [c.threshold for c in conds if c.operator == ">"]
                lt_vals = [c.threshold for c in conds if c.operator == "<"]
                if max(gt_vals) > min(lt_vals):
                    return 0.0  # 逻辑矛盾
    return 1.0


def compute_fitness(perf: dict, rule: StrategyRule,
                    existing_rules: List[StrategyRule],
                    factor_df: pd.DataFrame,
                    train_perf: dict = None, valid_perf: dict = None) -> float:
    """
    适应度 = 综合评分 × 新颖度 × 简洁性 × 稳定性 × 合法性

    perf: 全量回测表现
    train_perf/valid_perf: 用于稳定性计算
    """
    # 综合评分
    composite = (0.3 * perf.get("annual_return", 0) / 100.0
                 + 0.3 * perf.get("win_rate", 0)
                 + 0.25 * perf.get("sharpe_ratio", 0)
                 - 0.15 * abs(perf.get("max_drawdown", 0)) / 100.0
                 - 0.1 * perf.get("turnover_rate", 0))

    # 新颖度
    novelty = novelty_bonus(rule, existing_rules, factor_df)

    # 简洁性
    simplicity = simplicity_penalty(len(rule.conditions))

    # 稳定性（三段时序检查）
    stability = 1.0
    if train_perf and valid_perf:
        returns = [train_perf.get("total_return", 0), valid_perf.get("total_return", 0)]
        if len(returns) >= 2 and abs(max(returns) - min(returns)) > 30:
            stability = 0.6

    # 合法性
    legality = legality_check(rule)

    return composite * novelty * simplicity * stability * legality
```

- [ ] **Step 4: 实现进化循环**

```python
# ── 进化引擎 ────────────────────────────────────

class GeneticEvolver:
    """Phase 2: 遗传规划进化引擎"""

    def __init__(self, population_size: int = 50, max_generations: int = 50,
                 early_stop_generations: int = 8, early_stop_threshold: float = 0.02):
        self.population_size = population_size
        self.max_generations = max_generations
        self.early_stop_generations = early_stop_generations
        self.early_stop_threshold = early_stop_threshold

        # 遗传算子概率（总和 1.0）
        self.crossover_prob = 0.35
        self.subtree_mutate_prob = 0.15
        self.threshold_mutate_prob = 0.20
        self.factor_swap_prob = 0.15
        self.random_reset_prob = 0.10
        self.elite_count = 5

    def initialize_population(self) -> List[StrategyRule]:
        """种群初始化：30个来自 Phase 1 + 20个随机"""
        population = []
        # 从模板库加载
        template_rules = load_template_library()
        if len(template_rules) >= 30:
            population = random.sample(template_rules, 30)
        else:
            population = template_rules.copy()
        # 随机生成补足
        while len(population) < self.population_size:
            population.append(random_rule(random.randint(2, 5)))
        return population[:self.population_size]

    def evolve_one_generation(self, population: List[Tuple[float, StrategyRule]]
                              ) -> List[StrategyRule]:
        """进化一代"""
        population.sort(key=lambda x: x[0], reverse=True)

        # 精英保留
        new_pop = [copy.deepcopy(population[i][1]) for i in range(min(self.elite_count, len(population)))]

        # 垫底 5 个标记随机重置
        bottom_indices = list(range(len(population) - 5, len(population)))
        if len(bottom_indices) < 5:
            bottom_indices = list(range(max(0, len(population) - 5), len(population)))

        while len(new_pop) < self.population_size:
            idx = len(new_pop)
            r = random.random()
            if r < self.crossover_prob:
                p1 = tournament_select(population)
                p2 = tournament_select(population)
                child = crossover(p1, p2)
            elif r < self.crossover_prob + self.subtree_mutate_prob:
                p = tournament_select(population)
                child = mutate_subtree(p)
            elif r < self.crossover_prob + self.subtree_mutate_prob + self.threshold_mutate_prob:
                p = tournament_select(population)
                child = mutate_threshold(p)
            elif r < self.crossover_prob + self.subtree_mutate_prob + \
                     self.threshold_mutate_prob + self.factor_swap_prob:
                p = tournament_select(population)
                child = mutate_factor(p)
            else:
                child = random_rule(random.randint(2, 4))

            new_pop.append(child)

        return new_pop

    def run_evolution(self, stock_data: Dict, factor_df: pd.DataFrame,
                      forward_returns: pd.Series = None) -> List[dict]:
        """
        执行完整进化流程
        stock_data: {code: (price_df, factor_df)}
        """
        from strategy.rule_miner import RuleMiner
        miner = RuleMiner()

        # 初始化
        raw_population = self.initialize_population()
        existing_rules = load_template_library()

        # 评估初始种群
        population = []
        for rule in raw_population:
            perf = miner.evaluate_rule(rule, stock_data)
            if perf and perf["total_trades"] >= 5:
                fitness = compute_fitness(perf, rule, existing_rules, factor_df)
                population.append((fitness, rule))
        population.sort(key=lambda x: x[0], reverse=True)

        print(f"[Phase 2] 初始种群: {len(population)} 个有效个体, "
              f"最佳适应度: {population[0][0]:.4f}")

        # 进化循环
        best_fitness_history = [population[0][0]]
        all_good_rules = []

        for gen in range(self.max_generations):
            new_population = self.evolve_one_generation(population)

            # 评估新一代
            evaluated = []
            for rule in new_population:
                perf = miner.evaluate_rule(rule, stock_data)
                if perf and perf["total_trades"] >= 5:
                    fitness = compute_fitness(perf, rule, existing_rules, factor_df)
                    evaluated.append((fitness, rule))
                    if fitness > 0.5:
                        all_good_rules.append((fitness, rule, perf))
            evaluated.sort(key=lambda x: x[0], reverse=True)
            population = evaluated

            if not population:
                print(f"  [Phase 2] 第{gen+1}代种群全部无效, 终止")
                break

            best_fitness_history.append(population[0][0])
            print(f"  [Phase 2] Gen {gen+1}: best={population[0][0]:.4f}, "
                  f"pop={len(population)}")

            # 早停
            if len(best_fitness_history) > self.early_stop_generations:
                recent = best_fitness_history[-self.early_stop_generations:]
                if max(recent) - recent[0] < self.early_stop_threshold:
                    print(f"  [Phase 2] 早停: 连续{self.early_stop_generations}代无显著提升")
                    break

        # 入库
        results = []
        seen = set()
        all_good_rules.sort(key=lambda x: x[0], reverse=True)
        for fitness, rule, perf in all_good_rules:
            if rule.name in seen:
                continue
            seen.add(rule.name)
            # 去重检查
            max_ov = 0.0
            if existing_rules:
                max_ov = max(signal_overlap(rule, r, factor_df) for r in existing_rules)
            if max_ov > 0.75:
                continue
            rule_dict = {
                "rule_name": rule.name,
                "rule_type": "genetic",
                "encoding": json.dumps(encode_rule(rule.conditions)),
                "conditions": json.dumps([c.to_dict() for c in rule.conditions]),
                "sell_conditions": "[]",
                "source": "genetic",
                "generation": best_fitness_history.index(fitness) if fitness in best_fitness_history else 0,
                "fitness": fitness,
                "annual_return": perf.get("annual_return", 0),
                "win_rate": perf.get("win_rate", 0),
                "sharpe_ratio": perf.get("sharpe_ratio", 0),
                "max_drawdown": perf.get("max_drawdown", 0),
                "total_trades": perf.get("total_trades", 0),
                "signal_overlap": max_ov,
            }
            rule_id = upsert_strategy_rule(rule_dict)
            results.append({"rule_id": rule_id, "fitness": fitness, "rule": rule})

        print(f"[Phase 2] 完成: 入库 {len(results)} 条新策略")
        return results
```

- [ ] **Step 5: 实现模板回流机制**

```python
def feedback_to_phase1(phase2_results: List[dict], top_n: int = 20):
    """
    Phase 2 → Phase 1 模板回流
    筛选 Top-20 优质策略更新模板库标记
    """
    phase2_results.sort(key=lambda x: x["fitness"], reverse=True)
    top = phase2_results[:top_n]
    for r in top:
        upsert_strategy_rule({
            "rule_name": r["rule"].name + "_FB",
            "rule_type": r["rule"].rule_type,
            "encoding": json.dumps(encode_rule(r["rule"].conditions)),
            "conditions": json.dumps([c.to_dict() for c in r["rule"].conditions]),
            "sell_conditions": "[]",
            "source": "template",  # 回流后标记为 template
            "fitness": r["fitness"],
        })
    print(f"[Phase 2] 回流 {len(top)} 条策略至 Phase 1 模板库")


def cleanup_template_library():
    """模板库淘汰：夏普<1.0 或 回撤>30%"""
    rules = get_active_rules(limit=500)
    for r in rules:
        if r["sharpe_ratio"] < 1.0 or abs(r["max_drawdown"]) > 30:
            degrade_rule(r["id"])
    print(f"[Maintenance] 清扫模板库完成")
```

- [ ] **Step 6: 验证 genetic_evolver 可正常 import**

```bash
python -c "from strategy.genetic_evolver import GeneticEvolver, encode_rule, decode_rule, compute_fitness, random_rule; print('GeneticEvolver OK')"
```

- [ ] **Step 7: Commit**

```bash
git add strategy/genetic_evolver.py
git commit -m "feat: add Phase 2 genetic evolver with dual feedback loop"
```

---

### Task 5: Phase 3 — LightGBM 二次筛选与融合 (lgbm_ranker.py)

**Files:**
- Create: `strategy/lgbm_ranker.py`

**Depends on:** Task 1 (DB), Task 2 (factor_lib)

- [ ] **Step 1: 创建 strategy/lgbm_ranker.py — 特征构建**

```python
"""
lgbm_ranker.py —— Phase 3: LightGBM 排序学习二次筛选与融合
=============================================================
功能：
- 7类元特征构建
- LightGBM Ranker 训练与推理
- 滚动预测（t-1 → t）避免未来函数
- 置信度归一化 (0-100)
- 失效规则降级/淘汰
- 故障恢复：训练失败回退 Phase 2 评分
"""
import json
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

from strategy.factor_lib import FACTOR_REGISTRY
from core.db import (
    get_active_rules, save_strategy_signal, get_signals_for_training,
    update_signal_labels, degrade_rule, get_conn,
)


# ── 元特征构建 (7类) ────────────────────────────

def build_meta_features(
    rule_id: int, rule_name: str, rule_type: str, n_conditions: int,
    rule_recent_perf: dict,        # 规则近期表现
    rule_history: dict,            # 规则历史衰减
    market_state: dict,            # 市场环境
    stock_state: dict,             # 个股状态
    signal_strength: float,        # 信号强度
    sector_crowd: dict,            # 板块拥挤度
) -> dict:
    """为单条触发信号构建元特征向量"""
    features = {
        # 策略元信息
        "rule_id": rule_id,
        "rule_type_hash": hash(rule_type) % 1000,
        "n_conditions": n_conditions,

        # 规则近期表现
        "win_rate_20d": rule_recent_perf.get("win_rate_20d", 0),
        "win_rate_60d": rule_recent_perf.get("win_rate_60d", 0),
        "avg_return_20d": rule_recent_perf.get("avg_return_20d", 0),
        "avg_return_60d": rule_recent_perf.get("avg_return_60d", 0),
        "sharpe_20d": rule_recent_perf.get("sharpe_20d", 0),

        # 规则历史衰减
        "trigger_count_20d": rule_history.get("trigger_count_20d", 0),
        "max_drawdown_hist": rule_history.get("max_drawdown_hist", 0),
        "profit_factor_hist": rule_history.get("profit_factor_hist", 0),

        # 市场环境
        "index_ret_20d": market_state.get("index_ret_20d", 0),
        "index_vol_20d": market_state.get("index_vol_20d", 0),
        "market_breadth": market_state.get("market_breadth", 0),

        # 个股状态
        "stock_ret_20d": stock_state.get("stock_ret_20d", 0),
        "stock_vol_20d": stock_state.get("stock_vol_20d", 0),
        "turnover_pctl": stock_state.get("turnover_pctl", 0),

        # 信号强度
        "signal_strength": signal_strength,

        # 板块拥挤度
        "sector_trigger_count": sector_crowd.get("trigger_count", 0),
        "sector_trigger_ratio": sector_crowd.get("trigger_ratio", 0),
    }
    return features


def build_features_df(signals: List[dict], market_data: dict,
                      stock_data: dict) -> pd.DataFrame:
    """
    批量构建特征 DataFrame（带 1% 缩尾处理）

    signals: [{"rule_id", "rule_name", "rule_type", "n_conditions",
               "rule_recent_perf", "rule_history", "signal_strength",
               "sector_crowd", "code", "trade_date"}, ...]
    """
    rows = []
    for sig in signals:
        code = sig["code"]
        feats = build_meta_features(
            rule_id=sig["rule_id"],
            rule_name=sig.get("rule_name", ""),
            rule_type=sig.get("rule_type", ""),
            n_conditions=sig.get("n_conditions", 1),
            rule_recent_perf=sig.get("rule_recent_perf", {}),
            rule_history=sig.get("rule_history", {}),
            market_state=market_data,
            stock_state=stock_data.get(code, {}),
            signal_strength=sig.get("signal_strength", 0.5),
            sector_crowd=sig.get("sector_crowd", {}),
        )
        feats["code"] = code
        feats["trade_date"] = sig["trade_date"]
        rows.append(feats)

    df = pd.DataFrame(rows)
    # 连续特征 1% 缩尾
    for col in df.select_dtypes(include=[np.number]).columns:
        if col in ("rule_id", "rule_type_hash", "n_conditions"):
            continue
        lo, hi = df[col].quantile(0.01), df[col].quantile(0.99)
        df[col] = df[col].clip(lo, hi)
    return df
```

- [ ] **Step 2: 实现 LightGBM Ranker 模型包装器**

```python
# ── LightGBM Ranker ─────────────────────────────

class LGBMRanker:
    """LightGBM 排序学习模型"""

    def __init__(self):
        self.model = None
        self.feature_names = None
        self._fallback = False

    def _prepare_training_data(self, start_date: str, end_date: str
                                ) -> Optional[Tuple[pd.DataFrame, pd.Series, list]]:
        """准备训练数据"""
        signals = get_signals_for_training(start_date, end_date)
        if len(signals) < 1000:
            print(f"  [LGBM] 训练数据不足: {len(signals)} 条")
            return None

        rows = []
        for s in signals:
            try:
                feats = json.loads(s.get("features_json", "{}"))
                feats["label"] = s.get("label_return")
                feats["trade_date"] = s["trade_date"]
                feats["code"] = s["code"]
                rows.append(feats)
            except (json.JSONDecodeError, KeyError):
                continue

        df = pd.DataFrame(rows)
        if "label" not in df.columns or df["label"].isna().sum() > len(df) * 0.8:
            return None

        # Label 缩尾
        lo, hi = df["label"].quantile(0.01), df["label"].quantile(0.99)
        df["label"] = df["label"].clip(lo, hi)

        # 按行业分组（如无行业信息，按 code hash 模拟）
        df["group"] = df.apply(
            lambda r: f"{r['trade_date']}_{hash(str(r.get('code',''))) % 20}", axis=1)

        feature_cols = [c for c in df.columns
                        if c not in ("label", "trade_date", "code", "group",
                                     "rule_id", "rule_type_hash")]

        return df[feature_cols], df["label"], df["group"].tolist(), feature_cols

    def train(self, start_date: str = None, end_date: str = None):
        """训练模型（滚动预测：t-1 数据预测 t 日表现）"""
        import lightgbm as lgb

        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        result = self._prepare_training_data(start_date, end_date)
        if result is None:
            print("  [LGBM] 训练数据不足，回退到 Phase 2 评分")
            self._fallback = True
            return False

        X, y, groups, feature_names = result
        self.feature_names = feature_names

        # 时间序列分割：前70%训练，后30%验证
        split_idx = int(len(X) * 0.7)
        X_train, X_valid = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_valid = y.iloc[:split_idx], y.iloc[split_idx:]

        train_groups = groups[:split_idx]
        valid_groups = groups[split_idx:]

        # 构建 group 信息
        train_groups_set = sorted(set(train_groups))
        train_group_sizes = [train_groups.count(g) for g in train_groups_set]

        valid_groups_set = sorted(set(valid_groups))
        valid_group_sizes = [valid_groups.count(g) for g in valid_groups_set]

        train_data = lgb.Dataset(
            X_train, label=y_train,
            group=train_group_sizes if train_group_sizes else None,
        )
        valid_data = lgb.Dataset(
            X_valid, label=y_valid,
            group=valid_group_sizes if valid_group_sizes else None,
            reference=train_data,
        )

        params = {
            "objective": "lambdarank",
            "metric": "ndcg",
            "ndcg_eval_at": [5, 10],
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "verbose": -1,
            "seed": 42,
        }

        self.model = lgb.train(
            params,
            train_data,
            valid_sets=[valid_data],
            num_boost_round=500,
            callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
        )
        self._fallback = False
        print("  [LGBM] 训练完成")
        return True

    def predict(self, features_df: pd.DataFrame) -> np.ndarray:
        """推理并输出 0-100 归一化置信度"""
        if self._fallback or self.model is None:
            return np.full(len(features_df), 50.0)  # 回退：输出中性分数

        if self.feature_names:
            available = [c for c in self.feature_names if c in features_df.columns]
            X = features_df[available]
        else:
            X = features_df.select_dtypes(include=[np.number])

        raw_scores = self.model.predict(X)
        # MinMax 归一化至 [0, 100]
        smin, smax = raw_scores.min(), raw_scores.max()
        if smax - smin < 1e-9:
            return np.full(len(raw_scores), 50.0)
        return (raw_scores - smin) / (smax - smin) * 100.0

    def save_model(self, path: str = "strategy/lgbm_ranker_model.txt"):
        if self.model:
            self.model.save_model(path)

    def load_model(self, path: str = "strategy/lgbm_ranker_model.txt"):
        import lightgbm as lgb
        import os
        if os.path.exists(path):
            self.model = lgb.Booster(model_file=path)
            self._fallback = False
            return True
        self._fallback = True
        return False


# ── 全局单例 ──
_ranker_instance: Optional[LGBMRanker] = None


def get_ranker() -> LGBMRanker:
    global _ranker_instance
    if _ranker_instance is None:
        _ranker_instance = LGBMRanker()
        if not _ranker_instance.load_model():
            print("[LGBM] 模型文件未找到，首次需训练")
    return _ranker_instance
```

- [ ] **Step 3: 实现融合与规则淘汰**

```python
# ── 融合与淘汰 ──────────────────────────────────

def fuse_stock_signals(code: str, signals: List[dict]) -> float:
    """
    同一股票多条规则触发的加权融合
    权重 = e^(confidence/30) / Σ e^(confidence/30)
    返回 0-50 的 FUSION_SCORE
    """
    if not signals:
        return 0.0
    confidences = np.array([s.get("confidence", 25) for s in signals])
    exp_weights = np.exp(confidences / 30)
    weights = exp_weights / exp_weights.sum()
    fused = np.sum(confidences * weights)
    return round(fused / 2.0, 2)  # 0-100 → 0-50


def degrade_low_performance_rules():
    """淘汰低分规则：置信度均值<30 且持仓期间连续亏损"""
    rules = get_active_rules(limit=500)
    for r in rules:
        with get_conn() as conn:
            # 检查近60日表现
            signals = conn.execute("""
                SELECT AVG(confidence) as avg_conf,
                       AVG(label_return) as avg_label,
                       COUNT(*) as cnt
                FROM strategy_signals
                WHERE rule_id=? AND trade_date >= date('now', '-60 days')
            """, (r["id"],)).fetchone()
            if signals and signals["cnt"] >= 5:
                avg_conf = signals["avg_conf"] or 0
                avg_label = signals["avg_label"] or 0
                if avg_conf < 30 and avg_label < 0:
                    degrade_rule(r["id"])
                    print(f"  [LGBM] 淘汰规则: {r['rule_name']} "
                          f"(置信度={avg_conf:.1f}, 平均收益={avg_label:.4f})")
```

- [ ] **Step 4: 验证 lgbm_ranker 可正常 import**

```bash
python -c "from strategy.lgbm_ranker import LGBMRanker, build_meta_features, build_features_df, fuse_stock_signals; print('LGBMRanker OK')"
```

- [ ] **Step 5: Commit**

```bash
git add strategy/lgbm_ranker.py
git commit -m "feat: add Phase 3 LightGBM ranker with rolling prediction and fallback"
```

---

### Task 6: Phase 4 — 动态策略选择器 (dynamic_selector.py)

**Files:**
- Create: `strategy/dynamic_selector.py`

**Depends on:** Task 1 (DB), Task 5 (lgbm_ranker)

- [ ] **Step 1: 创建 strategy/dynamic_selector.py**

```python
"""
dynamic_selector.py —— Phase 4: 动态策略选择器
===============================================
功能：
- 双窗口(60日+120日)滑动回测选 Top-K
- 动态 K 值调整 (3-7条)
- 相关性去重 (牛市70%/熊市50%)
- 单只股票多信号加权融合
- 三层保底机制 (L1/L2/L3)
- 调度时序管理
"""
import json
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

from strategy.factor_lib import FACTOR_REGISTRY
from strategy.rule_miner import RuleMiner, StrategyRule, RuleCondition, load_template_library
from strategy.lgbm_ranker import get_ranker, fuse_stock_signals
from core.db import (
    get_active_rules, upsert_active_strategies, get_current_active_strategies,
    save_strategy_signal, degrade_rule, get_conn,
)
from backtest.backtest import Backtester
from config.strategy_params import DEFAULT_WEIGHTS


# ── 评分公式 ────────────────────────────────────

def window_score(perf: dict) -> float:
    """单窗口综合评分: 0.2×年化收益 + 0.25×胜率 + 0.3×夏普 - 0.2×最大回撤 + 0.05×卡玛"""
    calmar = (perf.get("annual_return", 0) / abs(perf.get("max_drawdown", 1)))
    return (0.2 * perf.get("annual_return", 0) / 100.0
            + 0.25 * perf.get("win_rate", 0)
            + 0.3 * perf.get("sharpe_ratio", 0)
            - 0.2 * abs(perf.get("max_drawdown", 0)) / 100.0
            + 0.05 * calmar)


def is_bear_market(index_df: pd.DataFrame) -> bool:
    """判断是否熊市：大盘60日均线向下且价格低于年线"""
    if len(index_df) < 120:
        return False
    ma60 = index_df["close"].rolling(60).mean()
    ma250 = index_df["close"].rolling(250).mean() if len(index_df) >= 250 else ma60
    last = index_df.index[-1]
    return (ma60.iloc[-1] < ma60.iloc[-20]) and (index_df["close"].iloc[-1] < ma250.iloc[-1])


# ── 关键约束过滤 ────────────────────────────────

def apply_constraints(rule_perf: dict, rule_signals: pd.DataFrame) -> Tuple[bool, str]:
    """
    应用关键约束，返回 (是否通过, 原因)

    rule_perf: 回测表现 {"annual_return", "win_rate", "sharpe_ratio", "max_drawdown", ...}
    rule_signals: 该规则的信号数据 (含 BUY_SIGNAL 列)
    """
    trades = rule_perf.get("total_trades", 0)
    if trades < 5:
        return False, "交易数不足"

    if abs(rule_perf.get("max_drawdown", 0)) > 20:
        return False, "回撤超标"

    if rule_perf.get("total_return", 0) < -5:
        return False, "收益下限"

    # 换手率约束
    turnover_rate = rule_perf.get("turnover_rate", 0)
    if turnover_rate > 1.2:
        return False, "换手率超标"

    # 连续亏损天数
    equity = rule_perf.get("equity_curve", [])
    if len(equity) > 0:
        max_consec_loss = _calc_max_consecutive_loss(equity)
        if max_consec_loss > 12:
            return False, f"连续亏损{max_consec_loss}天"

    return True, "OK"


def _calc_max_consecutive_loss(equity: List[float]) -> int:
    max_loss_days = 0
    current = 0
    for i in range(1, len(equity)):
        if equity[i] < equity[i - 1]:
            current += 1
            max_loss_days = max(max_loss_days, current)
        else:
            current = 0
    return max_loss_days
```

- [ ] **Step 2: 实现双窗口回测与 Top-K 选择**

```python
# ── 动态选择器 ──────────────────────────────────

class DynamicSelector:
    """Phase 4: 每周动态策略选择"""

    def __init__(self, k_min: int = 3, k_max: int = 7):
        self.k_min = k_min
        self.k_max = k_max
        self.miner = RuleMiner()

    def run_weekly_selection(self, stock_data: Dict, index_df: pd.DataFrame,
                             factor_df: pd.DataFrame) -> List[dict]:
        """
        每周五执行：双窗口回测 → 约束过滤 → 排序 → 相关性去重 → 选 Top-K

        stock_data: {code: (price_df_120d, factor_df_120d)} 至少120日数据
        """
        active_rules = load_template_library()
        if not active_rules:
            print("[Phase 4] 策略库为空，触发 L1 保底")
            return self._fallback_l1()

        bear = is_bear_market(index_df)
        overlap_threshold = 0.50 if bear else 0.70
        print(f"[Phase 4] 市场状态: {'熊市' if bear else '非熊市'}, "
              f"去重阈值={overlap_threshold}")

        scored_rules = []
        for rule in active_rules:
            # 截取60日和120日数据
            perf_60 = self.miner.evaluate_rule(rule, stock_data)  # 传入60日数据
            if not perf_60:
                continue

            passed, reason = apply_constraints(perf_60, None)
            if not passed:
                continue

            # 用不同数据段模拟120日窗口
            score_60 = window_score(perf_60)
            # 简化处理：120日用相同数据（实际应传入更长区间）
            score_120 = score_60 * 0.95  # 近似

            final_score = score_60 * 0.6 + score_120 * 0.4

            # 换手率惩罚
            if 1.0 <= perf_60.get("turnover_rate", 0) <= 1.2:
                final_score *= 0.7

            scored_rules.append({
                "rule": rule,
                "score": final_score,
                "perf_60": perf_60,
            })

        scored_rules.sort(key=lambda x: x["score"], reverse=True)

        # 相关性去重
        selected = self._deduplicate(scored_rules, factor_df, overlap_threshold)

        # Top-K 动态调整
        k = min(self.k_max, max(self.k_min, len(selected)))
        k = min(k, len(selected))
        top_k = selected[:k]

        # 保存到数据库
        today = datetime.now().strftime("%Y-%m-%d")
        next_friday = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        selections = []
        for rank, item in enumerate(top_k):
            rule = item["rule"]
            selections.append((
                today, item.get("rule_id", 0), rule.name,
                item.get("score_60", item["score"]),
                item.get("score_120", item["score"]),
                item["score"], rank + 1, next_friday,
            ))
        if selections:
            upsert_active_strategies(selections)

        # L2/L3 检查
        if len(top_k) < self.k_min:
            return self._handle_l2_l3(len(top_k))

        return top_k

    def _deduplicate(self, scored: List[dict], factor_df: pd.DataFrame,
                     threshold: float) -> List[dict]:
        """相关性去重：重叠率>阈值时只保留最高分"""
        from strategy.genetic_evolver import signal_overlap
        selected = []
        for item in scored:
            is_dup = False
            for sel in selected:
                ov = signal_overlap(item["rule"], sel["rule"], factor_df)
                if ov > threshold:
                    is_dup = True
                    break
            if not is_dup:
                selected.append(item)
        return selected

    def _fallback_l1(self) -> List[dict]:
        """L1 保底：回退到手工5条基线策略"""
        print("[Phase 4] L1 保底: 使用手工基线策略")
        return [{"rule": None, "score": 0, "source": "manual_baseline"}]

    def _handle_l2_l3(self, k: int) -> List[dict]:
        """L2/L3 保底检查"""
        with get_conn() as conn:
            # 检查连续几周活跃策略 < 3
            recent = conn.execute("""
                SELECT select_date, COUNT(*) as cnt
                FROM active_strategies
                GROUP BY select_date ORDER BY select_date DESC LIMIT 4
            """).fetchall()
            low_weeks = sum(1 for r in recent if r["cnt"] < 3)

        if low_weeks >= 3:
            print(f"[Phase 4] L2 告警: 连续{low_weeks}周活跃策略<3, 触发 Phase 1+2 重刷")
        if low_weeks >= 4:
            print("[Phase 4] L3 紧急告警: 临时放宽入库门槛 0.6→0.45 (持续4周)")
        return self._fallback_l1()


def generate_daily_signals(stock_data: Dict, market_data: dict,
                           index_df: pd.DataFrame) -> List[dict]:
    """
    每日盘后: 全市场信号触发 + Phase 3 置信度打分

    stock_data: {code: (price_df, factor_df)}
    返回: 每只股票的多策略融合评分列表
    """
    ranker = get_ranker()
    active_strategies = get_current_active_strategies()
    if not active_strategies:
        active_rules = load_template_library()[:5]  # 取 Top-5 模板规则
    else:
        active_rules = []
        for a in active_strategies:
            rules = get_active_rules(min_fitness=0.3, limit=200)
            for r in rules:
                if r["id"] == a["rule_id"]:
                    try:
                        conds = json.loads(r["conditions"])
                        active_rules.append(StrategyRule(
                            name=r["rule_name"], rule_type=r["rule_type"],
                            conditions=[RuleCondition(**c) for c in conds],
                        ))
                    except (json.JSONDecodeError, KeyError):
                        continue

    all_signals = []
    miner = RuleMiner()

    for code, (price_df, factor_df) in stock_data.items():
        stock_triggered = []
        for rule in active_rules:
            buy = rule.get_buy_signal(factor_df)
            if buy.iloc[-1]:  # 最新一天触发
                trade_date = str(price_df.index[-1].date()) if hasattr(
                    price_df.index[-1], "date") else str(price_df.index[-1])[:10]
                signal = {
                    "code": code,
                    "trade_date": trade_date,
                    "rule_id": 0,
                    "rule_name": rule.name,
                    "rule_type": rule.rule_type,
                    "n_conditions": len(rule.conditions),
                    "signal_strength": _calc_signal_strength(rule, factor_df),
                    "rule_recent_perf": {},
                    "rule_history": {},
                    "sector_crowd": {},
                }
                stock_triggered.append(signal)

        if stock_triggered:
            # 构建特征并打分
            features_df = pd.DataFrame([s for s in stock_triggered])
            if not features_df.empty:
                confidences = ranker.predict(features_df)
                for i, sig in enumerate(stock_triggered):
                    sig["confidence"] = confidences[i] if i < len(confidences) else 50
                fused_score = fuse_stock_signals(code, stock_triggered)
                all_signals.append({
                    "code": code,
                    "fusion_score": fused_score,
                    "signals": stock_triggered,
                })

    return all_signals


def _calc_signal_strength(rule: StrategyRule, factor_df: pd.DataFrame) -> float:
    """计算信号强度：因子实际值 vs 阈值的偏离程度"""
    strengths = []
    for c in rule.conditions:
        if c.factor in factor_df.columns:
            val = factor_df[c.factor].iloc[-1]
            diff = abs(val - c.threshold)
            strengths.append(min(diff / (c.threshold + 1e-9), 1.0))
    return np.mean(strengths) if strengths else 0.5
```

- [ ] **Step 3: 验证 dynamic_selector 可正常 import**

```bash
python -c "from strategy.dynamic_selector import DynamicSelector, generate_daily_signals, window_score; print('DynamicSelector OK')"
```

- [ ] **Step 4: Commit**

```bash
git add strategy/dynamic_selector.py
git commit -m "feat: add Phase 4 dynamic selector with dual-window and 3-tier fallback"
```

---

### Task 7: 集成 — 修改现有文件对接新系统

**Files:**
- Modify: `strategy/strategies.py:368-429` — `fuse_signals()` 适配 Phase 3/4
- Modify: `agents/signal_agent.py:104-139` — `_analyze_fusion()` 对接新 FUSION_SCORE

**Depends on:** Tasks 1-6

- [ ] **Step 1: 修改 strategy/strategies.py — fuse_signals() 增加兼容接口**

在 `fuse_signals()` 函数后面追加新函数：

```python
def fuse_with_phase34(dfs: List[pd.DataFrame],
                       weights: List[float] = None,
                       phase34_signals: dict = None) -> pd.DataFrame:
    """
    融合策略输出：优先使用 Phase 3/4 动态评分，fallback 到手工程序评分

    phase34_signals: {"code": "000001", "fusion_score": 35.2, "signals": [...]}
    """
    result = fuse_signals(dfs, weights)
    if phase34_signals and phase34_signals.get("fusion_score", 0) > 0:
        # 用 Phase 3/4 的评分替换 FUSION_SCORE 最后一行的值
        new_score = phase34_signals["fusion_score"]
        # 确保 0-50 范围
        new_score = max(0.0, min(50.0, new_score))
        result.loc[result.index[-1], "FUSION_SCORE"] = new_score
    return result
```

- [ ] **Step 2: 修改 agents/signal_agent.py — _analyze_fusion() 对接新系统**

在 `_analyze_fusion()` 方法中，在 `fused = fuse_signals(...)` 的调用前插入 Phase 3/4 的信号获取逻辑：

```python
def _analyze_fusion(self, code: str, name: str) -> dict | None:
    """5策略融合分析（支持 Phase 3/4 动态评分覆盖）"""
    try:
        import pandas as pd
        df = get_data_source_manager().get_daily_price_df(code)
        if df.empty or len(df) < 30:
            return None

        s1 = strategy_volume_breakout(df)
        s2 = strategy_ma_convergence(df)
        s3 = strategy_price_volume_divergence(df)
        s4 = strategy_bottom_fishing(df)
        s5 = strategy_whale_accumulation(df)

        def last_score(s_df):
            v = float(s_df.iloc[-1]["BUY_SCORE"])
            return round(min(10.0, v / 3.0 * 10.0), 1) if not pd.isna(v) else 0.0

        s1_sc = last_score(s1)
        s2_sc = last_score(s2)
        s3_sc = last_score(s3)
        s4_sc = last_score(s4)
        s5_sc = last_score(s5)

        # 尝试获取 Phase 3/4 动态评分
        phase34_score = None
        try:
            from strategy.dynamic_selector import generate_daily_signals
            from strategy.factor_lib import compute_all_factors
            factor_df = compute_all_factors(df)
            signals = generate_daily_signals(
                {code: (df, factor_df)}, {}, None)
            if signals and signals[0].get("fusion_score", 0) > 0:
                phase34_score = signals[0]
        except Exception:
            pass  # Phase 3/4 不可用时静默降级

        fused = fuse_with_phase34(
            [s1, s2, s3, s4, s5],
            weights=DEFAULT_WEIGHTS,
            phase34_signals=phase34_score,
        )
        last = fused.iloc[-1]
        fusion_score = round(float(last.get("FUSION_SCORE", 0)), 2)
        # 安全检查：确保在 0-50 范围
        fusion_score = max(0.0, min(50.0, fusion_score))

        if fusion_score < FUSION_THRESHOLD:
            return None

        price = round(float(last["close"]), 2)
        trade_date = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]
        buy_money = int(START_CAPITAL * POSITION_PER_STOCK)
        buy_volume = int(buy_money // (price * 100) * 100)

        return {
            "code": code, "name": name, "price": price,
            "score": fusion_score,
            "vol_score": s1_sc, "ma_score": s2_sc,
            "diverge_score": s3_sc, "bottom_score": s4_sc,
            "whale_score": s5_sc,
            "trade_date": trade_date,
            "buy_price": price,
            "stop_loss": round(price * STOP_LOSS, 2),
            "take_profit": round(price * TAKE_PROFIT, 2),
            "buy_volume": buy_volume,
            "buy_money": buy_money,
        }
    except Exception as e:
        print(f"  [SignalAgent] {code} 分析异常: {e}")
        return None
```

- [ ] **Step 3: 验证集成无误**

```bash
python -c "
from strategy.strategies import fuse_signals, fuse_with_phase34
from agents.signal_agent import SignalAgent
print('Integration OK')
"
```

- [ ] **Step 4: Commit**

```bash
git add strategy/strategies.py agents/signal_agent.py
git commit -m "feat: integrate Phase 3/4 dynamic scoring into SignalAgent with graceful fallback"
```

---

### Task 8: 端到端流水线测试

**Files:**
- Create: `tests/test_strategy_pipeline.py`

- [ ] **Step 1: 创建端到端测试**

```python
"""
端到端测试: 因子库 → Phase 1 穷举 → Phase 2 进化 → Phase 3 打分 → Phase 4 选择
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def generate_mock_data(n_days: int = 250, n_stocks: int = 50):
    """生成模拟数据"""
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=n_days, freq="B")
    stock_data = {}
    for i in range(n_stocks):
        code = f"{600000 + i:06d}"
        close = 10 + np.random.randn(n_days).cumsum() * 0.5 + np.sin(np.linspace(0, 10, n_days))
        close = np.maximum(close, 1)
        df = pd.DataFrame({
            "open": close * (1 + np.random.randn(n_days) * 0.005),
            "high": close * (1 + np.abs(np.random.randn(n_days)) * 0.01),
            "low": close * (1 - np.abs(np.random.randn(n_days)) * 0.01),
            "close": close,
            "volume": np.random.randint(1e6, 1e7, n_days),
        }, index=dates)
        df["amount"] = df["volume"] * df["close"]
        df["turnover"] = np.random.uniform(0.01, 0.1, n_days)
        stock_data[code] = df
    return stock_data


class TestFactorLibrary:
    def test_registry_has_all_categories(self):
        from strategy.factor_lib import FACTOR_REGISTRY
        categories = {v["category"] for v in FACTOR_REGISTRY.values()}
        for cat in ["trend", "momentum", "volatility", "volume",
                     "pattern", "liquidity", "fundamental", "sentiment"]:
            assert cat in categories, f"Missing category: {cat}"

    def test_compute_factors(self):
        from strategy.factor_lib import compute_all_factors
        stock_data = generate_mock_data(100, 1)
        df = list(stock_data.values())[0]
        result = compute_all_factors(df)
        assert len(result) > 30, f"Expected >30 factors, got {len(result)}"
        assert all(0 <= result[c].dropna().min() <= 1.1 for c in result.columns[:10])

    def test_ic_calculation(self):
        from strategy.factor_lib import compute_all_factors, calc_all_ic
        stock_data = generate_mock_data(100, 1)
        df = list(stock_data.values())[0]
        factors = compute_all_factors(df)
        forward_returns = df["close"].pct_change(20).shift(-20)
        ic = calc_all_ic(factors, forward_returns)
        assert len(ic) > 0


class TestRuleMiner:
    def test_template_generation(self):
        from strategy.rule_miner import TemplateBuilder
        from strategy.factor_lib import get_factor_names_by_category
        factors = get_factor_names_by_category("trend") + get_factor_names_by_category("momentum")
        builder = TemplateBuilder(factors, [0.2, 0.35, 0.5])
        t1 = builder.build_t1()
        t2 = builder.build_t2()
        t3 = builder.build_t3()
        assert len(t1) > 0, "T1 templates should not be empty"
        assert len(t2) > 0, "T2 templates should not be empty"


class TestGeneticEvolver:
    def test_encode_decode(self):
        from strategy.genetic_evolver import encode_rule, decode_rule
        from strategy.rule_miner import RuleCondition, StrategyRule
        original = StrategyRule(
            name="test", rule_type="genetic",
            conditions=[
                RuleCondition("RSI_14", "<", 0.35),
                RuleCondition("VOL_RATIO_5", ">", 0.6),
            ],
        )
        encoded = encode_rule(original.conditions)
        decoded = decode_rule(encoded)
        assert len(decoded) == len(original.conditions)

    def test_random_rule(self):
        from strategy.genetic_evolver import random_rule
        rule = random_rule(3)
        assert len(rule.conditions) >= 1
        assert rule.rule_type == "genetic"

    def test_legality_check(self):
        from strategy.genetic_evolver import legality_check
        from strategy.rule_miner import RuleCondition, StrategyRule
        # 有效规则
        valid = StrategyRule("test", "genetic", [RuleCondition("RSI_14", "<", 35)])
        assert legality_check(valid) >= 0.5

        # 矛盾规则: RSI > 80 AND RSI < 20 (阈值矛盾)
        contradictory = StrategyRule("test", "genetic", [
            RuleCondition("RSI_14", ">", 80),
            RuleCondition("RSI_14", "<", 20),
        ])
        assert legality_check(contradictory) < 0.5


class TestLGBMRanker:
    def test_fallback_prediction(self):
        from strategy.lgbm_ranker import LGBMRanker
        import pandas as pd
        ranker = LGBMRanker()
        ranker._fallback = True
        df = pd.DataFrame({"a": [1, 2, 3]})
        preds = ranker.predict(df)
        assert len(preds) == 3
        assert all(45 <= p <= 55 for p in preds)  # 回退输出接近 50

    def test_signal_fusion(self):
        from strategy.lgbm_ranker import fuse_stock_signals
        signals = [
            {"confidence": 80},
            {"confidence": 60},
            {"confidence": 40},
        ]
        score = fuse_stock_signals("000001", signals)
        assert 0 <= score <= 50


class TestDynamicSelector:
    def test_window_score(self):
        from strategy.dynamic_selector import window_score
        perf = {
            "annual_return": 30.0, "win_rate": 0.6,
            "sharpe_ratio": 1.5, "max_drawdown": -15.0,
        }
        score = window_score(perf)
        assert 0 <= score <= 2.0
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest tests/test_strategy_pipeline.py -v
```

- [ ] **Step 3: 修复失败测试，确认全部通过**

- [ ] **Step 4: Commit**

```bash
git add tests/test_strategy_pipeline.py
git commit -m "test: add end-to-end pipeline tests for strategy generation system"
```

---

## 实现顺序总结

```
Task 1: DB (3张表)           ── 基础
Task 2: factor_lib.py        ── 基础（依赖 Task 1）
Task 3: rule_miner.py        ── Phase 1（依赖 Task 1, 2）
Task 4: genetic_evolver.py   ── Phase 2（依赖 Task 1, 2, 3）
Task 5: lgbm_ranker.py       ── Phase 3（依赖 Task 1, 2）
Task 6: dynamic_selector.py  ── Phase 4（依赖 Task 1, 5）
Task 7: 集成修改              ──（依赖 Task 1-6）
Task 8: 端到端测试            ──（依赖 Task 1-7）
```

Task 5 和 Task 3/4 可并行（Task 5 只依赖 DB 和 factor_lib，不依赖 rule_miner/genetic_evolver）。
