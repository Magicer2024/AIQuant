# AIQuant 项目功能改进 — 设计文档

> 版本：v1.0 | 日期：2026-05-26
> 基于 `docs/功能改进方案.md`，经 brainstorm 讨论修订

---

## 整体架构

**9项改进，3阶段，每项独立PR，TDD驱动。**

```
阶段一（高优先级，3项）
├── PR1: 数据同步进度显示  → routes/system.py, core/sync.py, dashboard.html
├── PR2: 策略挖掘中断机制  → strategy/mining_state.py, routes/strategy.py, strategy/miner.py, dashboard.html
└── PR3: 回测参数可配置    → backtest/engine.py, routes/backtest.py, dashboard.html

阶段二（中优先级，3项）
├── PR4: 回测结果对比功能  → routes/backtest.py, dashboard.html
├── PR5: 前端搜索与筛选    → dashboard.html
└── PR6: 打分历史趋势      → routes/scoring.py, dashboard.html

阶段三（低优先级，3项）
├── PR7: 策略版本管理      → core/db.py, strategy/rules_store.py, routes/strategy.py, dashboard.html
├── PR8: 失败任务自动重试  → core/sync.py, scheduler/runner.py
└── PR9: 北交所数据支持提示 → core/sync.py, dashboard.html
```

**设计原则：**
- 后端状态用 Flask 模块级全局变量（单进程，够用），不引入 Redis/队列
- 前端用 `fetch` + 轮询（1秒间隔），不引入 WebSocket
- 每项先写测试再实现（TDD）
- 新API路由走现有 Blueprint，不改路由架构
- 参数持久化用前端 `localStorage`（Flask 未配置 secret_key，session 不可用）

---

## 阶段一：高优先级

### PR1: 数据同步进度显示

**状态管理** (`routes/system.py`):

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
```

状态放在路由层，`core/sync.py` 不做 HTTP 状态管理。

**同步层** (`core/sync.py`):

`daily_sync()` 接受可选 `progress_callback(current, total, success, failed)` 参数。每完成一只股票调用回调。路由层传入回调函数更新 `_sync_progress`。

**API:**

- `POST /api/sync` — 启动同步。若 `_sync_progress["running"]` 已为 `True`，返回 409 Conflict（并发安全）
- `GET /api/sync/progress` — 查询进度，返回 `_sync_progress` 字典

**前端** (`dashboard.html`):

点击"同步"按钮 → 调 POST /api/sync → 启动 1秒间隔轮询 GET /api/sync/progress → 渲染进度条（current/total + 百分比 + success/failed 计数）→ `running=false` 时停止轮询，弹完成提示。

**验收标准:**
- [ ] 前端显示进度条（百分比 + 数量）
- [ ] 每秒刷新一次进度
- [ ] 同步完成/失败时有明确提示
- [ ] 并发安全：多次点击不会创建多个进度状态
- [ ] last_error 字段记录最近错误

---

### PR2: 策略挖掘中断机制

**状态模块** (`strategy/mining_state.py` — 新建):

独立模块消除循环导入风险：

```python
_mining_status = {
    "running": False,
    "progress": None,
    "result": None,
    "stop_requested": False,
}

def get_mining_status() -> dict:
    return _mining_status

def update_mining_status(updates: dict):
    """部分更新状态，避免替换整个字典对象"""
    _mining_status.update(updates)

def request_stop():
    _mining_status["stop_requested"] = True
```

`routes/strategy.py` 和 `strategy/miner.py` 都通过 `mining_state` 模块访问状态，不互相导入。

**API** (`routes/strategy.py`):

- `POST /api/strategy/mine` — 启动挖掘（已有），重置 `stop_requested=False`
- `GET /api/strategy/mine/status` — 查询进度（已有）
- `POST /api/strategy/mine/stop` — 请求停止。多次调用幂等，已停止返回 200 + 提示 `{"message": "No mining in progress"}`（不返回 400）
- `POST /api/strategy/mine/reset` — 重置状态，允许重新挖掘

**挖掘引擎** (`strategy/miner.py`):

```python
from strategy.mining_state import get_mining_status

def mine_strategies(trade_date):
    for generation in range(max_generations):
        if get_mining_status().get("stop_requested"):
            print("[Miner] Stop requested, saving current progress...")
            save_partial_results()
            break
        # ... 挖掘逻辑
```

**前端** (`dashboard.html`):

挖掘中"开始挖掘"按钮切换为"停止挖掘"按钮，调用 `/api/strategy/mine/stop`。停止完成后显示"重置"按钮。

**验收标准:**
- [ ] 挖掘中显示"停止"按钮
- [ ] 点击后10秒内停止挖掘（保存部分结果后退出）
- [ ] 停止后状态正确更新
- [ ] 停止操作幂等（多次调用不报错）
- [ ] 可重置状态重新挖掘

---

### PR3: 回测参数可配置

**引擎** (`backtest/engine.py`):

`_generate_backtest_signals()` 新增参数：

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

替换第313行 `DEFAULT_STOP_LOSS` → `stop_loss`，第320行 `DEFAULT_TAKE_PROFIT` → `take_profit`。

`run_backtest()` 同样新增参数并透传给 `_generate_backtest_signals()`。

删除模块级常量 `DEFAULT_STOP_LOSS` / `DEFAULT_TAKE_PROFIT`。

**API** (`routes/backtest.py`):

```python
@backtest_bp.route("/run", methods=["POST"])
def start_backtest():
    body = request.get_json(silent=True) or {}
    stop_loss = max(min(body.get("stop_loss", -0.08), 0), -0.50)   # 钳制 -50%~0%
    take_profit = max(min(body.get("take_profit", 0.20), 1.00), 0) # 钳制 0%~100%
    holding_max = max(min(body.get("holding_max", 20), 100), 1)    # 钳制 1~100
```

**前端** (`dashboard.html`):

回测面板顶部参数区：止损(%)、止盈(%)、最大持仓(天) 三个数字输入框，默认值 -8/20/20。前端校验：止损 -50~0，止盈 0~100，持仓 1~100。值保存到 `localStorage`，下次自动填充。

**验收标准:**
- [ ] 前端可配置止损/止盈/最大持仓
- [ ] 参数正确传递到回测引擎
- [ ] 使用默认值时行为不变
- [ ] 参数范围校验（前端+后端双重校验，钳制非法值）
- [ ] 异常参数不会导致引擎崩溃
- [ ] 参数偏好通过 localStorage 持久化

---

## 阶段二：中优先级

### PR4: 回测结果对比功能

**API** (`routes/backtest.py`):

```python
@backtest_bp.route("/compare", methods=["GET"])
def compare_results():
    """GET /api/backtest/compare?ids=1,2,3"""
    ids = request.args.get("ids", "").split(",")
    results = []
    for rid in ids:
        try:
            detail = build_result_detail(int(rid))
            if detail:
                summary = detail["summary"]
                summary["result_id"] = int(rid)
                summary["rule_name"] = detail.get("rule_name", "")
                results.append(summary)
        except (ValueError, TypeError):
            continue
    return jsonify({"success": True, "data": results})
```

**前端** (`dashboard.html`):

回测结果表格每行前加复选框。底部"对比选中"按钮，选 <2 条时置灰。弹窗展示对比表格：

| 指标 | 规则A | 规则B | 规则C |
|------|-------|-------|-------|
| 年化收益 | **15.2%** (绿) | 10.1% | 8.3% |
| 胜率 | 52% | **58%** (绿) | 45% |
| ... | | | |

高亮逻辑：年化收益最高 → 绿色，最大回撤最小 → 绿色。缺失指标显示 "--"。表格支持横向滚动（响应式）。

**验收标准:**
- [ ] 可勾选多个回测结果
- [ ] 弹窗展示对比表格
- [ ] 关键指标高亮显示
- [ ] 对比弹窗关闭后复选框状态保留
- [ ] 空值处理：某指标缺失时显示"--"
- [ ] 响应式布局：窄屏下可横向滚动

---

### PR5: 前端搜索与筛选

**纯前端改动** (`dashboard.html`):

打分Tab顶部搜索栏：

```html
<div class="input-row">
    <span>搜索</span>
    <input type="text" id="stockSearch" placeholder="代码或名称"
           oninput="debounceFilter()" onkeydown="if(event.key==='Enter')filterScores()">
    <span>最低分</span>
    <input type="number" id="minScore" value="0"
           onchange="filterScores()" onkeydown="if(event.key==='Enter')filterScores()">
    <span id="filterCount"></span>
</div>
```

300ms 防抖：

```javascript
let filterTimer;
function debounceFilter() {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(filterScores, 300);
}

function filterScores() {
    const keyword = document.getElementById('stockSearch').value.toLowerCase();
    const minScore = parseFloat(document.getElementById('minScore').value) || 0;
    let visible = 0;
    const rows = document.querySelectorAll('#scoreTable tr');
    rows.forEach(row => {
        const code = row.querySelector('.code')?.textContent.toLowerCase() || '';
        const name = row.querySelector('.name')?.textContent.toLowerCase() || '';
        const score = parseFloat(row.querySelector('.score')?.textContent) || 0;
        const match = (!keyword || code.includes(keyword) || name.includes(keyword))
                   && score >= minScore;
        row.style.display = match ? '' : 'none';
        if (match) visible++;
    });
    document.getElementById('filterCount').textContent =
        visible === 0 ? '无匹配结果' : `显示 ${visible} / ${rows.length} 条`;
}
```

**验收标准:**
- [ ] 输入代码/名称实时过滤（大小写不敏感）
- [ ] 最低分筛选生效
- [ ] 筛选后显示匹配数量
- [ ] 无结果时显示"无匹配结果"
- [ ] 300ms 防抖防止卡顿
- [ ] Enter 键触发搜索

---

### PR6: 打分历史趋势

**API** (`routes/scoring.py`):

```python
@scoring_bp.route("/stock/<code>/history", methods=["GET"])
def stock_score_history(code: str):
    """GET /api/scoring/stock/000001/history?days=30"""
    days = request.args.get("days", "30")
    # 白名单校验，防注入
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

**前端** (`dashboard.html`):

打分列表中股票代码变为可点击链接。点击弹出模态窗：

- 时间范围下拉：[7天, 30天, 60天, 90天]，切换重新请求
- ECharts 折线图，按 `rule_name` 分组多条线（调色板区分）
- `toolbox.saveAsImage` 下载 PNG
- 数据 < 2天时显示"数据不足"而非空图
- 日期轴自适应：7天显示"MM-DD"，30天/60天/90天显示"YYYY-MM"

**验收标准:**
- [ ] 点击股票代码弹出趋势图
- [ ] 显示打分变化（按策略分组多条线）
- [ ] 时间范围可选（7/30/60/90天）
- [ ] 图表支持下载为PNG
- [ ] 多策略线条颜色区分明显
- [ ] 数据不足时显示"数据不足"

---

## 阶段三：低优先级

### PR7: 策略版本管理

**数据库** (`core/db.py`):

在 `init_db()` 中新增：

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

**业务逻辑** (`strategy/rules_store.py`):

三个新函数：

```python
def save_rule_version(rule_id: int) -> int | None:
    """保存当前规则为历史版本。仅在规则已存在（UPDATE）时调用。返回版本号。"""
    with get_conn() as conn:
        rule = conn.execute("SELECT * FROM strategy_rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            return None  # 新建规则不创建版本

        max_ver = conn.execute(
            "SELECT MAX(version) as v FROM strategy_rule_versions WHERE rule_id = ?",
            (rule_id,)
        ).fetchone()["v"] or 0

        conn.execute("""INSERT INTO strategy_rule_versions
            (rule_id, version, conditions, sell_conditions, holding_min, holding_max,
             fitness, annual_return, win_rate, sharpe_ratio, max_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rule_id, max_ver + 1, rule["conditions"], rule["sell_conditions"],
             rule["holding_min"], rule["holding_max"], rule["fitness"],
             rule["annual_return"], rule["win_rate"], rule["sharpe_ratio"],
             rule["max_drawdown"]))
        return max_ver + 1

def get_rule_versions(rule_id: int) -> list[dict]:
    """查询某规则所有历史版本，按版本号降序"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_rule_versions WHERE rule_id = ? ORDER BY version DESC",
            (rule_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def rollback_rule(rule_id: int, version: int) -> bool:
    """回滚到指定版本。先保存当前版本，再覆盖。"""
    with get_conn() as conn:
        # 验证版本存在
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
            sharpe_ratio = ?, max_drawdown = ?
            WHERE id = ?""",
            (ver["conditions"], ver["sell_conditions"],
             ver["holding_min"], ver["holding_max"],
             ver["fitness"], ver["annual_return"], ver["win_rate"],
             ver["sharpe_ratio"], ver["max_drawdown"],
             rule_id))
        conn.commit()
        return True
```

**避免重复版本**：`save_rule_version()` 中可添加检查，若当前规则内容与最新版本相同则跳过创建（通过比较 conditions 和 sell_conditions 的 hash 或直接字符串比较）。

触发时机：在 `save_rule()` 中，当规则已存在（UPDATE）时自动先调 `save_rule_version()`。新建（INSERT）时不调。

**API** (`routes/strategy.py`):

```python
@strategy_bp.route("/rules/<int:rule_id>/versions", methods=["GET"])
def rule_versions(rule_id):
    versions = get_rule_versions(rule_id)
    return jsonify({"success": True, "data": _sanitize(versions)})

@strategy_bp.route("/rules/<int:rule_id>/rollback/<int:version>", methods=["POST"])
def rollback_rule(rule_id, version):
    ok = rollback_rule(rule_id, version)
    if not ok:
        return jsonify({"success": False, "error": "Version not found"}), 404
    return jsonify({"success": True, "message": f"Rolled back to v{version}"})
```

**前端** (`dashboard.html`):

规则详情行增加"版本"按钮。点击弹窗列出历史版本表（版本号、fitness、年化收益、胜率、创建时间），每行有"回滚"按钮。回滚前 `confirm()` 确认。

**验收标准:**
- [ ] 修改规则时自动保存历史版本（UPDATE 时触发）
- [ ] 新建规则不创建版本记录
- [ ] 可查看规则版本列表（按版本号降序）
- [ ] 支持回滚到任意版本
- [ ] 回滚前自动保存当前版本（防止误操作）
- [ ] 版本号从1开始，每个 rule_id 独立计数
- [ ] 回滚不影响 created_at/status 等元数据字段

---

### PR8: 失败任务自动重试

**单股重试** (`core/sync.py`):

在现有 `sync_one_stock_with_timeout()` 内部加重试循环，合并超时+重试功能：

```python
def sync_one_stock_with_timeout(code: str, start_date: str = HISTORY_START,
                                end_date: str = None, verbose: bool = False,
                                auto_login: bool = True, timeout: float = 30.0,
                                max_retries: int = 2) -> bool:
    """带超时和重试的单股票同步"""
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

超时和失败都触发重试，指数退避（1s → 2s），总计最多3次尝试。

**调度器重试** (`scheduler/runner.py`):

同步任务整体失败后自动重试1次：

```python
_retry_queued = False  # 防重复

def run_sync_blocking():
    global _retry_queued
    try:
        daily_sync(verbose=True)
        SYNC_STATUS["last_result"] = "同步成功"
    except Exception as e:
        SYNC_STATUS["last_result"] = f"同步失败: {e}"
        if not _retry_queued:
            _retry_queued = True
            import threading
            def retry():
                global _retry_queued
                time.sleep(1800)  # 30分钟
                SYNC_STATUS["last_result"] = "自动重试中..."
                try:
                    daily_sync(verbose=True)
                    SYNC_STATUS["last_result"] = "重试成功"
                except Exception as e2:
                    SYNC_STATUS["last_result"] = f"重试失败: {e2}"
                finally:
                    _retry_queued = False
            threading.Thread(target=retry, daemon=True).start()
```

**注意**：`_retry_queued` 为模块级全局变量，服务重启后会重置。对于当前单进程架构已足够，若未来引入多进程需考虑持久化到文件或数据库。

**验收标准:**
- [ ] 单只股票超时/失败后自动重试2次（指数退避 1s/2s）
- [ ] 调度器失败后30分钟重试1次，不无限循环
- [ ] `_retry_queued` 标志防重复重试
- [ ] 超时和失败都触发重试
- [ ] 重试间隔可配置（常量定义）

---

### PR9: 北交所数据支持提示

**跳过统计** (`core/sync.py`):

```python
_sync_stats = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "skipped_bse": 0,
    "skipped_st": 0,
}
```

`_should_skip()` 保持返回 `bool`（不破坏5处现有调用），内部递增统计：

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

`_sync_stats` 在 `daily_sync()` 入口处重置为全0。

同步完成时 `POST /api/sync` 返回 `_sync_stats`：

```python
return jsonify({
    "status": "completed",
    "stats": _sync_stats,
})
```

**验收标准:**
- [ ] 同步完成显示跳过原因统计
- [ ] 明确标注北交所不支持
- [ ] 用户可清楚了解同步结果
- [ ] 每次同步启动时重置统计
- [ ] `_should_skip` 函数签名不变，向后兼容
- [ ] 跳过统计在同步日志中可追溯

---

## 附录：影响文件汇总

| 文件 | PR1 | PR2 | PR3 | PR4 | PR5 | PR6 | PR7 | PR8 | PR9 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| core/sync.py | ● | | | | | | | ● | ● |
| core/db.py | | | | | | | ● | | |
| routes/system.py | ● | | | | | | | | |
| routes/strategy.py | | ● | | | | | ● | | |
| routes/backtest.py | | | ● | ● | | | | | |
| routes/scoring.py | | | | | | ● | | | |
| backtest/engine.py | | | ● | | | | | | |
| strategy/rules_store.py | | | | | | | ● | | |
| strategy/miner.py | | ● | | | | | | | |
| strategy/mining_state.py | | ● | | | | | | | |
| scheduler/runner.py | | | | | | | | ● | |
| dashboard.html | ● | ● | ● | ● | ● | ● | ● | | ● |

- `strategy/mining_state.py` 为新建文件
- 其余均为现有文件修改

---

## 附录：测试策略

### 关键测试用例

| PR | 测试类型 | 测试场景 |
|----|---------|---------|
| PR1 | 单元测试 | 并发同步返回 409 |
| PR1 | 单元测试 | progress_callback 正确更新状态 |
| PR2 | 单元测试 | request_stop 后状态正确更新 |
| PR2 | 集成测试 | 挖掘中调用 stop 后 10 秒内退出 |
| PR3 | 单元测试 | 参数钳制逻辑（-0.5 ~ 0, 0 ~ 1.0） |
| PR3 | 集成测试 | 默认参数行为与改造前一致 |
| PR4 | 单元测试 | 对比 API 正确返回多个结果 |
| PR4 | 前端测试 | 高亮逻辑正确（年化最高/回撤最小） |
| PR5 | 前端测试 | 防抖 300ms 生效 |
| PR5 | 前端测试 | Enter 键触发搜索 |
| PR6 | 单元测试 | days 白名单校验 |
| PR6 | 前端测试 | 数据 < 2 天显示"数据不足" |
| PR7 | 单元测试 | 新建规则不创建版本 |
| PR7 | 单元测试 | 回滚后版本号正确递增 |
| PR8 | 单元测试 | 指数退避间隔正确（1s, 2s） |
| PR8 | 单元测试 | _retry_queued 防重复 |
| PR9 | 单元测试 | _should_skip 统计递增 |
| PR9 | 单元测试 | daily_sync 启动时重置统计 |

---

## 附录：实施顺序与依赖

```
PR1 ──────────────→ PR9 (PR9 依赖 PR1 的进度显示)
PR2 ──────────────→ (独立)
PR3 ──────────────→ PR4 (PR4 依赖 PR3 的回测参数)
PR5 ──────────────→ (独立)
PR6 ──────────────→ (独立)
PR7 ──────────────→ (独立)
PR8 ──────────────→ (独立)
```

**推荐实施顺序**：PR1 → PR9 → PR2 → PR3 → PR4 → PR5 → PR6 → PR7 → PR8
