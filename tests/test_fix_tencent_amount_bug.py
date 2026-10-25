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


def test_chinext_302_code_maps_to_shenzhen():
    """302 开头是深市创业板新号段：曾落进「默认 sh.」→ baostock 永远查不到，
    302132 整段历史因此走腾讯兜底（换手率/涨跌幅全 0）。"""
    from core.sync import _format_code_for_baostock
    from tools.fix_tencent_amount_bug import _bs_code

    assert _format_code_for_baostock("302132") == "sz.302132"
    assert _bs_code("302132") == "sz.302132"


def test_close_anchored_relaxation_accepts_small_ohlc_drift():
    """2026-10-23 全市场回补遗留 189 行：收盘按 q 折算完全吻合，
    只有开/高偏低 1%~2%（历史写入精度），严格 1% 闸门会误伤。"""
    from tools.backfill_pct_turnover import _close_anchored_match

    # 603165 2025-01-02 实况：库内 9.779/10.259/9.609/9.709，baostock 12.30/12.78/12.13/12.23
    assert _close_anchored_match(_stored(9.779, 10.259, 9.609, 9.709),
                                 12.30, 12.78, 12.13, 12.23)
    # 最差 2.07% 的上限内
    assert _close_anchored_match(_stored(10.0, 10.5, 9.8, 10.2),
                                 10.0, 10.71, 9.8, 10.2)


def test_close_anchored_relaxation_still_rejects_real_mismatch():
    from tools.backfill_pct_turnover import _close_anchored_match

    # 开盘偏 5%（超出 3% 上限）
    assert not _close_anchored_match(_stored(10.0, 10.5, 9.8, 10.2),
                                     10.5, 10.5, 9.8, 10.2)
    # 最低价偏 4%
    assert not _close_anchored_match(_stored(10.0, 10.5, 9.8, 10.2),
                                     10.0, 10.5, 10.2, 10.2)
    # 收盘对不上（q 无法解释）时的退化入参
    assert not _close_anchored_match(_stored(10.0, 10.5, 9.8, 10.2),
                                     0.0, 10.5, 9.8, 10.2)
