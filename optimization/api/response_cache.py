"""
API 缓存优化模块
多层缓存：内存 LRU + 磁盘持久化
"""

import time
import hashlib
import functools
from typing import Callable, Any, Optional, TypeVar, Dict, List
from collections import OrderedDict
from threading import Lock, RLock
from datetime import datetime, timedelta
import json
import os
import pickle

T = TypeVar('T')


class LRUCache:
    """
    线程安全的 LRU 缓存
    
    特性：
    - LRU 淘汰策略
    - TTL 过期机制
    - 线程安全
    - 统计信息
    
    使用示例：
        cache = LRUCache(max_size=1000, ttl_seconds=300)
        cache.set("key", value)
        value = cache.get("key")  # None if expired or missing
    """
    
    def __init__(self, max_size: int = 1000, ttl_seconds: int = 300, name: str = "default"):
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.name = name
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
        self._lock = Lock()
        
        # 统计
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expired": 0,
        }
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存值"""
        with self._lock:
            if key not in self._cache:
                self._stats["misses"] += 1
                return None
            
            # 检查过期
            if time.time() - self._timestamps[key] > self.ttl:
                del self._cache[key]
                del self._timestamps[key]
                self._stats["expired"] += 1
                self._stats["misses"] += 1
                return None
            
            # 移到末尾（最近使用）
            self._cache.move_to_end(key)
            self._stats["hits"] += 1
            return self._cache[key]
    
    def set(self, key: str, value: Any, ttl: int = None):
        """
        设置缓存值
        
        Args:
            key: 缓存键
            value: 缓存值
            ttl: 可选的 TTL，覆盖默认 TTL
        """
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                self._cache[key] = value
                # LRU 淘汰
                while len(self._cache) > self.max_size:
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]
                    del self._timestamps[oldest_key]
                    self._stats["evictions"] += 1
            
            self._timestamps[key] = time.time()
    
    def delete(self, key: str) -> bool:
        """删除缓存"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                del self._timestamps[key]
                return True
            return False
    
    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
    
    def invalidate(self, pattern: str = None):
        """
        失效缓存，支持模式匹配
        
        Args:
            pattern: 模式字符串，匹配键中包含此字符串的将被删除
        """
        with self._lock:
            if pattern is None:
                self.clear()
            else:
                keys_to_delete = [k for k in self._cache if pattern in k]
                for k in keys_to_delete:
                    del self._cache[k]
                    del self._timestamps[k]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            hit_rate = self._stats["hits"] / total if total > 0 else 0
            return {
                **self._stats,
                "size": len(self._cache),
                "hit_rate": round(hit_rate, 4),
            }
    
    def __len__(self) -> int:
        return len(self._cache)
    
    def __contains__(self, key: str) -> bool:
        with self._lock:
            if key not in self._cache:
                return False
            if time.time() - self._timestamps[key] > self.ttl:
                del self._cache[key]
                del self._timestamps[key]
                return False
            return True


class TimedCache:
    """
    基于时间的缓存（固定时间窗口）
    
    适用于：每日数据、定时刷新场景
    """
    
    def __init__(self, window_seconds: int = 3600):
        self.window_seconds = window_seconds
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._lock = Lock()
    
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                return None
            if time.time() - self._timestamps[key] > self.window_seconds:
                del self._cache[key]
                del self._timestamps[key]
                return None
            return self._cache[key]
    
    def set(self, key: str, value: Any):
        with self._lock:
            self._cache[key] = value
            self._timestamps[key] = time.time()
    
    def invalidate(self):
        """失效整个时间窗口"""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()


class DiskCache:
    """
    磁盘持久化缓存
    
    适用于：大数据、跨进程共享
    """
    
    def __init__(self, cache_dir: str = ".cache", max_size_mb: int = 500):
        self.cache_dir = cache_dir
        self.max_size_mb = max_size_mb
        os.makedirs(cache_dir, exist_ok=True)
        self._lock = RLock()
    
    def _get_path(self, key: str) -> str:
        key_hash = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{key_hash}.cache")
    
    def get(self, key: str, ttl_seconds: int = None) -> Optional[Any]:
        """获取缓存，TTL 可选"""
        path = self._get_path(key)
        if not os.path.exists(path):
            return None
        
        if ttl_seconds:
            mtime = os.path.getmtime(path)
            if time.time() - mtime > ttl_seconds:
                os.remove(path)
                return None
        
        try:
            with open(path, 'rb') as f:
                return pickle.load(f)
        except Exception:
            return None
    
    def set(self, key: str, value: Any):
        """设置缓存"""
        path = self._get_path(key)
        try:
            with open(path, 'wb') as f:
                pickle.dump(value, f)
            self._cleanup()
        except Exception as e:
            print(f"DiskCache 保存失败: {e}")
    
    def delete(self, key: str):
        """删除缓存"""
        path = self._get_path(key)
        if os.path.exists(path):
            os.remove(path)
    
    def _cleanup(self):
        """清理超出大小限制的文件"""
        total_size = sum(
            os.path.getsize(os.path.join(self.cache_dir, f))
            for f in os.listdir(self.cache_dir)
            if f.endswith('.cache')
        ) / 1024 / 1024
        
        if total_size > self.max_size_mb:
            # 删除最旧的文件
            files = [
                (os.path.getmtime(os.path.join(self.cache_dir, f)), f)
                for f in os.listdir(self.cache_dir)
                if f.endswith('.cache')
            ]
            files.sort()
            
            for _, filename in files[:len(files) // 2]:
                try:
                    os.remove(os.path.join(self.cache_dir, filename))
                except Exception:
                    pass


# ============================================================
# 全局缓存实例
# ============================================================

# API 响应缓存（5分钟 TTL）
_api_cache = LRUCache(max_size=2000, ttl_seconds=300, name="api")

# 股票数据缓存（1分钟 TTL，行情数据更新频繁）
_stock_cache = LRUCache(max_size=500, ttl_seconds=60, name="stock")

# 信号数据缓存（10分钟 TTL）
_signal_cache = LRUCache(max_size=100, ttl_seconds=600, name="signal")

# 回测结果缓存（1小时 TTL）
_backtest_cache = LRUCache(max_size=50, ttl_seconds=3600, name="backtest")

# 每日定时缓存（交易时段后刷新）
_daily_cache = TimedCache(window_seconds=86400)

# 磁盘缓存
_disk_cache = DiskCache(cache_dir=".cache", max_size_mb=500)


# ============================================================
# 缓存装饰器
# ============================================================

def cached_response(
    cache: LRUCache = None,
    ttl: int = None,
    key_func: Callable = None,
    condition: Callable = None
):
    """
    API 响应缓存装饰器
    
    用法：
        @cached_response(cache=_stock_cache)
        def get_stock_price(code: str):
            ...
    
        # 自定义缓存键
        @cached_response(cache=_api_cache, key_func=lambda code, date: f"{code}_{date}")
        def get_data(code, date):
            ...
    
        # 条件缓存
        @cached_response(condition=lambda result: result is not None)
        def get_data():
            ...
    """
    if cache is None:
        cache = _api_cache
    
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # 条件检查
            if condition is not None:
                result = func(*args, **kwargs)
                if not condition(result):
                    return result
                cache.set(_make_key(func, args, kwargs, key_func), result, ttl)
                return result
            
            # 生成缓存键
            cache_key = _make_key(func, args, kwargs, key_func)
            
            # 尝试获取缓存
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                return cached_result
            
            # 执行函数并缓存
            result = func(*args, **kwargs)
            if result is not None:
                cache.set(cache_key, result, ttl)
            return result
        
        # 添加缓存管理方法
        wrapper.cache = cache
        wrapper.cache_clear = lambda: cache.clear()
        wrapper.cache_invalidate = lambda p=None: cache.invalidate(p)
        wrapper.cache_stats = lambda: cache.get_stats()
        
        return wrapper
    
    return decorator


def _make_key(
    func: Callable,
    args: tuple,
    kwargs: dict,
    key_func: Callable = None
) -> str:
    """生成缓存键"""
    if key_func:
        return key_func(*args, **kwargs)
    
    # 默认：函数名 + 参数
    parts = [func.__name__]
    parts.extend(str(arg) for arg in args)
    parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return hashlib.md5(":".join(parts).encode()).hexdigest()


# ============================================================
# 便捷函数
# ============================================================

def get_api_cache() -> LRUCache:
    """获取 API 缓存"""
    return _api_cache


def get_stock_cache() -> LRUCache:
    """获取股票缓存"""
    return _stock_cache


def get_signal_cache() -> LRUCache:
    """获取信号缓存"""
    return _signal_cache


def clear_all_caches():
    """清空所有缓存"""
    _api_cache.clear()
    _stock_cache.clear()
    _signal_cache.clear()
    _backtest_cache.clear()
    _daily_cache.invalidate()


def get_cache_stats() -> Dict[str, Dict]:
    """获取所有缓存统计"""
    return {
        "api": _api_cache.get_stats(),
        "stock": _stock_cache.get_stats(),
        "signal": _signal_cache.get_stats(),
        "backtest": _backtest_cache.get_stats(),
    }
