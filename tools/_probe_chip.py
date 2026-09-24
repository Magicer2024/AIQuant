"""筹码分布模块正确性探针（临时脚本，纯读取）"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                              # noqa: E402
import pandas as pd                                             # noqa: E402

from core.db import get_conn                                    # noqa: E402
from strategy.chip import compute_chip_factors, turnover_series, chip_matrix, _MIDS  # noqa: E402


def main():
    codes = ["000651", "600519", "300750", "002594"]
    with get_conn() as conn:
        for code in codes:
            df = pd.read_sql_query(
                "SELECT trade_date, high, low, close, volume, turnover FROM daily_price "
                "WHERE code = ? ORDER BY trade_date", conn, params=(code,))
            row = conn.execute("SELECT name, circ_shares FROM stock_info WHERE code = ?",
                               (code,)).fetchone()
            name, cs = (row[0], row[1]) if row else (code, None)

            t0 = time.time()
            f = compute_chip_factors(df, cs)
            dt = time.time() - t0
            to = turnover_series(df, cs)

            print(f"\n--- {code} {name}  n={len(df)}  "
                  f"换手率均值={to.mean() * 100:.2f}%  耗时={dt:.2f}s")
            nan_all = [k for k in f.columns if f[k].isna().all()]
            print(f"    全NaN列: {nan_all or '无'}")
            for k in ("winner", "conc", "cost_ratio", "lock"):
                v = f[k].dropna()
                print(f"    {k:<11} P5={v.quantile(.05):+.3f} P50={v.quantile(.5):+.3f} "
                      f"P95={v.quantile(.95):+.3f}")

            last = df.iloc[-1]
            C = chip_matrix(df["high"], df["low"], df["close"], to)
            dist = C[-1]
            top = np.argsort(dist)[-4:][::-1]
            print(f"    最新 {last['trade_date']} close={last['close']}  "
                  f"winner={f['winner'].iloc[-1]:.3f}  "
                  f"筹码中枢={f['cost_ratio'].iloc[-1] * last['close']:.2f}")
            print(f"    筹码峰前4档: "
                  f"{[(round(float(_MIDS[i]), 2), round(float(dist[i]) * 100, 2)) for i in sorted(top)]}")
            # 分布完整性：行和必须为 1
            print(f"    行和检查 max|sum-1| = {np.abs(C.sum(axis=1) - 1).max():.2e}  "
                  f"有限值={np.isfinite(C).all()}")


if __name__ == "__main__":
    main()
