"""
test_em_speed.py —— 东财直连速度验证
=====================================
用法：
    python test_em_speed.py
"""
import time
import sys
import os

import pytest

# 把项目根加入 import 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 手动基准测试：依赖东财实时网络接口，pytest 默认跳过；
# 需要测速时直接运行 python tests/test_em_speed.py
pytestmark = pytest.mark.skip(reason="手动基准测试，依赖实时网络，直接运行脚本执行")


def test_realtime_all():
    """测试 1：一次拉全 A 最新行情"""
    print("\n=== 测试 1：fetch_realtime_all（一次拉全 A 最新价）===")
    from core.em_realtime import fetch_realtime_all

    t0 = time.time()
    df = fetch_realtime_all(progress_cb=lambda f, t: print(f"\r  拉取 {f}/{t}", end=""))
    elapsed = time.time() - t0

    print(f"\n  ✓ 全 A: {len(df)} 只")
    print(f"  ✓ 耗时: {elapsed:.2f}s")
    print(f"  ✓ 速度: {len(df)/elapsed:.0f} 只/s")
    if not df.empty:
        print("\n  样例数据（前 5 只）:")
        print(df.head(5).to_string(index=False))
    return elapsed, len(df)


def test_kline_one():
    """测试 2：单只股票全历史 K 线（一次 HTTP 拿全）"""
    print("\n=== 测试 2：fetch_kline（单只全历史）===")
    from core.em_kline import fetch_kline

    for code in ["000001", "600519", "300750"]:
        t0 = time.time()
        df = fetch_kline(code, adjust="qfq", klt="d")
        elapsed = time.time() - t0
        print(f"  {code}: {len(df)} 条, 耗时 {elapsed:.2f}s")


def test_kline_by_date():
    """测试 3：按交易日拉全 A 日线"""
    print("\n=== 测试 3：fetch_klines_by_date（按日拉全 A）===")
    from core.em_realtime import fetch_klines_by_date
    from datetime import date, timedelta

    # 取最近一个交易日（简单取昨天）
    target = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    t0 = time.time()
    df = fetch_klines_by_date(target, progress_cb=lambda f, t: print(f"\r  拉取 {f}/{t}", end=""))
    elapsed = time.time() - t0
    print(f"\n  ✓ {target} 全 A: {len(df)} 只, 耗时 {elapsed:.2f}s")
    if not df.empty:
        print("\n  样例数据（前 3 只）:")
        print(df.head(3).to_string(index=False))


def test_recommend_pool():
    """测试 4：推荐池并行拉取"""
    print("\n=== 测试 4：fetch_recommend_pool_parallel（推荐池并行）===")
    from core.sync import fetch_recommend_pool_parallel

    codes = [
        "000001", "600519", "300750", "601318", "000858",
        "600036", "601166", "600900", "000333", "002594",
        "600276", "000651", "600887", "601012", "300059",
    ]
    t0 = time.time()
    result = fetch_recommend_pool_parallel(codes, max_workers=8)
    elapsed = time.time() - t0
    print(f"\n  ✓ 推荐池 {len(codes)} 只, 成功 {len(result['success'])}, 失败 {len(result['failed'])}")
    print(f"  ✓ 耗时: {elapsed:.2f}s, 速度: {len(codes)/elapsed:.1f} 只/s")


if __name__ == "__main__":
    print("=" * 60)
    print("  AIQuant 东财直连速度验证")
    print("=" * 60)

    try:
        test_realtime_all()
    except Exception as e:
        print(f"\n  ✗ 测试 1 失败: {e}")

    try:
        test_kline_one()
    except Exception as e:
        print(f"\n  ✗ 测试 2 失败: {e}")

    try:
        test_kline_by_date()
    except Exception as e:
        print(f"\n  ✗ 测试 3 失败: {e}")

    try:
        test_recommend_pool()
    except Exception as e:
        print(f"\n  ✗ 测试 4 失败: {e}")

    print("\n" + "=" * 60)
    print("  验证完成")
    print("=" * 60)
