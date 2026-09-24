"""回填 stock_signal 里 long 信号的 long_mask / vol60 / dd250 + 计算 long_rank_key。

为什么必须回填
--------------
2026-09-20 长线选股重设计（long_lowvol_sort_enabled=1）依赖三样东西：
  - `long_mask`：票池过滤 `(long_mask & 7) = 7`（趋势+斜率+低波三项必需）
  - `vol60` / `dd250`：连续排序特征
  - `long_rank_key`：0.75×pctile(vol60) + 0.25×pctile(dd250)，**当日票池内**横截面百分位

历史 long 信号（2026-07-17 起）写入时这些列还不存在 ⇒ 全为 NULL ⇒
  (a) 票池过滤会筛掉**所有**历史票（0 条入选）；
  (b) 排序时 COALESCE 到 9.9 全部排最后，退化成随机。
⇒ 上线前必须先回填。这是 chip_conc / vol_ratio 踩过的同一个坑。

口径一致
--------
与 strategy/mid_long.py::scan_long_term **逐行对齐**：
    ma60 = close.rolling(60).mean();  ma120 = close.rolling(120).mean()
    bit0 = (ma60 > ma120) & (close > ma120)
    bit1 = ma120 > ma120.shift(21)
    vol60 = close.pct_change().rolling(60).std() * √252 ;  bit2 = vol60 < 0.35
    dd250 = 1 - close / close.rolling(250).max()      ;  bit3 = dd250 < 0.40
四项都是**有限滚动窗口**（最长 250 日），用全历史 daily_price 精确计算即可，
不存在 chip_conc 那种无限记忆的 warmup 收敛问题。

用法
----
    python tools/_backfill_long_pool_cols.py [--dry-run]
"""

import argparse
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "core", "quant.db")

VOL_MAX = 0.35          # 与 scan_long_term 的 vol_max 默认一致
DD_MAX = 0.40           # 与 max_drawdown_from_high 默认一致


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import pandas as pd
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1) 确保列存在（与 core/db.py::_safe_add_column 同语义）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_signal)")]
    for col, typ in (("long_mask", "INTEGER"), ("vol60", "REAL"),
                     ("dd250", "REAL"), ("long_rank_key", "REAL")):
        if col not in cols:
            if args.dry_run:
                print(f"[dry-run] 将新增列 stock_signal.{col} {typ}")
            else:
                conn.execute(f"ALTER TABLE stock_signal ADD COLUMN {col} {typ}")
                conn.commit()
                print(f"已新增列 stock_signal.{col} {typ}")
        else:
            print(f"列 stock_signal.{col} 已存在")

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
        ma60 = s.rolling(60).mean()
        ma120 = s.rolling(120).mean()
        ret = s.pct_change()
        vol60 = ret.rolling(60).std() * math.sqrt(252.0)
        high250 = s.rolling(250).max()
        dd250 = 1.0 - s / high250
        ma120_prev = ma120.shift(21)
        for d in dates:
            ts = pd.Timestamp(d)
            if ts not in s.index:
                n_null += 1
                continue
            c = s.loc[ts]
            m60, m120 = ma60.loc[ts], ma120.loc[ts]
            v60, dd = vol60.loc[ts], dd250.loc[ts]
            mp = ma120_prev.loc[ts]
            if not (math.isfinite(c) and math.isfinite(m60) and math.isfinite(m120)):
                n_null += 1
                continue
            mask = 0
            if m60 > m120 and c > m120:
                mask |= 1
            if math.isfinite(mp) and m120 > mp:
                mask |= 2
            v_ok = math.isfinite(v60)
            if v_ok and v60 < VOL_MAX:
                mask |= 4
            d_ok = math.isfinite(dd)
            if d_ok and dd < DD_MAX:
                mask |= 8
            updates.append((mask,
                            float(v60) if v_ok else None,
                            float(dd) if d_ok else None,
                            code, d))
            n_ok += 1
        if (i + 1) % 200 == 0:
            print(f"  ...已处理 {i+1}/{len(by_code)} 只", flush=True)

    print(f"可回填 {n_ok} 条，无法计算（历史不足/异常）{n_null} 条")
    if args.dry_run:
        print("[dry-run] 未写入")
        conn.close()
        return

    conn.executemany(
        "UPDATE stock_signal SET long_mask=?, vol60=?, dd250=? "
        "WHERE code=? AND scan_date=? AND COALESCE(horizon,'short')='long'",
        updates)
    conn.commit()
    print(f"已写入 {len(updates)} 条")

    # 3) 横截面排序键（必须整日信号齐了才能算）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.sync import recompute_long_rank_key
    n = recompute_long_rank_key()
    print(f"long_rank_key 回填 {n} 条")

    # 4) 校验
    print()
    print("=== 回填后校验 ===")
    tot = conn.execute(
        "SELECT COUNT(*) FROM stock_signal WHERE COALESCE(horizon,'short')='long'"
    ).fetchone()[0]
    pool = conn.execute(
        "SELECT COUNT(*) FROM stock_signal WHERE COALESCE(horizon,'short')='long' "
        "AND (COALESCE(long_mask,0) & 7) = 7").fetchone()[0]
    ranked = conn.execute(
        "SELECT COUNT(*) FROM stock_signal WHERE COALESCE(horizon,'short')='long' "
        "AND long_rank_key IS NOT NULL").fetchone()[0]
    print(f"  long 信号总数        {tot}")
    print(f"  票池 (mask&7)=7      {pool}  ({pool/max(1,tot)*100:.1f}%)")
    print(f"  有排序键 long_rank_key {ranked}")
    left = conn.execute(
        "SELECT COUNT(*) FROM stock_signal WHERE COALESCE(horizon,'short')='long' "
        "AND (COALESCE(long_mask,0) & 7) = 7 AND long_rank_key IS NULL").fetchone()[0]
    print(f"  ⚠ 票池内缺排序键     {left} 条（应为 0）")

    print()
    print("  按 scan_date 看票池规模（最近 8 个信号日）:")
    for r in conn.execute(
            "SELECT scan_date, COUNT(*) n, "
            "SUM(CASE WHEN (COALESCE(long_mask,0)&7)=7 THEN 1 ELSE 0 END) pool "
            "FROM stock_signal WHERE COALESCE(horizon,'short')='long' "
            "GROUP BY scan_date ORDER BY scan_date DESC LIMIT 8"):
        print(f"    {r['scan_date']}  总 {r['n']:>4}  票池 {r['pool']:>4}")
    conn.close()


if __name__ == "__main__":
    main()
