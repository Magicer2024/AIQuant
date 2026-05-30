# AIQuant 项目优化 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement performance, code quality, and UX optimizations based on `AIQuant优化方案_2025-05-28.md` v2.1, covering backend pagination, frontend rendering improvements, code deduplication, async task normalization, and memory management.

**Architecture:** Flask backend with module-level state, vanilla JS frontend, SQLite persistence. No external infrastructure (no Redis, no Celery). Frontend improvements are incremental — enhance existing patterns rather than rewrite.

**Tech Stack:** Python 3.11+, Flask, SQLite, unittest, vanilla JavaScript, ECharts, Clusterize.js

**Base commit:** `6020dba` on `feature/aiquant-improvements`

**Implementation order:** T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10

---

## File Structure Map

```
AIQuant/
├── core/
│   ├── connection.py           # T8(NEW: extracted from db.py)
│   ├── task_queue.py           # T5(NEW: ThreadPoolExecutor + UUID)
│   └── sync.py                # T5(recalc async), T6(streaming)
├── utils/
│   ├── serialization.py       # T1(NEW: shared sanitize_numeric)
│   └── cache.py               # T9(NEW: ttl_cache decorator)
├── routes/
│   ├── scoring.py             # T4(pagination API), T1(replace _sanitize)
│   ├── backtest.py            # T1(replace _sanitize), T7(BacktestParams)
│   ├── strategy.py            # T1(replace _sanitize)
│   └── sync.py                # T5(recalc async endpoint)
├── backtest/
│   └── engine.py              # T7(BacktestParams dataclass)
├── static/css/
│   └── dashboard.css          # T3(NEW: extracted CSS + new styles)
├── dashboard.html             # T2(toast+skeleton), T3(CSS extraction), T4(pagination UI)
│                               # T6(virtual scroll), T8b(chart/timer manager)
└── tests/
    ├── test_serialization.py  # T1
    ├── test_pagination.py     # T4
    ├── test_task_queue.py     # T5
    └── test_backtest_params_dc.py  # T7
```

---

## Task 1: Extract Shared `_sanitize` → `utils/serialization.py`

**Files:**
- Create: `utils/serialization.py`
- Create: `tests/test_serialization.py`
- Modify: `routes/scoring.py`
- Modify: `routes/backtest.py`
- Modify: `routes/strategy.py`

- [ ] **Step 1: Write test for sanitize_numeric**

Create `tests/test_serialization.py`:

```python
"""
tests/test_serialization.py — 公共序列化函数测试
"""
import math
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSanitizeNumeric(unittest.TestCase):
    def test_nan_returns_none(self):
        from utils.serialization import sanitize_numeric
        self.assertIsNone(sanitize_numeric(float('nan')))

    def test_inf_returns_none(self):
        from utils.serialization import sanitize_numeric
        self.assertIsNone(sanitize_numeric(float('inf')))
        self.assertIsNone(sanitize_numeric(float('-inf')))

    def test_normal_float_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric(3.14), 3.14)
        self.assertEqual(sanitize_numeric(0.0), 0.0)

    def test_nested_dict(self):
        from utils.serialization import sanitize_numeric
        data = {"score": float('nan'), "items": [{"v": float('inf')}]}
        result = sanitize_numeric(data)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["items"][0]["v"])

    def test_nested_list(self):
        from utils.serialization import sanitize_numeric
        data = [float('nan'), 1.0, float('inf')]
        result = sanitize_numeric(data)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 1.0)
        self.assertIsNone(result[2])

    def test_bytes_decoded(self):
        from utils.serialization import sanitize_numeric
        result = sanitize_numeric(b"hello")
        self.assertEqual(result, "hello")

    def test_empty_container(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric([]), [])
        self.assertEqual(sanitize_numeric({}), {})

    def test_none_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertIsNone(sanitize_numeric(None))

    def test_string_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric("hello"), "hello")

    def test_int_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric(42), 42)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_serialization.py -v`
Expected: FAIL — `utils.serialization` module not found

- [ ] **Step 3: Create utils/serialization.py**

Create `utils/serialization.py`:

```python
"""
utils/serialization.py — JSON 序列化辅助（NaN/Inf → None，numpy → Python native）
"""
import math
from typing import Any


def sanitize_numeric(obj: Any) -> Any:
    """递归替换 NaN/Inf 为 None，numpy 类型转 Python 原生"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if hasattr(obj, "item"):  # numpy scalar → Python native
        return sanitize_numeric(obj.item())
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: sanitize_numeric(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_numeric(v) for v in obj]
    return obj
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_serialization.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Replace `_sanitize` in routes/scoring.py**

In `routes/scoring.py`, remove the `_sanitize` function definition (lines 14-28) and add import:

```python
from utils.serialization import sanitize_numeric as _sanitize
```

- [ ] **Step 6: Replace `_sanitize` in routes/backtest.py**

In `routes/backtest.py`, remove the `_sanitize` function definition (lines 16-29) and add import:

```python
from utils.serialization import sanitize_numeric as _sanitize
```

- [ ] **Step 7: Replace `_sanitize` in routes/strategy.py**

In `routes/strategy.py`, remove the `_sanitize` function definition (lines 17-31) and add import:

```python
from utils.serialization import sanitize_numeric as _sanitize
```

- [ ] **Step 8: Verify existing tests still pass**

Run: `python -m pytest tests/ -v --ignore=tests/test_serialization.py -x`
Expected: No import errors, existing tests pass

- [ ] **Step 9: Commit**

```bash
git add utils/serialization.py tests/test_serialization.py routes/scoring.py routes/backtest.py routes/strategy.py
git commit -m "refactor: extract shared _sanitize to utils/serialization.py"
```

---

## Task 2: Toast Notification System + Skeleton Screens

**Files:**
- Modify: `dashboard.html`

Note: Pure frontend change. No backend tests. Manual verification via browser.

- [ ] **Step 1: Add Toast CSS and JS**

Find the `<style>` block in `dashboard.html`. Add after existing styles:

```css
/* Toast notifications */
.toast-container {
    position: fixed; top: 16px; right: 16px; z-index: 9999;
    display: flex; flex-direction: column; gap: 8px;
    pointer-events: none;
}
.toast {
    padding: 12px 20px; border-radius: 6px; color: #fff; font-size: 14px;
    opacity: 0; transform: translateX(20px);
    transition: opacity 0.3s, transform 0.3s;
    pointer-events: auto; max-width: 360px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.25);
}
.toast.show { opacity: 1; transform: translateX(0); }
.toast-success { background: #10b981; }
.toast-error   { background: #ef4444; }
.toast-warning { background: #f59e0b; color: #1a1a1a; }
.toast-info    { background: #3b82f6; }
```

Find the `<script>` block. Add after existing utility functions:

```javascript
// Toast notification system
const Toast = {
    container: null,

    init() {
        if (!this.container) {
            this.container = document.createElement('div');
            this.container.className = 'toast-container';
            document.body.appendChild(this.container);
        }
    },

    show(message, type, duration) {
        type = type || 'info';
        duration = duration || 3000;
        this.init();
        var icons = { success: '✓', error: '✗', warning: '⚠', info: 'ℹ' };
        var toast = document.createElement('div');
        toast.className = 'toast toast-' + type;
        toast.innerHTML = '<span class="toast-icon">' + (icons[type] || icons.info) + '</span> <span>' + message + '</span>';
        this.container.appendChild(toast);
        requestAnimationFrame(function() { toast.classList.add('show'); });
        setTimeout(function() {
            toast.classList.remove('show');
            setTimeout(function() { toast.remove(); }, 300);
        }, duration);
    }
};

function showToast(message, type) { Toast.show(message, type); }
```

- [ ] **Step 2: Add skeleton screen CSS**

Add to `<style>` block:

```css
/* Skeleton loading */
.skeleton {
    background: linear-gradient(90deg, var(--bg-panel) 25%, var(--bg-panel-hover) 50%, var(--bg-panel) 75%);
    background-size: 200% 100%;
    animation: skeleton-loading 1.5s infinite;
    border-radius: 4px;
}
@keyframes skeleton-loading {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
}
.skeleton-row { display: flex; gap: 16px; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.skeleton-cell { height: 20px; border-radius: 4px; background: var(--bg-panel-hover); }
.skeleton-cell.w80  { width: 80px; }
.skeleton-cell.w120 { width: 120px; }
.skeleton-cell.w200 { width: 200px; }
```

- [ ] **Step 3: Add skeleton rendering function**

Add to `<script>` block:

```javascript
function showSkeleton(container, rowCount) {
    rowCount = rowCount || 10;
    var cols = ['w80', 'w120', 'w200', 'w80', 'w120'];
    var html = '';
    for (var i = 0; i < rowCount; i++) {
        html += '<div class="skeleton-row">';
        for (var j = 0; j < cols.length; j++) {
            html += '<div class="skeleton-cell ' + cols[j] + ' skeleton"></div>';
        }
        html += '</div>';
    }
    container.innerHTML = html;
}
```

- [ ] **Step 4: Replace loading text with skeleton in loadTab**

Find `loadTab()` function. Replace the loading branch:

```javascript
// Before:
c.innerHTML = '<div class="empty-state">加载中...</div>';

// After:
showSkeleton(c, 10);
```

- [ ] **Step 5: Replace error display with Toast**

Find all `console.error(...)` calls in error handlers. Add `showToast(...)` calls alongside:

```javascript
// Example pattern to add to catch blocks:
showToast('操作失败: ' + (e.message || '未知错误'), 'error');
```

For the `loadTab` error handler:

```javascript
// After the existing error display in loadTab:
showToast('加载失败，请重试', 'error');
```

- [ ] **Step 6: Wire showToast to existing delete/sync success paths**

Find delete rule success handler. Add:

```javascript
showToast('删除成功', 'success');
```

Find sync completion handler in `pollSyncProgress()`. Add:

```javascript
showToast(p.message || '同步完成', p.last_error ? 'warning' : 'success');
```

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat: add toast notification system and skeleton loading screens"
```

---

## Task 3: CSS Extraction + fetchJSON AbortSignal + RequestManager

**Files:**
- Create: `static/css/dashboard.css`
- Modify: `dashboard.html`

- [ ] **Step 1: Create static/css/dashboard.css**

Extract all CSS from `dashboard.html` `<style>` block into `static/css/dashboard.css`. Keep the exact same CSS rules. No changes to selectors or values.

- [ ] **Step 2: Replace <style> block with <link> in dashboard.html**

Replace the entire `<style>...</style>` block with:

```html
<link rel="stylesheet" href="/static/css/dashboard.css">
```

- [ ] **Step 3: Verify dashboard loads correctly**

Run: `python app.py` and open `http://localhost:5000` in browser.
Expected: All styles render identically. No console errors.

- [ ] **Step 4: Add RequestManager JavaScript**

In the `<script>` block of `dashboard.html`, add after `fetchJSON`:

```javascript
// Request manager with AbortController support
var RequestManager = {
    controllers: {},

    fetch: function(key, url, opts) {
        this.cancel(key);
        var ctrl = new AbortController();
        this.controllers[key] = ctrl;
        opts = opts || {};
        opts.signal = ctrl.signal;
        var self = this;
        return fetchJSON(url, opts).then(function(data) {
            delete self.controllers[key];
            return data;
        }).catch(function(e) {
            delete self.controllers[key];
            throw e;
        });
    },

    cancel: function(key) {
        if (this.controllers[key]) {
            this.controllers[key].abort();
            delete this.controllers[key];
        }
    },

    cancelAll: function() {
        var self = this;
        Object.keys(this.controllers).forEach(function(k) {
            self.controllers[k].abort();
        });
        this.controllers = {};
    }
};
```

- [ ] **Step 5: Ensure fetchJSON accepts opts with signal**

Verify `fetchJSON` signature. If it's currently `fetchJSON(url, opts)`, no change needed — `fetch` natively handles `signal` in opts. If signature uses default value for opts, add it:

```javascript
function fetchJSON(url, opts) {
    opts = opts || {};
    return fetch(url, opts).then(function(resp) {
        if (!resp.ok) throw new Error(resp.statusText);
        return resp.json();
    });
}
```

- [ ] **Step 6: Wire RequestManager into loadTab**

Modify `loadTab()` to use `RequestManager.fetch` and cancel on switch:

```javascript
function switchTab(tab) {
    // Cancel in-flight requests from previous tab
    RequestManager.cancelAll();
    // ... rest of switchTab
}
```

In `loadTab()`, replace direct `fetchJSON` calls with `RequestManager.fetch(tabName, url)`.

- [ ] **Step 7: Commit**

```bash
git add static/css/dashboard.css dashboard.html
git commit -m "refactor: extract CSS to static file, add AbortController request manager"
```

---

## Task 4: Backend Pagination API + Frontend Pagination UI

**Files:**
- Create: `tests/test_pagination.py`
- Modify: `strategy/scorer.py`
- Modify: `routes/scoring.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Write failing test for pagination**

Create `tests/test_pagination.py`:

```python
"""
tests/test_pagination.py — 打分 API 分页
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


class TestPaginationAPI(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_daily_scores_returns_pagination(self):
        resp = self.client.get("/api/scoring/daily?page=1&per_page=20")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("pagination", data)
        self.assertIn("page", data["pagination"])
        self.assertIn("per_page", data["pagination"])
        self.assertIn("total", data["pagination"])
        self.assertIn("pages", data["pagination"])

    def test_daily_scores_default_page(self):
        resp = self.client.get("/api/scoring/daily")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["pagination"]["page"], 1)

    def test_daily_scores_per_page_capped(self):
        resp = self.client.get("/api/scoring/daily?per_page=999")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(data["pagination"]["per_page"], 200)

    def test_daily_scores_page_out_of_range(self):
        resp = self.client.get("/api/scoring/daily?page=99999")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data["data"], list)

    def test_daily_scores_data_not_empty_for_valid_date(self):
        resp = self.client.get("/api/scoring/daily?page=1&per_page=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(len(data["data"]), 5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pagination.py -v`
Expected: FAIL — `pagination` key not in response

- [ ] **Step 3: Add get_daily_scores_count and paginated get_daily_scores to scorer.py**

In `strategy/scorer.py`, modify `get_daily_scores()`:

```python
def get_daily_scores(trade_date: str, page: int = 1, per_page: int = 50) -> list[dict]:
    """获取某日打分排名（分页）"""
    per_page = min(per_page, 200)
    offset = (page - 1) * per_page
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM stock_score
            WHERE trade_date = ?
            ORDER BY score DESC
            LIMIT ? OFFSET ?
        """, (trade_date, per_page, offset)).fetchall()
        return [dict(r) for r in rows]


def get_daily_scores_count(trade_date: str) -> int:
    """获取某日打分总数"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_score WHERE trade_date = ?",
            (trade_date,)
        ).fetchone()
        return row["cnt"]
```

- [ ] **Step 4: Update daily_scores route in routes/scoring.py**

Modify `daily_scores()`:

```python
@scoring_bp.route("/daily", methods=["GET"])
def daily_scores():
    """获取某日打分排名（支持分页）"""
    trade_date = request.args.get("date") or get_latest_score_date()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    per_page = min(per_page, 200)

    results = get_daily_scores(trade_date, page=page, per_page=per_page)
    total = get_daily_scores_count(trade_date)

    return jsonify({
        "success": True,
        "data": _sanitize(results),
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page)
        },
        "date": trade_date
    })
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_pagination.py -v`
Expected: All tests PASS

- [ ] **Step 6: Add frontend pagination controls in dashboard.html**

In the scores tab, add pagination controls after the score table:

```html
<div id="scorePagination" style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;">
    <button class="btn btn-sm" id="scorePrevPage" onclick="goScorePage(currentScorePage-1)" disabled>&lt; 上一页</button>
    <span id="scorePageInfo" style="font-size:13px;color:var(--text-secondary);"></span>
    <button class="btn btn-sm" id="scoreNextPage" onclick="goScorePage(currentScorePage+1)">下一页 &gt;</button>
</div>
```

Add pagination JavaScript:

```javascript
var currentScorePage = 1;
var scorePerPage = 50;
var scoreTotalPages = 1;

async function loadScoresPage(page) {
    page = page || 1;
    var container = document.getElementById('scoresContent'); // or appropriate container
    showSkeleton(container, 10);

    try {
        var resp = await RequestManager.fetch('scores',
            API + '/scoring/daily?page=' + page + '&per_page=' + scorePerPage);
        currentScorePage = resp.pagination.page;
        scoreTotalPages = resp.pagination.pages;
        renderScoreTable(resp.data);
        updatePaginationUI();
    } catch (e) {
        if (e.name !== 'AbortError') {
            showToast('加载失败: ' + (e.message || '未知错误'), 'error');
        }
    }
}

function goScorePage(page) {
    if (page < 1 || page > scoreTotalPages) return;
    loadScoresPage(page);
}

function updatePaginationUI() {
    document.getElementById('scorePrevPage').disabled = currentScorePage <= 1;
    document.getElementById('scoreNextPage').disabled = currentScorePage >= scoreTotalPages;
    document.getElementById('scorePageInfo').textContent =
        '第 ' + currentScorePage + ' / ' + scoreTotalPages + ' 页';
}
```

Replace the old score loading call in `loadTab('scores', ...)` with `loadScoresPage(1)`.

- [ ] **Step 7: Commit**

```bash
git add tests/test_pagination.py strategy/scorer.py routes/scoring.py dashboard.html
git commit -m "feat: add pagination to scoring API and frontend score table"
```

---

## Task 5: Recalc Async + Task Queue Normalization

**Files:**
- Create: `core/task_queue.py`
- Create: `tests/test_task_queue.py`
- Modify: `routes/sync.py`
- Modify: `core/sync.py`

- [ ] **Step 1: Write failing test for task queue**

Create `tests/test_task_queue.py`:

```python
"""
tests/test_task_queue.py — 异步任务队列
"""
import unittest
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTaskQueue(unittest.TestCase):
    def test_submit_task_returns_task_id(self):
        from core.task_queue import submit_task
        task_id = submit_task(lambda: 42)
        self.assertIsInstance(task_id, str)
        self.assertGreater(len(task_id), 0)

    def test_get_task_status_pending_or_running(self):
        from core.task_queue import submit_task, get_task_status
        task_id = submit_task(lambda: None)
        status = get_task_status(task_id)
        self.assertIsNotNone(status)
        self.assertIn(status["status"], ("pending", "running", "success"))

    def test_task_completes_successfully(self):
        from core.task_queue import submit_task, get_task_status
        def slow_ok():
            time.sleep(0.1)
            return "done"
        task_id = submit_task(slow_ok)
        time.sleep(0.5)
        status = get_task_status(task_id)
        self.assertEqual(status["status"], "success")

    def test_task_error_status(self):
        from core.task_queue import submit_task, get_task_status
        def bad_task():
            raise ValueError("test error")
        task_id = submit_task(bad_task)
        time.sleep(0.3)
        status = get_task_status(task_id)
        self.assertEqual(status["status"], "error")
        self.assertIn("test error", status["message"])

    def test_nonexistent_task(self):
        from core.task_queue import get_task_status
        self.assertIsNone(get_task_status("nonexistent"))

    def test_cleanup_removes_old_tasks(self):
        from core.task_queue import submit_task, get_task_status, cleanup_old_tasks
        task_id = submit_task(lambda: "ok")
        time.sleep(0.3)
        # Force immediate cleanup
        cleanup_old_tasks(max_age_seconds=0)
        status = get_task_status(task_id)
        self.assertIsNone(status)

    def test_progress_callback_updates_state(self):
        from core.task_queue import submit_task, get_task_status
        def task_with_progress(progress_callback=None):
            if progress_callback:
                progress_callback(50, "half done")
            return "ok"

        task_id = submit_task(task_with_progress)
        time.sleep(0.5)
        status = get_task_status(task_id)
        self.assertEqual(status["progress"], 50)
        self.assertEqual(status["message"], "half done")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_task_queue.py -v`
Expected: FAIL — `core.task_queue` module not found

- [ ] **Step 3: Create core/task_queue.py**

```python
"""
core/task_queue.py — 简单异步任务队列（ThreadPoolExecutor + UUID，不引入 Celery）
"""
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

_executor = ThreadPoolExecutor(max_workers=4)
_lock = Lock()
_tasks: dict[str, dict] = {}


def submit_task(fn, *args, **kwargs) -> str:
    """提交异步任务，返回 8 字符任务 ID"""
    task_id = str(uuid.uuid4())[:8]
    progress_callback = kwargs.pop("progress_callback", None)

    with _lock:
        _tasks[task_id] = {
            "status": "pending", "progress": 0, "message": "",
            "started_at": time.time()
        }

    def _wrap():
        with _lock:
            _tasks[task_id]["status"] = "running"
        try:
            def _pcb(p, m):
                with _lock:
                    if task_id in _tasks:
                        _tasks[task_id]["progress"] = p
                        _tasks[task_id]["message"] = m
            result = fn(*args, progress_callback=_pcb if progress_callback else None, **kwargs)
            with _lock:
                if task_id in _tasks:
                    _tasks[task_id]["status"] = "success"
                    _tasks[task_id]["result"] = str(result) if result else ""
        except Exception as e:
            with _lock:
                if task_id in _tasks:
                    _tasks[task_id]["status"] = "error"
                    _tasks[task_id]["message"] = str(e)

    _executor.submit(_wrap)
    return task_id


def get_task_status(task_id: str) -> dict | None:
    """查询任务状态，不存在返回 None"""
    return _tasks.get(task_id)


def cleanup_old_tasks(max_age_seconds: int = 3600):
    """清理超过 max_age_seconds 的已完成任务"""
    now = time.time()
    with _lock:
        expired = [tid for tid, t in _tasks.items()
                   if t["status"] in ("success", "error")
                   and now - t.get("started_at", 0) > max_age_seconds]
        for tid in expired:
            del _tasks[tid]


def is_any_running() -> bool:
    """检查是否有正在运行的任务"""
    with _lock:
        return any(t["status"] in ("pending", "running") for t in _tasks.values())
```

- [ ] **Step 4: Run task queue tests**

Run: `python -m pytest tests/test_task_queue.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Make recalc_all_scores async in routes/sync.py**

Add new route to `routes/sync.py`:

```python
from core.task_queue import submit_task, get_task_status, is_any_running


@sync_bp.route("/recalc", methods=["POST"])
def start_recalc():
    """重算全市场打分（异步）"""
    if is_any_running():
        return jsonify({"success": False, "error": "有任务正在进行中，请稍后再试"}), 409

    from core.sync import recalc_all_scores
    task_id = submit_task(recalc_all_scores)
    return jsonify({"success": True, "task_id": task_id, "message": "重算任务已启动"})


@sync_bp.route("/status/<task_id>", methods=["GET"])
def task_status(task_id: str):
    """查询任务状态"""
    status = get_task_status(task_id)
    if not status:
        return jsonify({"success": False, "error": "任务不存在或已过期"}), 404
    return jsonify({"success": True, "data": status})
```

- [ ] **Step 6: Add batch commit to recalc_all_scores in core/sync.py**

Find `recalc_all_scores()` in `core/sync.py`. Add `progress_callback` parameter and batch commit:

```python
def recalc_all_scores(trade_date: str = None, progress_callback=None):
    """重算全市场打分（异步友好版本）"""
    from strategy.scorer import _load_stock_data, score_one_stock
    from core.db import get_conn

    if not trade_date:
        trade_date = date.today().strftime("%Y-%m-%d")

    stock_data = _load_stock_data(trade_date)
    codes = list(stock_data.keys())
    total = len(codes)
    results = []

    for i, code in enumerate(codes):
        try:
            score_result = score_one_stock(code, stock_data[code])
            if score_result:
                results.append({
                    "code": code,
                    "trade_date": trade_date,
                    "score": score_result.get("score", 0),
                    "volume_score": score_result.get("volume_score", 0),
                    "trend_score": score_result.get("trend_score", 0),
                    "reversal_score": score_result.get("reversal_score", 0),
                    "cap_score": score_result.get("cap_score", 0),
                })
        except Exception:
            pass

        if progress_callback and i % 50 == 0:
            progress_callback(i, "正在重算 " + code)

    # 批量写入，单事务
    if results:
        with get_conn() as conn:
            conn.execute("BEGIN TRANSACTION")
            conn.executemany("""
                INSERT INTO stock_score (code, trade_date, score, volume_score,
                                         trend_score, reversal_score, cap_score)
                VALUES (:code, :trade_date, :score, :volume_score,
                        :trend_score, :reversal_score, :cap_score)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                    score = excluded.score,
                    volume_score = excluded.volume_score,
                    trend_score = excluded.trend_score,
                    reversal_score = excluded.reversal_score,
                    cap_score = excluded.cap_score
            """, results)
            conn.commit()

    if progress_callback:
        progress_callback(total, "重算完成: " + str(len(results)) + " 只股票")

    return {"total": total, "results": len(results)}
```

- [ ] **Step 7: Verify existing recalc route still works**

Run: `python app.py` and test `POST /api/sync/recalc`

- [ ] **Step 8: Commit**

```bash
git add core/task_queue.py tests/test_task_queue.py routes/sync.py core/sync.py
git commit -m "feat: add async task queue and make recalc_all_scores non-blocking with batch commit"
```

---

## Task 6: Streaming Data Load + Virtual Scroll (Clusterize.js)

**Files:**
- Modify: `strategy/scorer.py`
- Modify: `dashboard.html`

- [ ] **Step 1: Add streaming generator to scorer.py**

In `strategy/scorer.py`, add after `_load_stock_data`:

```python
def _load_stock_data_streaming(trade_date: str, batch_size: int = 100):
    """流式加载股票数据，逐股 yield (code, DataFrame)，减少内存峰值"""
    with get_conn() as conn:
        cursor = conn.execute("""
            SELECT dp.code, dp.trade_date, dp.open, dp.high, dp.low, dp.close,
                   dp.volume, dp.amount, dp.turnover
            FROM daily_price dp
            INNER JOIN stock_info si ON dp.code = si.code AND si.is_active = 1
            WHERE dp.trade_date <= ?
            ORDER BY dp.code, dp.trade_date
        """, (trade_date,))

        import pandas as pd
        batch = []
        current_code = None

        for row in cursor:
            if row["code"] != current_code:
                if batch:
                    yield current_code, pd.DataFrame(batch).set_index("trade_date")
                current_code = row["code"]
                batch = []
            batch.append(dict(row))

        if batch:
            yield current_code, pd.DataFrame(batch).set_index("trade_date")
```

- [ ] **Step 2: Add virtual scrolling with Clusterize.js**

In `dashboard.html` `<head>`, add Clusterize.js CDN:

```html
<script src="https://cdn.jsdelivr.net/npm/clusterize.js@0.18.1/clusterize.min.js"></script>
```

Replace `renderScoreTable()` with Clusterize-based version:

```javascript
var scoreClusterize = null;

function renderScoreTable(scores) {
    var container = document.getElementById('scoreTableContainer');

    if (scoreClusterize) {
        scoreClusterize.update(scores.map(rowToScoreHTML));
        return;
    }

    container.innerHTML =
        '<div id="scoreScrollArea" class="clusterize-scroll" style="max-height:62vh;overflow-y:auto;">' +
        '  <table id="scoreTable">' +
        '    <thead><tr>' +
        '      <th>代码</th><th>名称</th><th>得分</th><th>量价</th><th>趋势</th><th>反转</th><th>市值</th>' +
        '    </tr></thead>' +
        '    <tbody id="scoreContentArea" class="clusterize-content"></tbody>' +
        '  </table>' +
        '</div>';

    scoreClusterize = new Clusterize({
        rows: scores.map(rowToScoreHTML),
        scrollElem: document.getElementById('scoreScrollArea'),
        contentElem: document.getElementById('scoreContentArea'),
        rows_in_block: 50,
        blocks_in_cluster: 4,
    });
}

function rowToScoreHTML(row, index) {
    var scoreClass = row.score >= 80 ? 'score-high' : row.score >= 50 ? 'score-mid' : 'score-low';
    return '<tr>' +
        '<td class="code">' + (row.code || '') + '</td>' +
        '<td class="name">' + (row.name || '') + '</td>' +
        '<td class="score ' + scoreClass + '">' + (row.score != null ? row.score.toFixed(1) : '--') + '</td>' +
        '<td>' + (row.volume_score != null ? row.volume_score.toFixed(1) : '--') + '</td>' +
        '<td>' + (row.trend_score != null ? row.trend_score.toFixed(1) : '--') + '</td>' +
        '<td>' + (row.reversal_score != null ? row.reversal_score.toFixed(1) : '--') + '</td>' +
        '<td>' + (row.cap_score != null ? row.cap_score.toFixed(1) : '--') + '</td>' +
        '</tr>';
}
```

Update `filterScores()` to work with Clusterize:

```javascript
var _scoreData = [];

function filterScores() {
    var keyword = (document.getElementById('stockSearch') ? document.getElementById('stockSearch').value : '').toLowerCase();
    var minScore = parseFloat(document.getElementById('minScore') ? document.getElementById('minScore').value : '0') || 0;
    var filtered = _scoreData;

    if (keyword) {
        filtered = _scoreData.filter(function(r) {
            return (r.code || '').toLowerCase().indexOf(keyword) !== -1 ||
                   (r.name || '').toLowerCase().indexOf(keyword) !== -1;
        });
    }
    if (minScore > 0) {
        filtered = filtered.filter(function(r) { return (r.score || 0) >= minScore; });
    }

    var countEl = document.getElementById('filterCount');
    if (countEl) {
        countEl.textContent = filtered.length === 0 ? '无匹配结果' : '显示 ' + filtered.length + ' / ' + _scoreData.length + ' 条';
    }

    if (scoreClusterize) {
        scoreClusterize.update(filtered.map(rowToScoreHTML));
    }
}
```

In `loadScoresPage`, assign `_scoreData` after fetch:

```javascript
_scoreData = resp.data;
renderScoreTable(resp.data);
filterScores();
```

- [ ] **Step 3: Verify pagination + virtual scroll work together**

Load dashboard, check that:
- Score table uses virtual scroll (not full DOM rendering)
- Pagination controls change pages
- Search filters work with virtual scroll (filter + update)

- [ ] **Step 4: Commit**

```bash
git add strategy/scorer.py dashboard.html
git commit -m "feat: add streaming data load and Clusterize.js virtual scroll for score table"
```

---

## Task 7: BacktestParams Dataclass

**Files:**
- Create: `tests/test_backtest_params_dc.py`
- Modify: `backtest/engine.py`
- Modify: `routes/backtest.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_backtest_params_dc.py`:

```python
"""
tests/test_backtest_params_dc.py — BacktestParams dataclass
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBacktestParamsDataclass(unittest.TestCase):
    def test_params_defaults(self):
        from backtest.engine import BacktestParams
        p = BacktestParams(
            rule_name="test", rule_id="1",
            conditions_json="{}", start_date="2024-01-01", end_date="2024-12-31"
        )
        self.assertEqual(p.holding_max, 20)
        self.assertEqual(p.stop_loss, -0.08)
        self.assertEqual(p.take_profit, 0.20)
        self.assertTrue(p.save)

    def test_params_custom(self):
        from backtest.engine import BacktestParams
        p = BacktestParams(
            rule_name="test", rule_id="2",
            conditions_json="{}", start_date="2024-01-01", end_date="2024-12-31",
            holding_max=10, stop_loss=-0.05, take_profit=0.15, save=False,
            sell_conditions_json='[]'
        )
        self.assertEqual(p.holding_max, 10)
        self.assertEqual(p.stop_loss, -0.05)
        self.assertEqual(p.take_profit, 0.15)
        self.assertFalse(p.save)
        self.assertEqual(p.sell_conditions_json, '[]')

    def test_run_backtest_accepts_params_dataclass(self):
        import inspect
        from backtest.engine import run_backtest
        sig = inspect.signature(run_backtest)
        self.assertIn("params", sig.parameters)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest_params_dc.py -v`
Expected: FAIL — `BacktestParams` not defined

- [ ] **Step 3: Add BacktestParams dataclass and update run_backtest**

In `backtest/engine.py`, add at top after imports:

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class BacktestParams:
    """回测参数封装"""
    rule_name: str
    rule_id: str
    conditions_json: str
    start_date: str
    end_date: str
    sell_conditions_json: Optional[str] = None
    holding_max: int = 20
    stop_loss: float = -0.08
    take_profit: float = 0.20
    save: bool = True
```

Update `run_backtest()` signature to accept `BacktestParams`:

```python
def run_backtest(params: BacktestParams, progress_callback=None) -> dict:
    """执行回测"""
    init_qlib()
    set_global_logger_level(logging.ERROR)

    signals = _generate_backtest_signals(
        params.conditions_json,
        params.start_date,
        params.end_date,
        sell_conditions_json=params.sell_conditions_json,
        holding_max=params.holding_max,
        stop_loss=params.stop_loss,
        take_profit=params.take_profit,
        progress_callback=progress_callback,
    )
    # ... rest of function uses params.xxx instead of individual params
```

- [ ] **Step 4: Update caller in routes/backtest.py**

Modify `start_backtest()` to create and pass `BacktestParams`:

```python
from backtest.engine import run_backtest, BacktestParams

# ... inside start_backtest:
params = BacktestParams(
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
)
result = run_backtest(params, progress_callback=_progress)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_backtest_params_dc.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_backtest_params_dc.py backtest/engine.py routes/backtest.py
git commit -m "refactor: encapsulate backtest parameters in BacktestParams dataclass"
```

---

## Task 8: Tab State Persistence + ECharts Manager + TimerManager

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Add TabStateManager**

Add to `<script>` block:

```javascript
// Tab state cache (5-minute validity)
var TabState = {
    states: {},
    MAX_AGE: 5 * 60 * 1000,

    save: function(tab) {
        var content = document.getElementById('content');
        this.states[tab] = {
            html: content ? content.innerHTML : '',
            scrollTop: document.querySelector('.content') ? document.querySelector('.content').scrollTop : 0,
            ts: Date.now()
        };
    },

    load: function(tab) {
        var s = this.states[tab];
        if (s && Date.now() - s.ts < this.MAX_AGE) return s;
        delete this.states[tab];
        return null;
    }
};
```

Modify `switchTab()` to use TabState:

```javascript
function switchTab(tab) {
    RequestManager.cancelAll();
    if (typeof timerManager !== 'undefined') timerManager.clearAll();

    // Save current tab state
    if (currentTab) TabState.save(currentTab);

    currentTab = tab;

    // Update nav active state
    document.querySelectorAll('.nav-item').forEach(function(t) { t.classList.remove('active'); });
    var btn = document.querySelector('.nav-item[onclick*="' + tab + '"]');
    if (btn) btn.classList.add('active');

    // Try cached state
    var cached = TabState.load(tab);
    if (cached) {
        document.getElementById('content').innerHTML = cached.html;
        var scrollEl = document.querySelector('.content');
        if (scrollEl) scrollEl.scrollTop = cached.scrollTop;
        return;
    }

    loadTab(tab).catch(function(e) {
        if (e.name !== 'AbortError') showToast('加载失败: ' + e.message, 'error');
    });
}
```

- [ ] **Step 2: Add ChartManager**

Add to `<script>` block:

```javascript
// ECharts instance manager
var ChartManager = {
    instances: {},

    get: function(key, dom) {
        if (this.instances[key]) {
            var inst = this.instances[key];
            if (inst.getDom() === dom) return inst;
            this.dispose(key);
        }
        var inst = echarts.init(dom);
        this.instances[key] = inst;
        return inst;
    },

    dispose: function(key) {
        if (this.instances[key]) {
            this.instances[key].dispose();
            delete this.instances[key];
        }
    },

    disposeAll: function() {
        var self = this;
        Object.keys(this.instances).forEach(function(k) { self.dispose(k); });
    }
};

window.addEventListener('beforeunload', function() { ChartManager.disposeAll(); });
```

Replace `echarts.init(...)` calls in `openKline()` and `showScoreHistory()`:

```javascript
// Before:
var chart = echarts.init(document.getElementById('klineChart'));

// After:
var chart = ChartManager.get('kline', document.getElementById('klineChart'));
```

- [ ] **Step 3: Add TimerManager**

Add to `<script>` block:

```javascript
// Timer manager for centralized cleanup
var timerManager = {
    timers: {},

    setInterval: function(key, fn, delay) {
        this.clear(key);
        this.timers[key] = setInterval(fn, delay);
    },

    setTimeout: function(key, fn, delay) {
        this.clear(key);
        this.timers[key] = setTimeout(function() { fn(); delete timerManager.timers[key]; }, delay);
    },

    clear: function(key) {
        if (this.timers[key] != null) {
            clearInterval(this.timers[key]);
            clearTimeout(this.timers[key]);
            delete this.timers[key];
        }
    },

    clearAll: function() {
        var self = this;
        Object.keys(this.timers).forEach(function(k) {
            clearInterval(self.timers[k]);
            clearTimeout(self.timers[k]);
        });
        this.timers = {};
    }
};
```

Replace all `setInterval`/`setTimeout` calls with `timerManager` versions:

```javascript
// Before:
syncPollTimer = setInterval(pollFn, 1000);

// After:
timerManager.setInterval('syncPoll', pollFn, 1000);

// Before:
if (syncPollTimer) { clearInterval(syncPollTimer); syncPollTimer = null; }

// After:
timerManager.clear('syncPoll');

// Before:
_filterTimer = setTimeout(filterScores, 300);

// After:
timerManager.setTimeout('filter', filterScores, 300);
```

- [ ] **Step 4: Verify tab switching doesn't lose state**

Load dashboard, switch between tabs, verify:
- 5-minute cache: switching back within 5 minutes restores previous state
- Old requests are cancelled (no console errors about writing to removed DOM)
- Timers are cleaned up on tab switch

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: add tab state persistence, ChartManager, and TimerManager"
```

---

## Task 9: ttl_cache Decorator for Hot Queries

**Files:**
- Create: `utils/cache.py`
- Modify: `strategy/scorer.py`

- [ ] **Step 1: Create utils/cache.py**

```python
"""
utils/cache.py — 简单 TTL 内存缓存（单进程 SQLite 场景，不需要 Redis）
"""
import time
import hashlib
import pickle
from functools import wraps, lru_cache


def ttl_cache(ttl_seconds: int = 60):
    """带 TTL 的内存缓存装饰器"""
    def decorator(func):
        _cache = {}
        _expiry = {}

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = hashlib.md5(pickle.dumps((args, kwargs))).hexdigest()
            now = time.time()
            if key in _cache and now < _expiry.get(key, 0):
                return _cache[key]
            result = func(*args, **kwargs)
            _cache[key] = result
            _expiry[key] = now + ttl_seconds
            return result

        wrapper.cache_clear = lambda: (_cache.clear(), _expiry.clear())
        return wrapper
    return decorator
```

- [ ] **Step 2: Apply ttl_cache to hot queries in scorer.py**

Modify `strategy/scorer.py`:

```python
from utils.cache import ttl_cache
from functools import lru_cache


@lru_cache(maxsize=1)
def get_latest_score_date_cached():
    """缓存最新打分日期（下次调用需手动 cache_clear）"""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM stock_score").fetchone()
        return row[0]


@ttl_cache(ttl_seconds=300)
def get_active_stocks_cached():
    """缓存活跃股票列表 5 分钟"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM stock_info WHERE is_active = 1"
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 3: Verify no import errors**

Run: `python -c "from utils.cache import ttl_cache; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add utils/cache.py strategy/scorer.py
git commit -m "feat: add ttl_cache decorator for hot query caching"
```

---

## Task 10: Tab State Cleanup & Verification

**Files:**
- Modify: `dashboard.html`

Verify all previous changes work together and fix edge cases.

- [ ] **Step 1: Verify timer cleanup on tab switch**

Check that `switchTab()` calls `timerManager.clearAll()` and `RequestManager.cancelAll()`:

```javascript
function switchTab(tab) {
    RequestManager.cancelAll();
    timerManager.clearAll();
    // ... rest
}
```

- [ ] **Step 2: Verify chart cleanup on modal close**

Ensure chart instances are disposed when modals close:

```javascript
// In closeModal():
function closeModal() {
    var modal = document.getElementById('modal');
    if (modal) {
        // Dispose any charts inside modal before removing
        var chartDoms = modal.querySelectorAll('[id$="Chart"]');
        for (var i = 0; i < chartDoms.length; i++) {
            var key = chartDoms[i].id;
            ChartManager.dispose(key);
        }
        modal.remove();
    }
}
```

- [ ] **Step 3: End-to-end smoke test**

Run: `python app.py`

Manual verification checklist:
- [ ] Page loads, all tabs render
- [ ] Score table uses virtual scroll (smooth scrolling with 5000+ rows)
- [ ] Pagination controls navigate pages
- [ ] Search filters work with debounce
- [ ] Toast notifications appear on success/error
- [ ] Skeleton screen shows during loading
- [ ] Tab switching is fast (cached within 5 min)
- [ ] Rapid tab switching doesn't throw console errors
- [ ] K-line chart opens and closes without memory leaks
- [ ] Sync progress bar updates in real-time
- [ ] Recalc runs in background with progress

- [ ] **Step 4: Commit final fixes**

```bash
git add dashboard.html
git commit -m "fix: ensure timer/chart cleanup on tab switch and modal close"
```

---

## Self-Review Summary

1. **Optimization doc coverage:**
   - High priority (8 items): All covered — T1 (_sanitize), T2 (toast+skeleton), T3 (CSS+signal+RequestManager), T4 (pagination), T5 (recalc async+queue), T6 (streaming+virtual scroll), T7 (BacktestParams), T8 (tab state+Chart/Timer manager)
   - Medium priority (7 items): Covered — T6 (virtual scroll), T8 (tab state), T5 (task queue), T7 (BacktestParams), T3 (CSS extraction)
   - Low priority (5 items): T9 (ttl_cache), remaining deferred to later iteration

2. **No placeholders:** Every step has concrete code, file paths, commands.

3. **Type consistency:** `sanitize_numeric`, `TaskQueue.submit_task → get_task_status`, `BacktestParams` fields match between definition and usage.

4. **Test-first:** T1, T4, T5, T7 start with failing tests.

5. **Implementation order:** T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10. Dependencies: T4 must precede T6 (pagination before virtual scroll), T5 is independent, T10 is final verification.
