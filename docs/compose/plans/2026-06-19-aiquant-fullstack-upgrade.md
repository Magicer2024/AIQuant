# AIQuant 全栈升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 AIQuant 从 Flask + SQLite + 单文件前端升级为 FastAPI + PostgreSQL + Vue 3 + Docker Compose 的现代化全栈应用

**Architecture:** 渐进式迁移，分4个阶段：后端迁移 → 数据库迁移 → 前端重构 → 容器化部署。每个阶段独立可验证，风险可控。

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0, PostgreSQL, Vue 3, Vite, TypeScript, Docker Compose

---

## 阶段1：Flask → FastAPI 迁移

### Task 1.1: 创建 FastAPI 项目骨架

**Covers:** [S1] 后端现代化架构

**Files:**
- Create: `main.py` (FastAPI 入口)
- Create: `requirements_fastapi.txt`
- Modify: `app.py` (保留兼容)

- [ ] **Step 1: 创建 FastAPI 依赖文件**

```txt
# requirements_fastapi.txt
fastapi==0.115.0
uvicorn[standard]==0.30.0
pydantic==2.9.0
sqlalchemy==2.0.35
asyncpg==0.29.0
python-dotenv==1.0.1
```

- [ ] **Step 2: 创建 FastAPI 入口**

```python
# main.py
"""
AIQuant —— FastAPI 入口
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="AIQuant API",
    description="A股量化交易系统 API",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health():
    return {"status": "ok", "message": "AIQuant API v2.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
```

- [ ] **Step 3: 测试 FastAPI 启动**

```bash
pip install -r requirements_fastapi.txt
python main.py
# 访问 http://localhost:5000/docs 查看 API 文档
```

- [ ] **Step 4: Commit**

```bash
git add main.py requirements_fastapi.txt
git commit -m "feat: add FastAPI entry point with auto docs"
```

---

### Task 1.2: 创建 Pydantic 模型

**Covers:** [S1] 类型安全

**Files:**
- Create: `schemas/__init__.py`
- Create: `schemas/stock.py`
- Create: `schemas/signal.py`
- Create: `schemas/strategy.py`
- Create: `schemas/backtest.py`

- [ ] **Step 1: 创建 schemas 目录和 __init__.py**

```python
# schemas/__init__.py
from .stock import StockInfo, StockPrice
from .signal import SignalRecord, StockSignal
from .strategy import StrategyRule, ActiveStrategy
from .backtest import BacktestResult, BacktestTrade
```

- [ ] **Step 2: 创建 Stock schemas**

```python
# schemas/stock.py
from pydantic import BaseModel
from typing import Optional
from datetime import date

class StockInfo(BaseModel):
    code: str
    name: str
    market: Optional[str] = None
    is_active: bool = True
    total_shares: Optional[float] = None
    circ_shares: Optional[float] = None

class StockPrice(BaseModel):
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    pct_change: Optional[float] = 0
    turnover: Optional[float] = 0

class StockScore(BaseModel):
    trade_date: date
    code: str
    name: Optional[str] = None
    score: float
    rule_id: Optional[str] = None
    rule_name: Optional[str] = None
```

- [ ] **Step 3: 创建 Signal schemas**

```python
# schemas/signal.py
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime

class SignalRecord(BaseModel):
    id: Optional[int] = None
    scan_time: datetime
    trade_date: date
    code: str
    name: Optional[str] = None
    price: float
    score: int
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    buy_volume: Optional[int] = None
    buy_money: Optional[float] = None

class StockSignal(BaseModel):
    id: Optional[int] = None
    scan_date: date
    trade_date: date
    code: str
    name: Optional[str] = None
    price: float
    fusion_score: float
    trigger_list: Optional[List[str]] = None
    buy_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
```

- [ ] **Step 4: 创建 Strategy schemas**

```python
# schemas/strategy.py
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime

class StrategyRule(BaseModel):
    id: Optional[int] = None
    rule_name: str
    rule_type: str
    encoding: str
    conditions: Optional[str] = None
    sell_conditions: Optional[str] = None
    holding_min: int = 3
    holding_max: int = 20
    fitness: float = 0
    annual_return: float = 0
    win_rate: float = 0
    sharpe_ratio: float = 0
    max_drawdown: float = 0
    is_active: bool = True

class ActiveStrategy(BaseModel):
    id: Optional[int] = None
    select_date: date
    rule_id: int
    rule_name: Optional[str] = None
    final_score: float
    rank: Optional[int] = None
    valid_until: date
```

- [ ] **Step 5: 创建 Backtest schemas**

```python
# schemas/backtest.py
from pydantic import BaseModel
from typing import Optional
from datetime import date

class BacktestResult(BaseModel):
    id: Optional[int] = None
    rule_id: str
    rule_name: Optional[str] = None
    start_date: date
    end_date: date
    annual_return: float = 0
    cumulative_return: float = 0
    win_rate: float = 0
    sharpe_ratio: float = 0
    max_drawdown: float = 0
    total_trades: int = 0

class BacktestTrade(BaseModel):
    id: Optional[int] = None
    result_id: Optional[int] = None
    code: str
    name: Optional[str] = None
    entry_date: date
    entry_price: float
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    holding_days: int = 0
    pnl_pct: float = 0
    exit_reason: Optional[str] = None
```

- [ ] **Step 6: Commit**

```bash
git add schemas/
git commit -m "feat: add Pydantic models for type safety"
```

---

### Task 1.3: 迁移 System 路由

**Covers:** [S1] API 路由迁移

**Files:**
- Create: `api/system.py`
- Modify: `main.py`

- [ ] **Step 1: 创建 System API 路由**

```python
# api/system.py
from fastapi import APIRouter
from scheduler.state import SYNC_STATUS, SCHEDULER_RUNNING
from scheduler.runner import start_scheduler

router = APIRouter(prefix="/api/system", tags=["system"])

@router.get("/status")
async def system_status():
    """获取系统状态"""
    from routes.sync import _sync_progress
    return {
        "sync": {
            "running": SYNC_STATUS["running"] or _sync_progress["running"],
            "last_time": SYNC_STATUS.get("last_time"),
            "last_result": SYNC_STATUS.get("last_result", ""),
        },
        "scheduler": {
            "enabled": SCHEDULER_RUNNING["enabled"],
        }
    }

@router.post("/scheduler")
async def toggle_scheduler(enable: bool):
    """启用/禁用定时任务"""
    if enable:
        if not SCHEDULER_RUNNING["enabled"]:
            SCHEDULER_RUNNING["enabled"] = True
            start_scheduler()
        return {"status": "enabled", "message": "定时任务已开启"}
    else:
        SCHEDULER_RUNNING["enabled"] = False
        return {"status": "disabled", "message": "定时任务已关闭"}
```

- [ ] **Step 2: 注册路由到 main.py**

```python
# main.py (添加)
from api.system import router as system_router
app.include_router(system_router)
```

- [ ] **Step 3: 测试 API**

```bash
curl http://localhost:5000/api/system/status
```

- [ ] **Step 4: Commit**

```bash
git add api/system.py main.py
git commit -m "feat: migrate system routes to FastAPI"
```

---

### Task 1.4: 迁移 Sync 路由

**Covers:** [S1] API 路由迁移

**Files:**
- Create: `api/sync.py`
- Modify: `main.py`

- [ ] **Step 1: 创建 Sync API 路由**

```python
# api/sync.py
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/sync", tags=["sync"])

class SyncRequest(BaseModel):
    sync_type: str = "incremental"
    force: bool = False

class SyncResponse(BaseModel):
    status: str
    message: str
    progress: Optional[dict] = None

@router.post("/start", response_model=SyncResponse)
async def start_sync(request: SyncRequest, background_tasks: BackgroundTasks):
    """启动数据同步"""
    from routes.sync import _run_sync_task, _sync_progress

    if _sync_progress.get("running"):
        return SyncResponse(
            status="already_running",
            message="同步正在进行中",
            progress=_sync_progress
        )

    background_tasks.add_task(_run_sync_task, request.sync_type, request.force)
    return SyncResponse(
        status="started",
        message="同步已启动",
        progress=_sync_progress
    )

@router.get("/progress")
async def get_sync_progress():
    """获取同步进度"""
    from routes.sync import _sync_progress
    return _sync_progress
```

- [ ] **Step 2: 注册路由**

```python
# main.py (添加)
from api.sync import router as sync_router
app.include_router(sync_router)
```

- [ ] **Step 3: Commit**

```bash
git add api/sync.py main.py
git commit -m "feat: migrate sync routes to FastAPI"
```

---

### Task 1.5: 迁移 Scoring 路由

**Covers:** [S1] API 路由迁移

**Files:**
- Create: `api/scoring.py`
- Modify: `main.py`

- [ ] **Step 1: 创建 Scoring API 路由**

```python
# api/scoring.py
from fastapi import APIRouter, Query
from typing import Optional, List
from datetime import date
from core.repository import score_repo

router = APIRouter(prefix="/api/scoring", tags=["scoring"])

@router.get("/today")
async def get_today_scores(limit: int = Query(50, ge=1, le=500)):
    """获取今日评分"""
    today = date.today().strftime("%Y-%m-%d")
    scores = score_repo.get_scores_by_date(today, limit)
    return {"date": today, "scores": scores}

@router.get("/history/{code}")
async def get_score_history(code: str, days: int = Query(30, ge=1, le=365)):
    """获取评分历史"""
    scores = score_repo.get_score_history(code, days)
    return {"code": code, "scores": scores}

@router.get("/ranking")
async def get_score_ranking(
    trade_date: Optional[str] = None,
    min_score: float = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500)
):
    """获取评分排行"""
    if not trade_date:
        trade_date = date.today().strftime("%Y-%m-%d")
    scores = score_repo.get_score_ranking(trade_date, min_score, limit)
    return {"date": trade_date, "scores": scores}
```

- [ ] **Step 2: 注册路由**

```python
# main.py (添加)
from api.scoring import router as scoring_router
app.include_router(scoring_router)
```

- [ ] **Step 3: Commit**

```bash
git add api/scoring.py main.py
git commit -m "feat: migrate scoring routes to FastAPI"
```

---

### Task 1.6: 迁移 Strategy 路由

**Covers:** [S1] API 路由迁移

**Files:**
- Create: `api/strategy.py`
- Modify: `main.py`

- [ ] **Step 1: 创建 Strategy API 路由**

```python
# api/strategy.py
from fastapi import APIRouter, Query
from typing import Optional, List
from core.repository import strategy_repo

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

@router.get("/rules")
async def get_strategy_rules(
    rule_type: Optional[str] = None,
    active_only: bool = True,
    limit: int = Query(100, ge=1, le=500)
):
    """获取策略规则列表"""
    rules = strategy_repo.get_rules(rule_type, active_only, limit)
    return {"rules": rules}

@router.get("/rules/{rule_id}")
async def get_strategy_rule(rule_id: int):
    """获取单个策略规则详情"""
    rule = strategy_repo.get_rule_by_id(rule_id)
    if not rule:
        return {"error": "规则不存在"}
    return {"rule": rule}

@router.get("/active")
async def get_active_strategies():
    """获取当前活跃策略"""
    strategies = strategy_repo.get_active_strategies()
    return {"strategies": strategies}

@router.post("/select")
async def select_strategies():
    """触发策略选择"""
    from strategy.dynamic_selector import select_strategies as do_select
    result = do_select()
    return {"status": "completed", "result": result}
```

- [ ] **Step 2: 注册路由**

```python
# main.py (添加)
from api.strategy import router as strategy_router
app.include_router(strategy_router)
```

- [ ] **Step 3: Commit**

```bash
git add api/strategy.py main.py
git commit -m "feat: migrate strategy routes to FastAPI"
```

---

### Task 1.7: 迁移 Backtest 路由

**Covers:** [S1] API 路由迁移

**Files:**
- Create: `api/backtest.py`
- Modify: `main.py`

- [ ] **Step 1: 创建 Backtest API 路由**

```python
# api/backtest.py
from fastapi import APIRouter, Query, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from datetime import date

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

class BacktestRequest(BaseModel):
    rule_id: str
    start_date: str
    end_date: str
    initial_capital: float = 100000

@router.get("/results")
async def get_backtest_results(
    rule_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200)
):
    """获取回测结果列表"""
    from core.db import get_conn
    sql = "SELECT * FROM backtest_results"
    params = []
    if rule_id:
        sql += " WHERE rule_id = ?"
        params.append(rule_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"results": [dict(r) for r in rows]}

@router.get("/results/{result_id}/trades")
async def get_backtest_trades(result_id: int):
    """获取回测交易明细"""
    from core.db import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE result_id = ? ORDER BY entry_date",
            (result_id,)
        ).fetchall()
    return {"trades": [dict(r) for r in rows]}

@router.post("/run")
async def run_backtest(request: BacktestRequest):
    """运行单次回测"""
    from backtest.unified_engine import run_backtest as do_backtest
    result = do_backtest(
        rule_id=request.rule_id,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital
    )
    return {"status": "completed", "result": result}
```

- [ ] **Step 2: 注册路由**

```python
# main.py (添加)
from api.backtest import router as backtest_router
app.include_router(backtest_router)
```

- [ ] **Step 3: Commit**

```bash
git add api/backtest.py main.py
git commit -m "feat: migrate backtest routes to FastAPI"
```

---

### Task 1.8: 添加静态文件服务和前端路由

**Covers:** [S1] 静态文件服务

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 添加静态文件服务**

```python
# main.py (添加)
from fastapi.responses import FileResponse, HTMLResponse

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("dashboard.html")

@app.get("/reports/{filename:path}")
async def serve_report(filename: str):
    return FileResponse(f"reports/{filename}")
```

- [ ] **Step 2: 测试所有路由**

```bash
python main.py
# 测试以下端点：
# GET /docs - API 文档
# GET /api/health - 健康检查
# GET /api/system/status - 系统状态
# GET /api/scoring/today - 今日评分
# GET / - 仪表板
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add static file serving and dashboard route"
```

---

## 阶段2：SQLite → PostgreSQL 迁移

### Task 2.1: 创建 PostgreSQL 配置

**Covers:** [S2] 数据库迁移

**Files:**
- Create: `config/database.py`
- Create: `.env.example`
- Modify: `requirements.txt`

- [ ] **Step 1: 创建数据库配置**

```python
# config/database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aiquant:aiquant@localhost:5432/aiquant"
)

engine = create_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 2: 创建 .env.example**

```bash
# .env.example
DATABASE_URL=postgresql://aiquant:aiquant@localhost:5432/aiquant
POSTGRES_USER=aiquant
POSTGRES_PASSWORD=aiquant
POSTGRES_DB=aiquant
```

- [ ] **Step 3: 更新 requirements.txt**

```txt
# 添加到 requirements.txt
sqlalchemy==2.0.35
asyncpg==0.29.0
psycopg2-binary==2.9.9
python-dotenv==1.0.1
alembic==1.13.2
```

- [ ] **Step 4: Commit**

```bash
git add config/database.py .env.example requirements.txt
git commit -m "feat: add PostgreSQL configuration with SQLAlchemy"
```

---

### Task 2.2: 创建 SQLAlchemy 模型

**Covers:** [S2] 数据模型定义

**Files:**
- Create: `models/__init__.py`
- Create: `models/stock.py`
- Create: `models/price.py`
- Create: `models/signal.py`
- Create: `models/strategy.py`
- Create: `models/backtest.py`

- [ ] **Step 1: 创建 models 目录和 __init__.py**

```python
# models/__init__.py
from .stock import StockInfo, StockPrice
from .signal import SignalRecord, StockSignal
from .strategy import StrategyRule, ActiveStrategy
from .backtest import BacktestResult, BacktestTrade
```

- [ ] **Step 2: 创建 Stock 模型**

```python
# models/stock.py
from sqlalchemy import Column, String, Float, Integer, DateTime, Index
from sqlalchemy.sql import func
from config.database import Base

class StockInfo(Base):
    __tablename__ = "stock_info"

    code = Column(String(10), primary_key=True)
    name = Column(String(50), nullable=False)
    market = Column(String(10))
    is_active = Column(Integer, default=1)
    total_shares = Column(Float)
    circ_shares = Column(Float)
    updated_at = Column(DateTime, server_default=func.now())

class StockPrice(Base):
    __tablename__ = "daily_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    trade_date = Column(String(10), nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)
    pct_change = Column(Float, default=0)
    turnover = Column(Float, default=0)

    __table_args__ = (
        Index("idx_daily_code_date", "code", "trade_date"),
        Index("idx_daily_date", "trade_date"),
    )
```

- [ ] **Step 3: 创建 Signal 模型**

```python
# models/signal.py
from sqlalchemy import Column, String, Float, Integer, DateTime, Date, Text, Index
from sqlalchemy.sql import func
from config.database import Base

class SignalRecord(Base):
    __tablename__ = "signal_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_time = Column(DateTime, nullable=False)
    trade_date = Column(String(10), nullable=False)
    code = Column(String(10), nullable=False)
    name = Column(String(50))
    price = Column(Float)
    score = Column(Integer)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    buy_volume = Column(Integer)
    buy_money = Column(Float)

    __table_args__ = (
        Index("idx_signal_date", "trade_date"),
    )

class StockSignal(Base):
    __tablename__ = "stock_signal"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_date = Column(String(10), nullable=False)
    trade_date = Column(String(10), nullable=False)
    code = Column(String(10), nullable=False)
    name = Column(String(50))
    price = Column(Float)
    fusion_score = Column(Float)
    trigger_list = Column(Text)
    buy_price = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_sig_scan_trade_code", "scan_date", "code", unique=True),
        Index("idx_sig_trade_date", "trade_date"),
    )
```

- [ ] **Step 4: 创建 Strategy 模型**

```python
# models/strategy.py
from sqlalchemy import Column, String, Float, Integer, DateTime, Date, Text, ForeignKey, Index
from sqlalchemy.sql import func
from config.database import Base

class StrategyRule(Base):
    __tablename__ = "strategy_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_name = Column(String(100), unique=True, nullable=False)
    rule_type = Column(String(50), nullable=False)
    encoding = Column(Text, nullable=False)
    conditions = Column(Text)
    sell_conditions = Column(Text)
    holding_min = Column(Integer, default=3)
    holding_max = Column(Integer, default=20)
    fitness = Column(Float, default=0)
    annual_return = Column(Float, default=0)
    win_rate = Column(Float, default=0)
    sharpe_ratio = Column(Float, default=0)
    max_drawdown = Column(Float, default=0)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_rules_type", "rule_type"),
        Index("idx_rules_active", "is_active"),
        Index("idx_rules_fitness", "fitness"),
    )

class ActiveStrategy(Base):
    __tablename__ = "active_strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    select_date = Column(String(10), nullable=False)
    rule_id = Column(Integer, ForeignKey("strategy_rules.id"), nullable=False)
    rule_name = Column(String(100))
    final_score = Column(Float)
    rank = Column(Integer)
    valid_until = Column(String(10), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_active_date", "select_date"),
        Index("idx_active_valid", "valid_until"),
    )
```

- [ ] **Step 5: 创建 Backtest 模型**

```python
# models/backtest.py
from sqlalchemy import Column, String, Float, Integer, DateTime, Date, Text, ForeignKey, Index
from sqlalchemy.sql import func
from config.database import Base

class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(String(50), nullable=False)
    rule_name = Column(String(100))
    start_date = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=False)
    annual_return = Column(Float, default=0)
    cumulative_return = Column(Float, default=0)
    win_rate = Column(Float, default=0)
    sharpe_ratio = Column(Float, default=0)
    max_drawdown = Column(Float, default=0)
    total_trades = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_bt_results_rule", "rule_id"),
    )

class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(Integer, ForeignKey("backtest_results.id"))
    code = Column(String(10), nullable=False)
    name = Column(String(50))
    entry_date = Column(String(10), nullable=False)
    entry_price = Column(Float)
    exit_date = Column(String(10))
    exit_price = Column(Float)
    holding_days = Column(Integer, default=0)
    pnl_pct = Column(Float, default=0)
    exit_reason = Column(String(50))

    __table_args__ = (
        Index("idx_bt_trades_result", "result_id"),
        Index("idx_bt_trades_code", "code"),
    )
```

- [ ] **Step 6: Commit**

```bash
git add models/
git commit -m "feat: add SQLAlchemy models for PostgreSQL"
```

---

### Task 2.3: 创建数据迁移脚本

**Covers:** [S2] 数据迁移

**Files:**
- Create: `scripts/migrate_sqlite_to_postgres.py`

- [ ] **Step 1: 创建迁移脚本**

```python
# scripts/migrate_sqlite_to_postgres.py
"""
SQLite to PostgreSQL migration script
"""
import sqlite3
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from models import *

load_dotenv()

SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "core", "quant.db")
POSTGRES_URL = os.getenv("DATABASE_URL", "postgresql://aiquant:aiquant@localhost:5432/aiquant")

def migrate():
    # Connect to SQLite
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    # Connect to PostgreSQL
    pg_engine = create_engine(POSTGRES_URL)
    PGSession = sessionmaker(bind=pg_engine)

    # Create all tables in PostgreSQL
    Base.metadata.create_all(pg_engine)

    # Migrate each table
    tables = [
        "stock_info", "daily_price", "signal_records", "stock_signal",
        "strategy_rules", "active_strategies", "backtest_results", "backtest_trades",
        "positions", "trades", "account_snapshots", "sync_log",
        "stock_margin", "stock_margin_detail", "stock_hsgt_north",
        "index_futures", "stock_lhb_detail", "stock_mgmt_holding",
        "audit_log", "factor_daily", "stock_score", "strategy_signals",
        "strategy_rule_versions"
    ]

    for table in tables:
        print(f"Migrating {table}...")
        try:
            rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                # Get column names from first row
                columns = rows[0].keys()
                placeholders = ", ".join([f":{col}" for col in columns])
                cols = ", ".join(columns)

                pg_session = PGSession()
                for row in rows:
                    data = {col: row[col] for col in columns}
                    pg_session.execute(
                        text(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"),
                        data
                    )
                pg_session.commit()
                print(f"  Migrated {len(rows)} rows")
            else:
                print(f"  No data to migrate")
        except Exception as e:
            print(f"  Error migrating {table}: {e}")

    sqlite_conn.close()
    pg_engine.dispose()
    print("Migration complete!")

if __name__ == "__main__":
    migrate()
```

- [ ] **Step 2: 测试迁移**

```bash
# 确保 PostgreSQL 已启动
python scripts/migrate_sqlite_to_postgres.py
```

- [ ] **Step 3: Commit**

```bash
git add scripts/migrate_sqlite_to_postgres.py
git commit -m "feat: add SQLite to PostgreSQL migration script"
```

---

### Task 2.4: 更新数据访问层

**Covers:** [S2] 数据访问重构

**Files:**
- Modify: `core/db.py` (添加 PostgreSQL 支持)
- Create: `core/db_postgres.py`

- [ ] **Step 1: 创建 PostgreSQL 数据访问层**

```python
# core/db_postgres.py
"""
PostgreSQL 数据访问层
"""
from contextlib import contextmanager
from sqlalchemy.orm import Session
from config.database import SessionLocal

@contextmanager
def get_pg_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def get_all_stocks(active_only=True):
    from models.stock import StockInfo
    with get_pg_session() as session:
        query = session.query(StockInfo)
        if active_only:
            query = query.filter(StockInfo.is_active == 1)
        return query.order_by(StockInfo.code).all()

def get_daily_price(code, start_date=None, end_date=None):
    from models.price import StockPrice
    with get_pg_session() as session:
        query = session.query(StockPrice).filter(StockPrice.code == code)
        if start_date:
            query = query.filter(StockPrice.trade_date >= start_date)
        if end_date:
            query = query.filter(StockPrice.trade_date <= end_date)
        return query.order_by(StockPrice.trade_date).all()
```

- [ ] **Step 2: 更新 requirements.txt**

```txt
# 添加到 requirements.txt
psycopg2-binary==2.9.9
```

- [ ] **Step 3: Commit**

```bash
git add core/db_postgres.py requirements.txt
git commit -m "feat: add PostgreSQL data access layer"
```

---

## 阶段3：Vue 3 前端重构

### Task 3.1: 创建 Vue 3 项目

**Covers:** [S3] 前端现代化

**Files:**
- Create: `frontend/` directory

- [ ] **Step 1: 创建 Vue 3 项目**

```bash
# 在项目根目录执行
npm create vite@latest frontend -- --template vue-ts
cd frontend
npm install
npm install vue-router@4 pinia axios echarts vue-echarts
```

- [ ] **Step 2: 配置项目结构**

```bash
frontend/
├── src/
│   ├── api/          # API 调用
│   ├── components/   # 组件
│   ├── views/        # 页面
│   ├── stores/       # 状态管理
│   ├── router/       # 路由
│   ├── types/        # 类型定义
│   └── utils/        # 工具函数
├── public/
└── index.html
```

- [ ] **Step 3: Commit**

```bash
git add frontend/
git commit -m "feat: scaffold Vue 3 + Vite project"
```

---

### Task 3.2: 创建 API 客户端

**Covers:** [S3] API 集成

**Files:**
- Create: `frontend/src/api/index.ts`
- Create: `frontend/src/api/stock.ts`
- Create: `frontend/src/api/strategy.ts`

- [ ] **Step 1: 创建 API 基础配置**

```typescript
// frontend/src/api/index.ts
import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    console.error('API Error:', error)
    return Promise.reject(error)
  }
)

export default api
```

- [ ] **Step 2: 创建 Stock API**

```typescript
// frontend/src/api/stock.ts
import api from './index'

export interface StockInfo {
  code: string
  name: string
  market: string
  is_active: boolean
}

export interface StockScore {
  trade_date: string
  code: string
  name: string
  score: number
}

export const stockApi = {
  getTodayScores(limit = 50) {
    return api.get('/scoring/today', { params: { limit } })
  },
  getScoreHistory(code: string, days = 30) {
    return api.get(`/scoring/history/${code}`, { params: { days } })
  },
  getScoreRanking(date?: string, minScore = 0, limit = 100) {
    return api.get('/scoring/ranking', {
      params: { trade_date: date, min_score: minScore, limit }
    })
  },
}
```

- [ ] **Step 3: 创建 Strategy API**

```typescript
// frontend/src/api/strategy.ts
import api from './index'

export interface StrategyRule {
  id: number
  rule_name: string
  rule_type: string
  fitness: number
  annual_return: number
  win_rate: number
  sharpe_ratio: number
  max_drawdown: number
  is_active: boolean
}

export const strategyApi = {
  getRules(ruleType?: string, activeOnly = true) {
    return api.get('/strategy/rules', {
      params: { rule_type: ruleType, active_only: activeOnly }
    })
  },
  getActiveStrategies() {
    return api.get('/strategy/active')
  },
  selectStrategies() {
    return api.post('/strategy/select')
  },
}
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/
git commit -m "feat: add API client for stock and strategy"
```

---

### Task 3.3: 创建主仪表板组件

**Covers:** [S3] 核心界面

**Files:**
- Create: `frontend/src/views/Dashboard.vue`
- Create: `frontend/src/components/StockTable.vue`
- Create: `frontend/src/components/ScoreChart.vue`

- [ ] **Step 1: 创建 Dashboard 页面**

```vue
<!-- frontend/src/views/Dashboard.vue -->
<template>
  <div class="dashboard">
    <header class="header">
      <h1>AIQuant 仪表板</h1>
      <div class="status-bar">
        <span :class="['status', syncStatus.running ? 'running' : 'idle']">
          {{ syncStatus.running ? '同步中' : '空闲' }}
        </span>
        <button @click="refreshData" :disabled="loading">
          {{ loading ? '刷新中...' : '刷新数据' }}
        </button>
      </div>
    </header>

    <main class="main">
      <section class="scores-section">
        <h2>今日评分排行</h2>
        <StockTable :scores="todayScores" :loading="loading" />
      </section>

      <section class="chart-section">
        <h2>评分分布</h2>
        <ScoreChart :scores="todayScores" />
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { stockApi } from '@/api/stock'
import StockTable from '@/components/StockTable.vue'
import ScoreChart from '@/components/ScoreChart.vue'

const todayScores = ref([])
const syncStatus = ref({ running: false })
const loading = ref(false)

const refreshData = async () => {
  loading.value = true
  try {
    const data = await stockApi.getTodayScores(100)
    todayScores.value = data.scores || []
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refreshData()
})
</script>

<style scoped>
.dashboard {
  max-width: 1400px;
  margin: 0 auto;
  padding: 20px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 30px;
}

.status-bar {
  display: flex;
  align-items: center;
  gap: 15px;
}

.status {
  padding: 5px 10px;
  border-radius: 4px;
  font-size: 14px;
}

.status.running {
  background: #e3f2fd;
  color: #1976d2;
}

.status.idle {
  background: #e8f5e9;
  color: #388e3c;
}

.main {
  display: grid;
  grid-template-columns: 1fr 400px;
  gap: 30px;
}

@media (max-width: 1024px) {
  .main {
    grid-template-columns: 1fr;
  }
}
</style>
```

- [ ] **Step 2: 创建 StockTable 组件**

```vue
<!-- frontend/src/components/StockTable.vue -->
<template>
  <div class="stock-table">
    <div v-if="loading" class="loading">加载中...</div>
    <table v-else>
      <thead>
        <tr>
          <th>代码</th>
          <th>名称</th>
          <th>评分</th>
          <th>趋势</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="stock in scores" :key="stock.code">
          <td>{{ stock.code }}</td>
          <td>{{ stock.name }}</td>
          <td :class="getScoreClass(stock.score)">{{ stock.score.toFixed(2) }}</td>
          <td>{{ getTrend(stock.score) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
interface Score {
  code: string
  name: string
  score: number
}

const props = defineProps<{
  scores: Score[]
  loading: boolean
}>()

const getScoreClass = (score: number) => ({
  'score-high': score >= 80,
  'score-medium': score >= 60 && score < 80,
  'score-low': score < 60,
})

const getTrend = (score: number) => {
  if (score >= 80) return '↑'
  if (score >= 60) return '→'
  return '↓'
}
</script>

<style scoped>
.stock-table {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th, td {
  padding: 12px;
  text-align: left;
  border-bottom: 1px solid #eee;
}

th {
  background: #f5f5f5;
  font-weight: 600;
}

.score-high { color: #f44336; }
.score-medium { color: #ff9800; }
.score-low { color: #4caf50; }
</style>
```

- [ ] **Step 3: 创建 ScoreChart 组件**

```vue
<!-- frontend/src/components/ScoreChart.vue -->
<template>
  <div class="score-chart">
    <v-chart :option="chartOption" autoresize style="height: 300px" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'

use([CanvasRenderer, BarChart, GridComponent, TooltipComponent])

interface Score {
  code: string
  name: string
  score: number
}

const props = defineProps<{
  scores: Score[]
}>()

const chartOption = computed(() => {
  const data = props.scores.slice(0, 20).map(s => ({
    name: s.name || s.code,
    value: s.score,
  }))

  return {
    tooltip: { trigger: 'axis' },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'category',
      data: data.map(d => d.name),
      axisLabel: { rotate: 45 },
    },
    yAxis: { type: 'value', min: 0, max: 100 },
    series: [{
      type: 'bar',
      data: data.map(d => d.value),
      itemStyle: {
        color: (params: any) => {
          const value = params.value
          if (value >= 80) return '#f44336'
          if (value >= 60) return '#ff9800'
          return '#4caf50'
        }
      }
    }]
  }
})
</script>
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/ frontend/src/components/
git commit -m "feat: add dashboard with stock table and score chart"
```

---

### Task 3.4: 创建路由和状态管理

**Covers:** [S3] 应用架构

**Files:**
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/stores/counter.ts`

- [ ] **Step 1: 创建路由**

```typescript
// frontend/src/router/index.ts
import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '@/views/Dashboard.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: Dashboard },
    { path: '/strategy', name: 'strategy', component: () => import('@/views/Strategy.vue') },
    { path: '/backtest', name: 'backtest', component: () => import('@/views/Backtest.vue') },
  ]
})

export default router
```

- [ ] **Step 2: 创建 Pinia Store**

```typescript
// frontend/src/stores/app.ts
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useAppStore = defineStore('app', () => {
  const syncRunning = ref(false)
  const lastSyncTime = ref<string | null>(null)

  const setSyncStatus = (running: boolean, lastTime?: string) => {
    syncRunning.value = running
    if (lastTime) lastSyncTime.value = lastTime
  }

  return { syncRunning, lastSyncTime, setSyncStatus }
})
```

- [ ] **Step 3: 更新 main.ts**

```typescript
// frontend/src/main.ts
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/router/ frontend/src/stores/ frontend/src/main.ts
git commit -m "feat: add Vue Router and Pinia store"
```

---

## 阶段4：Docker Compose 部署

### Task 4.1: 创建 Dockerfile

**Covers:** [S4] 容器化

**Files:**
- Create: `Dockerfile.backend`
- Create: `frontend/Dockerfile`

- [ ] **Step 1: 创建后端 Dockerfile**

```dockerfile
# Dockerfile.backend
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose port
EXPOSE 5000

# Run application
CMD ["python", "main.py"]
```

- [ ] **Step 2: 创建前端 Dockerfile**

```dockerfile
# frontend/Dockerfile
FROM node:20-alpine as builder

WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm ci

# Copy source
COPY . .

# Build
RUN npm run build

# Production stage
FROM nginx:alpine

# Copy built files
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
```

- [ ] **Step 3: 创建 nginx 配置**

```nginx
# frontend/nginx.conf
server {
    listen 80;
    server_name localhost;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://backend:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add Dockerfile.backend frontend/Dockerfile frontend/nginx.conf
git commit -m "feat: add Dockerfiles for backend and frontend"
```

---

### Task 4.2: 创建 Docker Compose

**Covers:** [S4] 服务编排

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: 创建 docker-compose.yml**

```yaml
# docker-compose.yml
version: '3.8'

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: aiquant
      POSTGRES_PASSWORD: aiquant
      POSTGRES_DB: aiquant
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U aiquant"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    build:
      context: .
      dockerfile: Dockerfile.backend
    ports:
      - "5000:5000"
    environment:
      DATABASE_URL: postgresql://aiquant:aiquant@postgres:5432/aiquant
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./data:/app/data
      - ./reports:/app/reports

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "80:80"
    depends_on:
      - backend

volumes:
  postgres_data:
```

- [ ] **Step 2: 测试部署**

```bash
# 构建并启动
docker-compose up -d --build

# 查看状态
docker-compose ps

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add docker-compose for full stack deployment"
```

---

### Task 4.3: 更新 .gitignore

**Covers:** [S4] 项目配置

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: 更新 .gitignore**

```gitignore
# Add to .gitignore

# Environment
.env
.env.local
.env.*.local

# Docker
postgres_data/

# Frontend
frontend/node_modules/
frontend/dist/

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
.venv/
```

- [ ] **Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: update gitignore for Docker and frontend"
```

---

## 验证清单

- [ ] FastAPI 启动正常，`/docs` 可访问
- [ ] 所有 API 端点返回正确响应
- [ ] PostgreSQL 数据库连接正常
- [ ] 数据迁移完成，数据完整
- [ ] Vue 3 前端构建成功
- [ ] Docker Compose 一键启动
- [ ] 前端可以正常访问后端 API

---

## 下一步

1. 完成阶段1后，运行测试验证 API 兼容性
2. 完成阶段2后，执行数据迁移并验证数据完整性
3. 完成阶段3后，进行前端功能测试
4. 完成阶段4后，进行端到端集成测试
