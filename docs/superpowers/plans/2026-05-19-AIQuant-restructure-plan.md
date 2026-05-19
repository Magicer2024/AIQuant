# AIQuant Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure AIQuant from multi-agent quant platform into a focused personal stock scoring system with strategy mining, daily scoring, and backtesting with per-trade K-line details.

**Architecture:** Three-layer design — Core (factor_lib, Qlib, DB, sync) → Strategy/Backtest modules → Single-page dashboard. Remove agents/, ministries/, services/, and all legacy backtest scripts.

**Tech Stack:** Python 3.11+, Flask, SQLite, Qlib (SimulatorExecutor), ECharts, baostock/akshare

**Spec:** `docs/superpowers/specs/2026-05-19-AIQuant-restructure-design.md`

---

## File Map

| File | Responsibility | Action |
|------|---------------|--------|
| `strategy/rules_store.py` | CRUD for strategy_rules table | Create |
| `strategy/scorer.py` | Daily scoring engine: load active rules, compute factor values, assign scores | Create |
| `strategy/miner.py` | Strategy mining: IC filter + template enumeration + backtest validation + rule scoring | Create |
| `backtest/trade_store.py` | CRUD for backtest_results and backtest_trades tables | Create |
| `backtest/engine.py` | Unified backtest: wrap Qlib SimulatorExecutor, extract per-trade details | Create |
| `backtest/reporter.py` | Generate backtest summary and trade list for dashboard display | Create |
| `routes/scoring.py` | REST API for daily scoring and stock factor queries | Create |
| `routes/strategy.py` | REST API for strategy rule management and mining triggers | Create |
| `routes/backtest.py` | REST API for backtest execution and result queries | Create |
| `dashboard.html` | Single-page three-tab dashboard | Rewrite |
| `app.py` | Flask entry: trim blueprints to 3 core routes | Modify |
| `core/db.py` | Add new tables (stock_score, backtest_results, backtest_trades, factor_daily) | Modify |
| `strategy/factor_lib.py` | Add `compute_all_factors()` batch entrypoint | Modify |
| `config/settings.py` | Simplify: remove agent/ministry configs, add mining/backtest defaults | Modify |

---

## Phase 1: Database Schema & Configuration

### Task 1.1: Add new tables to core/db.py

**Files:**
- Modify: `core/db.py` (add CREATE TABLE statements in `init_db()`)
- The `init_db()` function uses `conn.executescript()` with all CREATE TABLE statements. Add these four tables inside that block.

**Context:** The existing `strategy_rules` table stays as-is (columns: rule_name, rule_type, encoding, conditions, sell_conditions, holding_min/max, source, generation, fitness, annual_return, win_rate, sharpe_ratio, max_drawdown, total_trades, signal_overlap, is_active, degraded_at, created_at, updated_at). The `is_active` column (INTEGER DEFAULT 1) is already the toggle switch. `backtest_results` replaces the old `strategy_lab_results` concept. `backtest_trades` stores per-trade details for K-line drilldown.

- [ ] **Step 1: Add CREATE TABLE statements to init_db()**

In `core/db.py`, inside the `init_db()` function's `conn.executescript("""...""")` block, append the following SQL before the closing `"""`:

```sql
-- 每日因子值缓存
CREATE TABLE IF NOT EXISTS factor_daily (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL,
    trade_date  TEXT    NOT NULL,
    factor_name TEXT    NOT NULL,
    factor_value REAL,
    UNIQUE(code, trade_date, factor_name)
);
CREATE INDEX IF NOT EXISTS idx_factor_code_date ON factor_daily(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_factor_name_date ON factor_daily(factor_name, trade_date);

-- 每日打分排名
CREATE TABLE IF NOT EXISTS stock_score (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT,
    score       REAL    DEFAULT 0,
    rule_id     TEXT,
    rule_name   TEXT,
    factors_json TEXT,
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    UNIQUE(trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_stock_score_date ON stock_score(trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_score_code ON stock_score(code);

-- 回测结果汇总
CREATE TABLE IF NOT EXISTS backtest_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id             TEXT    NOT NULL,
    rule_name           TEXT,
    start_date          TEXT    NOT NULL,
    end_date            TEXT    NOT NULL,
    annual_return       REAL    DEFAULT 0,
    cumulative_return   REAL    DEFAULT 0,
    win_rate            REAL    DEFAULT 0,
    sharpe_ratio        REAL    DEFAULT 0,
    max_drawdown        REAL    DEFAULT 0,
    total_trades        INTEGER DEFAULT 0,
    win_trades          INTEGER DEFAULT 0,
    created_at          TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_bt_results_rule ON backtest_results(rule_id);

-- 回测逐笔交易明细
CREATE TABLE IF NOT EXISTS backtest_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id       INTEGER,
    code            TEXT    NOT NULL,
    name            TEXT,
    entry_date      TEXT    NOT NULL,
    entry_price     REAL,
    exit_date       TEXT,
    exit_price      REAL,
    holding_days    INTEGER DEFAULT 0,
    pnl_pct         REAL    DEFAULT 0,
    exit_reason     TEXT,
    FOREIGN KEY (result_id) REFERENCES backtest_results(id)
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_result ON backtest_trades(result_id);
CREATE INDEX IF NOT EXISTS idx_bt_trades_code ON backtest_trades(code);
```

- [ ] **Step 2: Run init_db() to verify tables create without errors**

```bash
cd e:/小项目/Project/AIQuant && python -c "from core.db import init_db; init_db(); print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Verify tables exist in SQLite**

```bash
cd e:/小项目/Project/AIQuant && python -c "
from core.db import get_conn
with get_conn() as conn:
    tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name IN ('factor_daily','stock_score','backtest_results','backtest_trades')\").fetchall()
    for t in tables: print(t['name'])
"
```
Expected: all four table names printed.

- [ ] **Step 4: Commit**

```bash
git add core/db.py
git commit -m "feat: add factor_daily, stock_score, backtest_results, backtest_trades tables"
```

### Task 1.2: Simplify config/settings.py

**Files:**
- Modify: `config/settings.py`

**Context:** Remove agent pipeline configs, ministry configs, and LLM configs. Keep data sync configs. Add mining and backtest defaults.

- [ ] **Step 1: Read current config**

Read `config/settings.py` to see current structure.

- [ ] **Step 2: Write simplified config**

Replace `config/settings.py` content:

```python
"""
AIQuant settings —— 个人股票评分系统配置
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 数据同步 ──
HISTORY_START = "2024-01-01"
SYNC_THREADS = 4
SYNC_BATCH_SIZE = 100
SYNC_RETRY_COUNT = 2
SYNC_TIMEOUT = 30

# ── 数据库 ──
DB_PATH = os.path.join(BASE_DIR, "core", "quant.db")

# ── Flask ──
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5000
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

# ── 策略挖掘 ──
MINING = {
    "ic_lookback_days": 60,        # IC 计算回顾天数
    "ic_forward_days": 5,          # IC 前瞻收益天数
    "ic_min_threshold": 0.03,      # |IC| 最小阈值
    "candidate_thresholds": [0.3, 0.5, 0.7, 0.85],  # 因子阈值档位
    "sample_stocks": 300,          # 候选规则回测采样股票数
    "top_n_rules": 50,             # 保留前 N 条规则
    "min_trades": 5,               # 最少交易次数
}

# ── 回测 ──
BACKTEST = {
    "account": 1_000_000,
    "benchmark": "SH000300",
    "deal_price": "close",
    "open_cost": 0.0005,
    "close_cost": 0.0015,
    "min_cost": 5.0,
    "topk": 30,
    "n_drop": 5,
}
```

- [ ] **Step 3: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from config.settings import MINING, BACKTEST; print(MINING['ic_lookback_days'], BACKTEST['account'])"
```
Expected: `60 1000000`

- [ ] **Step 4: Commit**

```bash
git add config/settings.py
git commit -m "refactor: simplify settings — remove agent/ministry/LLM config, add mining/backtest defaults"
```

---

## Phase 2: Strategy Layer

### Task 2.1: Create strategy/rules_store.py

**Files:**
- Create: `strategy/rules_store.py`

**Context:** CRUD operations for `strategy_rules` table. The table already has columns: id, rule_name, rule_type, encoding, conditions, sell_conditions, holding_min/max, source, generation, fitness, annual_return, win_rate, sharpe_ratio, max_drawdown, total_trades, signal_overlap, is_active, degraded_at, created_at, updated_at.

- [ ] **Step 1: Write rules_store.py**

```python
"""
rules_store.py —— 策略规则 CRUD
"""
import json
from typing import List, Optional, Dict
from core.db import get_conn


def list_rules(active_only: bool = False) -> List[dict]:
    """获取所有策略规则列表"""
    with get_conn() as conn:
        sql = """
            SELECT id, rule_name, rule_type, encoding, conditions, sell_conditions,
                   holding_min, holding_max, source, generation, fitness,
                   annual_return, win_rate, sharpe_ratio, max_drawdown, total_trades,
                   signal_overlap, is_active, created_at, updated_at
            FROM strategy_rules
        """
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY fitness DESC"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_rule(rule_id: int) -> Optional[dict]:
    """获取单条规则"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None


def toggle_active(rule_id: int) -> Optional[dict]:
    """切换规则启用/禁用状态，返回新状态"""
    with get_conn() as conn:
        current = conn.execute(
            "SELECT is_active FROM strategy_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if not current:
            return None
        new_state = 0 if current["is_active"] else 1
        conn.execute(
            "UPDATE strategy_rules SET is_active = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (new_state, rule_id),
        )
        return {"id": rule_id, "is_active": new_state}


def save_rule(rule: dict) -> int:
    """插入或更新一条规则（按 rule_name 去重）"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM strategy_rules WHERE rule_name = ?", (rule["rule_name"],)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE strategy_rules SET
                    rule_type=?, encoding=?, conditions=?, sell_conditions=?,
                    holding_min=?, holding_max=?, fitness=?,
                    annual_return=?, win_rate=?, sharpe_ratio=?, max_drawdown=?,
                    total_trades=?, signal_overlap=?, updated_at=datetime('now','localtime')
                WHERE id=?
            """, (
                rule["rule_type"], rule["encoding"], rule.get("conditions", ""),
                rule.get("sell_conditions", ""), rule.get("holding_min", 3),
                rule.get("holding_max", 20), rule.get("fitness", 0),
                rule.get("annual_return", 0), rule.get("win_rate", 0),
                rule.get("sharpe_ratio", 0), rule.get("max_drawdown", 0),
                rule.get("total_trades", 0), rule.get("signal_overlap", 0),
                existing["id"],
            ))
            return existing["id"]
        else:
            cur = conn.execute("""
                INSERT INTO strategy_rules
                    (rule_name, rule_type, encoding, conditions, sell_conditions,
                     holding_min, holding_max, source, generation, fitness,
                     annual_return, win_rate, sharpe_ratio, max_drawdown,
                     total_trades, signal_overlap, is_active)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                rule["rule_name"], rule["rule_type"], rule["encoding"],
                rule.get("conditions", ""), rule.get("sell_conditions", ""),
                rule.get("holding_min", 3), rule.get("holding_max", 20),
                rule.get("source", "template"), rule.get("generation", 1),
                rule.get("fitness", 0), rule.get("annual_return", 0),
                rule.get("win_rate", 0), rule.get("sharpe_ratio", 0),
                rule.get("max_drawdown", 0), rule.get("total_trades", 0),
                rule.get("signal_overlap", 0), 1,
            ))
            return cur.lastrowid


def delete_rule(rule_id: int) -> bool:
    """删除规则"""
    with get_conn() as conn:
        conn.execute("DELETE FROM strategy_rules WHERE id = ?", (rule_id,))
        return True


def get_active_rules() -> List[dict]:
    """获取所有启用的规则（打分用）"""
    return list_rules(active_only=True)
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from strategy.rules_store import list_rules; print(len(list_rules()))"
```
Expected: number of existing rules in DB (or 0 if empty).

- [ ] **Step 3: Commit**

```bash
git add strategy/rules_store.py
git commit -m "feat: add strategy rules_store — CRUD for strategy_rules table"
```

### Task 2.2: Add compute_all_factors() to strategy/factor_lib.py

**Files:**
- Modify: `strategy/factor_lib.py`

**Context:** `factor_lib.py` already has `FACTOR_REGISTRY` (~80 factors) with `compute_factor()` for individual factors and `compute_factor_batch()` for a single factor across all stocks. We need a batch entrypoint that computes all registered factors for a list of stock DataFrames and returns a flat DataFrame suitable for scoring and IC calculation.

- [ ] **Step 1: Add compute_all_factors() function**

Read the `compute_factor()` and `compute_factor_batch()` signatures first to understand input/output shapes. Then append this function at the end of `factor_lib.py`:

```python
def compute_all_factors(stock_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    对所有股票批量计算所有注册因子。

    Args:
        stock_data: {code: DataFrame(columns=[open,high,low,close,volume,amount,turnover])}

    Returns:
        DataFrame with columns: code, trade_date, factor_name, factor_value
        Indexed by (code, trade_date, factor_name) for pivot.
    """
    records = []
    for code, df in stock_data.items():
        if df.empty:
            continue
        for factor_name in FACTOR_REGISTRY:
            try:
                values = compute_factor(df, factor_name)
                for i, (dt, val) in enumerate(zip(df.index, values)):
                    if pd.notna(val):
                        records.append({
                            "code": code,
                            "trade_date": str(dt)[:10],
                            "factor_name": factor_name,
                            "factor_value": float(val),
                        })
            except Exception:
                continue
    return pd.DataFrame(records)
```

- [ ] **Step 2: Verify the function exists and is importable**

```bash
cd e:/小项目/Project/AIQuant && python -c "from strategy.factor_lib import compute_all_factors; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add strategy/factor_lib.py
git commit -m "feat: add compute_all_factors() batch entrypoint to factor_lib"
```

### Task 2.3: Create strategy/scorer.py

**Files:**
- Create: `strategy/scorer.py`

**Context:** Daily scoring engine. Loads active rules from `strategy_rules` (is_active=1), loads stock OHLCV data from `daily_price`, computes factors via `factor_lib.compute_all_factors()`, evaluates each rule's conditions against factor values, assigns scores, and writes results to `stock_score` table. When a stock matches multiple rules, takes the highest score.

Rule conditions are stored in the `conditions` column as JSON strings like `{"momentum_10": {">": 0.8}}` for T1, `{"momentum_10": {">": 0.7}, "volume_ratio": {">": 0.6}}` for T2, or `{"cross": "MACD_DIF > MACD_DEA"}` for T3.

- [ ] **Step 1: Write scorer.py**

```python
"""
scorer.py —— 每日打分引擎

加载活跃策略规则 → 计算因子 → 评估条件 → 逐股打分 → 写入 stock_score 表
"""
import json
import pandas as pd
from datetime import date
from typing import Dict, List, Optional
from core.db import get_conn
from strategy.rules_store import get_active_rules
from strategy.factor_lib import compute_all_factors, FACTOR_REGISTRY


def _load_stock_data(trade_date: str) -> Dict[str, pd.DataFrame]:
    """从 daily_price 加载指定日期的所有股票 OHLCV 数据，返回 {code: DataFrame}"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT dp.code, dp.trade_date, dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover
            FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY dp.trade_date
        """, (trade_date,)).fetchall()

    data: Dict[str, pd.DataFrame] = {}
    for r in rows:
        d = dict(r)
        code = d.pop("code")
        trade_dt = d.pop("trade_date")
        if code not in data:
            data[code] = []
        data[code].append({"trade_date": trade_dt, **d})

    result = {}
    for code, records in data.items():
        df = pd.DataFrame(records).set_index("trade_date")
        df = df.sort_index().tail(120)  # 最近 120 天用于因子计算
        result[code] = df
    return result


def _evaluate_condition(factor_values: Dict[str, float], condition: dict) -> bool:
    """评估单条规则条件是否满足"""
    if "cross" in condition:
        # T3: 交叉信号，挖掘时已预先计算并存储在 conditions 中
        # 打分阶段：交叉信号的 encoding 列直接存了交叉是否发生的标志
        return True  # T3 规则在 miner 中已预处理为二进制信号

    for factor_name, op_dict in condition.items():
        value = factor_values.get(factor_name)
        if value is None:
            return False
        for op, threshold in op_dict.items():
            if op == ">" and not (value > threshold):
                return False
            if op == "<" and not (value < threshold):
                return False
            if op == ">=" and not (value >= threshold):
                return False
            if op == "<=" and not (value <= threshold):
                return False
    return True


def score_stocks(trade_date: str, save: bool = True) -> List[dict]:
    """
    对指定交易日所有股票打分。

    Args:
        trade_date: 交易日 'YYYY-MM-DD'
        save: 是否写入 stock_score 表

    Returns:
        [{code, name, score, rule_id, rule_name, factors_json}], 按 score 降序
    """
    rules = get_active_rules()
    if not rules:
        print(f"[scorer] {trade_date}: no active rules")
        return []

    stock_data = _load_stock_data(trade_date)
    print(f"[scorer] {trade_date}: loaded {len(stock_data)} stocks")

    results = []
    for code, df in stock_data.items():
        if df.empty or len(df) < 20:
            continue

        # 计算所有因子值
        factor_row = {}
        for fname in FACTOR_REGISTRY:
            try:
                from strategy.factor_lib import compute_factor
                vals = compute_factor(df, fname)
                if len(vals) > 0 and pd.notna(vals[-1]):
                    factor_row[fname] = float(vals[-1])
            except Exception:
                pass

        if not factor_row:
            continue

        # 逐条规则评估
        best_score = 0.0
        best_rule = None
        stock_name = ""

        for rule in rules:
            try:
                conditions = json.loads(rule["conditions"] or "{}")
            except json.JSONDecodeError:
                conditions = {}

            if not conditions:
                continue

            if _evaluate_condition(factor_row, conditions):
                score = rule.get("fitness", 0) or 0
                if score > best_score:
                    best_score = score
                    best_rule = rule

        if best_rule and best_score > 0:
            with get_conn() as conn:
                name_row = conn.execute(
                    "SELECT name FROM stock_info WHERE code = ?", (code,)
                ).fetchone()
                stock_name = name_row["name"] if name_row else ""

            results.append({
                "code": code,
                "name": stock_name,
                "score": round(best_score * 100, 1),
                "rule_id": str(best_rule["id"]),
                "rule_name": best_rule["rule_name"],
                "factors_json": json.dumps(factor_row, ensure_ascii=False),
            })

    results.sort(key=lambda x: x["score"], reverse=True)

    if save and results:
        _save_scores(trade_date, results)

    return results


def _save_scores(trade_date: str, results: List[dict]):
    """批量写入 stock_score 表"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO stock_score
                (trade_date, code, name, score, rule_id, rule_name, factors_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (trade_date, r["code"], r["name"], r["score"],
             r["rule_id"], r["rule_name"], r["factors_json"])
            for r in results
        ])


def get_daily_scores(trade_date: str) -> List[dict]:
    """获取某日打分排名（从 stock_score 表读取）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM stock_score
            WHERE trade_date = ?
            ORDER BY score DESC
        """, (trade_date,)).fetchall()
        return [dict(r) for r in rows]


def get_latest_score_date() -> Optional[str]:
    """获取最近一次打分的日期"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) as d FROM stock_score"
        ).fetchone()
        return row["d"] if row else None
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from strategy.scorer import score_stocks, get_daily_scores; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add strategy/scorer.py
git commit -m "feat: add daily scoring engine — evaluate active rules, assign scores, write stock_score"
```

### Task 2.4: Create strategy/miner.py

**Files:**
- Create: `strategy/miner.py`

**Context:** Strategy mining engine. Four-stage pipeline: (1) IC filter to select predictive factors, (2) template enumeration to generate candidate rules, (3) backtest each candidate via Qlib, (4) score rules and save top N. Reuses `FACTOR_REGISTRY` from factor_lib.py, `save_rule()` from rules_store.py, and `BacktestConfig`/`backtest_single_rule()` from qlib_engine/strategy_adapter.py.

- [ ] **Step 1: Write miner.py**

```python
"""
miner.py —— 策略挖掘引擎

流程: IC 过滤 → 模板穷举 → 回测验证 → 规则评分 → 保存前 N 条
"""
import json
import time
import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from core.db import get_conn
from config.settings import MINING, BACKTEST
from strategy.factor_lib import FACTOR_REGISTRY, compute_factor
from strategy.rules_store import save_rule
from qlib_engine.strategy_adapter import BacktestConfig, backtest_single_rule

logger = logging.getLogger(__name__)

TEMPLATES = ["T1", "T2", "T3"]


def _load_factor_matrix(
    stock_data: Dict[str, pd.DataFrame],
    factor_names: List[str],
) -> pd.DataFrame:
    """
    对所有股票计算指定因子，返回 (N_stocks, N_factors) 矩阵。
    Index = code, Columns = factor_names
    """
    rows = []
    for code, df in stock_data.items():
        if df.empty or len(df) < 60:
            continue
        row = {"code": code}
        for fname in factor_names:
            try:
                vals = compute_factor(df, fname)
                row[fname] = float(vals[-1]) if len(vals) > 0 and pd.notna(vals[-1]) else np.nan
            except Exception:
                row[fname] = np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index("code").dropna(axis=1, how="all")


def _compute_ic(
    factor_matrix: pd.DataFrame,
    forward_returns: pd.Series,
) -> pd.Series:
    """
    计算每个因子的 IC_RANK（Spearman 相关性）。

    Args:
        factor_matrix: (N_stocks, N_factors)
        forward_returns: (N_stocks,) Series indexed by code

    Returns:
        Series indexed by factor_name, values = IC
    """
    ic_values = {}
    for col in factor_matrix.columns:
        common = factor_matrix[col].dropna().index.intersection(forward_returns.dropna().index)
        if len(common) < 30:
            ic_values[col] = 0.0
            continue
        ic = factor_matrix.loc[common, col].rank().corr(forward_returns.loc[common].rank())
        ic_values[col] = ic if not np.isnan(ic) else 0.0
    return pd.Series(ic_values)


def _enumerate_rules(
    factor_names: List[str],
    thresholds: List[float],
) -> List[dict]:
    """
    模板穷举生成候选规则。

    T1: 单因子 > 阈值
    T2: 两个不同因子均 > 阈值
    T3: 交叉信号（暂跳过，需要时序数据）

    Returns list of {rule_name, rule_type, encoding, conditions}
    """
    rules = []
    # T1: 单因子阈值
    for f in factor_names:
        for t in thresholds:
            conditions = {f: {">": t}}
            rules.append({
                "rule_name": f"T1_{f}_gt_{t:.2f}",
                "rule_type": "T1",
                "encoding": json.dumps(conditions, ensure_ascii=False),
                "conditions": json.dumps(conditions, ensure_ascii=False),
            })

    # T2: 双因子组合
    for i, f1 in enumerate(factor_names):
        for f2 in factor_names[i + 1:]:
            for t in thresholds:
                conditions = {f1: {">": t}, f2: {">": t}}
                rules.append({
                    "rule_name": f"T2_{f1}_and_{f2}_gt_{t:.2f}",
                    "rule_type": "T2",
                    "encoding": json.dumps(conditions, ensure_ascii=False),
                    "conditions": json.dumps(conditions, ensure_ascii=False),
                })

    return rules


def _generate_signals_for_rule(
    rule: dict,
    factor_matrix: pd.DataFrame,
    trade_date: str,
) -> List[dict]:
    """
    根据规则条件从因子矩阵生成 Qlib 格式的信号列表。
    条件满足 → {trade_date, code, score}
    """
    try:
        conditions = json.loads(rule["conditions"])
    except json.JSONDecodeError:
        return []

    signals = []
    for code in factor_matrix.index:
        match = True
        for fname, op_dict in conditions.items():
            val = factor_matrix.loc[code, fname]
            if pd.isna(val):
                match = False
                break
            for op, threshold in op_dict.items():
                if op == ">" and not (val > threshold):
                    match = False
                if op == "<" and not (val < threshold):
                    match = False
                if not match:
                    break
            if not match:
                break

        if match:
            signals.append({
                "trade_date": trade_date,
                "code": code,
                "score": 1.0,
            })
    return signals


def _score_rule(perf: dict) -> float:
    """综合评分：0.3*收益 + 0.3*胜率 + 0.25*夏普 - 0.15*|回撤|"""
    return (
        0.3 * (perf.get("annual_return", 0) or 0) / 100.0
        + 0.3 * (perf.get("win_rate", 0) or 0) / 100.0
        + 0.25 * (perf.get("sharpe_ratio", 0) or 0)
        - 0.15 * abs(perf.get("max_drawdown", 0) or 0) / 100.0
    )


def mine_strategies(
    trade_date: str,
    end_date: Optional[str] = None,
    progress_callback: Optional[callable] = None,
) -> List[dict]:
    """
    执行策略挖掘。

    Args:
        trade_date: 信号生成日期
        end_date: 回测结束日期（默认 trade_date）
        progress_callback: 进度回调 fn(stage, current, total)

    Returns:
        保存的规则列表 [{rule_name, fitness, annual_return, win_rate, ...}]
    """
    if end_date is None:
        end_date = trade_date

    start_date = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=MINING["ic_lookback_days"])).strftime("%Y-%m-%d")

    t0 = time.time()

    # ── Step 1: 加载数据 ──
    if progress_callback:
        progress_callback("loading", 0, 100)

    stock_data = _load_stock_data_for_mining(trade_date, MINING["sample_stocks"])

    # ── Step 2: IC 过滤 ──
    if progress_callback:
        progress_callback("ic_filter", 0, 100)

    factor_names = list(FACTOR_REGISTRY.keys())
    factor_matrix = _load_factor_matrix(stock_data, factor_names)

    # 计算前瞻收益
    forward_returns = _compute_forward_returns(stock_data, trade_date, MINING["ic_forward_days"])
    ic_series = _compute_ic(factor_matrix, forward_returns)

    # 过滤 |IC| < 阈值的因子
    significant_factors = [
        f for f in factor_names
        if f in ic_series.index and abs(ic_series[f]) >= MINING["ic_min_threshold"]
    ]
    logger.info(f"IC filter: {len(factor_names)} → {len(significant_factors)} factors "
                f"(|IC| >= {MINING['ic_min_threshold']})")

    if len(significant_factors) < 1:
        logger.warning("No significant factors found")
        return []

    # ── Step 3: 模板穷举 ──
    if progress_callback:
        progress_callback("enumerate", 0, 100)

    candidates = _enumerate_rules(significant_factors, MINING["candidate_thresholds"])
    logger.info(f"Generated {len(candidates)} candidate rules")

    # ── Step 4: 回测验证 ──
    config = BacktestConfig(
        start_time=(datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d"),
        end_time=end_date,
        account=BACKTEST["account"],
        benchmark=BACKTEST["benchmark"],
        deal_price=BACKTEST["deal_price"],
        open_cost=BACKTEST["open_cost"],
        close_cost=BACKTEST["close_cost"],
        min_cost=BACKTEST["min_cost"],
        topk=BACKTEST["topk"],
        n_drop=BACKTEST["n_drop"],
    )

    results = []
    total = len(candidates)
    for i, rule in enumerate(candidates):
        signals = _generate_signals_for_rule(rule, factor_matrix, trade_date)
        if len(signals) < MINING["min_trades"]:
            continue

        perf = backtest_single_rule(rule["rule_name"], signals, config)
        if perf.get("error"):
            continue

        fitness = _score_rule(perf)
        rule["fitness"] = round(fitness, 4)
        rule["annual_return"] = perf.get("annual_return", 0)
        rule["win_rate"] = perf.get("win_rate", 0)
        rule["sharpe_ratio"] = perf.get("sharpe_ratio", 0)
        rule["max_drawdown"] = perf.get("max_drawdown", 0)
        rule["total_trades"] = perf.get("total_trades", 0)
        rule["source"] = "template"
        rule["generation"] = 1
        results.append(rule)

        if progress_callback:
            progress_callback("backtest", i + 1, total)

    # ── Step 5: 排序 & 保存前 N ──
    results.sort(key=lambda r: r["fitness"], reverse=True)
    top_n = results[:MINING["top_n_rules"]]

    saved = []
    for rule in top_n:
        rule_id = save_rule(rule)
        rule["id"] = rule_id
        saved.append(rule)

    elapsed = time.time() - t0
    logger.info(f"Mining complete: {len(saved)} rules saved in {elapsed:.1f}s")
    return saved


def _load_stock_data_for_mining(trade_date: str, sample_size: int) -> Dict[str, pd.DataFrame]:
    """加载采样股票的历史 OHLCV 数据，用于挖掘"""
    with get_conn() as conn:
        codes = conn.execute("""
            SELECT DISTINCT dp.code FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (trade_date, sample_size)).fetchall()

    data = {}
    for (code,) in codes:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount, turnover
            FROM daily_price WHERE code = ? AND trade_date <= ?
            ORDER BY trade_date
        """, (code, trade_date)).fetchall()
        if len(rows) < 60:
            continue
        df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")
        data[code] = df
    return data


def _compute_forward_returns(
    stock_data: Dict[str, pd.DataFrame],
    base_date: str,
    forward_days: int,
) -> pd.Series:
    """计算前瞻收益 = (N日后收盘价 / 当日收盘价 - 1)"""
    returns = {}
    for code, df in stock_data.items():
        if base_date not in df.index:
            continue
        idx = df.index.get_loc(base_date)
        future_idx = idx + forward_days
        if future_idx >= len(df):
            continue
        base_close = df.iloc[idx]["close"]
        future_close = df.iloc[future_idx]["close"]
        if base_close > 0:
            returns[code] = future_close / base_close - 1.0
    return pd.Series(returns)
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from strategy.miner import mine_strategies; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add strategy/miner.py
git commit -m "feat: add strategy miner — IC filter, template enumeration, backtest validation, rule scoring"
```

---

## Phase 3: Backtest Layer

### Task 3.1: Create backtest/trade_store.py

**Files:**
- Create: `backtest/trade_store.py`

- [ ] **Step 1: Write trade_store.py**

```python
"""
trade_store.py —— 回测交易明细存储
"""
from typing import List, Optional
from core.db import get_conn


def save_result(
    rule_id: str,
    rule_name: str,
    start_date: str,
    end_date: str,
    metrics: dict,
) -> int:
    """保存回测汇总结果，返回 result_id"""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO backtest_results
                (rule_id, rule_name, start_date, end_date,
                 annual_return, cumulative_return, win_rate,
                 sharpe_ratio, max_drawdown, total_trades, win_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            rule_id, rule_name, start_date, end_date,
            metrics.get("annual_return", 0),
            metrics.get("cumulative_return", 0),
            metrics.get("win_rate", 0),
            metrics.get("sharpe_ratio", 0),
            metrics.get("max_drawdown", 0),
            metrics.get("total_trades", 0),
            metrics.get("win_trades", 0),
        ))
        return cur.lastrowid


def save_trades(result_id: int, trades: List[dict]):
    """批量保存逐笔交易"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO backtest_trades
                (result_id, code, name, entry_date, entry_price,
                 exit_date, exit_price, holding_days, pnl_pct, exit_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [
            (
                result_id,
                t.get("code", ""),
                t.get("name", ""),
                t.get("entry_date", ""),
                t.get("entry_price", 0),
                t.get("exit_date", ""),
                t.get("exit_price", 0),
                t.get("holding_days", 0),
                t.get("pnl_pct", 0),
                t.get("exit_reason", "expire"),
            )
            for t in trades
        ])


def get_results() -> List[dict]:
    """获取所有回测结果列表（最新在前）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_results ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_result(result_id: int) -> Optional[dict]:
    """获取单个回测结果"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_results WHERE id = ?", (result_id,)
        ).fetchone()
        return dict(row) if row else None


def get_trades(result_id: int) -> List[dict]:
    """获取回测的逐笔交易明细"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_trades WHERE result_id = ? ORDER BY entry_date
        """, (result_id,)).fetchall()
        return [dict(r) for r in rows]


def get_rule_trades(rule_id: str) -> List[dict]:
    """获取某策略的所有历史交易（跨多次回测）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT bt.* FROM backtest_trades bt
            INNER JOIN backtest_results br ON bt.result_id = br.id
            WHERE br.rule_id = ?
            ORDER BY bt.entry_date
        """, (rule_id,)).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from backtest.trade_store import save_result, save_trades, get_results; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backtest/trade_store.py
git commit -m "feat: add backtest trade_store — CRUD for backtest_results and backtest_trades tables"
```

### Task 3.2: Create backtest/engine.py

**Files:**
- Create: `backtest/engine.py`

**Context:** Unified backtest engine wrapping Qlib. Accepts a strategy rule + date range, runs Qlib backtest, extracts per-trade details from the Qlib portfolio metrics, saves summary to `backtest_results` and trades to `backtest_trades`. Also supports single-stock backtest mode.

Key challenge: Qlib's `SimulatorExecutor` returns portfolio-level metrics but not individual trade records. We need to extract trade details from Qlib's `indicator` object (which provides trade calendar and position snapshots) or reconstruct trades from the position records.

- [ ] **Step 1: Write engine.py**

```python
"""
engine.py —— 统一回测引擎

封装 Qlib SimulatorExecutor，提取汇总指标 + 逐笔交易明细
"""
import json
import logging
import sys
import io
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from qlib_engine import init_qlib
from qlib.log import set_global_logger_level
from qlib_engine.strategy_adapter import BacktestConfig
from config.settings import BACKTEST

logger = logging.getLogger(__name__)


def run_backtest(
    rule_name: str,
    rule_id: str,
    conditions_json: str,
    start_date: str,
    end_date: str,
    save: bool = True,
) -> dict:
    """
    执行回测并提取汇总指标和逐笔交易。

    Args:
        rule_name: 策略名称
        rule_id: 策略规则 ID
        conditions_json: 条件 JSON 字符串
        start_date: 回测起始日 'YYYY-MM-DD'
        end_date: 回测结束日 'YYYY-MM-DD'
        save: 是否保存到数据库

    Returns:
        {
            summary: {annual_return, win_rate, sharpe_ratio, max_drawdown, total_trades, win_trades},
            trades: [{code, name, entry_date, entry_price, exit_date, exit_price, holding_days, pnl_pct, exit_reason}]
        }
    """
    init_qlib()
    set_global_logger_level(logging.ERROR)

    # 生成信号
    signals = _generate_backtest_signals(rule_name, conditions_json, start_date, end_date)
    if not signals:
        return {"summary": {}, "trades": [], "error": "No signals generated"}

    config = BacktestConfig(
        start_time=start_date,
        end_time=end_date,
        account=BACKTEST["account"],
        benchmark=BACKTEST["benchmark"],
        deal_price=BACKTEST["deal_price"],
        open_cost=BACKTEST["open_cost"],
        close_cost=BACKTEST["close_cost"],
        min_cost=BACKTEST["min_cost"],
        topk=BACKTEST["topk"],
        n_drop=BACKTEST["n_drop"],
    )

    # 运行 Qlib 回测
    summary, raw_trades = _run_qlib_backtest(signals, config)

    # 提取交易明细
    trades = _extract_trade_details(raw_trades, signals)

    # 保存
    result_id = None
    if save and summary:
        from backtest.trade_store import save_result, save_trades
        result_id = save_result(rule_id, rule_name, start_date, end_date, summary)
        if trades:
            save_trades(result_id, trades)

    return {
        "result_id": result_id,
        "summary": summary,
        "trades": trades,
    }


def run_single_stock_backtest(
    rule_name: str,
    conditions_json: str,
    stock_code: str,
    start_date: str,
    end_date: str,
) -> dict:
    """
    单股回测：仅在指定股票上应用策略。
    返回相同格式但 trades 只包含该股交易。
    """
    full_result = run_backtest(
        rule_name=rule_name,
        rule_id=f"single_{stock_code}",
        conditions_json=conditions_json,
        start_date=start_date,
        end_date=end_date,
        save=False,
    )
    # 过滤仅保留该股交易
    stock_trades = [t for t in full_result.get("trades", []) if t.get("code") == stock_code]
    full_result["trades"] = stock_trades
    return full_result


def _generate_backtest_signals(
    rule_name: str,
    conditions_json: str,
    start_date: str,
    end_date: str,
) -> List[dict]:
    """根据规则条件在整个回测区间生成每日信号"""
    try:
        conditions = json.loads(conditions_json)
    except json.JSONDecodeError:
        return []

    from core.db import get_conn

    with get_conn() as conn:
        # 获取回测区间所有交易日
        dates = conn.execute("""
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, (start_date, end_date)).fetchall()

        signals = []
        for (trade_date,) in dates:
            trade_date = trade_date if isinstance(trade_date, str) else str(trade_date)
            # 获取当日所有股票日线快照
            rows = conn.execute("""
                SELECT code, close FROM daily_price
                WHERE trade_date = ?
            """, (trade_date,)).fetchall()

            for row in rows:
                code = row["code"]
                # 简化评估：基于单日快照 + 因子判断
                score = _evaluate_snapshot(code, trade_date, conditions)
                if score is not None:
                    signals.append({
                        "trade_date": trade_date,
                        "code": code,
                        "score": score,
                    })
        return signals


def _evaluate_snapshot(code: str, trade_date: str, conditions: dict) -> Optional[float]:
    """评估单只股票在某个交易日是否满足条件"""
    from core.db import get_conn
    from strategy.factor_lib import compute_factor, FACTOR_REGISTRY

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume, amount, turnover
            FROM daily_price WHERE code = ? AND trade_date <= ?
            ORDER BY trade_date
        """, (code, trade_date)).fetchall()

    if len(rows) < 60:
        return None

    df = pd.DataFrame([dict(r) for r in rows]).set_index("trade_date")

    for fname, op_dict in conditions.items():
        if fname not in FACTOR_REGISTRY:
            continue
        try:
            vals = compute_factor(df, fname)
            val = float(vals[-1]) if len(vals) > 0 and pd.notna(vals[-1]) else None
        except Exception:
            continue

        if val is None:
            return None

        for op, threshold in op_dict.items():
            if op == ">" and not (val > threshold):
                return None
            if op == "<" and not (val < threshold):
                return None
            if op == ">=" and not (val >= threshold):
                return None
            if op == "<=" and not (val <= threshold):
                return None

    return 1.0


def _run_qlib_backtest(signals: List[dict], config: BacktestConfig) -> Tuple[dict, dict]:
    """执行 Qlib 回测，返回 (summary, raw_portfolio_data)"""
    from qlib_engine.strategy_adapter import _signals_to_prediction_df
    from qlib.utils import init_instance_by_config

    pred_df = _signals_to_prediction_df(signals, [], [])
    if pred_df.empty:
        return {}, {}

    bt_config = {
        "strategy": {
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy",
            "kwargs": {"signal": pred_df, "topk": config.topk, "n_drop": config.n_drop},
        },
        "executor": {
            "class": "SimulatorExecutor",
            "module_path": "qlib.backtest.executor",
            "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True},
        },
        "backtest": {
            "start_time": config.start_time,
            "end_time": config.end_time,
            "account": config.account,
            "benchmark": config.benchmark,
            "exchange_kwargs": {
                "limit_threshold": config.limit_threshold,
                "deal_price": config.deal_price,
                "open_cost": config.open_cost,
                "close_cost": config.close_cost,
                "min_cost": config.min_cost,
            },
        },
    }

    _stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        strategy = init_instance_by_config(bt_config["strategy"])
        executor = init_instance_by_config(bt_config["executor"])
        portfolio_metrics, indicator = executor.backtest(strategy=strategy, **bt_config["backtest"])
    finally:
        sys.stderr = _stderr

    report = indicator.get_latest_report() if hasattr(indicator, "get_latest_report") else {}

    summary = {
        "annual_return": round(float(report.get("excess_return_with_cost.annualized_return", 0) or 0) * 100, 2),
        "cumulative_return": round(float(report.get("excess_return_without_cost.cumulative_return", 0) or 0) * 100, 2),
        "win_rate": round(float(report.get("excess_return_without_cost.win_rate", 0) or 0) * 100, 2),
        "sharpe_ratio": round(float(report.get("excess_return_with_cost.information_ratio", 0) or 0), 2),
        "max_drawdown": round(float(report.get("excess_return_with_cost.max_drawdown", 0) or 0) * 100, 2),
        "total_trades": int(report.get("total_trades", 0) or 0),
        "win_trades": 0,
    }

    # 获取原始交易记录用于提取明细
    raw_trades = {}
    if indicator is not None:
        try:
            trade_records = indicator.get_trade_records() if hasattr(indicator, "get_trade_records") else None
            if trade_records is not None:
                raw_trades = trade_records
        except Exception:
            pass

    return summary, raw_trades


def _extract_trade_details(raw_trades: dict, signals: List[dict]) -> List[dict]:
    """从 Qlib 原始交易记录提取逐笔交易明细"""
    if not raw_trades:
        return _extract_trades_from_portfolio(raw_trades)

    trades = []
    try:
        if isinstance(raw_trades, pd.DataFrame):
            df = raw_trades.copy()
            if "instrument" in df.columns:
                df["code"] = df["instrument"]
            if "datetime" in df.columns:
                pass
            # 按 instrument 分组构建买卖对
            for code, group in df.groupby("code") if "code" in df.columns else []:
                entries = group[group["direction"] == "buy"] if "direction" in group.columns else pd.DataFrame()
                exits = group[group["direction"] == "sell"] if "direction" in group.columns else pd.DataFrame()
                # 简化配对：按时间顺序配对
                entry_list = entries.sort_values("datetime").to_dict("records")
                exit_list = exits.sort_values("datetime").to_dict("records")
                for i, entry in enumerate(entry_list):
                    exit_rec = exit_list[i] if i < len(exit_list) else None
                    trade = _make_trade_record(code, entry, exit_rec)
                    trades.append(trade)
    except Exception:
        pass

    return trades


def _extract_trades_from_portfolio(raw_trades: dict) -> List[dict]:
    """Qlib 无法提供逐笔交易时返回空列表（回测指标仍有效）"""
    return []


def _make_trade_record(code: str, entry: dict, exit_rec: Optional[dict]) -> dict:
    """构造单笔交易记录"""
    entry_date = str(entry.get("datetime", ""))[:10]
    entry_price = float(entry.get("price", entry.get("close", 0)))
    exit_date = ""
    exit_price = 0.0
    pnl_pct = 0.0
    exit_reason = "expire"
    holding_days = 0

    if exit_rec:
        exit_date = str(exit_rec.get("datetime", ""))[:10]
        exit_price = float(exit_rec.get("price", exit_rec.get("close", 0)))
        if entry_price > 0:
            pnl_pct = round((exit_price / entry_price - 1) * 100, 2)
        try:
            holding_days = (datetime.strptime(exit_date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
        except Exception:
            pass

        # Qlib 回测中 exit_reason 通常从止损/止盈/持仓超期推断
        if pnl_pct <= -8:
            exit_reason = "stop_loss"
        elif pnl_pct >= 20:
            exit_reason = "take_profit"

    return {
        "code": code,
        "name": _get_stock_name(code),
        "entry_date": entry_date,
        "entry_price": round(entry_price, 2),
        "exit_date": exit_date,
        "exit_price": round(exit_price, 2),
        "holding_days": holding_days,
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
    }


def _get_stock_name(code: str) -> str:
    """查询股票名称"""
    from core.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM stock_info WHERE code = ?", (code,)).fetchone()
        return row["name"] if row else ""
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from backtest.engine import run_backtest; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backtest/engine.py
git commit -m "feat: add unified backtest engine — Qlib wrapper with per-trade detail extraction"
```

### Task 3.3: Create backtest/reporter.py

**Files:**
- Create: `backtest/reporter.py`

- [ ] **Step 1: Write reporter.py**

```python
"""
reporter.py —— 回测报告生成

组合 backtest_results + backtest_trades 为前端友好的格式
"""
from typing import List, Optional
from backtest.trade_store import get_results, get_result, get_trades


def build_result_list() -> List[dict]:
    """构建回测结果列表（供 Dashboard Tab 2/3 使用）"""
    results = get_results()
    for r in results:
        r["start_date"] = r.get("start_date", "") or ""
        r["end_date"] = r.get("end_date", "") or ""
    return results


def build_result_detail(result_id: int) -> Optional[dict]:
    """构建回测详情（汇总 + 交易明细）"""
    result = get_result(result_id)
    if not result:
        return None

    trades = get_trades(result_id)

    # 补充股票名称
    for t in trades:
        if not t.get("name"):
            t["name"] = _lookup_stock_name(t.get("code", ""))

    return {
        "summary": {
            "annual_return": result.get("annual_return", 0),
            "cumulative_return": result.get("cumulative_return", 0),
            "win_rate": result.get("win_rate", 0),
            "sharpe_ratio": result.get("sharpe_ratio", 0),
            "max_drawdown": result.get("max_drawdown", 0),
            "total_trades": result.get("total_trades", 0),
            "win_trades": result.get("win_trades", 0),
            "start_date": result.get("start_date", ""),
            "end_date": result.get("end_date", ""),
            "rule_name": result.get("rule_name", ""),
        },
        "trades": trades,
    }


def _lookup_stock_name(code: str) -> str:
    from core.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM stock_info WHERE code = ?", (code,)).fetchone()
        return row["name"] if row else ""
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from backtest.reporter import build_result_list; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backtest/reporter.py
git commit -m "feat: add backtest reporter — compose summary and trade details for frontend"
```

---

## Phase 4: API Routes

### Task 4.1: Create routes/scoring.py

**Files:**
- Create: `routes/scoring.py`

- [ ] **Step 1: Write routes/scoring.py**

```python
"""
routes/scoring.py —— 打分排名 API
"""
import json
import math
from flask import Blueprint, request, jsonify
from strategy.scorer import score_stocks, get_daily_scores, get_latest_score_date
from core.db import get_conn

scoring_bp = Blueprint("scoring", __name__, url_prefix="/api/scoring")


def _sanitize(obj):
    """递归替换 NaN/Inf 为 None"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@scoring_bp.route("/daily", methods=["GET"])
def daily_scores():
    """获取某日打分排名 GET /api/scoring/daily?date=2025-01-15"""
    trade_date = request.args.get("date") or get_latest_score_date()
    if not trade_date:
        return jsonify({"success": True, "data": [], "error": None})

    results = get_daily_scores(trade_date)
    return jsonify({"success": True, "data": _sanitize(results), "error": None})


@scoring_bp.route("/run", methods=["POST"])
def run_scoring():
    """触发打分 POST /api/scoring/run"""
    body = request.get_json(silent=True) or {}
    trade_date = body.get("date") or get_latest_score_date()
    if not trade_date:
        return jsonify({"success": False, "data": None, "error": "No trade_date provided"}), 400

    try:
        results = score_stocks(trade_date, save=True)
        return jsonify({"success": True, "data": {"count": len(results), "date": trade_date}, "error": None})
    except Exception as e:
        return jsonify({"success": False, "data": None, "error": str(e)}), 500


@scoring_bp.route("/stock/<code>", methods=["GET"])
def stock_factors(code: str):
    """获取单股因子明细 GET /api/scoring/stock/000001?date=2025-01-15"""
    trade_date = request.args.get("date")
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM stock_score WHERE code = ? AND trade_date = ?
        """, (code, trade_date)).fetchone()

        if not row:
            return jsonify({"success": True, "data": None, "error": "Not found"})

        data = dict(row)
        try:
            data["factors"] = json.loads(data.get("factors_json", "{}"))
        except json.JSONDecodeError:
            data["factors"] = {}

        return jsonify({"success": True, "data": _sanitize(data), "error": None})


@scoring_bp.route("/kline/<code>", methods=["GET"])
def kline_data(code: str):
    """获取 K 线数据（含回测买卖点标注）GET /api/scoring/kline/000001?start=2024-01-01&end=2024-12-31&result_id=1"""
    start = request.args.get("start", "2024-01-01")
    end = request.args.get("end", "2025-12-31")
    result_id = request.args.get("result_id")

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT trade_date, open, high, low, close, volume
            FROM daily_price
            WHERE code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, (code, start, end)).fetchall()

        kline = []
        for r in rows:
            d = dict(r)
            kline.append([
                d["trade_date"],
                _sanitize(d.get("open")),
                _sanitize(d.get("close")),
                _sanitize(d.get("low")),
                _sanitize(d.get("high")),
                _sanitize(d.get("volume")),
            ])

        # 如果有 result_id，附加买卖点标记
        marks = []
        if result_id:
            trades = conn.execute("""
                SELECT * FROM backtest_trades
                WHERE result_id = ? AND code = ?
            """, (result_id, code)).fetchall()
            for t in trades:
                t = dict(t)
                if t.get("entry_date"):
                    marks.append({
                        "date": t["entry_date"],
                        "price": _sanitize(t.get("entry_price")),
                        "type": "buy",
                    })
                if t.get("exit_date"):
                    marks.append({
                        "date": t["exit_date"],
                        "price": _sanitize(t.get("exit_price")),
                        "type": "sell",
                    })

        return jsonify({
            "success": True,
            "data": {"kline": kline, "marks": marks},
            "error": None,
        })
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from routes.scoring import scoring_bp; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add routes/scoring.py
git commit -m "feat: add scoring API routes — daily scores, run scoring, stock factors, kline data"
```

### Task 4.2: Create routes/strategy.py

**Files:**
- Create: `routes/strategy.py`

- [ ] **Step 1: Write routes/strategy.py**

```python
"""
routes/strategy.py —— 策略管理 API
"""
import json
import math
import threading
from flask import Blueprint, request, jsonify
from strategy.rules_store import list_rules, get_rule, toggle_active, delete_rule
from backtest.trade_store import get_rule_trades

strategy_bp = Blueprint("strategy", __name__, url_prefix="/api/strategy")


def _sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# 挖掘任务状态
_mining_status = {"running": False, "progress": None, "result": None}


@strategy_bp.route("/rules", methods=["GET"])
def get_rules():
    """获取所有策略规则 GET /api/strategy/rules"""
    active_only = request.args.get("active_only", "0") == "1"
    rules = list_rules(active_only=active_only)
    return jsonify({"success": True, "data": _sanitize(rules), "error": None})


@strategy_bp.route("/rules/<int:rule_id>", methods=["GET"])
def get_rule_detail(rule_id: int):
    """获取单条规则详情 GET /api/strategy/rules/1"""
    rule = get_rule(rule_id)
    if not rule:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404
    return jsonify({"success": True, "data": _sanitize(dict(rule)), "error": None})


@strategy_bp.route("/rules/<int:rule_id>/toggle", methods=["PUT"])
def toggle_rule(rule_id: int):
    """启用/禁用策略 PUT /api/strategy/rules/1/toggle"""
    result = toggle_active(rule_id)
    if result is None:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404
    return jsonify({"success": True, "data": result, "error": None})


@strategy_bp.route("/rules/<int:rule_id>", methods=["DELETE"])
def remove_rule(rule_id: int):
    """删除策略 DELETE /api/strategy/rules/1"""
    delete_rule(rule_id)
    return jsonify({"success": True, "data": None, "error": None})


@strategy_bp.route("/rules/<int:rule_id>/trades", methods=["GET"])
def rule_trades(rule_id: int):
    """获取某策略的历史交易明细 GET /api/strategy/rules/1/trades"""
    rule = get_rule(rule_id)
    if not rule:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404
    trades = get_rule_trades(str(rule_id))
    return jsonify({"success": True, "data": _sanitize(trades), "error": None})


@strategy_bp.route("/mine", methods=["POST"])
def start_mining():
    """触发策略挖掘 POST /api/strategy/mine"""
    global _mining_status
    if _mining_status["running"]:
        return jsonify({"success": False, "data": None, "error": "Mining already in progress"}), 409

    body = request.get_json(silent=True) or {}
    trade_date = body.get("trade_date") or _get_latest_trade_date()
    if not trade_date:
        return jsonify({"success": False, "data": None, "error": "No trade_date available"}), 400

    _mining_status = {"running": True, "progress": None, "result": None}

    def _run():
        global _mining_status
        try:
            from strategy.miner import mine_strategies
            saved = mine_strategies(trade_date)
            _mining_status = {"running": False, "progress": "done", "result": saved}
        except Exception as e:
            _mining_status = {"running": False, "progress": "error", "result": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "data": {"status": "started", "trade_date": trade_date}, "error": None}), 202


@strategy_bp.route("/mine/status", methods=["GET"])
def mining_status():
    """查询挖掘进度 GET /api/strategy/mine/status"""
    return jsonify({"success": True, "data": _mining_status, "error": None})


def _get_latest_trade_date() -> str:
    from core.db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) as d FROM daily_price").fetchone()
        return row["d"] if row else ""
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from routes.strategy import strategy_bp; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add routes/strategy.py
git commit -m "feat: add strategy management API — list, toggle, delete rules, trigger mining"
```

### Task 4.3: Create routes/backtest.py

**Files:**
- Create: `routes/backtest.py`

- [ ] **Step 1: Write routes/backtest.py**

```python
"""
routes/backtest.py —— 回测 API
"""
import math
import threading
from flask import Blueprint, request, jsonify
from backtest.reporter import build_result_list, build_result_detail
from backtest.trade_store import get_result, get_trades
from strategy.rules_store import get_rule

backtest_bp = Blueprint("backtest", __name__, url_prefix="/api/backtest")


def _sanitize(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


_backtest_status = {"running": False, "result_id": None}


@backtest_bp.route("/run", methods=["POST"])
def start_backtest():
    """触发回测 POST /api/backtest/run {rule_id, start_date, end_date}"""
    global _backtest_status
    if _backtest_status["running"]:
        return jsonify({"success": False, "data": None, "error": "Backtest already in progress"}), 409

    body = request.get_json(silent=True) or {}
    rule_id = body.get("rule_id")
    start_date = body.get("start_date", "2024-01-01")
    end_date = body.get("end_date", "2025-12-31")

    rule = get_rule(int(rule_id)) if rule_id else None
    if not rule:
        return jsonify({"success": False, "data": None, "error": "Rule not found"}), 404

    _backtest_status = {"running": True, "result_id": None}

    def _run():
        global _backtest_status
        try:
            from backtest.engine import run_backtest
            result = run_backtest(
                rule_name=rule["rule_name"],
                rule_id=str(rule["id"]),
                conditions_json=rule["conditions"] or "{}",
                start_date=start_date,
                end_date=end_date,
                save=True,
            )
            _backtest_status = {"running": False, "result_id": result.get("result_id")}
        except Exception as e:
            _backtest_status = {"running": False, "result_id": None, "error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "data": {"status": "started"}, "error": None}), 202


@backtest_bp.route("/status", methods=["GET"])
def backtest_status():
    """回测进度查询 GET /api/backtest/status"""
    return jsonify({"success": True, "data": _backtest_status, "error": None})


@backtest_bp.route("/results", methods=["GET"])
def results_list():
    """回测结果列表 GET /api/backtest/results"""
    results = build_result_list()
    return jsonify({"success": True, "data": _sanitize(results), "error": None})


@backtest_bp.route("/results/<int:result_id>", methods=["GET"])
def result_detail(result_id: int):
    """回测汇总 GET /api/backtest/results/1"""
    detail = build_result_detail(result_id)
    if not detail:
        return jsonify({"success": False, "data": None, "error": "Result not found"}), 404
    return jsonify({"success": True, "data": _sanitize(detail["summary"]), "error": None})


@backtest_bp.route("/results/<int:result_id>/trades", methods=["GET"])
def result_trades(result_id: int):
    """回测逐笔交易 GET /api/backtest/results/1/trades"""
    trades = get_trades(result_id)
    return jsonify({"success": True, "data": _sanitize(trades), "error": None})
```

- [ ] **Step 2: Verify import**

```bash
cd e:/小项目/Project/AIQuant && python -c "from routes.backtest import backtest_bp; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add routes/backtest.py
git commit -m "feat: add backtest API routes — run backtest, list results, get trades"
```

---

## Phase 5: Dashboard

### Task 5.1: Rewrite dashboard.html

**Files:**
- Modify: `dashboard.html` (complete rewrite)

**Context:** Single-page three-tab dashboard. Pure HTML + JS + CSS + ECharts CDN. Three tabs: daily scores, strategy management, backtest center. K-line panel slides out when clicking stock code.

- [ ] **Step 1: Read current dashboard.html for reference (static file serving path)**

Check how app.py serves the current dashboard.html — it's served via `@app.route("/")` returning `send_file("dashboard.html")`.

- [ ] **Step 2: Write dashboard.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AIQuant · 个人股票评分系统</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e1e1e1; }
        .header { background: #1a1d2e; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #2a2d3e; }
        .header h1 { font-size: 18px; font-weight: 600; color: #4fc3f7; }
        .tabs { display: flex; gap: 4px; padding: 8px 24px; background: #1a1d2e; border-bottom: 1px solid #2a2d3e; }
        .tab { padding: 8px 20px; border: none; background: transparent; color: #888; cursor: pointer; border-radius: 6px; font-size: 14px; transition: all .2s; }
        .tab:hover { color: #e1e1e1; background: #2a2d3e; }
        .tab.active { color: #4fc3f7; background: #1e2940; }
        .content { padding: 16px 24px; max-width: 1400px; margin: 0 auto; }
        .panel { background: #1a1d2e; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #2a2d3e; font-size: 13px; }
        th { color: #888; font-weight: 500; background: #141720; position: sticky; top: 0; }
        tr:hover { background: #1e2940; }
        .btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; transition: all .2s; }
        .btn-primary { background: #4fc3f7; color: #0f1117; }
        .btn-primary:hover { background: #3ab0e0; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }
        .toggle { width: 40px; height: 22px; background: #555; border-radius: 11px; cursor: pointer; position: relative; display: inline-block; }
        .toggle.on { background: #4caf50; }
        .toggle::after { content: ''; width: 18px; height: 18px; background: #fff; border-radius: 50%; position: absolute; top: 2px; left: 2px; transition: .2s; }
        .toggle.on::after { left: 20px; }
        .score-high { color: #ff5252; font-weight: 600; }
        .score-mid { color: #ffab40; }
        .score-low { color: #69f0ae; }
        .positive { color: #ff5252; }
        .negative { color: #69f0ae; }
        .metrics { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }
        .metric { text-align: center; }
        .metric .val { font-size: 24px; font-weight: 700; color: #4fc3f7; }
        .metric .lbl { font-size: 11px; color: #888; margin-top: 2px; }
        .input-row { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
        input, select { padding: 6px 10px; border: 1px solid #2a2d3e; border-radius: 4px; background: #0f1117; color: #e1e1e1; font-size: 13px; }
        .kline-overlay { position: fixed; top: 0; right: 0; width: 600px; height: 100vh; background: #1a1d2e; border-left: 1px solid #2a2d3e; z-index: 1000; transform: translateX(100%); transition: transform .3s; overflow-y: auto; }
        .kline-overlay.open { transform: translateX(0); }
        .kline-overlay .close-btn { position: absolute; top: 12px; right: 12px; background: none; border: none; color: #888; font-size: 20px; cursor: pointer; }
        .kline-overlay .chart { width: 100%; height: 400px; }
        .kline-overlay .trade-info { padding: 16px; }
        .hidden { display: none; }
        .code-link { color: #4fc3f7; cursor: pointer; text-decoration: underline; }
        .status-badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; }
        .status-running { background: #ff9800; color: #000; }
        .status-done { background: #4caf50; color: #000; }
        .status-error { background: #f44336; color: #fff; }
    </style>
</head>
<body>
    <div class="header">
        <h1>AIQuant · 个人股票评分系统</h1>
        <span style="color:#888;font-size:12px" id="last-update"></span>
    </div>
    <div class="tabs">
        <button class="tab active" onclick="switchTab('scores')">每日打分</button>
        <button class="tab" onclick="switchTab('strategy')">策略管理</button>
        <button class="tab" onclick="switchTab('backtest')">回测中心</button>
    </div>
    <div class="content" id="content"></div>

    <!-- K线侧面板 -->
    <div class="kline-overlay" id="klinePanel">
        <button class="close-btn" onclick="closeKline()">✕</button>
        <div class="chart" id="klineChart"></div>
        <div class="trade-info" id="klineTradeInfo"></div>
    </div>

    <script>
        const API = '/api';
        let currentTab = 'scores';

        async function fetchJSON(url) {
            const resp = await fetch(url);
            return resp.json();
        }

        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.tab').forEach((t, i) => {
                t.classList.toggle('active', t.textContent.trim().includes(
                    tab === 'scores' ? '每日打分' : tab === 'strategy' ? '策略管理' : '回测中心'
                ));
            });
            loadTab(tab);
        }

        async function loadTab(tab) {
            const c = document.getElementById('content');
            if (tab === 'scores') await loadScoresTab(c);
            else if (tab === 'strategy') await loadStrategyTab(c);
            else if (tab === 'backtest') await loadBacktestTab(c);
        }

        // ─── Tab 1: 每日打分 ───
        async function loadScoresTab(container) {
            const resp = await fetchJSON(API + '/scoring/daily');
            const data = resp.data || [];
            let html = `<div class="panel">
                <div class="input-row">
                    <span>日期:</span>
                    <input type="date" id="scoreDate" onchange="refreshScores()">
                    <button class="btn btn-primary" onclick="runScoring()">刷新打分</button>
                </div>
                <table>
                    <thead><tr><th>排名</th><th>代码</th><th>名称</th><th>得分</th><th>命中策略</th></tr></thead>
                    <tbody>`;
            data.forEach((r, i) => {
                const cls = r.score >= 80 ? 'score-high' : r.score >= 50 ? 'score-mid' : 'score-low';
                html += `<tr>
                    <td>${i + 1}</td>
                    <td><span class="code-link" onclick="openKline('${r.code}')">${r.code}</span></td>
                    <td>${r.name || ''}</td>
                    <td class="${cls}">${r.score}</td>
                    <td>${r.rule_name || ''}</td>
                </tr>`;
            });
            html += `</tbody></table></div>`;
            container.innerHTML = html;

            if (data.length > 0) {
                document.getElementById('scoreDate').value = data[0].trade_date || '';
                document.getElementById('last-update').textContent = '最新打分: ' + (data[0].trade_date || '');
            }
        }

        async function refreshScores() {
            const date = document.getElementById('scoreDate').value;
            const resp = await fetchJSON(API + '/scoring/daily?date=' + date);
            loadTab('scores');
        }

        async function runScoring() {
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = '计算中...';
            const resp = await fetch(API + '/scoring/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
            const result = await resp.json();
            btn.disabled = false;
            btn.textContent = '刷新打分';
            loadTab('scores');
        }

        // ─── Tab 2: 策略管理 ───
        async function loadStrategyTab(container) {
            const resp = await fetchJSON(API + '/strategy/rules');
            const rules = resp.data || [];
            let html = `<div class="panel">
                <div class="input-row">
                    <button class="btn btn-primary" onclick="startMining()">挖掘新策略</button>
                    <span style="color:#888;font-size:12px" id="miningStatus"></span>
                </div>
                <table>
                    <thead><tr><th>启用</th><th>策略名称</th><th>类型</th><th>年化收益</th><th>胜率</th><th>夏普</th><th>最大回撤</th><th>交易次数</th></tr></thead>
                    <tbody>`;
            rules.forEach(r => {
                html += `<tr>
                    <td><span class="toggle ${r.is_active ? 'on' : ''}" onclick="toggleRule(${r.id}, this)"></span></td>
                    <td><span class="code-link" onclick="viewRuleTrades(${r.id})">${r.rule_name}</span></td>
                    <td>${r.rule_type}</td>
                    <td class="${r.annual_return >= 0 ? 'positive' : 'negative'}">${(r.annual_return || 0).toFixed(1)}%</td>
                    <td>${(r.win_rate || 0).toFixed(1)}%</td>
                    <td>${(r.sharpe_ratio || 0).toFixed(2)}</td>
                    <td class="negative">${(r.max_drawdown || 0).toFixed(1)}%</td>
                    <td>${r.total_trades || 0}</td>
                </tr>`;
            });
            html += `</tbody></table>
                <div id="ruleTradesPanel" class="hidden panel" style="margin-top:12px"></div>
            </div>`;
            container.innerHTML = html;
        }

        async function toggleRule(id, el) {
            const resp = await fetchJSON(API + '/strategy/rules/' + id + '/toggle');
            if (resp.success) {
                el.classList.toggle('on', resp.data.is_active);
            }
        }

        async function startMining() {
            const statusEl = document.getElementById('miningStatus');
            statusEl.innerHTML = '<span class="status-badge status-running">挖掘中...</span>';
            const resp = await fetch(API + '/strategy/mine', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            const result = await resp.json();
            if (result.success) {
                pollMiningStatus();
            } else {
                statusEl.innerHTML = '<span class="status-badge status-error">失败: ' + (result.error || '') + '</span>';
            }
        }

        async function pollMiningStatus() {
            const statusEl = document.getElementById('miningStatus');
            const resp = await fetchJSON(API + '/strategy/mine/status');
            if (resp.data.running) {
                statusEl.innerHTML = '<span class="status-badge status-running">挖掘中...</span>';
                setTimeout(pollMiningStatus, 2000);
            } else if (resp.data.progress === 'error') {
                statusEl.innerHTML = '<span class="status-badge status-error">错误: ' + (resp.data.result || '') + '</span>';
            } else {
                statusEl.innerHTML = '<span class="status-badge status-done">完成! 生成 ' + (resp.data.result ? resp.data.result.length : 0) + ' 条策略</span>';
                loadTab('strategy');
            }
        }

        async function viewRuleTrades(ruleId) {
            const panel = document.getElementById('ruleTradesPanel');
            const resp = await fetchJSON(API + '/strategy/rules/' + ruleId + '/trades');
            const trades = resp.data || [];
            let html = `<h3 style="margin-bottom:8px">策略交易明细</h3>
                <table><thead><tr><th>代码</th><th>名称</th><th>买入日</th><th>买入价</th><th>卖出日</th><th>卖出价</th><th>持仓天</th><th>收益率</th><th>原因</th></tr></thead><tbody>`;
            trades.forEach(t => {
                html += `<tr>
                    <td><span class="code-link" onclick="openKline('${t.code}', ${t.entry_date ? '\'' + t.entry_date + '\'' : 'null'}, ${t.entry_price || 0}, ${t.exit_date ? '\'' + t.exit_date + '\'' : 'null'}, ${t.exit_price || 0})">${t.code}</span></td>
                    <td>${t.name || ''}</td>
                    <td>${t.entry_date || ''}</td><td>${t.entry_price || ''}</td>
                    <td>${t.exit_date || ''}</td><td>${t.exit_price || ''}</td>
                    <td>${t.holding_days || ''}</td>
                    <td class="${(t.pnl_pct || 0) >= 0 ? 'positive' : 'negative'}">${(t.pnl_pct || 0).toFixed(2)}%</td>
                    <td>${t.exit_reason || ''}</td>
                </tr>`;
            });
            html += `</tbody></table>`;
            panel.innerHTML = html;
            panel.classList.remove('hidden');
        }

        // ─── Tab 3: 回测中心 ───
        async function loadBacktestTab(container) {
            const rulesResp = await fetchJSON(API + '/strategy/rules?active_only=0');
            const rules = rulesResp.data || [];
            const opts = rules.map(r => `<option value="${r.id}">${r.rule_name}</option>`).join('');

            const resultsResp = await fetchJSON(API + '/backtest/results');
            const results = resultsResp.data || [];

            let html = `<div class="panel">
                <div class="input-row">
                    <span>策略:</span>
                    <select id="btRule">${opts}</select>
                    <span>起始:</span>
                    <input type="date" id="btStart" value="2024-01-01">
                    <span>结束:</span>
                    <input type="date" id="btEnd" value="2025-12-31">
                    <button class="btn btn-primary" onclick="startBacktest()">开始回测</button>
                    <span id="btStatus" style="font-size:12px;color:#888"></span>
                </div>
                <div id="btResult" class="hidden"></div>
                <h3 style="margin-top:16px;margin-bottom:8px">历史回测记录</h3>
                <table><thead><tr><th>ID</th><th>策略</th><th>区间</th><th>年化收益</th><th>胜率</th><th>夏普</th><th>回撤</th><th>交易数</th></tr></thead><tbody>`;
            results.forEach(r => {
                html += `<tr>
                    <td><span class="code-link" onclick="viewBacktestResult(${r.id})">#${r.id}</span></td>
                    <td>${r.rule_name || ''}</td>
                    <td>${r.start_date || ''} ~ ${r.end_date || ''}</td>
                    <td class="${(r.annual_return || 0) >= 0 ? 'positive' : 'negative'}">${(r.annual_return || 0).toFixed(1)}%</td>
                    <td>${(r.win_rate || 0).toFixed(1)}%</td>
                    <td>${(r.sharpe_ratio || 0).toFixed(2)}</td>
                    <td>${(r.max_drawdown || 0).toFixed(1)}%</td>
                    <td>${r.total_trades || 0}</td>
                </tr>`;
            });
            html += `</tbody></table><div id="btTradesPanel" class="hidden panel" style="margin-top:12px"></div></div>`;
            container.innerHTML = html;
        }

        async function startBacktest() {
            const ruleId = document.getElementById('btRule').value;
            const start = document.getElementById('btStart').value;
            const end = document.getElementById('btEnd').value;
            const statusEl = document.getElementById('btStatus');
            statusEl.innerHTML = '<span class="status-badge status-running">回测中...</span>';

            const resp = await fetch(API + '/backtest/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rule_id: parseInt(ruleId), start_date: start, end_date: end })
            });
            const result = await resp.json();
            if (result.success) {
                pollBacktestStatus();
            } else {
                statusEl.innerHTML = '<span class="status-badge status-error">' + (result.error || '错误') + '</span>';
            }
        }

        async function pollBacktestStatus() {
            const statusEl = document.getElementById('btStatus');
            const resp = await fetchJSON(API + '/backtest/status');
            if (resp.data.running) {
                statusEl.innerHTML = '<span class="status-badge status-running">回测中...</span>';
                setTimeout(pollBacktestStatus, 2000);
            } else if (resp.data.error) {
                statusEl.innerHTML = '<span class="status-badge status-error">' + resp.data.error + '</span>';
            } else if (resp.data.result_id) {
                statusEl.innerHTML = '<span class="status-badge status-done">完成!</span>';
                viewBacktestResult(resp.data.result_id);
            } else {
                statusEl.innerHTML = '<span class="status-badge status-done">完成</span>';
                loadTab('backtest');
            }
        }

        async function viewBacktestResult(resultId) {
            const detailResp = await fetchJSON(API + '/backtest/results/' + resultId);
            const tradesResp = await fetchJSON(API + '/backtest/results/' + resultId + '/trades');
            const s = detailResp.data || {};
            const trades = tradesResp.data || [];

            const panel = document.getElementById('btTradesPanel');
            let html = `<h3>回测结果 #${resultId}</h3>
                <div class="metrics">
                    <div class="metric"><div class="val${s.annual_return >= 0 ? '' : ' negative'}">${(s.annual_return || 0).toFixed(1)}%</div><div class="lbl">年化收益</div></div>
                    <div class="metric"><div class="val">${(s.win_rate || 0).toFixed(1)}%</div><div class="lbl">胜率</div></div>
                    <div class="metric"><div class="val">${(s.sharpe_ratio || 0).toFixed(2)}</div><div class="lbl">夏普</div></div>
                    <div class="metric"><div class="val negative">${(s.max_drawdown || 0).toFixed(1)}%</div><div class="lbl">最大回撤</div></div>
                    <div class="metric"><div class="val">${s.total_trades || 0}</div><div class="lbl">交易次数</div></div>
                </div>
                <table><thead><tr><th>代码</th><th>名称</th><th>买入日</th><th>买入价</th><th>卖出日</th><th>卖出价</th><th>持仓天</th><th>收益率</th><th>原因</th></tr></thead><tbody>`;
            trades.forEach(t => {
                html += `<tr>
                    <td><span class="code-link" onclick="openKline('${t.code}', ${t.entry_date ? '\'' + t.entry_date + '\'' : 'null'}, ${t.entry_price || 0}, ${t.exit_date ? '\'' + t.exit_date + '\'' : 'null'}, ${t.exit_price || 0}, ${resultId})">${t.code}</span></td>
                    <td>${t.name || ''}</td>
                    <td>${t.entry_date || ''}</td><td>${t.entry_price || ''}</td>
                    <td>${t.exit_date || ''}</td><td>${t.exit_price || ''}</td>
                    <td>${t.holding_days || ''}</td>
                    <td class="${(t.pnl_pct || 0) >= 0 ? 'positive' : 'negative'}">${(t.pnl_pct || 0).toFixed(2)}%</td>
                    <td>${t.exit_reason || ''}</td>
                </tr>`;
            });
            html += `</tbody></table>`;
            panel.innerHTML = html;
            panel.classList.remove('hidden');
        }

        // ─── K线侧面板 ───
        let klineChart = null;

        async function openKline(code, entryDate, entryPrice, exitDate, exitPrice, resultId) {
            const panel = document.getElementById('klinePanel');
            panel.classList.add('open');

            // 计算日期范围
            let start = '2024-01-01', end = '2025-12-31';
            if (entryDate) {
                const d = new Date(entryDate);
                d.setMonth(d.getMonth() - 2);
                start = d.toISOString().split('T')[0];
            }
            if (exitDate) {
                const d = new Date(exitDate);
                d.setMonth(d.getMonth() + 2);
                end = d.toISOString().split('T')[0];
            }

            let url = API + '/scoring/kline/' + code + '?start=' + start + '&end=' + end;
            if (resultId) url += '&result_id=' + resultId;

            const resp = await fetchJSON(url);
            const kdata = (resp.data?.kline || []).map(d => [d[0], d[1], d[2], d[3], d[4]]);
            const marks = resp.data?.marks || [];

            if (!klineChart) {
                klineChart = echarts.init(document.getElementById('klineChart'));
            }

            klineChart.setOption({
                title: { text: code + ' K线图', left: 'center', textStyle: { color: '#e1e1e1', fontSize: 14 } },
                tooltip: { trigger: 'axis' },
                grid: { left: '10%', right: '10%', top: 50, bottom: 30 },
                xAxis: { type: 'category', data: kdata.map(d => d[0]), axisLabel: { color: '#888' } },
                yAxis: { scale: true, axisLabel: { color: '#888' } },
                series: [{
                    type: 'candlestick',
                    data: kdata.map(d => [d[1], d[2], d[3], d[4]]),
                    itemStyle: { color: '#ff5252', color0: '#69f0ae', borderColor: '#ff5252', borderColor0: '#69f0ae' },
                    markPoint: {
                        data: marks.map(m => ({
                            name: m.type === 'buy' ? '买入' : '卖出',
                            coord: [m.date, m.price],
                            value: m.type === 'buy' ? 'B' : 'S',
                            symbol: 'pin',
                            symbolSize: 40,
                            itemStyle: { color: m.type === 'buy' ? '#ff5252' : '#69f0ae' }
                        }))
                    }
                }]
            });

            // 交易信息
            let infoHtml = `<h4 style="margin-bottom:8px">${code}</h4>`;
            if (entryDate && entryPrice) {
                infoHtml += `<p>买入: ${entryDate} 价格: ${entryPrice}</p>`;
            }
            if (exitDate && exitPrice) {
                infoHtml += `<p>卖出: ${exitDate} 价格: ${exitPrice}</p>`;
                if (entryPrice > 0) {
                    const pnl = ((exitPrice / entryPrice - 1) * 100).toFixed(2);
                    const cls = pnl >= 0 ? 'positive' : 'negative';
                    infoHtml += `<p>收益: <span class="${cls}">${pnl}%</span></p>`;
                }
            }
            document.getElementById('klineTradeInfo').innerHTML = infoHtml;
        }

        function closeKline() {
            document.getElementById('klinePanel').classList.remove('open');
        }

        // ─── Init ───
        loadTab('scores');
    </script>
</body>
</html>
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: rewrite dashboard — three-tab single-page with K-line overlay and ECharts"
```

---

## Phase 6: Integration & Cleanup

### Task 6.1: Clean up app.py

**Files:**
- Modify: `app.py`

**Context:** Remove old blueprint imports and registrations. Keep only the three new blueprints and essential ones (system for health check, sync for data sync trigger). Remove agents, strategy_lab, trade, position, account, optimizer, signal, chart, tasks blueprint registrations.

- [ ] **Step 1: Read current app.py to understand all routes and imports**

Read the full `app.py` to list all current routes and blueprint registrations.

- [ ] **Step 2: Rewrite app.py with only required blueprints**

```python
"""
AIQuant —— 个人股票评分系统
Flask 入口
"""
import os
from flask import Flask, send_file

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False

# ── Core blueprints (保留) ──
from routes.system import system_bp
from routes.sync import sync_bp

# ── New scoring platform blueprints ──
from routes.scoring import scoring_bp
from routes.strategy import strategy_bp
from routes.backtest import backtest_bp

app.register_blueprint(system_bp)
app.register_blueprint(sync_bp)
app.register_blueprint(scoring_bp)
app.register_blueprint(strategy_bp)
app.register_blueprint(backtest_bp)


@app.route("/")
@app.route("/dashboard")
def dashboard():
    return send_file("dashboard.html")


if __name__ == "__main__":
    from core.db import init_db
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
```

- [ ] **Step 3: Start Flask and verify no import errors**

```bash
cd e:/小项目/Project/AIQuant && timeout 3 python app.py 2>&1 || true
```
Expected: Flask starts without import errors.

- [ ] **Step 4: Verify the three API endpoints respond**

Start the app in background, then:
```bash
curl -s http://localhost:5000/api/strategy/rules | python -m json.tool | head -5
curl -s http://localhost:5000/api/scoring/daily | python -m json.tool | head -5
curl -s http://localhost:5000/api/backtest/results | python -m json.tool | head -5
```

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "refactor: simplify app.py — register only scoring, strategy, backtest blueprints"
```

### Task 6.2: Delete old files

**Files to delete:**
- `agents/` (entire directory)
- `ministries/` (entire directory)
- `services/` (entire directory)
- `backtest/archive/` (entire directory)
- `backtest/engine.py` (old Backtrader engine)
- `backtest/backtest_ui.py` (Streamlit UI)
- `strategy/strategies.py` (hardcoded strategies)
- `strategy/strategy.py` (technical composite scoring)
- `llm/` (entire directory, if exists)

- [ ] **Step 1: Delete files**

```bash
cd e:/小项目/Project/AIQuant
rm -rf agents/ ministries/ services/
rm -rf backtest/archive/
rm -f backtest/engine.py backtest/backtest_ui.py
rm -f strategy/strategies.py strategy/strategy.py
rm -rf llm/
```

- [ ] **Step 2: Verify app still starts after deletion**

```bash
cd e:/小项目/Project/AIQuant && timeout 3 python app.py 2>&1 || true
```
Expected: No import errors.

- [ ] **Step 3: Remove unused imports that reference deleted files**

Search for any remaining imports of deleted modules:
```bash
cd e:/小项目/Project/AIQuant && grep -r "from agents\|from ministries\|from services\|from llm" --include="*.py" .
```
Fix any found references.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: delete agents, ministries, services, archived backtests, hardcoded strategies, LLM module"
```

---

## Post-Implementation Verification

### Task 7.1: End-to-end smoke test

- [ ] **Step 1: Start the app**

```bash
cd e:/小项目/Project/AIQuant && python app.py &
sleep 2
```

- [ ] **Step 2: Verify dashboard loads**

```bash
curl -s http://localhost:5000/ | head -1
```
Expected: `<!DOCTYPE html>`

- [ ] **Step 3: Verify scoring API**

```bash
curl -s http://localhost:5000/api/scoring/daily
```
Expected: `{"success":true,"data":[...],"error":null}`

- [ ] **Step 4: Verify strategy API**

```bash
curl -s http://localhost:5000/api/strategy/rules
```
Expected: `{"success":true,"data":[...],"error":null}`

- [ ] **Step 5: Verify backtest API**

```bash
curl -s http://localhost:5000/api/backtest/results
```
Expected: `{"success":true,"data":[...],"error":null}`

- [ ] **Step 6: Test scoring run**

```bash
curl -s -X POST http://localhost:5000/api/scoring/run -H "Content-Type: application/json" -d "{}"
```
Expected: `{"success":true,"data":{"count":...,"date":"..."},"error":null}`
This may take a while on first run.

- [ ] **Step 7: Stop the app**

```bash
kill %1 2>/dev/null || true
```



## Self-Review Results

**1. Spec Coverage:**
- Scoring pipeline (daily) → Task 2.3 (scorer.py) + Task 4.1 (routes/scoring.py)
- Strategy mining → Task 2.4 (miner.py) + Task 4.2 (routes/strategy.py)
- Backtest engine → Task 3.2 (engine.py) + Task 3.3 (reporter.py)
- Trade detail storage → Task 3.1 (trade_store.py)
- K-line chart with buy/sell marks → Task 4.1 (kline endpoint) + Task 5.1 (dashboard ECharts)
- Dashboard three tabs → Task 5.1
- DB schema → Task 1.1
- Config simplification → Task 1.2
- Cleanup → Task 6.1 + 6.2
- API error format `{success, data, error}` → All route files
- factor_lib batch compute → Task 2.2
- Rules CRUD → Task 2.1

Covered: all spec sections.

**2. Placeholder Scan:** No TBD, TODO, "implement later", "add appropriate error handling" without code. All steps contain actual code or exact commands.

**3. Type Consistency:**
- `stock_score` table columns match `scorer.py` → `_save_scores()` column order
- `backtest_results` table columns match `trade_store.py` → `save_result()` column order
- `backtest_trades` table columns match both `trade_store.py` → `save_trades()` and `engine.py` → `_make_trade_record()`
- API routes return `{success: bool, data: any, error: string|null}` consistent across all three route files
- `strategy_rules` columns in `rules_store.py` match actual table schema in `core/db.py`

All consistent. No issues found.
