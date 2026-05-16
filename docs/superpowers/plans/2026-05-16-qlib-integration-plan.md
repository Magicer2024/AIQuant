# Qlib Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AIQuant's self-built quant engine (factor_lib, indicators, backtest engines) with Microsoft Qlib while preserving Flask API, HTML dashboards, and agent pipeline metaphor.

**Architecture:** Qlib handles data/factor/model/backtest layers. AIQuant keeps SQLite metadata (rules, signals, strategies), Flask routes, HTML UI. A new `qlib_engine/` module bridges the two: migration tool, data bridge, and strategy adapter.

**Tech Stack:** Python 3.11+, Microsoft Qlib, Flask, SQLite, AkShare/baostock, LightGBM (existing), PyTorch (Qlib dependency)

**Spec:** `docs/superpowers/specs/2026-05-16-qlib-integration-design.md`

---

## File Structure

### New files (4)

```
qlib_engine/
├── __init__.py           # qlib.init() wrapper, singleton pattern
├── migration.py          # One-time: SQLite daily_price → Qlib binary
├── data_bridge.py        # Daily incremental: AkShare/baostock → Qlib binary
└── strategy_adapter.py   # Rule → Qlib Strategy + backtest runner
```

### Deleted files (16)

```
backtest/backtest.py, backtest/engine.py, backtest/backtest_v4.py,
backtest/custom_strategy_backtest.py, backtest/strategy_screen_backtest.py,
backtest/unified_engine.py, backtest/indicator_engine.py,
backtest/condition_builder.py, backtest/optimize_params.py,
backtest/param_optimizer.py, backtest/strategy_interface.py,
strategy/factor_lib.py, strategy/indicators.py, strategy/strategies.py,
ai/predictor.py, ai/features.py
```

### Modified files (18)

```
core/sync.py, core/db.py, core/data_fetcher.py,
strategy/rule_miner.py, strategy/genetic_evolver.py,
strategy/dynamic_selector.py, strategy/lgbm_ranker.py,
services/strategy_lab_service.py, services/backtest_service.py,
services/signal_service.py, services/data_fetcher.py,
agents/signal_agent.py, agents/data_agent.py, agents/backtest_agent.py,
routes/backtest.py, routes/backtest_new.py, routes/scoring.py,
routes/ai.py, app.py, requirements.txt
```

---

## Phase 1: Data Migration

### Task 1.1: Install Qlib and verify setup

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add Qlib to requirements**

```bash
pip install pyqlib
```

Add to `requirements.txt` after `lightgbm>=4.0.0`:

```
pyqlib>=0.9.0
numpy>=1.26.0
```

- [ ] **Step 2: Verify Qlib imports**

```python
python -c "import qlib; print(qlib.__version__)"
```

Expected: prints version number, no errors.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add pyqlib dependency"
```

---

### Task 1.2: Create qlib_engine/__init__.py

**Files:**
- Create: `qlib_engine/__init__.py`

- [ ] **Step 1: Create the init module**

```python
"""
qlib_engine/__init__.py —— Qlib 引擎初始化模块

职责：
  1. qlib.init() 封装
  2. 确保 Qlib 数据目录存在
  3. 提供 get_qlib_initialized() 状态查询
"""

import os
import qlib
from qlib.constant import REG_CN

_initialized = False
PROVIDER_URI = os.path.expanduser("~/.qlib/qlib_data/cn_data")


def init_qlib(provider_uri: str = None) -> None:
    """
    Initialize Qlib with the binary data directory.

    Idempotent — safe to call multiple times.
    """
    global _initialized
    if _initialized:
        return

    uri = provider_uri or PROVIDER_URI
    os.makedirs(uri, exist_ok=True)
    qlib.init(provider_uri=uri, region=REG_CN)
    _initialized = True


def is_initialized() -> bool:
    return _initialized


def get_provider_uri() -> str:
    return PROVIDER_URI
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from qlib_engine import init_qlib; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add qlib_engine/__init__.py
git commit -m "feat: add qlib_engine init module"
```

---

### Task 1.3: Create qlib_engine/migration.py

**Files:**
- Create: `qlib_engine/migration.py`

- [ ] **Step 1: Write the migration tool**

```python
"""
qlib_engine/migration.py —— 一次性数据迁移工具

用法：
  python -m qlib_engine.migration

将 SQLite daily_price + index_daily + stock_info 迁移到 Qlib 二进制格式。
迁移完成后 old tables 保留为只读备份，不自动删除。
"""

import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime
from collections import defaultdict

from core.db import get_conn, init_db
from qlib_engine import PROVIDER_URI


def export_instruments(output_dir: str) -> int:
    """
    Generate instruments/all.txt from stock_info table.

    Format: <code>\t<name>\t<listing_date>

    Returns number of instruments written.
    """
    os.makedirs(os.path.join(output_dir, "instruments"), exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM stock_info WHERE is_active=1 ORDER BY code"
        ).fetchall()

    path = os.path.join(output_dir, "instruments", "all.txt")
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            code = r["code"]
            name = r["name"]
            # Qlib format: code start_date end_date
            # Use 2024-01-01 as default listing date (A-share all listed before this)
            f.write(f"{code}\t{name}\t2024-01-01\t2099-12-31\n")
            count += 1

    print(f"[Migration] Wrote {count} instruments to {path}")
    return count


def export_calendars(output_dir: str) -> int:
    """
    Generate calendars/day.txt from index_daily or daily_price distinct trade_date.

    Returns number of trading days written.
    """
    os.makedirs(os.path.join(output_dir, "calendars"), exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM index_daily ORDER BY trade_date"
        ).fetchall()

    if not rows:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date"
        ).fetchall()

    path = os.path.join(output_dir, "calendars", "day.txt")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r['trade_date']}\n")

    print(f"[Migration] Wrote {len(rows)} trading days to {path}")
    return len(rows)


def export_features(output_dir: str) -> dict:
    """
    Export OHLCV data from daily_price to Qlib binary format.

    For each stock, creates:
      features/<code>/open.bin
      features/<code>/high.bin
      features/<code>/low.bin
      features/<code>/close.bin
      features/<code>/volume.bin

    Returns stats dict: {stocks_exported, total_rows}.
    """
    import struct

    features_dir = os.path.join(output_dir, "features")
    os.makedirs(features_dir, exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, trade_date, open, high, low, close, volume "
            "FROM daily_price ORDER BY code, trade_date"
        ).fetchall()

    if not rows:
        print("[Migration] No data in daily_price table")
        return {"stocks_exported": 0, "total_rows": 0}

    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    fields = ["open", "high", "low", "close", "volume"]
    stats = {"stocks_exported": 0, "total_rows": len(df)}

    for code, group in df.groupby("code"):
        code_dir = os.path.join(features_dir, code)
        os.makedirs(code_dir, exist_ok=True)

        group = group.sort_values("trade_date")

        for field in fields:
            values = group[field].fillna(0).values.astype(np.float32)
            bin_path = os.path.join(code_dir, f"{field}.bin")

            # Qlib .bin format: [number_of_elements (int32)] + [data (float32 * N)]
            with open(bin_path, "wb") as f:
                f.write(struct.pack("i", len(values)))
                f.write(values.tobytes())

        # Write date index mapping
        date_path = os.path.join(code_dir, "trade_date.bin")
        date_ints = group["trade_date"].apply(
            lambda d: int(d.strftime("%Y%m%d"))
        ).values.astype(np.int32)
        with open(date_path, "wb") as f:
            f.write(struct.pack("i", len(date_ints)))
            f.write(date_ints.tobytes())

        stats["stocks_exported"] += 1

    print(f"[Migration] Exported {stats['stocks_exported']} stocks, "
          f"{stats['total_rows']} rows to {features_dir}")
    return stats


def run_migration(output_dir: str = None) -> dict:
    """
    Run the full migration: instruments → calendars → features.

    Returns summary dict.
    """
    start = time.time()
    output_dir = output_dir or PROVIDER_URI

    print(f"[Migration] Starting migration to {output_dir}")
    init_db()

    instruments = export_instruments(output_dir)
    calendars = export_calendars(output_dir)
    features = export_features(output_dir)

    elapsed = time.time() - start
    summary = {
        "instruments": instruments,
        "trading_days": calendars,
        "stocks_exported": features["stocks_exported"],
        "total_rows": features["total_rows"],
        "elapsed_seconds": round(elapsed, 1),
    }

    print(f"[Migration] Complete in {elapsed:.1f}s: {summary}")
    return summary


if __name__ == "__main__":
    run_migration()
```

- [ ] **Step 2: Run migration and verify**

```bash
python -m qlib_engine.migration
```

Expected: prints stock count, trading days, elapsed time.

- [ ] **Step 3: Verify Qlib can load the data**

```python
from qlib_engine import init_qlib
init_qlib()
from qlib.data import D
instruments = D.instruments(market="all")
print(f"Instruments: {len(instruments)}")
calendar = D.calendar()
print(f"Trading days: {len(calendar)}")
```

Expected: prints instrument count matching export, calendar days matching export.

- [ ] **Step 4: Commit**

```bash
git add qlib_engine/migration.py
git commit -m "feat: add one-time SQLite-to-Qlib migration tool"
```

---

### Task 1.4: Create qlib_engine/data_bridge.py

**Files:**
- Create: `qlib_engine/data_bridge.py`

- [ ] **Step 1: Write the incremental sync bridge**

```python
"""
qlib_engine/data_bridge.py —— 日常增量同步桥接

将 AkShare/baostock 拉取的增量数据直接写入 Qlib 二进制格式。
替代 core/sync.py 中直接写入 daily_price 表的逻辑。

用法：
  from qlib_engine.data_bridge import append_daily_data
  append_daily_data(code, df_new)
"""

import os
import struct
import numpy as np
import pandas as pd
from typing import List

from qlib_engine import PROVIDER_URI


def append_daily_data(code: str, df_new: pd.DataFrame) -> int:
    """
    Append new OHLCV rows to an existing stock's Qlib binary files.

    Args:
        code: stock code (e.g. '000001.SZ')
        df_new: DataFrame with columns [trade_date, open, high, low, close, volume]
                trade_date must be datetime or string 'YYYY-MM-DD'

    Returns:
        Number of new rows appended.
    """
    if df_new.empty:
        return 0

    df = df_new.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["trade_date"]):
        df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date")

    code_dir = os.path.join(PROVIDER_URI, "features", code)
    os.makedirs(code_dir, exist_ok=True)

    fields = ["open", "high", "low", "close", "volume"]
    new_count = 0

    for field in fields:
        bin_path = os.path.join(code_dir, f"{field}.bin")
        new_values = df[field].fillna(0).values.astype(np.float32)

        if os.path.exists(bin_path):
            with open(bin_path, "rb") as f:
                old_count = struct.unpack("i", f.read(4))[0]
                old_data = np.frombuffer(f.read(), dtype=np.float32)

            merged = np.concatenate([old_data, new_values])
            with open(bin_path, "wb") as f:
                f.write(struct.pack("i", len(merged)))
                f.write(merged.tobytes())
        else:
            with open(bin_path, "wb") as f:
                f.write(struct.pack("i", len(new_values)))
                f.write(new_values.tobytes())

        new_count = len(new_values)

    # Update trade_date index
    date_path = os.path.join(code_dir, "trade_date.bin")
    new_dates = df["trade_date"].apply(
        lambda d: int(d.strftime("%Y%m%d"))
    ).values.astype(np.int32)

    if os.path.exists(date_path):
        with open(date_path, "rb") as f:
            old_count = struct.unpack("i", f.read(4))[0]
            old_dates = np.frombuffer(f.read(), dtype=np.int32)
        merged_dates = np.concatenate([old_dates, new_dates])
        with open(date_path, "wb") as f:
            f.write(struct.pack("i", len(merged_dates)))
            f.write(merged_dates.tobytes())
    else:
        with open(date_path, "wb") as f:
            f.write(struct.pack("i", len(new_dates)))
            f.write(new_dates.tobytes())

    return new_count


def batch_append_daily(data: dict[str, pd.DataFrame]) -> dict:
    """
    Append daily data for multiple stocks at once.

    Args:
        data: {code: df_new} mapping

    Returns:
        {code: rows_appended} mapping
    """
    result = {}
    for code, df in data.items():
        try:
            n = append_daily_data(code, df)
            result[code] = n
        except Exception as e:
            result[code] = {"error": str(e)}
    return result


def append_calendar_dates(new_dates: List[str]) -> int:
    """
    Append new trading dates to calendars/day.txt.
    Skips dates already present.

    Returns count of new dates added.
    """
    cal_path = os.path.join(PROVIDER_URI, "calendars", "day.txt")
    os.makedirs(os.path.dirname(cal_path), exist_ok=True)

    existing = set()
    if os.path.exists(cal_path):
        with open(cal_path, "r") as f:
            existing = set(line.strip() for line in f)

    new_set = set(new_dates) - existing
    if new_set:
        with open(cal_path, "a") as f:
            for d in sorted(new_set):
                f.write(f"{d}\n")

    return len(new_set)
```

- [ ] **Step 2: Verify import**

```bash
python -c "from qlib_engine.data_bridge import append_daily_data; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add qlib_engine/data_bridge.py
git commit -m "feat: add Qlib incremental data sync bridge"
```

---

### Task 1.5: Modify core/sync.py — use Qlib bridge

**Files:**
- Modify: `core/sync.py`
- Modify: `core/db.py` (remove upsert_daily_price dependency)

- [ ] **Step 1: Read current sync.py daily price writing logic**

Find all calls to `upsert_daily_price(...)` in `core/sync.py` — these must be replaced with `append_daily_data()` calls.

- [ ] **Step 2: Add Qlib bridge import and replace upsert**

At top of `core/sync.py`, add:

```python
from qlib_engine.data_bridge import append_daily_data, batch_append_daily, append_calendar_dates
from qlib_engine import init_qlib
```

Replace each `upsert_daily_price(conn, code, row_dict)` call with:

```python
import pandas as pd
df_new = pd.DataFrame([{
    "trade_date": row_dict["trade_date"],
    "open": row_dict["open"],
    "high": row_dict["high"],
    "low": row_dict["low"],
    "close": row_dict["close"],
    "volume": row_dict["volume"],
}])
n = append_daily_data(code, df_new)
```

- [ ] **Step 3: Add Qlib init at module load time**

At the bottom of `core/sync.py` (or at the sync trigger point), add:

```python
init_qlib()  # Ensure Qlib is initialized before any data operations
```

- [ ] **Step 4: Update sync_log to still log but not write daily_price**

The `sync_log` table remains. Only `daily_price` writes are redirected.

- [ ] **Step 5: Commit**

```bash
git add core/sync.py
git commit -m "refactor: redirect sync output to Qlib binary via data_bridge"
```

---

### Task 1.6: Stub deprecated daily_price functions in core/db.py

**Files:**
- Modify: `core/db.py`

- [ ] **Step 1: Identify all daily_price and index_daily functions**

In `core/db.py`, find: `upsert_daily_price`, `get_daily_price`, `get_latest_date`, `get_latest_date_all`, `get_stock_count_in_db` (if specific to daily_price), `upsert_index_daily`, `get_index_daily`.

- [ ] **Step 2: Add deprecation warnings**

```python
import warnings

def upsert_daily_price(*args, **kwargs):
    warnings.warn(
        "upsert_daily_price is deprecated — use qlib_engine.data_bridge",
        DeprecationWarning, stacklevel=2
    )
    # No-op: Qlib handles this now

def get_daily_price(*args, **kwargs):
    warnings.warn(
        "get_daily_price is deprecated — use Qlib DataHandler",
        DeprecationWarning, stacklevel=2
    )
    from qlib.data import D
    # Return empty DataFrame — callers should migrate to Qlib
    import pandas as pd
    return pd.DataFrame()

def upsert_index_daily(*args, **kwargs):
    warnings.warn(
        "upsert_index_daily is deprecated",
        DeprecationWarning, stacklevel=2
    )

def get_index_daily(*args, **kwargs):
    warnings.warn(
        "get_index_daily is deprecated",
        DeprecationWarning, stacklevel=2
    )
    import pandas as pd
    return pd.DataFrame()
```

- [ ] **Step 3: Keep all other db.py functions untouched**

`stock_info`, `strategy_rules`, `strategy_signals`, `active_strategies`, `risk_*`, `audit_log`, `positions`, `trades`, etc. — all unchanged.

- [ ] **Step 4: Commit**

```bash
git add core/db.py
git commit -m "refactor: stub deprecated daily_price functions in db.py"
```

---

## Phase 2: Model Integration

### Task 2.1: Create Qlib model training runner

**Files:**
- Create: `qlib_engine/model_runner.py`

- [ ] **Step 1: Write the Qlib model training pipeline**

```python
"""
qlib_engine/model_runner.py —— Qlib 模型训练与预测

用法：
  runner = ModelRunner()
  runner.train(model_name="LGBModel", train_start="2020-01-01", train_end="2024-12-31")
  predictions = runner.predict("2025-01-01", "2025-06-01")
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, List

from qlib_engine import init_qlib


class ModelRunner:
    """
    Thin wrapper around Qlib's standard model workflow.

    Models: LGBModel, GRU, ALSTM, etc.
    All use the standard fit/predict interface.
    """

    def __init__(self):
        init_qlib()

    def train(
        self,
        model_name: str = "LGBModel",
        train_start: str = "2020-01-01",
        train_end: str = "2024-12-31",
        valid_start: str = "2025-01-01",
        valid_end: str = "2025-04-30",
        handler_class: str = "Alpha158",
        **model_kwargs,
    ) -> dict:
        """
        Train a Qlib model on Alpha158 features.

        Args:
            model_name: 'LGBModel', 'GRU', 'ALSTM'
            train_start/end: training period
            valid_start/end: validation period
            handler_class: feature set ('Alpha158' or 'Alpha360')

        Returns:
            dict with metrics (IC, ICIR, Rank IC, etc.)
        """
        from qlib.contrib.data.handler import Alpha158
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandlerLP
        from qlib.contrib.model.gbdt import LGBModel
        from qlib.workflow import R
        from qlib.workflow.record_temp import SignalRecord, SigAnaRecord
        from qlib.utils import init_instance_by_config

        # Build dataset config
        handler_config = {
            "class": "Alpha158",
            "module_path": "qlib.contrib.data.handler",
            "kwargs": {
                "start_time": train_start,
                "end_time": valid_end,
                "fit_start_time": train_start,
                "fit_end_time": train_end,
                "instruments": "all",
            },
        }
        handler = init_instance_by_config(handler_config)

        dataset_config = {
            "class": "DatasetH",
            "module_path": "qlib.data.dataset",
            "kwargs": {
                "handler": handler,
                "segments": {
                    "train": (train_start, train_end),
                    "valid": (valid_start, valid_end),
                },
            },
        }
        dataset = init_instance_by_config(dataset_config)

        # Build model config
        model_map = {
            "LGBModel": ("qlib.contrib.model.gbdt", "LGBModel"),
            "GRU": ("qlib.contrib.model.pytorch_gru", "GRU"),
            "ALSTM": ("qlib.contrib.model.pytorch_alstm", "ALSTM"),
        }
        module_path, class_name = model_map.get(
            model_name, model_map["LGBModel"]
        )

        model_config = {
            "class": class_name,
            "module_path": module_path,
            "kwargs": model_kwargs or {},
        }
        model = init_instance_by_config(model_config)

        # Train with MLflow recording
        with R.start(experiment_name=f"aiquant_{model_name}"):
            R.log_params(flatten_dict={
                "model": model_name,
                "train": f"{train_start}-{train_end}",
                "valid": f"{valid_start}-{valid_end}",
            })
            model.fit(dataset)
            R.save_objects(**{"trained_model.pkl": model})

            # Signal record
            recorder = R.get_recorder()
            sr = SignalRecord(model, dataset, recorder)
            sr.generate()

            # Signal analysis
            sar = SigAnaRecord(recorder)
            sar.generate()

            metrics = recorder.list_metrics()
            self.model = model
            self.dataset = dataset

        return metrics

    def predict(
        self,
        start_date: str,
        end_date: str,
        stock_list: Optional[List[str]] = None,
    ) -> pd.Series:
        """
        Generate predictions for a date range.

        Args:
            start_date, end_date: prediction period
            stock_list: optional stock code filter

        Returns:
            pd.Series with MultiIndex (datetime, instrument), values = predicted return
        """
        if not hasattr(self, "model"):
            raise RuntimeError("Model not trained. Call train() first.")

        predictions = self.model.predict(self.dataset, segment="test")

        if predictions is None or len(predictions) == 0:
            # If no test segment in dataset, predict on the full range
            if hasattr(self, "dataset"):
                predictions = self.model.predict(self.dataset)

        if stock_list and predictions is not None:
            predictions = predictions[predictions.index.get_level_values(1).isin(stock_list)]

        return predictions
```

- [ ] **Step 2: Verify module imports**

```bash
python -c "from qlib_engine.model_runner import ModelRunner; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add qlib_engine/model_runner.py
git commit -m "feat: add Qlib model training runner"
```

---

### Task 2.2: Modify strategy/lgbm_ranker.py — add Qlib prediction input

**Files:**
- Modify: `strategy/lgbm_ranker.py`

- [ ] **Step 1: Add Qlib prediction feature to meta-features**

In `build_meta_features()`, add a new feature field after `sector_crowd`:

```python
def build_meta_features(
    rule_id: int, rule_name: str, rule_type: str, n_conditions: int,
    rule_recent_perf: dict,
    rule_history: dict,
    market_state: dict,
    stock_state: dict,
    signal_strength: float,
    sector_crowd: dict,
    qlib_pred_score: Optional[float] = None,  # NEW
) -> dict:
    """为单条触发信号构建元特征向量"""
    features = {
        "rule_id": rule_id,
        "rule_type_hash": hash(rule_type) % 1000,
        "n_conditions": n_conditions,
        "win_rate_20d": rule_recent_perf.get("win_rate_20d", 0),
        "win_rate_60d": rule_recent_perf.get("win_rate_60d", 0),
        # ... existing fields unchanged ...
        "qlib_score": qlib_pred_score if qlib_pred_score is not None else 0.0,  # NEW
    }
    return features
```

- [ ] **Step 2: Update the training pipeline to pass Qlib predictions**

Where `build_meta_features()` is called (in `fuse_stock_signals` or the train method), add Qlib prediction lookup:

```python
from qlib_engine.model_runner import ModelRunner

# Get Qlib predictions for the stocks in the candidate pool
runner = ModelRunner()
qlib_preds = runner.predict(start_date, end_date, stock_list=candidate_codes)

# When building features for each signal:
stock_code = signal["code"]
qlib_score = qlib_preds.xs(stock_code, level=1).mean() if stock_code in qlib_preds.index.get_level_values(1) else 0.0
features = build_meta_features(..., qlib_pred_score=qlib_score)
```

- [ ] **Step 3: Commit**

```bash
git add strategy/lgbm_ranker.py
git commit -m "feat: add Qlib prediction score as Ranker meta-feature"
```

---

## Phase 3: Backtest Replacement

### Task 3.1: Create qlib_engine/strategy_adapter.py

**Files:**
- Create: `qlib_engine/strategy_adapter.py`

- [ ] **Step 1: Write the backtest adapter**

```python
"""
qlib_engine/strategy_adapter.py —— 规则 → Qlib 回测适配层

职责：
  1. 将 AIQuant 规则信号转为 Qlib Strategy
  2. 单条/批量规则回测
  3. 返回 AIQuant 兼容的绩效 dict
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

from qlib_engine import init_qlib
from qlib.backtest import backtest as qlib_backtest
from qlib.backtest.decision import Order, OrderDir
from qlib.contrib.strategy import TopkDropoutStrategy


@dataclass
class BacktestConfig:
    """Qlib backtest configuration."""
    start_time: str
    end_time: str
    account: float = 1_000_000
    benchmark: str = "SH000300"
    deal_price: str = "close"
    open_cost: float = 0.0005
    close_cost: float = 0.0015
    min_cost: float = 5.0
    limit_threshold: float = 0.095
    topk: int = 30
    n_drop: int = 5


def _signals_to_prediction_df(
    signals: List[dict],
    calendar: List[str],
    instruments: List[str],
) -> pd.Series:
    """
    Convert AIQuant signal list to Qlib-compatible prediction Series.

    Args:
        signals: [{code, trade_date, score, rule_name}]
        calendar: trading day list
        instruments: stock code list

    Returns:
        pd.Series with MultiIndex (datetime, instrument), values = prediction score
    """
    records = []
    for s in signals:
        score = s.get("score", s.get("confidence", 0))
        records.append({
            "datetime": pd.Timestamp(s["trade_date"]),
            "instrument": s["code"],
            "score": float(score),
        })

    if not records:
        return pd.Series([], dtype=float)

    df = pd.DataFrame(records)
    idx = pd.MultiIndex.from_arrays(
        [df["datetime"], df["instrument"]],
        names=["datetime", "instrument"],
    )
    return pd.Series(df["score"].values, index=idx)


def backtest_single_rule(
    rule_name: str,
    signals: List[dict],
    config: BacktestConfig,
) -> dict:
    """
    Backtest a single rule's signals and return performance metrics.

    Args:
        rule_name: rule identifier
        signals: list of triggered signals with score
        config: backtest parameters

    Returns:
        dict with annual_return, sharpe_ratio, max_drawdown, win_rate, total_trades, etc.
    """
    init_qlib()

    pred_df = _signals_to_prediction_df(signals, [], [])

    if pred_df.empty:
        return {
            "rule_name": rule_name,
            "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0,
            "win_rate": 0, "total_trades": 0, "error": "No signals",
        }

    # Build Qlib backtest config
    bt_config = {
        "strategy": {
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy",
            "kwargs": {
                "signal": pred_df,
                "topk": config.topk,
                "n_drop": config.n_drop,
            },
        },
        "executor": {
            "class": "SimulatorExecutor",
            "module_path": "qlib.backtest.executor",
            "kwargs": {
                "time_per_step": "day",
                "generate_portfolio_metrics": True,
            },
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

    try:
        # Execute Qlib backtest
        portfolio_metrics, indicator = qlib_backtest(
            executor=bt_config["executor"],
            strategy=bt_config["strategy"],
            **bt_config["backtest"],
        )

        # Extract standard metrics
        metrics = portfolio_metrics[0] if isinstance(portfolio_metrics, tuple) else portfolio_metrics
        report = indicator.get_latest_report() if hasattr(indicator, "get_latest_report") else {}

        return {
            "rule_name": rule_name,
            "annual_return": round(float(report.get("excess_return_with_cost.annualized_return", 0) or 0) * 100, 2),
            "sharpe_ratio": round(float(report.get("excess_return_with_cost.information_ratio", 0) or 0), 2),
            "max_drawdown": round(float(report.get("excess_return_with_cost.max_drawdown", 0) or 0) * 100, 2),
            "win_rate": round(float(report.get("excess_return_without_cost.win_rate", 0) or 0) * 100, 2),
            "total_trades": int(report.get("total_trades", 0) or 0),
            "calmar_ratio": round(float(report.get("excess_return_with_cost.annualized_return", 0) or 0)
                                  / max(abs(float(report.get("excess_return_with_cost.max_drawdown", 0) or 0)), 1e-9), 2),
        }
    except Exception as e:
        return {
            "rule_name": rule_name,
            "error": str(e),
            "annual_return": 0, "sharpe_ratio": 0, "max_drawdown": 0,
            "win_rate": 0, "total_trades": 0,
        }


def backtest_rules(
    rules_and_signals: Dict[str, List[dict]],
    config: BacktestConfig,
) -> List[dict]:
    """
    Batch backtest multiple rules.

    Args:
        rules_and_signals: {rule_name: [signals]} mapping
        config: shared backtest configuration

    Returns:
        List of performance dicts, one per rule.
    """
    results = []
    for rule_name, signals in rules_and_signals.items():
        result = backtest_single_rule(rule_name, signals, config)
        results.append(result)
    return results
```

- [ ] **Step 2: Commit**

```bash
git add qlib_engine/strategy_adapter.py
git commit -m "feat: add Qlib backtest strategy adapter"
```

---

### Task 3.2: Modify strategy/rule_miner.py — replace Backtester

**Files:**
- Modify: `strategy/rule_miner.py`

- [ ] **Step 1: Replace backtest import**

Remove:
```python
from backtest.backtest import Backtester
```

Add:
```python
from qlib_engine.strategy_adapter import backtest_rules, BacktestConfig
```

- [ ] **Step 2: Replace Backtester usage in run_phase1()**

Find wherever `Backtester` is instantiated and called. Replace with:

```python
config = BacktestConfig(
    start_time="2024-01-01",
    end_time=datetime.now().strftime("%Y-%m-%d"),
)
rules_signals = {rule.name: rule.generate_signals(stock_data) for rule in candidate_rules}
results = backtest_rules(rules_signals, config)

# results format: [{rule_name, annual_return, sharpe_ratio, max_drawdown, win_rate, ...}]
```

- [ ] **Step 3: Remove all references to backtest.backtest**

Search and replace all `from backtest` imports that reference the old engine.

- [ ] **Step 4: Commit**

```bash
git add strategy/rule_miner.py
git commit -m "refactor: rule_miner uses Qlib backtest adapter"
```

---

### Task 3.3: Modify strategy/genetic_evolver.py — replace Backtester

**Files:**
- Modify: `strategy/genetic_evolver.py`

- [ ] **Step 1: Replace backtest import and usage**

Same pattern as Task 3.2:
```python
from qlib_engine.strategy_adapter import backtest_rules, BacktestConfig
```

Replace fitness evaluation that calls old `Backtester` with `backtest_rules()`.

- [ ] **Step 2: Commit**

```bash
git add strategy/genetic_evolver.py
git commit -m "refactor: genetic_evolver uses Qlib backtest adapter"
```

---

### Task 3.4: Modify strategy/dynamic_selector.py — replace backtest

**Files:**
- Modify: `strategy/dynamic_selector.py`

- [ ] **Step 1: Replace dual-window backtest**

The current `window_score()` function receives perf dicts from the old Backtester. Replace the call that generates those perf dicts:

```python
from qlib_engine.strategy_adapter import backtest_single_rule, BacktestConfig

def backtest_rule_window(rule_name, signals, window_start, window_end):
    config = BacktestConfig(start_time=window_start, end_time=window_end)
    return backtest_single_rule(rule_name, signals, config)
```

The `window_score()` function itself stays — it just takes the score dict, which the new adapter outputs in compatible format.

- [ ] **Step 2: Commit**

```bash
git add strategy/dynamic_selector.py
git commit -m "refactor: dynamic_selector uses Qlib backtest adapter"
```

---

### Task 3.5: Modify services/strategy_lab_service.py

**Files:**
- Modify: `services/strategy_lab_service.py`

- [ ] **Step 1: Update _load_stock_data_for_pipeline()**

This function currently loads data from `daily_price` via SQLite. Replace with Qlib DataHandler:

```python
def _load_stock_data_for_pipeline_qlib(max_stocks: int = 100) -> tuple:
    """
    Load stock data via Qlib DataHandler instead of SQLite daily_price.
    Returns (stock_data, factor_df, forward_returns, index_df).
    """
    from qlib_engine import init_qlib
    from qlib.data import D
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset import DatasetH

    init_qlib()

    instruments = D.instruments(market="all")[:max_stocks]
    handler = Alpha158(
        start_time="2024-01-01",
        end_time=datetime.now().strftime("%Y-%m-%d"),
        fit_start_time="2024-01-01",
        fit_end_time=datetime.now().strftime("%Y-%m-%d"),
        instruments=instruments,
    )

    # Get factor DataFrame from Qlib handler
    # handler.fetch() returns processed feature DataFrame
    factor_df = handler.fetch(col_set="feature")

    # Build stock_data dict compatible with existing P1/P2/P4 code
    stock_data = {}
    for code in instruments:
        price_slice = D.features([code], ["$open", "$high", "$low", "$close", "$volume"],
                                 start_time="2024-01-01", end_time=datetime.now().strftime("%Y-%m-%d"))
        if not price_slice.empty:
            stock_data[code] = (price_slice, factor_df.xs(code, level=1))

    # Calculate forward returns
    close_prices = D.features(instruments, ["$close"],
                              start_time="2024-01-01", end_time=datetime.now().strftime("%Y-%m-%d"))
    forward_returns = close_prices.groupby(level=1)["$close"].transform(
        lambda x: x.shift(-5) / x - 1
    )

    # Index data
    index_close = D.features(["SH000300"], ["$close"],
                             start_time="2024-01-01", end_time=datetime.now().strftime("%Y-%m-%d"))

    return stock_data, factor_df, forward_returns, index_close
```

- [ ] **Step 2: Update each run_phase function to use new loader**

In `run_phase1_mine()`, `run_phase2_evolve()`, `run_phase4_select()`:
```python
# Old:
stock_data, factor_df, forward_returns, _ = _load_stock_data_for_pipeline(80)
# New:
stock_data, factor_df, forward_returns, _ = _load_stock_data_for_pipeline_qlib(80)
```

- [ ] **Step 3: Update run_full_pipeline() if needed**

The `run_full_pipeline()` calls each individual `run_phase*()` function — no changes needed since each phase updates its loader independently.

- [ ] **Step 4: Commit**

```bash
git add services/strategy_lab_service.py
git commit -m "refactor: strategy_lab uses Qlib DataHandler for data loading"
```

---

### Task 3.6: Delete deprecated backtest files

**Files:**
- Delete: 11 files in `backtest/`, 3 files in `strategy/`, 2 files in `ai/`

- [ ] **Step 1: Remove backtest engine files**

```bash
rm backtest/backtest.py
rm backtest/engine.py
rm backtest/backtest_v4.py
rm backtest/custom_strategy_backtest.py
rm backtest/strategy_screen_backtest.py
rm backtest/unified_engine.py
rm backtest/indicator_engine.py
rm backtest/condition_builder.py
rm backtest/optimize_params.py
rm backtest/param_optimizer.py
rm backtest/strategy_interface.py
```

- [ ] **Step 2: Remove deprecated strategy files**

```bash
rm strategy/factor_lib.py
rm strategy/indicators.py
rm strategy/strategies.py
```

- [ ] **Step 3: Remove AI prediction files**

```bash
rm ai/predictor.py
rm ai/features.py
```

- [ ] **Step 4: Verify no imports break**

```bash
python -c "
# Quick check that all remaining modules still import
from strategy.rule_miner import RuleMiner
from strategy.genetic_evolver import GeneticEvolver
from strategy.dynamic_selector import DynamicSelector
from strategy.lgbm_ranker import get_ranker
print('All imports OK')
"
```

Expected: `All imports OK` (after Tasks 3.2-3.5 are complete, which remove the backtest imports).

- [ ] **Step 5: Commit**

```bash
git add -u backtest/ strategy/ ai/
git commit -m "refactor: delete deprecated backtest/strategy/AI engine files"
```

---

## Phase 4: Agent Fusion & Routes

### Task 4.1: Modify agents/signal_agent.py — fusion layer

**Files:**
- Modify: `agents/signal_agent.py`

- [ ] **Step 1: Read current SignalAgent**

Read the existing `SignalAgent.run()` method to understand current flow.

- [ ] **Step 2: Rewrite run() with fusion logic**

```python
def run(self, ctx: "AgentContext") -> "AgentResult":
    """
    SignalAgent: 融合层 — 规则筛选 + Qlib 模型排序 → 最终交易信号
    """
    from core.db import get_current_active_strategies, save_strategy_signal
    from strategy.rule_miner import RuleMiner, StrategyRule, RuleCondition
    from qlib_engine.model_runner import ModelRunner
    from qlib_engine.strategy_adapter import BacktestConfig
    import json

    # 1. Load active rules from P4 selection
    active_strategies = get_current_active_strategies()
    if not active_strategies:
        return AgentResult(success=False, error="No active strategies")

    # 2. Load rule definitions and trigger signals across all stocks
    active_rules = {}
    for s in active_strategies:
        rule_id = s["rule_id"]
        if rule_id not in active_rules:
            # Load rule from DB
            active_rules[rule_id] = RuleMiner.load_rule(rule_id)

    # 3. Generate candidate signals (rules × stocks)
    candidate_signals = []
    for rule_id, rule in active_rules.items():
        rule_signals = rule.generate_signals(ctx.get("stock_data", {}))
        candidate_signals.extend(rule_signals)

    if not candidate_signals:
        return AgentResult(success=True, data={"signals": [], "message": "No signals triggered"})

    # 4. Get Qlib model predictions for candidate stocks
    candidate_codes = list(set(s["code"] for s in candidate_signals))
    runner = ModelRunner()
    qlib_preds = runner.predict(
        start_date=ctx.get("date"),
        end_date=ctx.get("date"),
        stock_list=candidate_codes,
    )

    # 5. Fusion: rule score × Qlib prediction → final ranking
    for sig in candidate_signals:
        code = sig["code"]
        qlib_score = 0.0
        try:
            qlib_score = float(qlib_preds.xs(code, level=1).iloc[-1])
        except (KeyError, IndexError):
            pass
        sig["final_score"] = sig.get("score", 50) * (0.5 + 0.5 * (1.0 / (1.0 + np.exp(-qlib_score))))

    # Sort by final score, take top-K
    candidate_signals.sort(key=lambda s: s["final_score"], reverse=True)
    top_signals = candidate_signals[:ctx.get("max_signals", 30)]

    # 6. Save to DB
    for sig in top_signals:
        save_strategy_signal(
            code=sig["code"],
            rule_id=sig.get("rule_id"),
            rule_name=sig.get("rule_name", ""),
            score=sig["final_score"],
            trade_date=ctx.get("date"),
        )

    return AgentResult(
        success=True,
        data={"signals": top_signals, "count": len(top_signals)},
    )
```

- [ ] **Step 3: Commit**

```bash
git add agents/signal_agent.py
git commit -m "feat: SignalAgent fusion layer — rules + Qlib predictions"
```

---

### Task 4.2: Modify agents/data_agent.py — data validation

**Files:**
- Modify: `agents/data_agent.py`

- [ ] **Step 1: Simplify to data integrity validation**

```python
def run(self, ctx: "AgentContext") -> "AgentResult":
    """
    DataAgent: 验证 Qlib 数据完整性，触发增量同步。
    不再直接抓取数据 — 由 core/sync.py 定时任务负责。
    """
    from qlib_engine import init_qlib, get_provider_uri
    from qlib.data import D
    import os

    init_qlib()

    # Validate data exists
    provider_uri = get_provider_uri()
    features_dir = os.path.join(provider_uri, "features")
    if not os.path.exists(features_dir) or not os.listdir(features_dir):
        return AgentResult(success=False, error="Qlib data not found. Run migration first.")

    calendar = D.calendar()
    instruments = D.instruments(market="all")

    ctx.set("calendar", calendar)
    ctx.set("instruments", instruments)
    ctx.set("latest_trade_date", calendar[-1] if len(calendar) > 0 else None)

    return AgentResult(success=True, data={
        "instruments_count": len(instruments),
        "calendar_days": len(calendar),
        "latest_date": str(calendar[-1]) if len(calendar) > 0 else None,
    })
```

- [ ] **Step 2: Commit**

```bash
git add agents/data_agent.py
git commit -m "refactor: DataAgent validates Qlib data integrity"
```

---

### Task 4.3: Modify agents/backtest_agent.py — Qlib executor

**Files:**
- Modify: `agents/backtest_agent.py`

- [ ] **Step 1: Replace Backtrader with Qlib executor**

```python
def run(self, ctx: "AgentContext") -> "AgentResult":
    """
    BacktestAgent: 使用 Qlib SimulatorExecutor 回测信号。
    """
    from qlib_engine.strategy_adapter import backtest_single_rule, BacktestConfig

    signals = ctx.get("signals", [])
    if not signals:
        return AgentResult(success=False, error="No signals to backtest")

    date = ctx.get("date")
    config = BacktestConfig(
        start_time="2024-01-01",
        end_time=date,
    )

    # Group signals by rule for per-rule backtest
    from collections import defaultdict
    by_rule = defaultdict(list)
    for s in signals:
        by_rule[s.get("rule_name", "unknown")].append(s)

    results = {}
    for rule_name, rule_signals in by_rule.items():
        result = backtest_single_rule(rule_name, rule_signals, config)
        results[rule_name] = result

    ctx.set("backtest_results", results)

    return AgentResult(success=True, data={
        "rules_tested": len(results),
        "results": results,
    })
```

- [ ] **Step 2: Commit**

```bash
git add agents/backtest_agent.py
git commit -m "refactor: BacktestAgent uses Qlib SimulatorExecutor"
```

---

### Task 4.4: Modify routes — adapt to Qlib

**Files:**
- Modify: `routes/backtest.py`, `routes/backtest_new.py`, `routes/scoring.py`, `routes/ai.py`

- [ ] **Step 1: routes/backtest.py — redirect to Qlib adapter**

Replace imports from old `backtest.engine` / `backtest.backtest` with:

```python
from qlib_engine.strategy_adapter import backtest_rules, BacktestConfig
```

Update endpoint handler to construct `BacktestConfig` from request params and call `backtest_rules()`.

- [ ] **Step 2: routes/backtest_new.py — same treatment**

Same replacement as above.

- [ ] **Step 3: routes/scoring.py — redirect scoring**

Replace scoring endpoint to use Qlib model predictions:

```python
from qlib_engine.model_runner import ModelRunner

@scoring_bp.route("/score", methods=["POST"])
def score_stocks():
    data = request.get_json()
    codes = data.get("codes", [])

    runner = ModelRunner()
    preds = runner.predict(
        start_date=data.get("date"),
        end_date=data.get("date"),
        stock_list=codes,
    )

    return jsonify({"scores": preds.to_dict()})
```

- [ ] **Step 4: routes/ai.py — remove or redirect**

Since `ai/predictor.py` is deleted, redirect routes to Qlib model runner or remove the endpoint if unused.

- [ ] **Step 5: Commit**

```bash
git add routes/backtest.py routes/backtest_new.py routes/scoring.py routes/ai.py
git commit -m "refactor: routes adapted to Qlib engine"
```

---

### Task 4.5: Modify app.py — add Qlib init

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add Qlib initialization at startup**

Add after the `from routes.strategy_lab import strategy_lab_bp` import:

```python
# Initialize Qlib engine at startup
try:
    from qlib_engine import init_qlib
    init_qlib()
    print("[Qlib] Engine initialized")
except Exception as e:
    print(f"[Qlib] Initialization skipped: {e}")
```

- [ ] **Step 2: Commit**

```bash
git add app.py
git commit -m "feat: auto-initialize Qlib engine at Flask startup"
```

---

### Task 4.6: Update remaining service files

**Files:**
- Modify: `services/backtest_service.py`, `services/signal_service.py`, `services/data_fetcher.py`

- [ ] **Step 1: services/backtest_service.py**

Replace backtest engine calls with `qlib_engine.strategy_adapter.backtest_rules()`.

- [ ] **Step 2: services/signal_service.py**

Add Qlib prediction lookup when computing final scores.

- [ ] **Step 3: services/data_fetcher.py**

Adjust to pass data through `qlib_engine.data_bridge` instead of writing to SQLite.

- [ ] **Step 4: Commit**

```bash
git add services/backtest_service.py services/signal_service.py services/data_fetcher.py
git commit -m "refactor: services adapted to Qlib engine"
```

---

### Task 4.7: End-to-end validation

**Files:**
- Test: manual validation

- [ ] **Step 1: Start Flask server**

```bash
python app.py
```

Expected: Server starts on port 5000, prints "[Qlib] Engine initialized".

- [ ] **Step 2: Run data migration**

```bash
python -m qlib_engine.migration
```

Expected: Migration completes with stock count, trading day count, elapsed time.

- [ ] **Step 3: Test health endpoint**

```bash
curl http://localhost:5000/api/health
```

Expected: `{"status": "ok", "message": "A股量化系统运行中"}`

- [ ] **Step 4: Test quant pipeline (one-click)**

Open `http://localhost:5000/quant` → strategy lab tab → click "一键执行 P1→P4"

Expected: All four phases complete, status shows `[phase1:✓ phase2:✓ phase3:✓ phase4:✓]`.

- [ ] **Step 5: Test agent pipeline**

Navigate to `http://localhost:5000/dashboard` → Agent pipeline → trigger pipeline.

Expected: DataAgent → SignalAgent → [RiskAgent ∥ BacktestAgent] → ReportAgent all succeed.

- [ ] **Step 6: Verify old engines are gone**

```bash
python -c "import backtest.backtest" 2>&1
```

Expected: `ModuleNotFoundError` — old backtest engine deleted.

```bash
python -c "from qlib_engine.strategy_adapter import backtest_rules; print('Qlib adapter OK')"
```

Expected: `Qlib adapter OK`

---

## Summary

| Phase | Tasks | Files Created | Files Modified | Files Deleted |
|-------|-------|---------------|----------------|---------------|
| 1: Data | 6 | 2 | 3 | 0 |
| 2: Model | 2 | 1 | 1 | 0 |
| 3: Backtest | 6 | 1 | 3 | 16 |
| 4: Agents | 7 | 0 | 8 | 0 |
| **Total** | **21** | **4** | **15** | **16** |

**Rollback strategy**: Keep `daily_price` and `index_daily` tables until Phase 4 validation passes. Restore from git at any point.
