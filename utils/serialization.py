"""
utils/serialization.py — JSON 序列化辅助（NaN/Inf → None，numpy → Python native）
"""
import math
from typing import Any


def sanitize_numeric(obj: Any) -> Any:
    """递归替换 NaN/Inf 为 None，numpy 类型转 Python native"""
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
