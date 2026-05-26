"""
qlib_engine/__init__.py —— Qlib 引擎初始化模块

职责：
  1. qlib.init() 封装
  2. 确保 Qlib 数据目录存在
  3. 提供 get_qlib_initialized() 状态查询
  4. 修补 Qlib locate_index → future=True（Qlib 数据只到 2020，回测用 2024+）
"""

import os
import functools
import qlib
from qlib.constant import REG_CN

_initialized = False
PROVIDER_URI = os.path.expanduser("~/.qlib/qlib_data/cn_data")


def _patch_locate_index():
    """Monkey-patch Cal.locate_index to default future=True.
    Qlib cn_data ends at 2020-09-25 but our backtests use 2024+ dates.
    Several internal callers don't pass future=True, causing IndexError."""
    from qlib.data.data import Cal

    _orig = Cal.locate_index

    @functools.wraps(_orig)
    def _patched(start_time, end_time, freq, future=True):
        return _orig(start_time, end_time, freq, future=future)

    Cal.locate_index = _patched


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
    _patch_locate_index()
    _initialized = True


def is_initialized() -> bool:
    return _initialized


def get_provider_uri() -> str:
    return PROVIDER_URI
