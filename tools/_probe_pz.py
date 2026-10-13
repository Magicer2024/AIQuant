"""tools/_probe_pz.py —— T6：东财 clist 单页 pz 参数敏感性实测

固定沪市段、第 1 页，只改 pz，数服务端实际返回条数，确认 51 请求/轮的根因
是"pz 被服务端静默降级"还是分页逻辑。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import em_realtime as em


def probe(pz: int) -> None:
    params = {
        "pn": "1", "pz": str(pz), "po": "1", "np": "1", "ut": em._UT,
        "fl": em._FIELDS_REALTIME, "invt": "2", "fid": "f3",
        "fs": "m:1+t:2,m:1+t:23",
        "fields": "f1,f2,f3,f12,f14",
        "_": str(int(time.time() * 1000)),
    }
    try:
        r = em._SESSION.get("https://push2.eastmoney.com/api/qt/clist/get",
                            params=params, timeout=10)
        payload = (r.json() or {}).get("data") or {}
        n = len(payload.get("diff") or [])
        total = payload.get("total")
        print(f"pz={pz:<5} 实际返回 {n:<4} 条, 该段 total={total}")
    except Exception as e:
        print(f"pz={pz:<5} 失败: {e}")


if __name__ == "__main__":
    for pz in (100, 200, 500, 1000, 2000, 5000):
        probe(pz)
        time.sleep(0.3)
