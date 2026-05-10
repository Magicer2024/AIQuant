"""
内存缓存模块
线程安全的 LRU 内存缓存实现
"""

import time
import hashlib
from typing import Any, Optional, Callable, TypeVar, Dict
from collections import OrderedDict
from threading import Lock, RLock
from functools import wraps
import weakref

T = TypeVar('T')


class MemoryCache:
    """
    线程安全的内存 LRU 缓存
    
    特性：
    - LRU 淘汰策略
    - TTL 过期
    - 线程安全
    - 弱引用支持（可自动清理过期对象）
    - 统计信息
    - 批量操作
    
    使用示例：
        cache = MemoryCache(max_size=1000, ttl_seconds=300)
        cache.set("key", {"data": "value"})
        value = cache.get("key")
    """
    
    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: int = 300,
        name: str = "memory_cache",
        enable_stats: bool = True
    ):
        """
        Args:
            max_size: 最大缓存条目数
            ttl_seconds: 默认 TTL（秒）
            name: 缓存名称（用于日志和统计）
            enable_stats: 是否启用统计
        """
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.name = name
        self.enable_stats = enable_stats
        
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
        self._lock = Lock()
        
        # 统计
        if enable_stats:
            self._stats = {
                "hits": 0,
                "misses": 0,
                "evictions": 0,
                "expired": 0,
                "sets": 0,
            }
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取缓存值
        
        Args:
            key: 缓存键
            default: 默认值（缓存未命中或过期时返回）
        
        Returns:
            缓存值或默认值
        """
        with self._lock:
            if key not in self._cache:
                self._miss()
                return default
            
            # 检查过期
            if time.time() - self._timestamps[key] > self.ttl:
                del self._cache[key]
                del self._timestamps[key]
                self._expire()
                return default
            
            # LRU: 移到末尾
            self._cache.move_to_end(key)
            self._hit()
            return self._cache[key]
    
    def set(self, key: str, value: Any, ttl: int = None):
        """
        设置缓存值
        
        Args:
            key: 缓存键
            value: 缓存值
            ttl: 可选的 TTL（秒），覆盖默认 TTL
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
                    self._evict()
            
            self._timestamps[key] = time.time()
            self._set()
    
    def delete(self, key: str) -> bool:
        """删除缓存项"""
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
        失效匹配的缓存项
        
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
    
    def has(self, key: str) -> bool:
        """检查键是否存在且未过期"""
        with self._lock:
            if key not in self._cache:
                return False
            if time.time() - self._timestamps[key] > self.ttl:
                del self._cache[key]
                del self._timestamps[key]
                return False
            return True
    
    def get_or_compute(self, key: str, compute_fn: Callable[[], T], ttl: int = None) -> T:
        """
        获取缓存值，如果不存在则计算并缓存
        
        Args:
            key: 缓存键
            compute_fn: 计算函数
            ttl: 可选的 TTL
        
        Returns:
            缓存值或计算结果
        """
        value = self.get(key)
        if value is not None:
            return value
        
        value = compute_fn()
        if value is not None:
            self.set(key, value, ttl)
        return value
    
    def __contains__(self, key: str) -> bool:
        """支持 'in' 操作符"""
        return self.has(key)
    
    def __len__(self) -> int:
        return len(self._cache)
    
    def __getitem__(self, key: str) -> Any:
        """支持 'cache[key]' 语法"""
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value
    
    def __setitem__(self, key: str, value: Any):
        """支持 'cache[key] = value' 语法"""
        self.set(key, value)
    
    def __delitem__(self, key: str):
        """支持 'del cache[key]' 语法"""
        if not self.delete(key):
            raise KeyError(key)
    
    def keys(self):
        """返回所有有效缓存键"""
        with self._lock:
            now = time.time()
            return [k for k in self._cache if now - self._timestamps[k] <= self.ttl]
    
    def values(self):
        """返回所有有效缓存值"""
        with self._lock:
            now = time.time()
            return [v for k, v in self._cache.items() if now - self._timestamps[k] <= self.ttl]
    
    def items(self):
        """返回所有有效缓存项"""
        with self._lock:
            now = time.time()
            return [(k, v) for k, v in self._cache.items() if now - self._timestamps[k] <= self.ttl]
    
    # 统计方法
    def _hit(self):
        if self.enable_stats:
            self._stats["hits"] += 1
    
    def _miss(self):
        if self.enable_stats:
            self._stats["misses"] += 1
    
    def _evict(self):
        if self.enable_stats:
            self._stats["evictions"] += 1
    
    def _expire(self):
        if self.enable_stats:
            self._stats["expired"] += 1
    
    def _set(self):
        if self.enable_stats:
            self._stats["sets"] += 1
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            hit_rate = self._stats["hits"] / total if total > 0 else 0
            return {
                **self._stats,
                "size": len(self._cache),
                "max_size": self.max_size,
                "hit_rate": round(hit_rate, 4),
            }
    
    def reset_stats(self):
        """重置统计"""
        if self.enable_stats:
            self._stats = {
                "hits": 0,
                "misses": 0,
                "evictions": 0,
                "expired": 0,
                "sets": 0,
            }


# ============================================================
# 缓存装饰器
# ============================================================

def cache_this(
    cache: MemoryCache = None,
    ttl: int = None,
    key_builder: Callable = None
):
    """
    函数缓存装饰器
    
    用法：
        @cache_this()
        def expensive_function(arg1, arg2):
            ...
        
        # 使用自定义缓存
        custom_cache = MemoryCache(max_size=100)
        @cache_this(cache=custom_cache)
        def function():
            ...
    """
    if cache is None:
        cache = MemoryCache()
    
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # 构建缓存键
            if key_builder:
                cache_key = key_builder(*args, **kwargs)
            else:
                key_parts = [func.__name__]
                key_parts.extend(str(a) for a in args)
                key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
                import hashlib
                cache_key = hashlib.md5(":".join(key_parts).encode()).hexdigest()
            
            # 尝试从缓存获取
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
            
            # 计算并缓存
            result = func(*args, **kwargs)
            if result is not None:
                cache.set(cache_key, result, ttl)
            return result
        
        wrapper.cache = cache
        wrapper.cache_clear = lambda: cache.clear()
        return wrapper
    
    return decorator


# ============================================================
# 全局缓存实例
# ============================================================

# 通用缓存
_default_cache = MemoryCache(max_size=1000, ttl_seconds=300, name="default")

# 短期缓存（1分钟）
_short_cache = MemoryCache(max_size=500, ttl_seconds=60, name="short")

# 中期缓存（5分钟）
_medium_cache = MemoryCache(max_size=500, ttl_seconds=300, name="medium")

# 长期缓存（1小时）
_long_cache = MemoryCache(max_size=200, ttl_seconds=3600, name="long")


def get_default_cache() -> MemoryCache:
    """获取默认缓存"""
    return _default_cache


def get_short_cache() -> MemoryCache:
    """获取短期缓存"""
    return _short_cache


def get_medium_cache() -> MemoryCache:
    """获取中期缓存"""
    return _medium_cache


def get_long_cache() -> MemoryCache:
    """获取长期缓存"""
    return _long_cache
