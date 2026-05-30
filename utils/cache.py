"""
utils/cache.py — 简单 TTL 内存缓存（单进程 SQLite 场景，不需要 Redis）
"""
import time
import hashlib
import pickle
from functools import wraps


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
