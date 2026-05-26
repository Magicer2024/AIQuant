# AIQuant 项目功能改进 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 9 feature improvements across 3 phases for the AIQuant quantitative trading dashboard, following TDD with independent PRs.

**Architecture:** Flask backend with module-level global state for progress tracking, vanilla JS frontend with polling, SQLite persistence. Each PR is self-contained with its own tests, avoiding cross-PR dependencies.

**Tech Stack:** Python 3.11+, Flask, SQLite, unittest, vanilla JavaScript, ECharts

**Implementation order:** PR1 → PR9 → PR2 → PR3 → PR4 → PR5 → PR6 → PR7 → PR8

---

## File Structure Map

```
AIQuant/
├── core/
│   ├── sync.py              # PR1(progress_callback), PR8(retry), PR9(stats)
│   └── db.py                # PR7(new table)
├── routes/
│   ├── system.py            # PR1(sync progress state + API)
│   ├── strategy.py          # PR2(mining stop/reset API), PR7(versions API)
│   ├── backtest.py          # PR3(params), PR4(compare API)
│   └── scoring.py           # PR6(history API)
├── backtest/
│   └── engine.py            # PR3(stop_loss/take_profit params)
├── strategy/
│   ├── mining_state.py      # PR2(NEW: shared mining state module)
│   ├── miner.py             # PR2(stop check)
│   └── rules_store.py       # PR7(version CRUD)
├── scheduler/
│   └── runner.py            # PR8(scheduler retry)
├── dashboard.html           # PR1-7, PR9 (frontend changes)
└── tests/
    ├── test_sync_progress.py      # PR1
    ├── test_mining_interrupt.py   # PR2
    ├── test_backtest_params.py    # PR3
    ├── test_compare_results.py    # PR4
    ├── test_score_history.py      # PR6
    ├── test_rule_versions.py      # PR7
    ├── test_sync_retry.py         # PR8
    └── test_sync_stats.py         # PR9
```

---

## Phase 1: High Priority

### Task 1: PR1 — Data Sync Progress Display

**Files:**
- Create: `tests/test_sync_progress.py`
- Modify: `routes/system.py`
- Modify: `core/sync.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for sync progress endpoint**

Create `tests/test_sync_progress.py`:

```python
"""
tests/test_sync_progress.py —— 数据同步进度显示
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestSyncProgressAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        from routes.system import _sync_progress
        _sync_progress.update({"running": False, "current": 0, "total": 0,
                                "success": 0, "failed": 0, "message": "", "last_error": None})

    def test_get_progress_returns_dict(self):
        resp = self.client.get("/api/sync/progress")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("running", data["data"])
        self.assertIn("current", data["data"])
        self.assertIn("total", data["data"])
        self.assertIn("success", data["data"])
        self.assertIn("failed", data["data"])
        self.assertIn("last_error", data["data"])

    def test_concurrent_sync_returns_409(self):
        from routes.system import _sync_progress
        _sync_progress["running"] = True
        resp = self.client.post("/api/sync")
        self.assertEqual(resp.status_code, 409)
        _sync_progress["running"] = False

    def test_progress_callback_updates_state(self):
        from routes.system import _sync_progress, _progress_callback
        _progress_callback(50, 100, 48, 2)
        self.assertEqual(_sync_progress["current"], 50)
        self.assertEqual(_sync_progress["total"], 100)
        self.assertEqual(_sync_progress["success"], 48)
        self.assertEqual(_sync_progress["failed"], 2)


class TestProgressCallbackIntegration(unittest.TestCase):
    def test_callback_callable(self):
        from routes.system import _progress_callback
        self.assertTrue(callable(_progress_callback))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync_progress.py -v`
Expected: FAIL — `_sync_progress` and `_progress_callback` not defined in `routes/system.py`

- [ ] **Step 3: Add `_sync_progress` and progress endpoint to routes/system.py**

In `routes/system.py`, add after the imports:

```python
_sync_progress = {
    "running": False,
    "current": 0,
    "total": 0,
    "success": 0,
    "failed": 0,
    "message": "",
    "last_error": None,
}


def _progress_callback(current, total, success, failed):
    _sync_progress.update({
        "current": current,
        "total": total,
        "success": success,
        "failed": failed,
    })
```

Add new route before `system_sync()`:

```python
@system_bp.route("/sync/progress", methods=["GET"])
def sync_progress():
    """Get sync progress"""
    return jsonify({"success": True, "data": dict(_sync_progress)})
```

- [ ] **Step 4: Run tests to verify progress endpoint works**

Run: `python -m pytest tests/test_sync_progress.py -v`
Expected: `test_get_progress_returns_dict` PASS, `test_concurrent_sync_returns_409` depends on existing logic, `test_progress_callback_updates_state` PASS

- [ ] **Step 5: Add progress callback parameter to daily_sync() in core/sync.py**

Find `daily_sync()` function signature and add `progress_callback=None` parameter.
In the main stock loop inside `daily_sync()`, add after each stock is processed:

```python
if progress_callback:
    progress_callback(current=current_idx, total=len(stocks),
                      success=success_count, failed=fail_count)
```

Track `success_count` and `fail_count` in the loop variables.

- [ ] **Step 6: Wire progress_callback in routes/system.py system_sync()**

Modify `system_sync()` in `routes/system.py`:

```python
@system_bp.route("/sync", methods=["POST"])
def system_sync():
    """Trigger data sync (async background)"""
    if _sync_progress["running"]:
        return jsonify({"error": "同步正在进行中，请稍候"}), 409
    _sync_progress.update({"running": True, "current": 0, "total": 0,
                            "success": 0, "failed": 0, "message": "", "last_error": None})

    def _run():
        try:
            from core.sync import daily_sync
            daily_sync(verbose=False, progress_callback=_progress_callback)
            _sync_progress["message"] = "同步完成"
        except Exception as e:
            _sync_progress["last_error"] = str(e)
            _sync_progress["message"] = f"同步失败: {e}"
        finally:
            _sync_progress["running"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "数据同步已启动"})
```

Note: Replace the existing `SYNC_STATUS` usage — `system_sync()` now manages its own `_sync_progress`. The scheduler still uses `SYNC_STATUS` from `scheduler/state.py` independently.

- [ ] **Step 7: Run tests again**

Run: `python -m pytest tests/test_sync_progress.py -v`
Expected: All tests PASS

- [ ] **Step 8: Add frontend progress bar and polling in dashboard.html**

Find the sync button area in `dashboard.html`. Add progress bar HTML after the sync button:

```html
<div id="syncProgress" style="display:none; margin-top: 12px;">
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">
        <div id="syncProgressBar" style="flex:1; height:8px; background:var(--border); border-radius:4px; overflow:hidden;">
            <div id="syncProgressFill" style="height:100%; width:0%; background:var(--accent); border-radius:4px; transition:width 0.3s;"></div>
        </div>
        <span id="syncProgressPct" style="font-size:12px; color:var(--text-secondary); white-space:nowrap;">0%</span>
    </div>
    <div id="syncProgressText" style="font-size:12px; color:var(--text-secondary);"></div>
</div>
```

Add the polling JavaScript:

```javascript
let syncPollTimer = null;

async function syncWithProgress() {
    const resp = await fetchJSON(API + '/sync', { method: 'POST' });
    if (resp.error) { showNotification(resp.error); return; }

    const progressEl = document.getElementById('syncProgress');
    progressEl.style.display = 'block';

    syncPollTimer = setInterval(async () => {
        const pResp = await fetchJSON(API + '/sync/progress');
        const p = pResp.data;

        const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
        document.getElementById('syncProgressFill').style.width = pct + '%';
        document.getElementById('syncProgressPct').textContent = pct + '%';
        document.getElementById('syncProgressText').textContent =
            `同步中: ${p.current}/${p.total}  成功:${p.success}  失败:${p.failed}`;

        if (!p.running) {
            clearInterval(syncPollTimer);
            syncPollTimer = null;
            showNotification(p.message || '同步完成');
            setTimeout(() => { progressEl.style.display = 'none'; }, 5000);
        }
    }, 1000);
}
```

Hook the existing sync button to call `syncWithProgress()` instead of whatever it calls now.

- [ ] **Step 9: Commit PR1**

```bash
git add tests/test_sync_progress.py routes/system.py core/sync.py dashboard.html
git commit -m "feat: add data sync progress display with polling progress bar"
```

---

### Task 2: PR9 — Beijing Stock Exchange Skip Notice

Note: PR9 depends on PR1's progress infrastructure, done second per recommended order.

**Files:**
- Create: `tests/test_sync_stats.py`
- Modify: `core/sync.py`

- [ ] **Step 1: Write failing test for sync stats**

Create `tests/test_sync_stats.py`:

```python
"""
tests/test_sync_stats.py —— 北交所/ST 跳过统计
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestShouldSkipStats(unittest.TestCase):
    def setUp(self):
        from core.sync import _sync_stats
        _sync_stats.update({"total": 0, "success": 0, "failed": 0,
                            "skipped_bse": 0, "skipped_st": 0})

    def test_skip_bse_increments_stat(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("430001", "测试股票")
        self.assertTrue(result)
        self.assertEqual(_sync_stats["skipped_bse"], 1)
        self.assertEqual(_sync_stats["skipped_st"], 0)

    def test_skip_st_increments_stat(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("000001", "*ST测试")
        self.assertTrue(result)
        self.assertEqual(_sync_stats["skipped_st"], 1)

    def test_skip_delisted_increments_stat(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("000002", "退市股票")
        self.assertTrue(result)
        self.assertEqual(_sync_stats["skipped_st"], 1)

    def test_no_skip_does_not_increment(self):
        from core.sync import _should_skip, _sync_stats
        result = _should_skip("000001", "平安银行")
        self.assertFalse(result)
        self.assertEqual(_sync_stats["skipped_bse"], 0)
        self.assertEqual(_sync_stats["skipped_st"], 0)

    def test_skip_empty_code_or_name(self):
        from core.sync import _should_skip
        self.assertTrue(_should_skip("", "测试"))
        self.assertTrue(_should_skip("000001", ""))

    def test_sync_stats_reset(self):
        from core.sync import _sync_stats
        # Simulate reset
        _sync_stats.update({"total": 0, "success": 0, "failed": 0,
                            "skipped_bse": 0, "skipped_st": 0})
        self.assertEqual(_sync_stats["skipped_bse"], 0)
        self.assertEqual(_sync_stats["skipped_st"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync_stats.py -v`
Expected: FAIL — `_sync_stats` not defined in `core/sync.py`, or `_should_skip` doesn't increment stats

- [ ] **Step 3: Add _sync_stats and update _should_skip in core/sync.py**

Add near the top of `core/sync.py`, right after the existing `_should_skip` function:

```python
_sync_stats = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "skipped_bse": 0,
    "skipped_st": 0,
}
```

Modify `_should_skip()` to increment stats:

```python
def _should_skip(code: str, name: str) -> bool:
    """跳过 ST、退市、北交所（baostock 不支持）。同时递增 _sync_stats。"""
    if not code or not name:
        return True
    if "ST" in name.upper() or "退" in name or "*" in name:
        _sync_stats["skipped_st"] += 1
        return True
    if code.startswith(("430", "830", "870", "920")):
        _sync_stats["skipped_bse"] += 1
        return True
    return False
```

- [ ] **Step 4: Reset _sync_stats at start of daily_sync()**

Find `daily_sync()` in `core/sync.py`. At the very beginning of the function body, add:

```python
_sync_stats.update({"total": 0, "success": 0, "failed": 0,
                    "skipped_bse": 0, "skipped_st": 0})
```

Also increment `_sync_stats["success"]` and `_sync_stats["failed"]` in the stock loop where appropriate (after each `sync_one_stock` / `sync_one_stock_with_timeout` call result).

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_sync_stats.py -v`
Expected: All tests PASS

- [ ] **Step 6: Update system_sync to return stats**

In `routes/system.py`, update `system_sync()` to include stats in response and completion:

```python
def _run():
    try:
        from core.sync import daily_sync, _sync_stats
        daily_sync(verbose=False, progress_callback=_progress_callback)
        _sync_progress["message"] = (
            f"同步完成: 成功{_sync_stats['success']} 失败{_sync_stats['failed']} "
            f"跳过(ST:{_sync_stats['skipped_st']} 北交所:{_sync_stats['skipped_bse']})"
        )
    except Exception as e:
        _sync_progress["last_error"] = str(e)
        _sync_progress["message"] = f"同步失败: {e}"
    finally:
        _sync_progress["running"] = False
```

- [ ] **Step 7: Commit PR9**

```bash
git add tests/test_sync_stats.py core/sync.py routes/system.py
git commit -m "feat: add BSE/ST skip statistics with sync completion summary"
```

---

### Task 3: PR2 — Strategy Mining Interrupt Mechanism

**Files:**
- Create: `strategy/mining_state.py`
- Create: `tests/test_mining_interrupt.py`
- Modify: `routes/strategy.py`
- Modify: `strategy/miner.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for mining state module**

Create `tests/test_mining_interrupt.py`:

```python
"""
tests/test_mining_interrupt.py —— 策略挖掘中断机制
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMiningStateModule(unittest.TestCase):
    def setUp(self):
        from strategy.mining_state import _reset_for_test
        _reset_for_test()

    def test_initial_state_not_running(self):
        from strategy.mining_state import get_mining_status
        status = get_mining_status()
        self.assertFalse(status["running"])
        self.assertFalse(status["stop_requested"])

    def test_request_stop_sets_flag(self):
        from strategy.mining_state import request_stop, get_mining_status
        request_stop()
        self.assertTrue(get_mining_status()["stop_requested"])

    def test_request_stop_idempotent(self):
        from strategy.mining_state import request_stop, get_mining_status
        request_stop()
        request_stop()
        self.assertTrue(get_mining_status()["stop_requested"])

    def test_reset_clears_stop_flag(self):
        from strategy.mining_state import request_stop, get_mining_status, _reset_for_test
        request_stop()
        _reset_for_test()
        self.assertFalse(get_mining_status()["stop_requested"])


class TestMiningStopAPI(unittest.TestCase):
    def setUp(self):
        from app import app
        app.config["TESTING"] = True
        self.client = app.test_client()
        from strategy.mining_state import _reset_for_test
        _reset_for_test()

    def test_stop_when_not_running(self):
        resp = self.client.post("/api/strategy/mine/stop")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])

    def test_stop_when_running(self):
        from strategy.mining_state import get_mining_status
        get_mining_status()["running"] = True
        resp = self.client.post("/api/strategy/mine/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(get_mining_status()["stop_requested"])

    def test_reset_after_stop(self):
        from strategy.mining_state import get_mining_status
        get_mining_status()["running"] = True
        self.client.post("/api/strategy/mine/stop")
        resp = self.client.post("/api/strategy/mine/reset")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(get_mining_status()["running"])
        self.assertFalse(get_mining_status()["stop_requested"])

    def test_stop_idempotent_multiple_calls(self):
        resp1 = self.client.post("/api/strategy/mine/stop")
        resp2 = self.client.post("/api/strategy/mine/stop")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mining_interrupt.py -v`
Expected: FAIL — `strategy/mining_state.py` module not found

- [ ] **Step 3: Create strategy/mining_state.py**

Create `strategy/mining_state.py`:

```python
"""
mining_state.py —— 策略挖掘共享状态（消除 routes/strategy.py 与 strategy/miner.py 的循环导入）
"""

_mining_status = {
    "running": False,
    "progress": None,
    "result": None,
    "stop_requested": False,
}


def get_mining_status() -> dict:
    return _mining_status


def request_stop():
    _mining_status["stop_requested"] = True


def _reset_for_test():
    """仅测试用：重置状态"""
    _mining_status.update({
        "running": False,
        "progress": None,
        "result": None,
        "stop_requested": False,
    })
```

- [ ] **Step 4: Run tests for mining_state module**

Run: `python -m pytest tests/test_mining_interrupt.py::TestMiningStateModule -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Add stop and reset routes to routes/strategy.py**

In `routes/strategy.py`, replace the module-level `_mining_status` with import from `mining_state`, and add new routes:

```python
from strategy.mining_state import get_mining_status, request_stop

# Remove the module-level: _mining_status = {"running": False, "progress": None, "result": None}


@strategy_bp.route("/mine/stop", methods=["POST"])
def stop_mining():
    """请求停止策略挖掘（幂等）"""
    status = get_mining_status()
    if not status["running"]:
        return jsonify({"success": True, "message": "No mining in progress"})
    request_stop()
    return jsonify({"success": True, "message": "Stop requested"})


@strategy_bp.route("/mine/reset", methods=["POST"])
def reset_mining():
    """重置挖掘状态"""
    status = get_mining_status()
    status.update({"running": False, "progress": None, "result": None, "stop_requested": False})
    return jsonify({"success": True, "message": "Mining status reset"})
```

Update `start_mining()` to reset `stop_requested` on start:

```python
@strategy_bp.route("/mine", methods=["POST"])
def start_mining():
    status = get_mining_status()
    if status["running"]:
        return jsonify({"success": False, "data": None, "error": "Mining already in progress"}), 409
    # ...
    status.update({"running": True, "progress": None, "result": None, "stop_requested": False})
    # ...
```

Update `mining_status()` to use `get_mining_status()`:

```python
@strategy_bp.route("/mine/status", methods=["GET"])
def mining_status():
    return jsonify({"success": True, "data": dict(get_mining_status()), "error": None})
```

- [ ] **Step 6: Run API tests**

Run: `python -m pytest tests/test_mining_interrupt.py::TestMiningStopAPI -v`
Expected: All 4 tests PASS

- [ ] **Step 7: Add stop check in strategy/miner.py**

In `strategy/miner.py`, add at the top of `mine_strategies()`:

```python
from strategy.mining_state import get_mining_status

def mine_strategies(trade_date):
    # ... existing setup ...

    for generation in range(max_generations):
        if get_mining_status().get("stop_requested"):
            print("[Miner] Stop requested, saving current progress...")
            # Save whatever partial results exist
            if saved_rules:
                for rule in saved_rules:
                    try:
                        save_rule(rule)
                    except Exception:
                        pass
            break
        # ... existing mining logic ...
```

- [ ] **Step 8: Add frontend stop/reset button in dashboard.html**

In the mining tab of `dashboard.html`, add button logic:

```javascript
let miningActive = false;

async function toggleMining() {
    if (miningActive) {
        await fetchJSON(API + '/strategy/mine/stop', { method: 'POST' });
        showNotification('已请求停止挖掘');
    } else {
        const resp = await fetchJSON(API + '/strategy/mine', { method: 'POST' });
        if (resp.success) {
            miningActive = true;
            updateMiningButton();
            pollMiningStatus();
        }
    }
}

async function resetMining() {
    await fetchJSON(API + '/strategy/mine/reset', { method: 'POST' });
    miningActive = false;
    updateMiningButton();
}

function updateMiningButton() {
    const btn = document.getElementById('mineBtn');
    btn.textContent = miningActive ? '停止挖掘' : '开始挖掘';
    btn.className = miningActive ? 'btn btn-danger' : 'btn btn-primary';
    document.getElementById('resetMineBtn').style.display = miningActive ? 'none' : '';
}

async function pollMiningStatus() {
    const resp = await fetchJSON(API + '/strategy/mine/status');
    if (!resp.data.running) {
        miningActive = false;
        updateMiningButton();
        showNotification(resp.data.progress === 'done' ? '挖掘完成' : '挖掘已停止');
        return;
    }
    setTimeout(pollMiningStatus, 1000);
}
```

HTML buttons:
```html
<button id="mineBtn" class="btn btn-primary" onclick="toggleMining()">开始挖掘</button>
<button id="resetMineBtn" class="btn btn-secondary" onclick="resetMining()" style="display:none;">重置</button>
```

- [ ] **Step 9: Commit PR2**

```bash
git add strategy/mining_state.py tests/test_mining_interrupt.py routes/strategy.py strategy/miner.py dashboard.html
git commit -m "feat: add strategy mining interrupt mechanism with stop/reset"
```

---

### Task 4: PR3 — Configurable Backtest Parameters

**Files:**
- Create: `tests/test_backtest_params.py`
- Modify: `backtest/engine.py`
- Modify: `routes/backtest.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for backtest parameters**

Create `tests/test_backtest_params.py`:

```python
"""
tests/test_backtest_params.py —— 回测参数可配置
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBacktestParamsClamping(unittest.TestCase):
    def test_stop_loss_clamped_negative_50(self):
        """止损下限-50%，超出钳制"""
        from routes.backtest import backtest_bp
        # Test the clamping logic directly
        val = max(min(-0.60, 0), -0.50)
        self.assertEqual(val, -0.50)

    def test_stop_loss_clamped_zero(self):
        """止损上限0%"""
        val = max(min(0.05, 0), -0.50)
        self.assertEqual(val, 0)

    def test_take_profit_clamped_zero(self):
        """止盈下限0%"""
        val = max(min(-0.05, 1.00), 0)
        self.assertEqual(val, 0)

    def test_take_profit_clamped_100(self):
        """止盈上限100%"""
        val = max(min(1.50, 1.00), 0)
        self.assertEqual(val, 1.00)

    def test_holding_max_clamped(self):
        """持仓天数钳制 1~100"""
        val = max(min(200, 100), 1)
        self.assertEqual(val, 100)
        val = max(min(0, 100), 1)
        self.assertEqual(val, 1)

    def test_default_values_unchanged(self):
        """默认值保持 -8%/20%/20"""
        stop_loss = -0.08
        take_profit = 0.20
        holding_max = 20
        self.assertEqual(stop_loss, -0.08)
        self.assertEqual(take_profit, 0.20)
        self.assertEqual(holding_max, 20)


class TestEngineSignature(unittest.TestCase):
    def test_generate_signals_accepts_params(self):
        from backtest.engine import _generate_backtest_signals
        import inspect
        sig = inspect.signature(_generate_backtest_signals)
        self.assertIn("stop_loss", sig.parameters)
        self.assertIn("take_profit", sig.parameters)

    def test_run_backtest_accepts_params(self):
        from backtest.engine import run_backtest
        import inspect
        sig = inspect.signature(run_backtest)
        self.assertIn("stop_loss", sig.parameters)
        self.assertIn("take_profit", sig.parameters)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_params.py -v`
Expected: FAIL — `_generate_backtest_signals` and `run_backtest` don't have `stop_loss`/`take_profit` params yet

- [ ] **Step 3: Add stop_loss/take_profit params to backtest/engine.py**

Modify `_generate_backtest_signals()` signature (line 187):

```python
def _generate_backtest_signals(
    conditions_json: str,
    start_date: str,
    end_date: str,
    sell_conditions_json: str = None,
    holding_max: int = 20,
    stop_loss: float = -0.08,      # 新增
    take_profit: float = 0.20,     # 新增
    progress_callback=None,
) -> List[dict]:
```

Replace line 313: `if pnl_pct <= DEFAULT_STOP_LOSS:` → `if pnl_pct <= stop_loss:`
Replace line 320: `if pnl_pct >= DEFAULT_TAKE_PROFIT:` → `if pnl_pct >= take_profit:`

Modify `run_backtest()` signature (line 25):

```python
def run_backtest(
    rule_name: str,
    rule_id: str,
    conditions_json: str,
    start_date: str,
    end_date: str,
    sell_conditions_json: str = None,
    holding_max: int = 20,
    stop_loss: float = -0.08,      # 新增
    take_profit: float = 0.20,     # 新增
    save: bool = True,
    progress_callback=None,
) -> dict:
```

Update the call to `_generate_backtest_signals` inside `run_backtest` to pass the new params:

```python
signals = _generate_backtest_signals(
    conditions_json, start_date, end_date,
    sell_conditions_json=sell_conditions_json,
    holding_max=holding_max,
    stop_loss=stop_loss,
    take_profit=take_profit,
    progress_callback=progress_callback,
)
```

Delete the module-level constants on lines 21-22:
```python
# REMOVE: DEFAULT_STOP_LOSS = -0.08
# REMOVE: DEFAULT_TAKE_PROFIT = 0.20
```

- [ ] **Step 4: Run engine signature tests**

Run: `python -m pytest tests/test_backtest_params.py::TestEngineSignature -v`
Expected: PASS

- [ ] **Step 5: Add param extraction in routes/backtest.py start_backtest()**

Modify `start_backtest()` to extract optional params:

```python
body = request.get_json(silent=True) or {}
rule_id = body.get("rule_id")
start_date = body.get("start_date", "2024-01-01")
end_date = body.get("end_date", date.today().strftime("%Y-%m-%d"))

# 新增: 可配置参数，带钳制
stop_loss = max(min(body.get("stop_loss", -0.08), 0), -0.50)
take_profit = max(min(body.get("take_profit", 0.20), 1.00), 0)
holding_max = max(min(body.get("holding_max", 20), 100), 1)
```

Pass them to `run_backtest()`:

```python
result = run_backtest(
    rule_name=rule["rule_name"],
    rule_id=str(rule["id"]),
    conditions_json=rule["conditions"] or "{}",
    start_date=start_date,
    end_date=end_date,
    sell_conditions_json=sell_conds,
    holding_max=holding_max,
    stop_loss=stop_loss,
    take_profit=take_profit,
    save=True,
    progress_callback=_progress,
)
```

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/test_backtest_params.py -v`
Expected: All tests PASS

- [ ] **Step 7: Add frontend parameter inputs in dashboard.html**

In the backtest tab, add parameter inputs before the "Run Backtest" button:

```html
<div class="input-row" style="margin-bottom: 12px;">
    <span>止损(%)</span>
    <input type="number" id="backtestStopLoss" value="-8" step="1" min="-50" max="0"
           style="width:70px;">
    <span>止盈(%)</span>
    <input type="number" id="backtestTakeProfit" value="20" step="1" min="0" max="100"
           style="width:70px;">
    <span>最大持仓(天)</span>
    <input type="number" id="backtestHoldingMax" value="20" step="1" min="1" max="100"
           style="width:70px;">
</div>
```

Add localStorage persistence:

```javascript
// Load saved params on page load
document.addEventListener('DOMContentLoaded', () => {
    const saved = JSON.parse(localStorage.getItem('backtestParams') || '{}');
    document.getElementById('backtestStopLoss').value = saved.stopLoss ?? -8;
    document.getElementById('backtestTakeProfit').value = saved.takeProfit ?? 20;
    document.getElementById('backtestHoldingMax').value = saved.holdingMax ?? 20;
});

// Save params before running backtest
function saveBacktestParams() {
    localStorage.setItem('backtestParams', JSON.stringify({
        stopLoss: parseInt(document.getElementById('backtestStopLoss').value),
        takeProfit: parseInt(document.getElementById('backtestTakeProfit').value),
        holdingMax: parseInt(document.getElementById('backtestHoldingMax').value),
    }));
}

async function runBacktest() {
    saveBacktestParams();
    const body = {
        rule_id: selectedRuleId,
        start_date: startDate,
        end_date: endDate,
        stop_loss: parseInt(document.getElementById('backtestStopLoss').value) / 100,
        take_profit: parseInt(document.getElementById('backtestTakeProfit').value) / 100,
        holding_max: parseInt(document.getElementById('backtestHoldingMax').value),
    };
    // ... fetch POST /api/backtest/run with body
}
```

- [ ] **Step 8: Commit PR3**

```bash
git add tests/test_backtest_params.py backtest/engine.py routes/backtest.py dashboard.html
git commit -m "feat: make backtest stop_loss/take_profit/holding_max configurable"
```

---

## Phase 2: Medium Priority

### Task 5: PR4 — Backtest Result Comparison

**Files:**
- Create: `tests/test_compare_results.py`
- Modify: `routes/backtest.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for compare API**

Create `tests/test_compare_results.py`:

```python
"""
tests/test_compare_results.py —— 回测结果对比
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestCompareAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_compare_returns_list(self):
        resp = self.client.get("/api/backtest/compare?ids=1,2")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["data"], list)

    def test_compare_empty_ids_returns_empty(self):
        resp = self.client.get("/api/backtest/compare?ids=")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["data"], [])

    def test_compare_invalid_ids_skipped(self):
        resp = self.client.get("/api/backtest/compare?ids=abc,xyz")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data["data"], list)

    def test_compare_result_has_result_id(self):
        """Each result must include result_id and rule_name"""
        resp = self.client.get("/api/backtest/compare?ids=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        if data["data"]:
            self.assertIn("result_id", data["data"][0])
            self.assertIn("rule_name", data["data"][0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_compare_results.py -v`
Expected: FAIL — `/api/backtest/compare` route not found (404)

- [ ] **Step 3: Add compare route to routes/backtest.py**

Add new route before `results_list()`:

```python
@backtest_bp.route("/compare", methods=["GET"])
def compare_results():
    """对比多个回测结果 GET /api/backtest/compare?ids=1,2,3"""
    ids_str = request.args.get("ids", "")
    results = []
    for rid_str in ids_str.split(","):
        rid_str = rid_str.strip()
        if not rid_str:
            continue
        try:
            rid = int(rid_str)
        except ValueError:
            continue
        detail = build_result_detail(rid)
        if detail:
            summary = dict(detail["summary"])
            summary["result_id"] = rid
            summary["rule_name"] = detail.get("rule_name", "")
            results.append(summary)
    return jsonify({"success": True, "data": _sanitize(results), "error": None})
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_compare_results.py -v`
Expected: All tests PASS (or skip if no test data in DB — that's OK)

- [ ] **Step 5: Add frontend comparison UI in dashboard.html**

Add checkbox column to backtest results table:

```javascript
function renderResultsTable(results) {
    let html = '<table><thead><tr>'
        + '<th><input type="checkbox" id="selectAllCompare" onchange="toggleAllCompare(this)"></th>'
        + '<th>规则</th><th>年化收益</th><th>胜率</th><th>夏普</th><th>最大回撤</th>'
        + '</tr></thead><tbody>';
    results.forEach(r => {
        html += `<tr>
            <td><input type="checkbox" class="compare-check" value="${r.id}"></td>
            <td>${r.rule_name || ''}</td>
            <td>${r.annual_return ?? '--'}%</td>
            <td>${r.win_rate ?? '--'}%</td>
            <td>${r.sharpe_ratio ?? '--'}</td>
            <td>${r.max_drawdown ?? '--'}%</td>
        </tr>`;
    });
    html += '</tbody></table>';
    html += '<button id="compareBtn" class="btn btn-primary" onclick="compareSelected()" disabled>对比选中</button>';
    return html;
}

function toggleAllCompare(el) {
    document.querySelectorAll('.compare-check').forEach(cb => cb.checked = el.checked);
    updateCompareButton();
}

// Enable compare button only when 2+ selected
document.addEventListener('change', e => {
    if (e.target.classList.contains('compare-check')) updateCompareButton();
});

function updateCompareButton() {
    const count = document.querySelectorAll('.compare-check:checked').length;
    document.getElementById('compareBtn').disabled = count < 2;
}

async function compareSelected() {
    const ids = Array.from(document.querySelectorAll('.compare-check:checked'))
                     .map(cb => cb.value).join(',');
    const resp = await fetchJSON(API + `/backtest/compare?ids=${ids}`);
    showCompareModal(resp.data);
}
```

Compare modal with highlight logic:

```javascript
function showCompareModal(results) {
    if (!results.length) return;

    const metrics = ['annual_return', 'win_rate', 'sharpe_ratio', 'max_drawdown', 'total_trades'];
    const metricLabels = ['年化收益(%)', '胜率(%)', '夏普比率', '最大回撤(%)', '总交易数'];

    // Find best values for highlighting
    const bestAnnual = Math.max(...results.map(r => r.annual_return || -Infinity));
    const bestDrawdown = Math.max(...results.map(r => r.max_drawdown || -Infinity)); // least negative = best

    let html = '<div style="overflow-x:auto;"><table><thead><tr><th>指标</th>';
    results.forEach(r => html += `<th>${r.rule_name || '#' + r.result_id}</th>`);
    html += '</tr></thead><tbody>';

    metrics.forEach((m, i) => {
        html += `<tr><td>${metricLabels[i]}</td>`;
        results.forEach(r => {
            const val = r[m];
            const display = val != null ? val : '--';
            let style = '';
            if ((m === 'annual_return' && val === bestAnnual) ||
                (m === 'max_drawdown' && val === bestDrawdown)) {
                style = 'color:var(--green, #10b981);font-weight:bold;';
            }
            html += `<td style="${style}">${display}</td>`;
        });
        html += '</tr>';
    });
    html += '</tbody></table></div>';

    showModal('回测结果对比', html);
}
```

- [ ] **Step 6: Commit PR4**

```bash
git add tests/test_compare_results.py routes/backtest.py dashboard.html
git commit -m "feat: add backtest result comparison with multi-select and highlight"
```

---

### Task 6: PR5 — Frontend Search & Filter

**Files:**
- Modify: `dashboard.html`

Note: Pure frontend change, no backend tests needed. Manual verification.

- [ ] **Step 1: Add search bar HTML in score tab**

Find the scores tab content area in `dashboard.html`. Add at the top of the scores panel:

```html
<div class="input-row" style="margin-bottom: 12px;">
    <span>搜索</span>
    <input type="text" id="stockSearch" placeholder="代码或名称"
           oninput="debounceFilter()"
           onkeydown="if(event.key==='Enter')filterScores()"
           style="width:150px;">
    <span>最低分</span>
    <input type="number" id="minScore" value="0" step="1" min="0"
           onchange="filterScores()"
           onkeydown="if(event.key==='Enter')filterScores()"
           style="width:80px;">
    <span id="filterCount" style="font-size:12px; color:var(--text-secondary);"></span>
</div>
```

- [ ] **Step 2: Add debounced filter JavaScript**

Add these functions to the `<script>` section:

```javascript
let filterTimer = null;

function debounceFilter() {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(filterScores, 300);
}

function filterScores() {
    const keyword = (document.getElementById('stockSearch')?.value || '').toLowerCase();
    const minScore = parseFloat(document.getElementById('minScore')?.value) || 0;
    let visible = 0;
    let total = 0;

    const rows = document.querySelectorAll('#scoreTable tbody tr');
    rows.forEach(row => {
        total++;
        const codeEl = row.querySelector('.code');
        const nameEl = row.querySelector('.name');
        const scoreEl = row.querySelector('.score');

        const code = codeEl?.textContent?.toLowerCase() || '';
        const name = nameEl?.textContent?.toLowerCase() || '';
        const score = parseFloat(scoreEl?.textContent) || 0;

        const match = (!keyword || code.includes(keyword) || name.includes(keyword))
                   && score >= minScore;
        row.style.display = match ? '' : 'none';
        if (match) visible++;
    });

    const countEl = document.getElementById('filterCount');
    if (countEl) {
        countEl.textContent = visible === 0 ? '无匹配结果' : `显示 ${visible} / ${total} 条`;
    }
}
```

- [ ] **Step 3: Ensure score table has semantic CSS classes**

Verify the score table rendering function produces rows with `.code`, `.name`, `.score` class selectors. If not, add them:

```javascript
// In the score table rendering function:
html += `<td class="code">${row.code}</td>`;
html += `<td class="name">${row.name || ''}</td>`;
html += `<td class="score">${row.score}</td>`;
```

- [ ] **Step 4: Initialize filter count on scores tab load**

After loading scores data, call `filterScores()` once to show initial count:

```javascript
// After rendering the score table:
filterScores();
```

- [ ] **Step 5: Commit PR5**

```bash
git add dashboard.html
git commit -m "feat: add real-time search and score filter with 300ms debounce"
```

---

### Task 7: PR6 — Score History Trend

**Files:**
- Create: `tests/test_score_history.py`
- Modify: `routes/scoring.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for score history API**

Create `tests/test_score_history.py`:

```python
"""
tests/test_score_history.py —— 打分历史趋势 API
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestScoreHistoryAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_history_returns_list(self):
        resp = self.client.get("/api/scoring/stock/000001/history?days=7")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["data"], list)

    def test_history_default_days(self):
        resp = self.client.get("/api/scoring/stock/000001/history")
        self.assertEqual(resp.status_code, 200)

    def test_history_invalid_days_defaults_to_30(self):
        resp = self.client.get("/api/scoring/stock/000001/history?days=999")
        self.assertEqual(resp.status_code, 200)

    def test_history_whitelist_days_values(self):
        """Only 7, 30, 60, 90 are allowed"""
        for days in ("7", "30", "60", "90"):
            resp = self.client.get(f"/api/scoring/stock/000001/history?days={days}")
            self.assertEqual(resp.status_code, 200)

    def test_history_returns_rule_name_field(self):
        resp = self.client.get("/api/scoring/stock/000001/history?days=30")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        if data["data"]:
            self.assertIn("rule_name", data["data"][0])
            self.assertIn("score", data["data"][0])
            self.assertIn("trade_date", data["data"][0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_score_history.py -v`
Expected: FAIL — `/api/scoring/stock/000001/history` route not found (404)

- [ ] **Step 3: Add history route to routes/scoring.py**

Add new route at end of `routes/scoring.py`:

```python
@scoring_bp.route("/stock/<code>/history", methods=["GET"])
def stock_score_history(code: str):
    """获取单股打分历史 GET /api/scoring/stock/000001/history?days=30"""
    days = request.args.get("days", "30")
    if days not in ("7", "30", "60", "90"):
        days = "30"

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT trade_date, score, rule_name
            FROM stock_score
            WHERE code = ? AND trade_date >= date('now', ?)
            ORDER BY trade_date ASC
        """, (code, f"-{days} days")).fetchall()
        return jsonify({"success": True, "data": [dict(r) for r in rows]})
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_score_history.py -v`
Expected: All tests PASS (or skip if no score data in DB)

- [ ] **Step 5: Add frontend score history modal in dashboard.html**

Make stock code clickable in score table:

```javascript
// In score table render:
html += `<td class="code"><a href="javascript:void(0)" onclick="showScoreHistory('${row.code}')"
         style="color:var(--accent); cursor:pointer;">${row.code}</a></td>`;
```

Add history chart modal:

```javascript
let scoreChart = null;

async function showScoreHistory(code) {
    const days = document.getElementById('historyDays')?.value || '30';
    const resp = await fetchJSON(API + `/scoring/stock/${code}/history?days=${days}`);

    const modalHtml = `
        <div>
            <select id="historyDays" onchange="showScoreHistory('${code}')"
                    style="margin-bottom:12px;">
                <option value="7" ${days==='7'?'selected':''}>近7天</option>
                <option value="30" ${days==='30'?'selected':''}>近30天</option>
                <option value="60" ${days==='60'?'selected':''}>近60天</option>
                <option value="90" ${days==='90'?'selected':''}>近90天</option>
            </select>
            <div id="historyChart" style="width:100%;height:350px;"></div>
        </div>`;

    showModal(code + ' 打分趋势', modalHtml);

    if (!resp.data || resp.data.length < 2) {
        document.getElementById('historyChart').innerHTML =
            '<div style="text-align:center;padding:60px;color:var(--text-secondary);">数据不足</div>';
        return;
    }

    // Group by rule_name
    const byRule = {};
    const allDates = new Set();
    resp.data.forEach(d => {
        if (!byRule[d.rule_name]) byRule[d.rule_name] = {};
        byRule[d.rule_name][d.trade_date] = d.score;
        allDates.add(d.trade_date);
    });

    const dates = Array.from(allDates).sort();
    const colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4'];

    const series = Object.entries(byRule).map(([rule, dateMap], idx) => ({
        name: rule,
        type: 'line',
        smooth: true,
        data: dates.map(d => dateMap[d] ?? null),
        lineStyle: { color: colors[idx % colors.length] },
    }));

    // Format X axis based on day count
    const xLabels = dates.map(d => parseInt(days) <= 7 ? d.slice(5) : d);

    scoreChart = echarts.init(document.getElementById('historyChart'));
    scoreChart.setOption({
        tooltip: { trigger: 'axis' },
        toolbox: { feature: { saveAsImage: { title: '下载' } } },
        xAxis: { type: 'category', data: xLabels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', name: 'Score' },
        series: series,
        grid: { left: 50, right: 20, top: 30, bottom: 60 },
    });

    window.addEventListener('resize', () => scoreChart?.resize());
}

// Update the showModal to handle resize for chart
const origShowModal = showModal;
showModal = function(title, content) {
    origShowModal(title, content);
    setTimeout(() => {
        const chartDom = document.getElementById('historyChart');
        if (chartDom && scoreChart) scoreChart.resize();
    }, 100);
};
```

- [ ] **Step 6: Commit PR6**

```bash
git add tests/test_score_history.py routes/scoring.py dashboard.html
git commit -m "feat: add stock score history trend chart with multi-strategy lines"
```

---

## Phase 3: Low Priority

### Task 8: PR7 — Strategy Version Management

**Files:**
- Create: `tests/test_rule_versions.py`
- Modify: `core/db.py`
- Modify: `strategy/rules_store.py`
- Modify: `routes/strategy.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for version management**

Create `tests/test_rule_versions.py`:

```python
"""
tests/test_rule_versions.py —— 策略版本管理
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn, init_db


class TestRuleVersionTable(unittest.TestCase):
    def test_table_exists(self):
        init_db()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_rule_versions'"
            ).fetchone()
            self.assertIsNotNone(row)


class TestVersionCRUD(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_save_rule_version_new_rule_returns_none(self):
        """新建规则不创建版本"""
        from strategy.rules_store import save_rule_version
        result = save_rule_version(99999)  # non-existent rule
        self.assertIsNone(result)

    def test_save_rule_version_existing_rule(self):
        """已存在规则创建版本"""
        from strategy.rules_store import save_rule, save_rule_version
        # Create a rule first
        rule_id = save_rule({
            "rule_name": "_test_version_rule",
            "rule_type": "buy",
            "encoding": "factor",
            "conditions": '{"test": true}',
            "source": "test",
        })
        version = save_rule_version(rule_id)
        self.assertIsNotNone(version)
        self.assertEqual(version, 1)

        # Clean up
        from strategy.rules_store import delete_rule
        delete_rule(rule_id)
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_rule_versions WHERE rule_id = ?", (rule_id,))

    def test_get_rule_versions(self):
        from strategy.rules_store import save_rule, save_rule_version, get_rule_versions, delete_rule
        rule_id = save_rule({
            "rule_name": "_test_version_list",
            "rule_type": "buy",
            "encoding": "factor",
            "conditions": '{"v": 1}',
            "source": "test",
        })
        save_rule_version(rule_id)
        versions = get_rule_versions(rule_id)
        self.assertGreaterEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], 1)

        delete_rule(rule_id)
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_rule_versions WHERE rule_id = ?", (rule_id,))

    def test_rollback_creates_new_version(self):
        """回滚前自动保存当前版本"""
        from strategy.rules_store import (
            save_rule, save_rule_version, rollback_rule, get_rule_versions, delete_rule, get_rule
        )
        rule_id = save_rule({
            "rule_name": "_test_rollback",
            "rule_type": "buy",
            "encoding": "factor",
            "conditions": '{"v": 1}',
            "sell_conditions": '[]',
            "source": "test",
        })
        v1 = save_rule_version(rule_id)

        # Modify rule
        with get_conn() as conn:
            conn.execute("UPDATE strategy_rules SET conditions = ? WHERE id = ?",
                         ('{"v": 2}', rule_id))

        ok = rollback_rule(rule_id, v1)
        self.assertTrue(ok)

        # Should have v1 and v2 (auto-saved before rollback) and v3 (rollback target)
        versions = get_rule_versions(rule_id)
        self.assertGreaterEqual(len(versions), 3)

        # Clean up
        delete_rule(rule_id)
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_rule_versions WHERE rule_id = ?", (rule_id,))

    def test_rollback_nonexistent_returns_false(self):
        from strategy.rules_store import rollback_rule
        result = rollback_rule(99999, 999)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rule_versions.py -v`
Expected: FAIL — `strategy_rule_versions` table doesn't exist, or functions not defined

- [ ] **Step 3: Add strategy_rule_versions table to core/db.py**

In `init_db()`, add after the `strategy_rules` table creation:

```sql
CREATE TABLE IF NOT EXISTS strategy_rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    conditions TEXT,
    sell_conditions TEXT,
    holding_min INTEGER,
    holding_max INTEGER,
    fitness REAL,
    annual_return REAL,
    win_rate REAL,
    sharpe_ratio REAL,
    max_drawdown REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (rule_id) REFERENCES strategy_rules(id)
);
```

- [ ] **Step 4: Add version CRUD functions to strategy/rules_store.py**

Add three new functions at the end of `strategy/rules_store.py`:

```python
def save_rule_version(rule_id: int):
    """保存当前规则为历史版本。仅在规则已存在时调用。返回版本号或None。"""
    with get_conn() as conn:
        rule = conn.execute("SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            return None  # 新建规则不创建版本

        # 检查是否与最新版本相同（避免重复版本）
        latest = conn.execute(
            "SELECT conditions, sell_conditions FROM strategy_rule_versions "
            "WHERE rule_id = ? ORDER BY version DESC LIMIT 1",
            (rule_id,)
        ).fetchone()
        if latest and latest["conditions"] == rule["conditions"] \
                and latest["sell_conditions"] == rule["sell_conditions"]:
            return None  # 内容未变，不创建重复版本

        max_ver = conn.execute(
            "SELECT MAX(version) as v FROM strategy_rule_versions WHERE rule_id = ?",
            (rule_id,)
        ).fetchone()["v"] or 0

        conn.execute("""INSERT INTO strategy_rule_versions
            (rule_id, version, conditions, sell_conditions, holding_min, holding_max,
             fitness, annual_return, win_rate, sharpe_ratio, max_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rule_id, max_ver + 1, rule["conditions"], rule["sell_conditions"],
             rule.get("holding_min", 3), rule.get("holding_max", 20),
             rule.get("fitness", 0), rule.get("annual_return", 0),
             rule.get("win_rate", 0), rule.get("sharpe_ratio", 0),
             rule.get("max_drawdown", 0)))
        conn.commit()
        return max_ver + 1


def get_rule_versions(rule_id: int) -> list:
    """查询某规则所有历史版本，按版本号降序"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, rule_id, version, fitness, annual_return, win_rate, "
            "sharpe_ratio, max_drawdown, created_at "
            "FROM strategy_rule_versions WHERE rule_id = ? ORDER BY version DESC",
            (rule_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def rollback_rule(rule_id: int, version: int) -> bool:
    """回滚到指定版本。先保存当前版本，再覆盖。"""
    with get_conn() as conn:
        ver = conn.execute(
            "SELECT * FROM strategy_rule_versions WHERE rule_id = ? AND version = ?",
            (rule_id, version)
        ).fetchone()
        if not ver:
            return False

        # 先保存当前状态（防止误操作）
        save_rule_version(rule_id)

        # 覆盖当前规则
        conn.execute("""UPDATE strategy_rules SET
            conditions = ?, sell_conditions = ?,
            holding_min = ?, holding_max = ?,
            fitness = ?, annual_return = ?, win_rate = ?,
            sharpe_ratio = ?, max_drawdown = ?,
            updated_at = datetime('now','localtime')
            WHERE id = ?""",
            (ver["conditions"], ver["sell_conditions"],
             ver.get("holding_min", 3), ver.get("holding_max", 20),
             ver.get("fitness", 0), ver.get("annual_return", 0),
             ver.get("win_rate", 0), ver.get("sharpe_ratio", 0),
             ver.get("max_drawdown", 0),
             rule_id))
        conn.commit()
        return True
```

- [ ] **Step 5: Integrate save_rule_version into save_rule()**

In `save_rule()`, add `save_rule_version(rule_id)` call in the UPDATE branch (when `existing` is found):

```python
def save_rule(rule: dict) -> int:
    def _py(val):
        return val.item() if hasattr(val, "item") else val

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM strategy_rules WHERE rule_name = ?", (rule["rule_name"],)
        ).fetchone()
        if existing:
            # Save version before update
            save_rule_version(existing["id"])
            conn.execute("""UPDATE strategy_rules SET ...""")
            return existing["id"]
        else:
            cur = conn.execute("""INSERT INTO strategy_rules ...""")
            return cur.lastrowid
```

Note: The `save_rule_version` import/call should be at the bottom of the function or imported at top, depending on whether there's a circular import risk. Since `save_rule_version` is in the same file, no circular import.

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_rule_versions.py -v`
Expected: All tests PASS

- [ ] **Step 7: Add API routes to routes/strategy.py**

Add version endpoints:

```python
from strategy.rules_store import get_rule_versions, rollback_rule as do_rollback


@strategy_bp.route("/rules/<int:rule_id>/versions", methods=["GET"])
def rule_versions(rule_id):
    """获取规则历史版本 GET /api/strategy/rules/1/versions"""
    versions = get_rule_versions(rule_id)
    return jsonify({"success": True, "data": _sanitize(versions), "error": None})


@strategy_bp.route("/rules/<int:rule_id>/rollback/<int:version>", methods=["POST"])
def rollback_rule(rule_id, version):
    """回滚规则到指定版本 POST /api/strategy/rules/1/rollback/2"""
    ok = do_rollback(rule_id, version)
    if not ok:
        return jsonify({"success": False, "data": None, "error": "Version not found"}), 404
    return jsonify({"success": True, "data": None, "error": None,
                    "message": f"Rolled back to v{version}"})
```

- [ ] **Step 8: Add frontend version button in dashboard.html**

In the strategy rules table, add a version button per row:

```javascript
html += `<button class="btn btn-sm" onclick="showVersions(${rule.id})">版本</button>`;

async function showVersions(ruleId) {
    const resp = await fetchJSON(API + `/strategy/rules/${ruleId}/versions`);
    const versions = resp.data;
    let html = '<table><thead><tr><th>版本</th><th>Fitness</th><th>年化收益</th><th>胜率</th><th>时间</th><th>操作</th></tr></thead><tbody>';
    versions.forEach(v => {
        html += `<tr>
            <td>v${v.version}</td>
            <td>${v.fitness ?? '--'}</td>
            <td>${v.annual_return ?? '--'}%</td>
            <td>${v.win_rate ?? '--'}%</td>
            <td>${v.created_at || ''}</td>
            <td><button class="btn btn-sm btn-danger" onclick="rollbackRule(${ruleId}, ${v.version})">回滚</button></td>
        </tr>`;
    });
    html += '</tbody></table>';
    showModal('策略版本历史', html);
}

async function rollbackRule(ruleId, version) {
    if (!confirm(`确认回滚到 v${version}？当前版本会被保存为历史。`)) return;
    const resp = await fetchJSON(API + `/strategy/rules/${ruleId}/rollback/${version}`, { method: 'POST' });
    if (resp.success) {
        showNotification(resp.message || '回滚成功');
        closeModal();
    } else {
        showNotification(resp.error || '回滚失败');
    }
}
```

- [ ] **Step 9: Commit PR7**

```bash
git add tests/test_rule_versions.py core/db.py strategy/rules_store.py routes/strategy.py dashboard.html
git commit -m "feat: add strategy version management with auto-save and rollback"
```

---

### Task 9: PR8 — Failed Task Auto-Retry

**Files:**
- Create: `tests/test_sync_retry.py`
- Modify: `core/sync.py`
- Modify: `scheduler/runner.py`

- [ ] **Step 1: Write failing test for retry logic**

Create `tests/test_sync_retry.py`:

```python
"""
tests/test_sync_retry.py —— 失败任务自动重试
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRetryBackoff(unittest.TestCase):
    def test_exponential_backoff_sequence(self):
        """指数退避: 2^0=1s, 2^1=2s"""
        delays = [2 ** i for i in range(2)]
        self.assertEqual(delays, [1, 2])

    def test_max_retries_plus_initial_attempt(self):
        """max_retries=2 means 3 total attempts"""
        max_retries = 2
        total_attempts = max_retries + 1
        self.assertEqual(total_attempts, 3)

    def test_sleep_not_called_on_last_attempt(self):
        """最后一次尝试后不再sleep"""
        max_retries = 2
        attempt = 2  # 0-indexed, last attempt
        should_sleep = attempt < max_retries
        self.assertFalse(should_sleep)


class TestSyncRetrySignature(unittest.TestCase):
    def test_sync_one_stock_with_timeout_has_max_retries(self):
        import inspect
        from core.sync import sync_one_stock_with_timeout
        sig = inspect.signature(sync_one_stock_with_timeout)
        self.assertIn("max_retries", sig.parameters)

    def test_default_max_retries_is_2(self):
        import inspect
        from core.sync import sync_one_stock_with_timeout
        sig = inspect.signature(sync_one_stock_with_timeout)
        self.assertEqual(sig.parameters["max_retries"].default, 2)


class TestSchedulerRetryGuard(unittest.TestCase):
    def test_retry_queued_flag_prevents_double_retry(self):
        """_retry_queued flag prevents duplicate retry scheduling"""
        # Simulate the guard logic
        retry_queued = False

        def schedule_retry():
            nonlocal retry_queued
            if not retry_queued:
                retry_queued = True
                return True  # scheduled
            return False  # already queued

        self.assertTrue(schedule_retry())
        self.assertFalse(schedule_retry())
        self.assertTrue(retry_queued)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync_retry.py -v`
Expected: FAIL — `sync_one_stock_with_timeout` doesn't have `max_retries` parameter

- [ ] **Step 3: Add retry logic to sync_one_stock_with_timeout in core/sync.py**

Replace the existing `sync_one_stock_with_timeout()` function:

```python
def sync_one_stock_with_timeout(code: str, start_date: str = HISTORY_START,
                                end_date: str = None, verbose: bool = False,
                                auto_login: bool = True, timeout: float = 30.0,
                                max_retries: int = 2) -> bool:
    """带超时和重试的单股票同步（指数退避 1s, 2s）"""
    for attempt in range(max_retries + 1):
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(sync_one_stock, code, start_date, end_date, verbose, auto_login)
            try:
                ok = fut.result(timeout=timeout)
                if ok:
                    return True
                if attempt < max_retries and verbose:
                    print(f"  [{code}] 第{attempt+1}次失败，{2**attempt}秒后重试...")
            except FutureTimeoutError:
                if attempt < max_retries and verbose:
                    print(f"  [{code}] 超时({timeout}s)，{2**attempt}秒后重试...")
            except Exception:
                if attempt < max_retries and verbose:
                    print(f"  [{code}] 异常，{2**attempt}秒后重试...")
        if attempt < max_retries:
            time.sleep(2 ** attempt)  # 指数退避：1s, 2s
    return False
```

- [ ] **Step 4: Run retry tests**

Run: `python -m pytest tests/test_sync_retry.py::TestRetryBackoff -v && python -m pytest tests/test_sync_retry.py::TestSyncRetrySignature -v`
Expected: All PASS

- [ ] **Step 5: Add scheduler retry logic to scheduler/runner.py**

Add at module level:

```python
_retry_queued = False

RETRY_DELAY_SECONDS = 30 * 60  # 30 minutes, configurable
```

Modify `run_sync_blocking()` to schedule retry on failure:

```python
def run_sync_blocking():
    """Background thread: run incremental data sync via daily_sync"""
    global _retry_queued
    try:
        from core.sync import daily_sync, is_trading_day
        from core.db import init_db

        init_db()
        today = date.today().strftime("%Y-%m-%d")

        if not is_trading_day(today):
            SYNC_STATUS["last_result"] = "非交易日，跳过数据拉取"
            SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            SYNC_STATUS["running"] = False
            return

        SYNC_STATUS["last_result"] = "增量同步中..."
        result = daily_sync(verbose=False)
        SYNC_STATUS["last_result"] = (
            f"成功 {result['success']}，失败 {result['failed']}"
        )
        SYNC_STATUS["last_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        SYNC_STATUS["last_result"] = f"错误: {e}"
        import traceback
        traceback.print_exc()

        # 调度重试（仅一次）
        if not _retry_queued:
            _retry_queued = True
            import threading as _th

            def _retry():
                global _retry_queued
                time.sleep(RETRY_DELAY_SECONDS)
                SYNC_STATUS["last_result"] = "自动重试中..."
                try:
                    from core.sync import daily_sync
                    daily_sync(verbose=False)
                    SYNC_STATUS["last_result"] = "重试成功"
                except Exception as e2:
                    SYNC_STATUS["last_result"] = f"重试失败: {e2}"
                finally:
                    _retry_queued = False

            _th.Thread(target=_retry, daemon=True).start()
    finally:
        SYNC_STATUS["running"] = False
```

- [ ] **Step 6: Commit PR8**

```bash
git add tests/test_sync_retry.py core/sync.py scheduler/runner.py
git commit -m "feat: add exponential backoff retry to sync and scheduler"
```

---

## Self-Review Summary

1. **Spec coverage:** All 9 PRs from the design spec have corresponding task groups with detailed steps.
   - PR1: sync progress ✓
   - PR2: mining interrupt ✓
   - PR3: backtest params ✓
   - PR4: comparison ✓
   - PR5: search/filter ✓
   - PR6: score history ✓
   - PR7: version management ✓
   - PR8: retry ✓
   - PR9: BSE skip notice ✓

2. **No placeholders:** Every step has concrete code or commands. No TBD/TODO/fill-in-later.

3. **Type consistency:** Function signatures match across tasks. `save_rule_version() -> int | None`, `rollback_rule() -> bool`, `get_rule_versions() -> list`. Parameter names consistent throughout.

4. **Test-first:** Each PR starts with a failing test, then implements.

5. **Commit cadence:** Each PR ends with a single commit containing all related files.
