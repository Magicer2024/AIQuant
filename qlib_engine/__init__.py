"""
qlib_engine/__init__.py —— Qlib 引擎初始化模块

职责：
  1. qlib.init() 封装
  2. 确保 Qlib 数据目录存在
  3. 提供 get_qlib_initialized() 状态查询
"""

import os
import qlib
from qlib.constant import REG_CN

_initialized = False
PROVIDER_URI = os.path.expanduser("~/.qlib/qlib_data/cn_data")


def init_qlib(provider_uri: str = None) -> None:
    """
    Initialize Qlib with the binary data directory.

    Idempotent — safe to call multiple times.
    """
    global _initialized
    if _initialized:
        return

    uri = provider_uri or PROVIDER_URI
    os.makedirs(uri, exist_ok=True)
    qlib.init(provider_uri=uri, region=REG_CN)
    _initialized = True


def is_initialized() -> bool:
    return _initialized


def get_provider_uri() -> str:
    return PROVIDER_URI
