"""
utils/finance_data.py
======================
为自定义策略函数提供财务基本面数据接口。

数据来源：从 stock_info 表及 daily_price 表获取静态/动态数据。
由于本项目数据库暂无独立财务报表表，此模块提供两层降级策略：
  1. 优先从 stock_info 扩展字段读取（如已同步 total_shares/float_shares/eps）
  2. 若字段不存在，则从 daily_price 的 amount/volume 反推换手率，其余字段返回 None

策略函数调用示例：
    from utils.finance_data import get_total_shares, get_float_shares, get_stock_finance
    total = get_total_shares('000001')   # 返回总股本（股），例 1000000000
    float_s = get_float_shares('000001') # 返回流通股本（股），例 800000000
    finance = get_stock_finance('000001')
    eps = finance.get('eps')             # 每股收益
"""

import os
import sys
import logging

# 保证能 import core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# ─── 内部缓存（同一进程内复用，避免重复查 DB）───────────────────────────
_cache_total_shares: dict  = {}
_cache_float_shares: dict  = {}
_cache_finance:      dict  = {}

# ─── 列存在性标志（避免每次 ALTER TABLE 查询）────────────────────────────
_stock_info_cols: set | None = None

def _get_stock_info_cols() -> set:
    """惰性获取 stock_info 表的列名集合"""
    global _stock_info_cols
    if _stock_info_cols is not None:
        return _stock_info_cols
    try:
        from core.db import get_conn
        with get_conn() as conn:
            rows = conn.execute("PRAGMA table_info(stock_info)").fetchall()
            _stock_info_cols = {r[1] for r in rows}  # r[1] = column name
    except Exception:
        _stock_info_cols = set()
    return _stock_info_cols


def _ensure_finance_cols():
    """
    如果 stock_info 还没有财务字段，自动添加（仅在需要时执行一次）。
    新增字段：total_shares, float_shares, eps, pe_ttm, pb
    """
    cols = _get_stock_info_cols()
    needed = {
        'total_shares': 'REAL',
        'float_shares':  'REAL',
        'eps':           'REAL',
        'pe_ttm':        'REAL',
        'pb':            'REAL',
    }
    missing = {k: v for k, v in needed.items() if k not in cols}
    if not missing:
        return
    try:
        from core.db import get_conn
        with get_conn() as conn:
            for col, col_type in missing.items():
                try:
                    conn.execute(f"ALTER TABLE stock_info ADD COLUMN {col} {col_type}")
                    logger.info(f"[finance_data] 添加列 stock_info.{col}")
                except Exception:
                    pass
        # 清空缓存，下次重新探测
        global _stock_info_cols
        _stock_info_cols = None
    except Exception as e:
        logger.warning(f"[finance_data] 添加财务列失败: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# 公共接口
# ──────────────────────────────────────────────────────────────────────────────

def get_total_shares(stock_code: str) -> float | None:
    """
    获取总股本（单位：股）。
    - 优先从 stock_info.total_shares 读取
    - 若为 None，返回 None（调用方需做判 None 处理）
    """
    if stock_code in _cache_total_shares:
        return _cache_total_shares[stock_code]

    _ensure_finance_cols()
    cols = _get_stock_info_cols()
    result = None

    if 'total_shares' in cols:
        try:
            from core.db import get_conn
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT total_shares FROM stock_info WHERE code=?", (stock_code,)
                ).fetchone()
            if row and row[0] is not None:
                result = float(row[0])
        except Exception as e:
            logger.warning(f"[finance_data] get_total_shares({stock_code}): {e}")

    _cache_total_shares[stock_code] = result
    return result


def get_float_shares(stock_code: str) -> float | None:
    """
    获取流通股本（单位：股）。
    - 优先从 stock_info.float_shares 读取
    - 若为 None，返回 None
    """
    if stock_code in _cache_float_shares:
        return _cache_float_shares[stock_code]

    _ensure_finance_cols()
    cols = _get_stock_info_cols()
    result = None

    if 'float_shares' in cols:
        try:
            from core.db import get_conn
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT float_shares FROM stock_info WHERE code=?", (stock_code,)
                ).fetchone()
            if row and row[0] is not None:
                result = float(row[0])
        except Exception as e:
            logger.warning(f"[finance_data] get_float_shares({stock_code}): {e}")

    _cache_float_shares[stock_code] = result
    return result


def calculate_turnover_rate(stock_code: str, volume: float, price: float = None) -> float | None:
    """
    计算换手率（%）。
    formula: volume / float_shares * 100

    参数：
        stock_code: 股票代码
        volume:     当日成交量（股）
        price:      当日收盘价（备用，暂未使用）

    返回：换手率（%），例如 2.35 表示 2.35%
    若流通股本未知则返回 None
    """
    float_s = get_float_shares(stock_code)
    if float_s and float_s > 0:
        return volume / float_s * 100
    return None


def get_stock_finance(stock_code: str) -> dict:
    """
    获取股票财务数据字典，包含：
      - eps:     每股收益（元）
      - pe_ttm:  市盈率（TTM，动态）
      - pb:      市净率
      - total_shares: 总股本（股）
      - float_shares: 流通股本（股）

    调用示例：
        finance = get_stock_finance('000001')
        eps  = finance.get('eps')      # 可能为 None
        pe   = finance.get('pe_ttm')   # 可能为 None

    注意：字段可能为 None，策略代码中需要做判 None 处理。
    """
    if stock_code in _cache_finance:
        return _cache_finance[stock_code]

    _ensure_finance_cols()
    cols = _get_stock_info_cols()

    # 从 stock_info 读取可用的财务字段
    select_fields = ['code', 'name']
    for f in ['total_shares', 'float_shares', 'eps', 'pe_ttm', 'pb']:
        if f in cols:
            select_fields.append(f)

    result: dict = {
        'eps':          None,
        'pe_ttm':       None,
        'pb':           None,
        'total_shares': None,
        'float_shares': None,
    }

    try:
        from core.db import get_conn
        sql = f"SELECT {', '.join(select_fields)} FROM stock_info WHERE code=?"
        with get_conn() as conn:
            row = conn.execute(sql, (stock_code,)).fetchone()
        if row:
            for f in ['total_shares', 'float_shares', 'eps', 'pe_ttm', 'pb']:
                if f in cols:
                    v = row[f] if hasattr(row, '__getitem__') and f in (row.keys() if hasattr(row, 'keys') else []) else None
                    if v is not None:
                        result[f] = float(v)
    except Exception as e:
        logger.warning(f"[finance_data] get_stock_finance({stock_code}): {e}")

    _cache_finance[stock_code] = result
    return result


def update_stock_finance(stock_code: str, total_shares: float = None,
                          float_shares: float = None, eps: float = None,
                          pe_ttm: float = None, pb: float = None):
    """
    更新 stock_info 中的财务字段（回测前可批量导入数据时调用）。
    只更新传入的非 None 字段。
    """
    _ensure_finance_cols()

    updates = {}
    if total_shares is not None: updates['total_shares'] = total_shares
    if float_shares  is not None: updates['float_shares']  = float_shares
    if eps           is not None: updates['eps']           = eps
    if pe_ttm        is not None: updates['pe_ttm']        = pe_ttm
    if pb            is not None: updates['pb']            = pb

    if not updates:
        return

    set_clause = ', '.join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [stock_code]

    try:
        from core.db import get_conn
        with get_conn() as conn:
            conn.execute(
                f"UPDATE stock_info SET {set_clause} WHERE code=?", values
            )
        # 清空相关缓存
        _cache_total_shares.pop(stock_code, None)
        _cache_float_shares.pop(stock_code, None)
        _cache_finance.pop(stock_code, None)
        logger.info(f"[finance_data] 更新 {stock_code} 财务数据: {updates}")
    except Exception as e:
        logger.error(f"[finance_data] update_stock_finance({stock_code}): {e}")


def clear_cache():
    """清空内存缓存（通常不需要手动调用）"""
    global _cache_total_shares, _cache_float_shares, _cache_finance, _stock_info_cols
    _cache_total_shares.clear()
    _cache_float_shares.clear()
    _cache_finance.clear()
    _stock_info_cols = None
