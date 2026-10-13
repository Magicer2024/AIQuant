"""tools/_probe_realtime_reqcount.py —— T6 核实：全 A 实时行情一轮实际 HTTP 请求数

不改生产代码，只包一层 _SESSION.get 计数。绕过 em_guard 缓存直接调底层
_fetch_realtime_all_em，数原始 HTTP 请求次数（周六/非交易日返回最近交易日快照）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import em_realtime as em


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    calls = {"n": 0, "hosts": {}}
    _orig_get = em._SESSION.get

    def _counting_get(url, *a, **kw):
        calls["n"] += 1
        host = url.split("/")[2]
        calls["hosts"][host] = calls["hosts"].get(host, 0) + 1
        return _orig_get(url, *a, **kw)

    em._SESSION.get = _counting_get
    t0 = time.time()
    df = em._fetch_realtime_all_em()
    elapsed = time.time() - t0
    em._SESSION.get = _orig_get

    print(f"东财 clist 直连一轮：HTTP 请求 {calls['n']} 个，耗时 {elapsed:.1f}s，股票 {len(df)} 只")
    print(f"按主机：{calls['hosts']}")
    if len(df):
        print(f"平均每个请求覆盖 {len(df)/max(calls['n'],1):.0f} 只")


if __name__ == "__main__":
    raise SystemExit(main())
