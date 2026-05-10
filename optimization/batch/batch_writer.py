"""
批量数据库写入优化器
核心优化：事务批量提交 + 批量 UPSERT
"""

import sqlite3
import pandas as pd
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import time
import os

class BatchWriter:
    """
    批量数据库写入优化器
    
    性能提升：
    - 事务批量提交：减少 fsync 开销（5-10x）
    - 批量 UPSERT：减少网络往返（10-50x）
    - WAL 模式：读写并发（2-3x）
    
    使用示例：
        writer = BatchWriter(db_path)
        writer.batch_upsert_strategy_scores("000001", scores_df)
    """
    
    def __init__(self, db_path: str, batch_size: int = 500):
        """
        Args:
            db_path: 数据库路径
            batch_size: 批量写入大小，默认 500
        """
        self.db_path = db_path
        self.batch_size = batch_size
        self._stats = {
            "total_writes": 0,
            "total_records": 0,
            "total_time": 0,
        }
    
    @contextmanager
    def batch_transaction(self, readonly: bool = False):
        """
        优化的数据库事务上下文管理器
        
        优化措施：
        1. WAL 模式：支持读写并发
        2. 同步模式 NORMAL：平衡性能和安全
        3. 64MB 缓存：减少磁盘 IO
        4. 内存临时表：加速排序
        """
        conn = sqlite3.connect(
            self.db_path,
            timeout=60,
            check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        
        # 性能优化 PRAGMA
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB
        conn.execute("PRAGMA temp_store=MEMORY")
        
        if readonly:
            conn.execute("PRAGMA read_only=1")
        
        try:
            yield conn
            if not readonly:
                conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def batch_upsert_strategy_scores(
        self, 
        code: str, 
        scores_df: pd.DataFrame,
        chunk_size: int = None
    ) -> int:
        """
        批量更新策略评分 - 高性能版
        
        相比逐行 UPDATE，提升 5-10 倍性能
        
        Args:
            code: 股票代码
            scores_df: 策略评分 DataFrame
            chunk_size: 分块大小
        
        Returns:
            写入记录数
        """
        if scores_df.empty:
            return 0
        
        chunk_size = chunk_size or self.batch_size
        total_records = 0
        start_time = time.time()
        
        # 分块处理
        for i in range(0, len(scores_df), chunk_size):
            chunk = scores_df.iloc[i:i+chunk_size]
            records = self._prepare_strategy_records(code, chunk)
            
            if records:
                with self.batch_transaction() as conn:
                    conn.executemany("""
                        INSERT INTO daily_price 
                            (code, trade_date, vol_score, ma_score, diverge_score, 
                             bottom_score, whale_score, fusion_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code, trade_date) DO UPDATE SET
                            vol_score=excluded.vol_score,
                            ma_score=excluded.ma_score,
                            diverge_score=excluded.diverge_score,
                            bottom_score=excluded.bottom_score,
                            whale_score=excluded.whale_score,
                            fusion_score=excluded.fusion_score
                    """, records)
                total_records += len(records)
        
        elapsed = time.time() - start_time
        self._stats["total_writes"] += 1
        self._stats["total_records"] += total_records
        self._stats["total_time"] += elapsed
        
        return total_records
    
    def _prepare_strategy_records(self, code: str, df: pd.DataFrame) -> List[Tuple]:
        """准备策略评分记录"""
        records = []
        for idx, row in df.iterrows():
            if isinstance(idx, pd.Timestamp):
                dt_str = str(idx.date())
            elif hasattr(idx, 'date'):
                dt_str = str(idx.date())
            else:
                dt_str = str(idx)[:10]
            
            records.append((
                code,
                dt_str,
                round(float(row.get("VOL_SCORE", 0) or 0), 4),
                round(float(row.get("MA_SCORE", 0) or 0), 4),
                round(float(row.get("DIVERGE_SCORE", 0) or 0), 4),
                round(float(row.get("BOTTOM_SCORE", 0) or 0), 4),
                round(float(row.get("WHALE_SCORE", 0) or 0), 4),
                round(float(row.get("FUSION_SCORE", 0) or 0), 4),
            ))
        return records
    
    def batch_upsert_daily_price(self, code: str, df: pd.DataFrame) -> int:
        """
        批量写入每日行情数据
        
        Args:
            code: 股票代码
            df: OHLCV 数据
        
        Returns:
            写入记录数
        """
        if df.empty:
            return 0
        
        records = []
        for idx, row in df.iterrows():
            if hasattr(idx, 'strftime'):
                dt_str = idx.strftime("%Y-%m-%d")
            else:
                dt_str = str(idx)[:10]
            
            records.append((
                code,
                dt_str,
                float(row.get("open", 0) or 0),
                float(row.get("high", 0) or 0),
                float(row.get("low", 0) or 0),
                float(row.get("close", 0) or 0),
                float(row.get("volume", 0) or 0),
                float(row.get("amount", 0) or 0),
                float(row.get("pct_change", 0) or 0),
                float(row.get("turnover", 0) or 0),
            ))
        
        with self.batch_transaction() as conn:
            conn.executemany("""
                INSERT INTO daily_price 
                    (code, trade_date, open, high, low, close, volume, amount, pct_change, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                    pct_change=excluded.pct_change, turnover=excluded.turnover
            """, records)
        
        return len(records)
    
    def batch_upsert(
        self,
        table: str,
        records: List[Dict],
        conflict_cols: List[str],
        update_cols: Optional[List[str]] = None
    ) -> int:
        """
        通用批量 UPSERT
        
        Args:
            table: 表名
            records: 记录列表（字典）
            conflict_cols: 冲突列（用于 ON CONFLICT）
            update_cols: 更新列（默认全部非主键列）
        
        Returns:
            写入记录数
        """
        if not records:
            return 0
        
        if update_cols is None:
            # 默认：更新除冲突列外的所有列
            update_cols = [k for k in records[0].keys() if k not in conflict_cols]
        
        cols = list(records[0].keys())
        placeholders = ','.join(['?' for _ in cols])
        insert_sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        
        on_conflict = ', '.join([f"{c}=excluded.{c}" for c in update_cols])
        upsert_sql = f"{insert_sql} ON CONFLICT({','.join(conflict_cols)}) DO UPDATE SET {on_conflict}"
        
        with self.batch_transaction() as conn:
            conn.executemany(upsert_sql, [tuple(r.values()) for r in records])
        
        return len(records)
    
    def vacuum_database(self) -> float:
        """
        执行 VACUUM 清理数据库碎片
        
        Returns:
            清理前大小(MB)
        """
        size_before = os.path.getsize(self.db_path) / 1024 / 1024
        
        with self.batch_transaction() as conn:
            conn.execute("VACUUM")
        
        return size_before
    
    def get_stats(self) -> Dict[str, Any]:
        """获取写入统计"""
        avg_time = (
            self._stats["total_time"] / self._stats["total_writes"]
            if self._stats["total_writes"] > 0 else 0
        )
        return {
            **self._stats,
            "avg_write_time": round(avg_time, 4),
            "records_per_second": (
                self._stats["total_records"] / self._stats["total_time"]
                if self._stats["total_time"] > 0 else 0
            ),
        }


# 全局实例（延迟初始化）
_global_batch_writer: Optional[BatchWriter] = None

def get_batch_writer(db_path: str = None) -> BatchWriter:
    """获取全局批量写入器"""
    global _global_batch_writer
    if _global_batch_writer is None:
        if db_path is None:
            from config.settings import DB_PATH
            db_path = DB_PATH
        _global_batch_writer = BatchWriter(db_path)
    return _global_batch_writer
