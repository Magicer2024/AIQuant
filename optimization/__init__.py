"""
优化模块初始化
导出所有优化组件
"""

from .batch.batch_writer import BatchWriter
from .batch.incremental_calculator import IncrementalStrategyCalculator
from .api.response_cache import LRUCache, cached_response, _api_cache, _stock_cache, _signal_cache
from .api.db_pool import ConnectionPool, get_db_pool

__all__ = [
    "BatchWriter",
    "IncrementalStrategyCalculator", 
    "LRUCache",
    "cached_response",
    "_api_cache",
    "_stock_cache",
    "_signal_cache",
    "ConnectionPool",
    "get_db_pool",
]

__version__ = "1.0.0"
