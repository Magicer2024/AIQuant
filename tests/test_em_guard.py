"""
test_em_guard.py —— 验证东财护栏三道防线
=========================================
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from core.em_guard import (
    cached_fetch, guard_status, force_clear_cache,
    _ensure_db, GUARD_CONFIG, _conn,
)


def _clean_slate(name: str):
    GUARD_CONFIG[name] = {
        "window_sec": 60, "max_in_window": 5, "default_ttl": 60, "daily_quota": 100,
    }
    with _conn() as conn:
        conn.execute("DELETE FROM em_calls WHERE name=?", (name,))
        conn.execute("DELETE FROM em_cache WHERE name=?", (name,))
    _ensure_db()


def make_dummy_fetcher(data_factory):
    counter = {"n": 0}
    def _f():
        counter["n"] += 1
        return data_factory(counter["n"])
    return _f, counter


def test_cache_hit():
    print("\n=== 测试 1：缓存命中（fresh 路径）===")
    name = "_t1_cache_hit"
    _clean_slate(name)
    fetcher, counter = make_dummy_fetcher(lambda n: pd.DataFrame({"n": [n], "ts": [time.time()]}))

    df1, src1 = cached_fetch(name, {"k": 1}, fetcher, ttl=60)
    print(f"  [1st] source={src1} counter={counter['n']}")
    assert counter["n"] == 1 and src1 == "network"

    df2, src2 = cached_fetch(name, {"k": 1}, fetcher, ttl=60)
    print(f"  [2nd] source={src2} counter={counter['n']}")
    assert counter["n"] == 1 and src2 == "fresh", f"期望 fresh/1 实际 {src2}/{counter['n']}"

    print("  [OK] 第二次调用直接命中缓存，0 次网络请求")


def test_frequency_limit():
    print("\n=== 测试 2：频次超限返回 stale ===")
    name = "_t2_freq_limit"
    GUARD_CONFIG[name] = {"window_sec": 60, "max_in_window": 3, "default_ttl": 0, "daily_quota": 100}
    with _conn() as conn:
        conn.execute("DELETE FROM em_calls WHERE name=?", (name,))
        conn.execute("DELETE FROM em_cache WHERE name=?", (name,))
    fetcher, counter = make_dummy_fetcher(lambda n: pd.DataFrame({"n": [n]}))

    for i in range(3):
        df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=0)
        print(f"  [call {i+1}] source={src} counter={counter['n']}")

    df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=0)
    print(f"  [call 4] source={src} counter={counter['n']}")
    assert src == "stale", f"期望 stale 实际 {src}"
    assert counter["n"] == 3, f"超限后不应再发请求，实际 {counter['n']}"
    print("  [OK] 第 4 次触发频次限制，返回 stale，未发起新请求")


def test_daily_quota():
    print("\n=== 测试 3：每日配额用尽 ===")
    name = "_t3_quota"
    GUARD_CONFIG[name] = {"window_sec": 86400, "max_in_window": 100, "default_ttl": 0, "daily_quota": 2}
    with _conn() as conn:
        conn.execute("DELETE FROM em_calls WHERE name=?", (name,))
        conn.execute("DELETE FROM em_cache WHERE name=?", (name,))
    fetcher, counter = make_dummy_fetcher(lambda n: pd.DataFrame({"n": [n]}))

    df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=0)
    print(f"  [call 1] source={src} counter={counter['n']}")
    df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=0)
    print(f"  [call 2] source={src} counter={counter['n']}")
    df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=0)
    print(f"  [call 3] source={src} counter={counter['n']}")
    assert src == "stale", f"配额用尽应返回 stale，实际 {src}"
    print("  [OK] 每日配额用尽后返回 stale")


def test_source_field():
    print("\n=== 测试 4：source 字段流转 ===")
    name = "_t4_source"
    _clean_slate(name)
    fetcher, counter = make_dummy_fetcher(lambda n: pd.DataFrame({"n": [n]}))

    df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=60)
    print(f"  第一次: source={src}")
    assert src == "network"
    df, src = cached_fetch(name, {"k": 1}, fetcher, ttl=60)
    print(f"  第二次: source={src}")
    assert src == "fresh"
    print("  [OK] network → fresh 流转正确")


def test_guard_status():
    print("\n=== 测试 5：guard_status() 状态查询 ===")
    status = guard_status()
    print(f"  监控接口: {list(status.keys())}")
    for k, v in status.items():
        if v["cached_count"] > 0 or v["in_window"] > 0:
            print(f"  - {k}: 窗口 {v['in_window']}/{v['max_window']}, "
                  f"今日 {v['today']}/{v['daily_quota']}, 缓存 {v['cached_count']}条")
    print("  [OK] 状态查询接口工作正常")


if __name__ == "__main__":
    print("=" * 60)
    print("  东财护栏三道防线验证")
    print("=" * 60)

    test_cache_hit()
    test_frequency_limit()
    test_daily_quota()
    test_source_field()
    test_guard_status()

    print("\n" + "=" * 60)
    print("  ✓ 全部测试通过")
    print("=" * 60)
