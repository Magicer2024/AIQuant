"""股本取证 + `stock_info` 股本列修备工具

**取证结论（2026-09-15，重要）**
------------------------------
`stock_info.total_shares` 与 `circ_shares` 在库中完全相等，两者**存的都是「流通股本」**
（不是总股本）。取证依据见下「数据源」一节：判别子集（真实流通 ≠ 总股本，2,338/4,509 只）中
**64.9% 的库值 ≈ 真实流通股，仅 1.2% ≈ 真实总股本**。

因此：
- `chip.py` / `build_factors()` 的换手率回退 `volume / circ_shares` **分母本来就是对的**，
  **不需要修数据**；把口径统一改成它之后，筹码因子在 26,746 条信号行上仅 18 行变化。
- 但 `QUALITY_FILTER` 里 `市值 = total_shares × close` 实际算的是**流通市值**（列名有误导）。
- ⚠ **本工具的 `--apply` 本次不需要执行**；保留它是为了复发时能一键回填，以及留下取证路径。

原始动机（存档）
----------------
最初怀疑：`circ_shares` 等于 `total_shares` ⇒ 没有真正的流通股本 ⇒ 换手率被系统性低估。
实测证伪了这条推断（见上）。

数据源（为什么用腾讯）
----------------------
- 东财 `push2.eastmoney.com` 在**本机不可达**（直连 ConnectionError / 走代理 ProxyError），
  `akshare.stock_zh_a_spot_em()` 因此失败；
- 腾讯 `qt.gtimg.cn` **可达**，且直接返回**股本**而非只给市值。字段（`~` 分隔、0 基下标）：

    [3]  当前价(元)        [38] 换手率(%)      [72] 流通股(股)     [73] 总股本(股)
    [44] 流通市值(亿)      [45] 总市值(亿)     [1] 名称            [30] 行情时间戳

  校验：sz003816 解析出 [44]=1699.28亿 / [45]=2181.55亿，与东财 `f21`/`f20`
  （169,928,059,818 / 218,154,919,818）**完全一致**；sh601398 [72]=2696.12亿股(A股流通)、
  [73]=3564.06亿股(含H股) → 符合工行实际。

⚠ 这是**当前快照**，历史解禁未还原（判别子集里 34% 不吻合多源于此）。

用法
----
    python tools/fix_circ_shares.py            # 干跑：只报告（推荐，含取证全部证据）
    python tools/fix_circ_shares.py --apply    # 备份表 + 写 circ_shares（当前不需要）
    python tools/fix_circ_shares.py --apply --update-total   # 连 total_shares 一起修
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "core", "quant.db")
CACHE_DIR = os.path.join(_ROOT, "data_cache")

_URL = "https://qt.gtimg.cn/q="
_BATCH = 300
I_NAME, I_CLOSE, I_TURNOVER, I_FLOAT_SH, I_TOTAL_SH = 1, 3, 38, 72, 73


def to_symbol(code: str) -> str | None:
    """6 位代码 → 腾讯符号（sh/sz/bj）。"""
    if not code or len(code) != 6 or not code.isdigit():
        return None
    h = code[0]
    if h in "56":
        return "sh" + code
    if h in "013":
        return "sz" + code
    if h in "489":
        return "bj" + code
    return None


def fetch_quotes(codes: list[str], cache: str) -> pd.DataFrame:
    """批量拉取腾讯行情 → DataFrame(code,name,close,turnover_pct,float_shares,total_shares)。"""
    if os.path.exists(cache):
        df = pd.read_csv(cache, dtype={"code": str})
        print(f"读取快照缓存 {os.path.basename(cache)}（{len(df):,} 行）")
        return df

    import requests
    syms = [(c, to_symbol(c)) for c in codes]
    syms = [(c, s) for c, s in syms if s]
    print(f"待抓 {len(syms):,} 只（{_BATCH}/批 → {-(-len(syms) // _BATCH)} 批）")

    rows = []
    for i in range(0, len(syms), _BATCH):
        chunk = syms[i:i + _BATCH]
        q = ",".join(s for _, s in chunk)
        last = None
        for attempt in range(4):
            try:
                r = requests.get(_URL + q, timeout=25, headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
                r.encoding = "gbk"
                last = r.text
                break
            except Exception as e:                               # noqa: BLE001
                last = None
                print(f"      批 {i // _BATCH + 1} 重试 {attempt + 1}/4（{type(e).__name__}）")
                time.sleep(1.2 * (attempt + 1))
        if last is None:
            print(f"      ⚠ 批 {i // _BATCH + 1} 放弃（{len(chunk)} 只）")
            continue

        got = 0
        for line in last.strip().split("\n"):
            if '"' not in line:
                continue
            f = line.split('"')[1].split("~")
            if len(f) < 74:
                continue
            code = f[2].strip()
            if not code or not code.isdigit():
                continue

            def num(idx, div=1.0):
                try:
                    return float(f[idx]) * div
                except (ValueError, IndexError):
                    return None

            rows.append({
                "code": code,
                "name": f[I_NAME].strip(),
                "close": num(I_CLOSE),
                "turnover_pct": num(I_TURNOVER),
                "float_shares": num(I_FLOAT_SH),
                "total_shares": num(I_TOTAL_SH),
            })
            got += 1
        print(f"  批 {i // _BATCH + 1}/{-(-len(syms) // _BATCH)}  "
              f"请求 {len(chunk)} → 解析 {got}（累计 {len(rows):,}）")
        time.sleep(0.2)

    df = pd.DataFrame(rows)
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(cache, index=False, encoding="utf-8-sig")
    print(f"快照已缓存 → {cache}（{len(df):,} 行）")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（默认只干跑）")
    ap.add_argument("--update-total", action="store_true",
                    help="同时修 total_shares（默认只修 circ_shares）")
    ap.add_argument("--cache", default=os.path.join(CACHE_DIR, "tencent_shares.csv"))
    args = ap.parse_args()

    c = sqlite3.connect(DB)
    si = pd.read_sql_query("SELECT code, name, total_shares, circ_shares FROM stock_info", c)
    print(f"库中 stock_info {len(si):,} 只")

    spot = fetch_quotes(si["code"].astype(str).tolist(), args.cache)
    spot = spot[spot["float_shares"].notna() & (spot["float_shares"] > 0)]
    print(f"快照有效 {len(spot):,} 只")

    m = si.merge(spot[["code", "name", "close", "turnover_pct",
                       "float_shares", "total_shares"]],
                 on="code", how="left", suffixes=("", "_spot"))
    miss = m["float_shares"].isna().sum()
    print(f"匹配成功 {len(m) - miss:,} / {len(m):,}（未匹配 {miss:,}）")
    if miss:
        print("  未匹配示例:", m.loc[m["float_shares"].isna(), "code"].head(12).tolist())

    v = m[m["float_shares"].notna()].copy()
    v["float_ratio"] = v["float_shares"] / v["total_shares"]

    print("\n=== 真实流通股 / 总股本 ===")
    q = v["float_ratio"].quantile([.05, .25, .5, .75, .95])
    print(f"  P5={q.iloc[0]:.3f} P25={q.iloc[1]:.3f} P50={q.iloc[2]:.3f} "
          f"P75={q.iloc[3]:.3f} P95={q.iloc[4]:.3f}")
    for lo, hi in ((0, .3), (.3, .7), (.7, .999), (.999, 1.001), (1.001, 9)):
        n = int(((v["float_ratio"] >= lo) & (v["float_ratio"] < hi)).sum())
        print(f"  比例 [{lo:.0%}, {hi:.0%}) : {n:>5} 只 ({n / len(v) * 100:>5.1f}%)")

    print("\n=== 库中现有值到底等于谁？(库值 ÷ 快照值) ===")
    for col, snew in (("total_shares", "total_shares_spot"), ("circ_shares", "float_shares")):
        r = v[col] / v[snew]
        q = r.quantile([.05, .25, .5, .75, .95])
        near1 = float(((r > 0.99) & (r < 1.01)).mean() * 100)
        print(f"  {col:<14} P5={q.iloc[0]:.3f} P25={q.iloc[1]:.3f} P50={q.iloc[2]:.3f} "
              f"P75={q.iloc[3]:.3f} P95={q.iloc[4]:.3f}  ≈1(±1%) 占比 {near1:.1f}%")

    print("\n=== 若写入，会改变多少只（容差 1%）===")
    chg = v[(v["circ_shares"] - v["float_shares"]).abs() > v["float_shares"] * 0.01]
    print(f"  circ_shares 变化 >1%：{len(chg):,} / {len(v):,}（{len(chg) / len(v) * 100:.1f}%）")
    chg_t = v[(v["total_shares"] - v["total_shares_spot"]).abs() > v["total_shares_spot"] * 0.01]
    print(f"  total_shares 变化 >1%：{len(chg_t):,} / {len(v):,}"
          f"（{len(chg_t) / len(v) * 100:.1f}%）")

    v["to_scale"] = v["total_shares"] / v["float_shares"]
    print("\n=== 对换手率的直接影响（回退行放大倍数 = 总股本/真实流通股）===")
    q = v["to_scale"].quantile([.05, .25, .5, .75, .9, .95])
    for lab, val in zip(("P5", "P25", "P50", "P75", "P90", "P95"), q):
        print(f"  {lab:<4} ×{val:.3f}")
    print(f"  → 中位放大 ×{q.iloc[2]:.3f} → 筹码衰减加快、记忆期缩短约 "
          f"{(1 - 1 / q.iloc[2]) * 100:.0f}%")

    print("\n=== 抽样对照（003816/601398 与东财基准核对）===")
    for code in ("003816", "601398", "600519", "000651"):
        r = v[v["code"] == code]
        if not len(r):
            continue
        r = r.iloc[0]
        print(f"  {code} {str(r['name_spot']):<8} 现价 {r['close']:>8.2f}  "
              f"流通股 {r['float_shares'] / 1e8:>8.2f}亿  总股本 "
              f"{r['total_shares_spot'] / 1e8:>8.2f}亿  流通比 "
              f"{r['float_ratio']:.3f}  快照换手 {r['turnover_pct']:.2f}%")

    if not args.apply:
        print("\n[干跑] 未写库。确认上面数字后加 --apply 执行。")
        c.close()
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bk = f"stock_info_shares_bak_{stamp}"
    c.execute(f"CREATE TABLE {bk} AS SELECT code, total_shares, circ_shares FROM stock_info")
    c.commit()
    print(f"\n[备份] 已建表 {bk}"
          f"（{c.execute(f'SELECT COUNT(*) FROM {bk}').fetchone()[0]:,} 行）")

    cur = c.cursor()
    n = 0
    for row in v.itertuples(index=False):
        if args.update_total:
            cur.execute("UPDATE stock_info SET circ_shares=?, total_shares=? WHERE code=?",
                        (float(row.float_shares), float(row.total_shares_spot), row.code))
        else:
            cur.execute("UPDATE stock_info SET circ_shares=? WHERE code=?",
                        (float(row.float_shares), row.code))
        n += cur.rowcount
    c.commit()
    print(f"[写库] 更新 {n:,} 行"
          f"{'（circ + total）' if args.update_total else '（仅 circ_shares）'}")

    r = c.execute("SELECT COUNT(*), SUM(total_shares=circ_shares) FROM stock_info").fetchone()
    print(f"[校验] 总 {r[0]:,} 行，其中 total==circ 的仍有 {r[1]:,} 行"
          f"（{r[1] / r[0] * 100:.1f}%）")
    c.close()
    print("\n下一步：重算筹码 → python tools/eval_chip_factor.py --rebuild --workers 8")


if __name__ == "__main__":
    main()
