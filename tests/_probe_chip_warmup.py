"""探针：筹码因子的 warmup 窗口敏感性（只读，不写库）。

背景
----
影子列 `stock_signal.chip_conc` 要写入线上链路，但线上增量路径
（`core/sync.py` 约 1817 行）只读 **scan_date 前 700 自然日**（≈480 交易日）的窗口，
并在 `len(df) < 260` 时回退全历史。

而回测缓存 `logs/_chip_factors_d0.65f0.003.pkl` 用的是
`tools/eval_chip_factor.py::PRICE_START = "2021-06-01"`（≈1290 交易日 warmup）。

筹码分布是**几何衰减的无限记忆**（每日至少衰减 `DEFAULT_DECAY_FLOOR=0.003`），
不是滚动窗口指标 —— 故两个 warmup 长度可能给出不同的 `conc`。
若差异显著，影子列与前向记录就**不是同一个因子**，前向对照会失效。

本探针回答
----------
Q1 窗口敏感性：conc(W=480) vs conc(W=1290) 差多少？分换手率档看。
Q2 缓存口径是否够长：conc(W=1290) vs conc(全历史) 差多少？

用法
----
    python tests/_probe_chip_warmup.py            # 默认
    python tests/_probe_chip_warmup.py --n 120
"""
from __future__ import annotations

import argparse
import io
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import get_conn                      # noqa: E402
from strategy.chip import compute_chip_factors    # noqa: E402

OUT = os.path.join(ROOT, "logs", "_chip_warmup.txt")
WINDOWS = [300, 480, 750, 1290]     # 0 表示全历史
MEGA = ["601398", "601857", "600519", "000001", "003816", "601288", "600028"]


def load_prices(conn, code: str, end: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT trade_date, high, low, close, volume, turnover "
        "FROM daily_price WHERE code=? AND trade_date<=? ORDER BY trade_date",
        conn, params=(code, end))
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=80, help="随机抽样股票数")
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--dates", default="", help="逗号分隔的评估日，默认取库中最后 3 个交易日")
    args = ap.parse_args()

    buf = io.StringIO()

    def w(s=""):
        print(s)
        buf.write(s + "\n")

    rng = np.random.default_rng(args.seed)
    with get_conn() as conn:
        sh = pd.read_sql_query(
            "SELECT code, name, total_shares, circ_shares FROM stock_info "
            "WHERE is_active=1 AND circ_shares > 0", conn)
        all_codes = sh["code"].tolist()
        pick = list(rng.choice(all_codes, size=min(args.n, len(all_codes)),
                               replace=False))
        pick = list(dict.fromkeys(MEGA + pick))

        if args.dates:
            dates = [d.strip() for d in args.dates.split(",")]
        else:
            dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_price "
                "ORDER BY trade_date DESC LIMIT 3").fetchall()]

        shares = dict(zip(sh["code"], sh["circ_shares"]))

        w("=" * 78)
        w("筹码 warmup 窗口敏感性探针")
        w(f"抽样 {len(pick)} 只（含 {len(MEGA)} 只大盘股）｜评估日 {dates}")
        w(f"decay=0.65  decay_floor=0.003  turnover=auto  "
          f"（窗口 0=全历史）")
        w("=" * 78)

        rows = []
        for d in dates:
            w("")
            w(f"── 评估日 {d} ──")
            for code in pick:
                px = load_prices(conn, code, d)
                if len(px) < 300:
                    continue
                cs = shares.get(code)
                # 该股换手率档（用 volume/circ_shares 口径，判断是否触发 decay_floor）
                to = px["volume"].to_numpy(float) / float(cs)
                to_med = float(np.nanmedian(to[-250:]))
                vals = {}
                for W in WINDOWS + [0]:
                    sub = px if W == 0 else px.tail(W)
                    try:
                        f = compute_chip_factors(sub, cs)
                        v = float(f["conc"].iloc[-1])
                    except Exception:
                        v = float("nan")
                    vals[W] = v
                base = vals[1290]
                if not np.isfinite(base):
                    continue
                rows.append({
                    "date": d, "code": code, "n_hist": len(px),
                    "to_med_pct": round(to_med * 100, 3),
                    "floor_hit": int(0.65 * to_med < 0.003),
                    **{f"conc_W{k}": vals[k] for k in WINDOWS + [0]},
                    "d_480": vals[480] - base,
                    "d_750": vals[750] - base,
                    "d_all_vs_1290": vals[0] - base,
                })

        df = pd.DataFrame(rows)
        if df.empty:
            w("!! 无有效样本")
            io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
            return

        # ── Q1 / Q2 汇总 ──
        w("")
        w("=" * 78)
        w("Q1 窗口敏感性：conc(短窗口) − conc(W=1290)")
        w("=" * 78)
        w(f"{'窗口':>8} {'n':>6} {'中位差':>10} {'绝对值中位':>12} "
          f"{'P95|差|':>10} {'最大|差|':>10} {'>0.005 占比':>12}")
        for W in [300, 480, 750]:
            dd = (df[f"conc_W{W}"] - df["conc_W1290"]).dropna()
            if not len(dd):
                continue
            big = float((dd.abs() > 0.005).mean() * 100)
            w(f"{W:>8} {len(dd):>6} {dd.median():>10.6f} {dd.abs().median():>12.6f} "
              f"{dd.abs().quantile(0.95):>10.6f} {dd.abs().max():>10.6f} {big:>11.1f}%")

        w("")
        w("Q2 缓存 warmup 是否够：conc(W=1290) − conc(全历史)")
        dd = (df["conc_W1290"] - df["conc_W0"]).dropna()
        w(f"{'n':>6} {'中位差':>10} {'绝对值中位':>12} {'P95|差|':>10} "
          f"{'最大|差|':>10} {'>0.005 占比':>12}")
        w(f"{len(dd):>6} {dd.median():>10.6f} {dd.abs().median():>12.6f} "
          f"{dd.abs().quantile(0.95):>10.6f} {dd.abs().max():>10.6f} "
          f"{float((dd.abs() > 0.005).mean() * 100):>11.1f}%")

        # ── 分换手率档 ──
        w("")
        w("按换手率档拆（窗口 480 的偏差是否集中在低换手股）")
        w(f"{'换手档':>12} {'n':>5} {'命中floor':>10} {'|差|中位':>12} "
          f"{'|差|P95':>10} {'|差|最大':>10} {'>0.005':>9}")
        df["_b"] = pd.cut(df["to_med_pct"],
                          [-0.01, 0.2, 0.5, 1.0, 2.0, 5.0, 1e9],
                          labels=["<0.2%", "0.2-0.5%", "0.5-1%", "1-2%",
                                  "2-5%", ">5%"])
        for lab, g in df.groupby("_b", observed=True):
            dd = g["d_480"].dropna()
            if not len(dd):
                continue
            w(f"{str(lab):>12} {len(dd):>5} {int(g['floor_hit'].sum()):>10} "
              f"{dd.abs().median():>12.6f} {dd.abs().quantile(0.95):>10.6f} "
              f"{dd.abs().max():>10.6f} "
              f"{float((dd.abs() > 0.005).mean() * 100):>8.1f}%")

        # ── 偏差最大的个股 ──
        w("")
        w("窗口 480 偏差最大的 12 只")
        w(f"{'code':>8} {'换手%':>7} {'floor':>6} {'历史行':>7} "
          f"{'conc480':>9} {'conc1290':>9} {'差':>10}")
        for _, r in df.reindex(df["d_480"].abs().sort_values(ascending=False).index).head(12).iterrows():
            w(f"{r['code']:>8} {r['to_med_pct']:>7.3f} {r['floor_hit']:>6} "
              f"{r['n_hist']:>7} {r['conc_W480']:>9.4f} {r['conc_W1290']:>9.4f} "
              f"{r['d_480']:>+10.6f}")

        w("")
        w("判读标准：")
        w("  · |差| 中位 < 1e-3 且 >0.005 占比 < 5%  → 480 窗口可用（线上增量路径不必改）")
        w("  · 否则 → 影子列必须显式指定 warmup（建议与缓存一致：固定回看 1290 交易日）")

    io.open(OUT, "w", encoding="utf-8").write(buf.getvalue())
    print(f"\n[写出] {OUT}")


if __name__ == "__main__":
    main()
