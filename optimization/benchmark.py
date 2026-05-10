"""
性能基准测试
用于验证优化效果
"""

import time
import os
import sys
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class PerformanceBenchmark:
    """
    性能基准测试
    
    测试项目：
    1. 数据库写入性能
    2. 策略计算性能
    3. 查询缓存性能
    4. 并行计算性能
    """
    
    def __init__(self):
        self.results = {}
        self.db_path = project_root / "core" / "quant.db"
        self.test_results_dir = project_root / ".benchmark_results"
        self.test_results_dir.mkdir(exist_ok=True)
    
    def run_all(self) -> dict:
        """运行所有基准测试"""
        print("=" * 60)
        print("🚀 AIQuant 性能基准测试")
        print("=" * 60)
        
        tests = [
            ("数据库写入", self.benchmark_db_write),
            ("策略计算", self.benchmark_strategy_calculation),
            ("查询缓存", self.benchmark_cache),
            ("批量优化", self.benchmark_batch_write),
        ]
        
        for name, test_fn in tests:
            print(f"\n📊 测试: {name}")
            print("-" * 40)
            try:
                result = test_fn()
                self.results[name] = result
                print(f"   ✅ 完成: {result}")
            except Exception as e:
                print(f"   ❌ 失败: {e}")
                self.results[name] = {"error": str(e)}
        
        self._save_results()
        self._print_summary()
        
        return self.results
    
    def benchmark_db_write(self) -> dict:
        """测试数据库写入性能"""
        # 准备测试数据
        n_records = 1000
        test_records = [
            {
                "code": f"00000{i%100:03d}",
                "trade_date": (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"),
                "close": 10.0 + np.random.random() * 100,
                "volume": 1000000 + np.random.random() * 1000000,
                "vol_score": np.random.random() * 10,
                "ma_score": np.random.random() * 10,
            }
            for i in range(n_records)
        ]
        
        # 逐条写入测试
        start = time.time()
        conn = sqlite3.connect(str(self.db_path))
        for rec in test_records[:100]:  # 测试100条
            conn.execute("""
                INSERT INTO daily_price 
                    (code, trade_date, close, volume, vol_score, ma_score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (rec["code"], rec["trade_date"], rec["close"], 
                  rec["volume"], rec["vol_score"], rec["ma_score"]))
        conn.commit()
        conn.close()
        single_time = time.time() - start
        
        # 批量写入测试
        start = time.time()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executemany("""
            INSERT INTO daily_price 
                (code, trade_date, close, volume, vol_score, ma_score)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            (r["code"], r["trade_date"], r["close"], r["volume"], r["vol_score"], r["ma_score"])
            for r in test_records[:100]
        ])
        conn.commit()
        conn.close()
        batch_time = time.time() - start
        
        speedup = single_time / batch_time if batch_time > 0 else 1
        
        return {
            "逐条写入(100条)": f"{single_time*1000:.2f} ms",
            "批量写入(100条)": f"{batch_time*1000:.2f} ms",
            "速度提升": f"{speedup:.2f}x",
        }
    
    def benchmark_strategy_calculation(self) -> dict:
        """测试策略计算性能"""
        # 准备测试数据
        n_days = 250  # 约一年交易日
        dates = pd.date_range(end=datetime.now(), periods=n_days)
        prices = 10.0 + np.cumsum(np.random.randn(n_days) * 0.5)
        volumes = 1000000 + np.abs(np.random.randn(n_days)) * 500000
        
        df = pd.DataFrame({
            "close": prices,
            "open": prices * (1 + np.random.randn(n_days) * 0.01),
            "high": prices * (1 + np.abs(np.random.randn(n_days)) * 0.02),
            "low": prices * (1 - np.abs(np.random.randn(n_days)) * 0.02),
            "volume": volumes,
        })
        
        # 全量计算测试
        start = time.time()
        for _ in range(5):
            ma5 = df["close"].rolling(5).mean()
            ma10 = df["close"].rolling(10).mean()
            ma20 = df["close"].rolling(20).mean()
            vol5 = df["volume"].rolling(5).mean()
            
            vr = df["volume"] / vol5.clip(lower=1e-9)
            score = (vr.clip(upper=3.0) / 3.0).fillna(0)
        full_time = (time.time() - start) / 5
        
        # 增量计算测试（假设只计算最后10天）
        start = time.time()
        for _ in range(5):
            df_tail = df.tail(10)
            ma5 = df_tail["close"].rolling(5).mean()
            ma10 = df_tail["close"].rolling(10).mean()
            ma20 = df_tail["close"].rolling(20).mean()
            vol5 = df_tail["volume"].rolling(5).mean()
            
            vr = df_tail["volume"] / vol5.clip(lower=1e-9)
            score = (vr.clip(upper=3.0) / 3.0).fillna(0)
        incremental_time = (time.time() - start) / 5
        
        speedup = full_time / incremental_time if incremental_time > 0 else 1
        
        return {
            "全量计算(250天)": f"{full_time*1000:.2f} ms",
            "增量计算(10天)": f"{incremental_time*1000:.2f} ms",
            "速度提升": f"{speedup:.2f}x",
        }
    
    def benchmark_cache(self) -> dict:
        """测试缓存性能"""
        from optimization.api.response_cache import LRUCache
        
        cache = LRUCache(max_size=1000, ttl_seconds=60)
        
        # 准备测试数据
        n_ops = 10000
        keys = [f"key_{i%500}" for i in range(n_ops)]  # 500个不同键，重复访问
        values = list(range(500))
        
        # 写操作测试
        start = time.time()
        for i, key in enumerate(keys[:5000]):
            cache.set(key, values[i % 500])
        write_time = time.time() - start
        
        # 读操作测试
        cache.clear()
        for key in keys:
            cache.set(key, values[int(key.split("_")[1])])
        
        start = time.time()
        hits = 0
        for key in keys:
            if cache.get(key) is not None:
                hits += 1
        read_time = time.time() - start
        
        return {
            "写操作(5000次)": f"{write_time*1000:.2f} ms",
            "读操作(10000次)": f"{read_time*1000:.2f} ms",
            "缓存命中率": f"{hits/len(keys)*100:.1f}%",
        }
    
    def benchmark_batch_write(self) -> dict:
        """测试批量写入优化"""
        from optimization.batch.batch_writer import BatchWriter
        
        writer = BatchWriter(str(self.db_path), batch_size=500)
        
        # 准备测试数据
        n_records = 1000
        dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_records)]
        
        scores_df = pd.DataFrame({
            "VOL_SCORE": np.random.random(n_records) * 10,
            "MA_SCORE": np.random.random(n_records) * 10,
            "DIVERGE_SCORE": np.random.random(n_records) * 10,
            "BOTTOM_SCORE": np.random.random(n_records) * 10,
            "WHALE_SCORE": np.random.random(n_records) * 10,
            "FUSION_SCORE": np.random.random(n_records) * 50,
        }, index=pd.to_datetime(dates))
        
        # 测试批量写入
        start = time.time()
        n = writer.batch_upsert_strategy_scores("TEST999", scores_df)
        batch_time = time.time() - start
        
        stats = writer.get_stats()
        
        # 清理测试数据
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DELETE FROM daily_price WHERE code='TEST999'")
        conn.commit()
        conn.close()
        
        return {
            "写入记录数": n,
            "耗时": f"{batch_time*1000:.2f} ms",
            "速度": f"{n/batch_time:.0f} 记录/秒",
        }
    
    def _save_results(self):
        """保存结果"""
        import json
        
        output_file = self.test_results_dir / f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "results": self.results,
            }, f, ensure_ascii=False, indent=2)
        
        print(f"\n📁 结果已保存: {output_file}")
    
    def _print_summary(self):
        """打印摘要"""
        print("\n" + "=" * 60)
        print("📊 基准测试摘要")
        print("=" * 60)
        
        for name, result in self.results.items():
            if isinstance(result, dict) and "error" not in result:
                print(f"\n【{name}】")
                for k, v in result.items():
                    print(f"   {k}: {v}")


if __name__ == "__main__":
    benchmark = PerformanceBenchmark()
    benchmark.run_all()
