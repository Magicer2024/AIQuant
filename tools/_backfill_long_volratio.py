"""回填 stock_signal 里 long 信号的 vol_ratio（长线波动收敛度排序键）。

为什么必须回填
--------------
`long_vol_sort_enabled=1` 后，long 组排序第一层是 `vol_ratio ASC`。历史 long 信号
（2026-07-17 起 16122 条）写入时该列还不存在 ⇒ 全为 NULL ⇒ 排序时 COALESCE 到 9.9
**全部排最后** ⇒ 两层排序退化成「在 NULL 堆里随机取」——比原来的 fusion DESC 更糟。
chip_conc 踩过同一个坑（实验前 NULL 残渣永久污染），故上线前必须先回填。

口径一致
--------
与 core/sync.py::_vol_ratio_at **完全一致**：
    vol_ratio = (close.pct_change().rolling(60).std() * √252)
              / (close.pct_change().rolling(250).std() * √252)
即 60 日年化波动 ÷ 250 日年化波动。有限记忆滚动窗口（最长 250 日），
不存在 chip_conc 那种 warmup 收敛问题，取全历史精确即可。

用法
----
    python tools/_backfill_long_volratio.py [--dry-run]
"""

import argparse
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import pandas as pd
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1) 确保列存在（与 core/db.py 的 _safe_add_column 同语义）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_signal)")]
    if "vol_ratio" not in cols:
        if args.dry_run:
            print("[dry-run] 将新增列 stock_signal.vol_ratio REAL")
        else:
            conn.execute("ALTER TABLE stock_signal ADD COLUMN vol_ratio REAL")
            conn.commit()
            print("已新增列 stock_signal.vol_ratio REAL")
    else:
        print("列 stock_signal.vol_ratio 已存在")

    # 2) 取所有 long 信号的 (code, scan_date)
    targets = conn.execute(
        "SELECT DISTINCT code, scan_date FROM stock_signal "
        "WHERE COALESCE(horizon,'short')='long' ORDER BY code, scan_date"
    ).fetchall()
    print(f"long 信号去重 {len(targets)} 条，涉及 "
          f"{len(set(r['code'] for r in targets))} 只股票")

    by_code = {}
    for r in targets:
        by_code.setdefault(r["code"], []).append(r["scan_date"])

    n_ok = n_null = 0
    updates = []
    for i, (code, dates) in enumerate(by_code.items()):
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE code=? "
            "ORDER BY trade_date ASC", (code,)).fetchall()
        if len(rows) < 260:
            n_null += len(dates)
            continue
        s = pd.Series([float(r["close"]) for r in rows],
                      index=pd.DatetimeIndex([r["trade_date"] for r in rows]))
        ret = s.pct_change()
        v60 = ret.rolling(60).std() * math.sqrt(252.0)
        v250 = ret.rolling(250).std() * math.sqrt(252.0)
        ratio = v60 / v250
        for d in dates:
            ts = pd.Timestamp(d)
            if ts in ratio.index:
                v = ratio.loc[ts]
                if v is not None and math.isfinite(v) and v > 0:
                    updates.append((float(v), code, d))
                    n_ok += 1
                else:
                    n_null += 1
            else:
                n_null += 1
        if (i + 1) % 200 == 0:
            print(f"  ...已处理 {i+1}/{len(by_code)} 只", flush=True)

    print(f"可回填 {n_ok} 条，无法计算（历史不足/异常）{n_null} 条")
    if args.dry_run:
        print("[dry-run] 未写入")
        return

    conn.executemany(
        "UPDATE stock_signal SET vol_ratio=? WHERE code=? AND scan_date=? "
        "AND COALESCE(horizon,'short')='long'", updates)
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM stock_signal WHERE COALESCE(horizon,'short')='long' "
        "AND vol_ratio IS NULL").fetchone()[0]
    print(f"已写入 {len(updates)} 条；剩余 NULL {left} 条")

    # 3) 抽查分布
    vals = [r[0] for r in conn.execute(
        "SELECT vol_ratio FROM stock_signal WHERE COALESCE(horizon,'short')='long' "
        "AND vol_ratio IS NOT NULL")]
    if vals:
        vals.sort()
        import statistics as st
        print(f"vol_ratio 分布：n={len(vals)} 最小 {vals[0]:.3f}  "
              f"中位 {st.median(vals):.3f}  最大 {vals[-1]:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
