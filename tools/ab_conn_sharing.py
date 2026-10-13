"""tools/ab_conn_sharing.py —— 增量重算连接复用同机 A/B 对照（T5-3 验收）

动机：全量 bench 跨天对照（昨天 01:29 vs 今天 11:59）出现 load_price -70%
但 build_signal +15% 的矛盾，疑似跨时段机器负载噪声。本脚本在同一克隆库、
同一进程内做 A/B，消除跨天差异：
  A = 当前实现：重算循环共享单连接
  B = 旧行为：monkeypatch 丢弃 conn，每票 get_daily_price 独立开/关连接
顺序 A→B（B 后跑占文件缓存预热便宜；若 B 仍慢，则共享连接优化成立）。

用法：
  python tools/ab_conn_sharing.py --db data_cache/recalc_probe_20261011_115928.db
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    from config import settings
    settings.DB_PATH = args.db
    import core.db as db
    db.DB_PATH = args.db

    import core.sync as sync
    from core.repository import price_repo

    with db.get_conn() as conn:
        scan_date = str(conn.execute(
            "SELECT MAX(trade_date) FROM daily_price").fetchone()[0])
    print(f"库：{args.db}\n信号评估日：{scan_date}\n")

    _orig_gdp = price_repo.get_daily_price

    def _independent_gdp(code, *a, **kw):
        # B 模式：强制丢弃共享连接，回退每票独立连接（旧行为）
        kw.pop("conn", None)
        return _orig_gdp(code, *a, **kw)

    results = {}
    # A 先跑（冷），B 后跑（热缓存）——对 B 有利的不公平顺序
    for mode, patch in (("A_shared", False), ("B_independent", True)):
        if patch:
            sync.get_daily_price = _independent_gdp
        else:
            sync.get_daily_price = _orig_gdp
        r = sync.recalc_incremental_signals([scan_date.replace("-", "")], verbose=False)
        st = r["stages"]["stages"]
        results[mode] = (r, st)
        print(f"[{mode}] wall={r['elapsed_s']}s signals={r['signals']} codes={r['codes']}")
        for k, v in st.items():
            print(f"    {k:<15} {v['ms']/1000:8.1f}s  calls={v['calls']}")
        print()

    # 汇总
    print("── A/B 汇总 ──")
    a, b = results["A_shared"][1], results["B_independent"][1]
    for k in ("load_price", "build_signal", "write_signals"):
        av, bv = a[k]["ms"] / 1000, b[k]["ms"] / 1000
        pct = (av - bv) / bv * 100 if bv else 0
        print(f"  {k:<15} A={av:7.1f}s  B={bv:7.1f}s  A-B={pct:+.1f}%")
    ta = results["A_shared"][0]["elapsed_s"]
    tb = results["B_independent"][0]["elapsed_s"]
    print(f"  {'wall':<15} A={ta:7.1f}s  B={tb:7.1f}s  A-B={(ta-tb)/tb*100:+.1f}%")
    sig_ok = results["A_shared"][0]["signals"] == results["B_independent"][0]["signals"]
    print(f"  signals 一致：{sig_ok}（{results['A_shared'][0]['signals']}）")


if __name__ == "__main__":
    raise SystemExit(main())
