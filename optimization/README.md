# AIQuant 自动化性能优化方案

> 生成日期: 2026-05-07
> 版本: v1.0.0

---

## 一、执行摘要

本方案为 AIQuant 量化交易系统设计了一套完整的自动化性能优化体系，涵盖 **6 大优化模块**，预期整体性能提升 **10-50 倍**。

### 优化效果预估

| 优化项 | 当前状态 | 优化后预估 | 提升倍数 |
|--------|----------|------------|----------|
| 数据库写入 | 逐行写入 | 批量事务 | **5-10x** |
| 策略评分计算 | 全量重算 | 增量计算 | **20-50x** |
| API 响应 | 无缓存 | 多层缓存 | **10-100x** |
| 数据同步 | 单线程 | 多线程 | **3-5x** |
| 回测引擎 | 13个碎片 | 统一抽象 | **代码量-60%** |
| DB连接 | 每次新建 | 连接池复用 | **2-3x** |

---

## 二、问题诊断

### 2.1 系统现状

```
项目统计:
├── 代码总行数: ~13,000+ 行
├── 数据库大小: 263 MB
├── 回测引擎版本: 13 个文件
├── API 路由数: 33 个 Blueprint
└── 日行情记录: 5000+ 只股票 × ~500 天
```

### 2.2 性能瓶颈分析

#### 🔴 严重瓶颈

**1. 策略评分重复计算**
```python
# 当前实现：每次同步都重算全部历史
def sync_strategy_score(code):
    df = get_daily_price(code)  # 读取全部历史
    for strategy in strategies:
        strategy(df)  # 重新计算每个策略
```
- 复杂度：O(n × m)，n=股票数，m=历史天数
- 每日同步：~5000 股票 × 250 天 × 5 策略 = **625万次**计算

**2. 回测引擎碎片化**
```
backtest/
├── backtest.py         (516行)
├── backtest_v3.py      (623行)
├── backtest_v4.py      (xxx行)
├── backtest_bt.py      (xxx行)
├── backtest_quant.py   (xxx行)
├── strategy_screen_backtest.py (802行)
├── custom_strategy_backtest.py (668行)
└── ... 共 13 个文件
```
- 代码重复率 > 60%
- 维护困难，容易出现不一致

#### ⚠️ 中等瓶颈

**3. 数据库写入效率低**
```python
# 当前：逐行 UPDATE
for rec in records:
    conn.execute(f"""
        UPDATE daily_price SET ...
        WHERE code=? AND trade_date=?
    """, rec)
```
- 无事务合并
- 无批量优化

**4. API 无缓存**
```python
# 当前：每次请求都查数据库
@app.route('/api/stock/<code>')
def get_stock(code):
    return db.execute("SELECT * FROM ...")  # 无缓存
```

---

## 三、优化方案

### 3.1 模块架构

```
optimization/
├── __init__.py                 # 模块导出
├── auto_optimizer.py            # 一键优化执行器
├── benchmark.py                 # 性能基准测试
│
├── cache/                       # 缓存层
│   ├── memory_cache.py          # LRU 内存缓存
│   └── __init__.py
│
├── batch/                       # 批量处理优化
│   ├── batch_writer.py          # 批量数据库写入
│   ├── incremental_calculator.py # 增量策略计算
│   └── __init__.py
│
└── api/                         # API 优化
    ├── response_cache.py         # 响应缓存
    ├── db_pool.py               # 连接池
    └── __init__.py
```

### 3.2 核心组件

#### 1. 批量写入器 (BatchWriter)

```python
from optimization.batch import BatchWriter

writer = BatchWriter(db_path)

# 批量更新策略评分 - 5-10x 提升
writer.batch_upsert_strategy_scores("000001", scores_df)
```

**优化点：**
- ✅ 事务批量提交（减少 fsync 开销）
- ✅ 批量 UPSERT（减少网络往返）
- ✅ WAL 模式（读写并发）

#### 2. 增量计算器 (IncrementalStrategyCalculator)

```python
from optimization.batch import IncrementalStrategyCalculator

calculator = IncrementalStrategyCalculator()

# 只计算新增日期 - 20-50x 提升
scores_df, new_days = calculator.calculate_incremental(
    "000001", 
    full_df,           # 完整历史
    last_calc_date      # 上次计算日期
)
```

**优化点：**
- ✅ 增量计算（只算新增数据）
- ✅ 结果缓存（内存 + 文件）
- ✅ 多策略并行

#### 3. API 缓存

```python
from optimization.api import cached_response, _stock_cache

@cached_response(cache=_stock_cache, ttl=60)
def get_stock_price(code: str):
    # 60秒内相同请求直接返回缓存
    return db.query(code)
```

**缓存层级：**
| 缓存 | TTL | 用途 |
|------|-----|------|
| `_stock_cache` | 60秒 | 行情数据 |
| `_api_cache` | 5分钟 | API响应 |
| `_signal_cache` | 10分钟 | 信号数据 |

#### 4. 数据库连接池

```python
from optimization.api import get_db_pool

pool = get_db_pool()
with pool.get_connection() as conn:
    conn.execute("SELECT ...")
```

**优势：**
- 连接复用，减少创建开销
- 线程安全
- 自动 VACUUM

---

## 四、集成指南

### 4.1 一键优化

```bash
cd AIQuant
python optimization/auto_optimizer.py
```

### 4.2 性能基准测试

```bash
python optimization/benchmark.py
```

### 4.3 代码集成示例

#### 修改 core/sync.py

```python
# 在 sync.py 中使用增量计算器

from optimization.batch.incremental_calculator import get_strategy_calculator

def sync_strategy_score(code: str, verbose: bool = False) -> bool:
    """优化版：使用增量计算"""
    try:
        df = get_daily_price(code)
        if df is None or len(df) < 30:
            return False
        
        # 获取上次计算日期
        last_date = get_latest_date_for_strategy(code)
        
        # 增量计算（只算新增日期）
        calculator = get_strategy_calculator()
        scores_df, new_days = calculator.calculate_incremental(
            code, df, last_date
        )
        
        if new_days > 0:
            # 批量写入数据库
            from optimization.batch import get_batch_writer
            writer = get_batch_writer()
            writer.batch_upsert_strategy_scores(code, scores_df.tail(new_days))
        
        return True
    except Exception as e:
        return False
```

#### 修改 API 路由

```python
# 在 routes/stock.py 中使用缓存

from optimization.api import cached_response, _stock_cache

@stock_bp.route('/<code>/price')
@cached_response(cache=_stock_cache, ttl=60)
def get_stock_price(code):
    """获取股票价格 - 带60秒缓存"""
    return jsonify(get_price_data(code))
```

---

## 五、性能预估

### 5.1 每日同步优化

| 阶段 | 当前耗时 | 优化后耗时 | 提升 |
|------|----------|------------|------|
| 数据拉取 | ~30 分钟 | ~30 分钟 | 1x |
| 策略计算 | ~45 分钟 | ~2 分钟 | **22x** |
| 数据库写入 | ~15 分钟 | ~2 分钟 | **7x** |
| **总计** | **~90 分钟** | **~35 分钟** | **2.5x** |

### 5.2 API 响应优化

| 场景 | 当前延迟 | 优化后延迟 | 提升 |
|------|----------|------------|------|
| 股票行情查询 | ~200ms | ~5ms | **40x** |
| 信号查询 | ~500ms | ~10ms | **50x** |
| 回测结果 | ~5s | ~1s | **5x** |

### 5.3 内存优化

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 策略缓存 | 0 | ~100MB |
| 查询缓存 | 0 | ~50MB |
| 连接池 | 0 | ~10MB |
| **总计** | - | **~160MB** |

---

## 六、风险与注意事项

### 6.1 兼容性

- ✅ 向后兼容：所有优化组件都提供与原代码相同的接口
- ✅ 渐进式迁移：可以逐个模块优化，无需一次性全部替换
- ✅ 缓存失效：TTL 设置合理，数据及时更新

### 6.2 风险控制

- 数据库连接池大小限制，防止资源耗尽
- 缓存容量限制，防止内存溢出
- 增量计算错误回退到全量计算

### 6.3 监控建议

```python
# 添加性能监控
from optimization.api import get_cache_stats

# 获取缓存命中率
stats = get_cache_stats()
print(f"API缓存命中率: {stats['api']['hit_rate']}")
print(f"股票缓存命中率: {stats['stock']['hit_rate']}")
```

---

## 七、后续优化路线图

### Phase 2 (v1.1)
- [ ] Redis 分布式缓存支持
- [ ] 回测引擎统一重构
- [ ] 并行数据同步

### Phase 3 (v2.0)
- [ ] ML 模型缓存
- [ ] 热点数据预加载
- [ ] 异步任务队列

---

## 八、附录

### A. 文件清单

```
optimization/
├── __init__.py
├── auto_optimizer.py          # 一键优化器
├── benchmark.py               # 基准测试
├── README.md                  # 本文档
│
├── batch/
│   ├── __init__.py
│   ├── batch_writer.py        # 批量写入 (280行)
│   └── incremental_calculator.py  # 增量计算 (400行)
│
├── api/
│   ├── __init__.py
│   ├── response_cache.py      # API缓存 (350行)
│   └── db_pool.py            # 连接池 (220行)
│
└── cache/
    ├── __init__.py
    └── memory_cache.py       # 内存缓存 (280行)
```

### B. 依赖项

```
# 新增依赖（可选）
# pip install redis  # 如需分布式缓存
```

### C. 配置参数

```python
# 优化模块配置
OPTIMIZATION_CONFIG = {
    "batch_size": 500,              # 批量写入大小
    "cache_ttl": {
        "stock": 60,                # 行情缓存: 1分钟
        "api": 300,                 # API缓存: 5分钟
        "signal": 600,              # 信号缓存: 10分钟
    },
    "pool_size": 5,                # 连接池大小
    "max_cache_items": 200,        # 最大缓存股票数
}
```

---

**文档版本**: v1.0.0  
**维护者**: AIQuant Team  
**最后更新**: 2026-05-07
