"""tests/test_fix_tencent_amount_bug.py —— 修复工具的 OHLC 对齐校验

`_scaled_match` 是防止「把别的股票/别的日期的量额写进来」的唯一闸门：
除权后库内前复权价与外部不复权价天然差一个系数 q，必须允许 q≠1，
但 OHLC 四项在折算后必须同时对齐，容忍 1%。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fix_tencent_amount_bug import _scaled_match


def _stored(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_same_basis_matches():
    assert _scaled_match(_stored(10.0, 10.5, 9.8, 10.2), 10.0, 10.5, 9.8, 10.2)


def test_dividend_scaled_matches():
    # 库内为旧基准 7.67，外部不复权 11.04（10 转 4 类除权），q≈1.44
    q = 11.04 / 7.67
    assert _scaled_match(_stored(7.60, 7.80, 7.50, 7.67),
                         7.60 * q, 7.80 * q, 7.50 * q, 11.04)


def test_other_stock_rejected():
    # 只对齐收盘、开盘严重偏离 → 不是同一标的
    assert not _scaled_match(_stored(10.0, 10.5, 9.8, 10.2), 13.0, 13.6, 12.9, 10.2)


def test_degenerate_values_rejected():
    assert not _scaled_match(_stored(0, 10.5, 9.8, 10.2), 10.0, 10.5, 9.8, 10.2)
    assert not _scaled_match(_stored(10.0, 10.5, 9.8, 0), 10.0, 10.5, 9.8, 10.2)
