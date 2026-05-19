"""
routes/common.py —— 路由层公共工具函数
"""
import math
import numpy as np
import pandas as pd


def safe_float(val, default=None):
    """安全转换为 float，处理 NaN/Inf"""
    if val is None:
        return default
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return default
        return round(f, 4)
    except Exception:
        return default


def sanitize_json(obj):
    """递归清理对象中的 NaN/Inf，替换为 None（JSON null）"""
    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


def df_to_json_safe(df: pd.DataFrame, cols: list) -> list:
    """将 DataFrame 转换为 JSON 安全的列表"""
    result = []
    for idx, row in df.iterrows():
        item = {"date": str(idx.date())}
        for col in cols:
            if col in row.index:
                item[col] = safe_float(row[col])
        result.append(item)
    return result
