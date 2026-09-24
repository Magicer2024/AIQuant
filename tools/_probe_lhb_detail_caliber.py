"""探针：评估 stock_lhb_detail 的口径污染面。

问题：akshare stock_lhb_detail_em 同一 (trade_date, code) 可因多条上榜原因产生多行，
而「市场总成交额/买入额/卖出额/净买额」的统计窗口随原因变化：
  - 「日涨幅偏离7%」等日榜   → 单日
  - 「连续三个交易日内…」    → N 日累计
现网 fetch_lhb_detail 用 groupby().first 聚合 ⇒ 取到哪行全看接口返回顺序，金额口径随缘。

本脚本回答三个问题：
  ① 重复组里单日/多日口径各占多少，first 聚合会错多少
  ② 同一组内是否总是同时存在单日行（若存在，改为「优先取单日行」即可零成本修复）
  ③ 受影响的下游字段（net_buy / net_buy_ratio）偏差有多大
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
    os.environ.pop(k, None)

import akshare as ak  # noqa: E402


def window_days(reason: str) -> int:
    r = str(reason or "")
    if "连续" not in r:
        return 1
    import re
    m = re.search(r"连续([一二三四五1-5])个交易日", r)
    return {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}.get(
        m.group(1), 3) if m else 3


def main():
    start, end = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-08-01", "2026-09-22")
    df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
    print(f"区间 {start}~{end}: {len(df)} 行")
    df["wd"] = df["上榜原因"].map(window_days)

    g = df.groupby(["上榜日期", "代码"])
    tot = dup = 0
    dup_mixed = dup_pure_multi = dup_pure_single = 0
    first_single = first_multi = 0
    # 组内：若存在单日行，取单日行能否救回
    rescue = 0
    for _, sub in g:
        tot += 1
        if len(sub) < 2:
            continue
        dup += 1
        wds = set(sub["wd"])
        if 1 in wds and len(wds) > 1:
            dup_mixed += 1
        elif 1 not in wds:
            dup_pure_multi += 1
        else:
            dup_pure_single += 1
        f = sub.iloc[0]
        if f["wd"] == 1:
            first_single += 1
        else:
            first_multi += 1
        if 1 in wds:
            rescue += 1

    print(f"\n=== 组统计（按 上榜日期+代码）===")
    print(f"  总组数 {tot} | 重复组 {dup} ({dup/tot*100:.1f}%)")
    print(f"    纯单日重复   {dup_pure_single}")
    print(f"    单日+多日混合 {dup_mixed}   ← 危险：first 可能取到多日行")
    print(f"    纯多日重复   {dup_pure_multi}")
    print(f"  first 聚合取到：单日行 {first_single} / 多日行 {first_multi}")
    print(f"  组内存在单日行（可救）: {rescue}/{dup}")

    print("\n=== 金额口径证据：市场总成交额 vs 单日榜中位数 ===")
    for lbl, grp in df.groupby("wd"):
        print(f"  window={lbl}: n={len(grp)}  "
              f"成交额中位 {grp['市场总成交额'].median()/1e8:.2f}亿  "
              f"净买额中位 {grp['净买额'].median()/1e8:.4f}亿")

    print("\n=== 混合组样例（展示 first 会取到哪行）===")
    shown = 0
    for k, sub in g:
        if len(sub) < 2 or shown >= 3:
            continue
        if not (1 in set(sub["wd"]) and len(set(sub["wd"])) > 1):
            continue
        shown += 1
        print(f"\n  【{k[0]} {k[1]}】")
        cols = ["上榜原因", "wd", "市场总成交额", "净买额", "龙虎榜净买额占比", "收盘价"]
        cols = [c for c in cols if c in sub.columns]
        for _, r in sub.iterrows():
            rs = str(r["上榜原因"])[:34]
            print(f"    w={r['wd']} 成交额 {r['市场总成交额']/1e8:8.2f}亿 "
                  f"净买 {r['净买额']/1e8:7.3f}亿  {rs}")

    # 偏差量化：同一组内 first 取多日行 vs 取单日行，净买额差多少倍
    print("\n=== first 取多日行时的净买额放大倍数（混合组）===")
    mults = []
    for k, sub in g:
        if len(sub) < 2:
            continue
        wds = set(sub["wd"])
        if not (1 in wds and len(wds) > 1):
            continue
        f = sub.iloc[0]
        if f["wd"] == 1:
            continue
        s1 = sub[sub["wd"] == 1].iloc[0]
        a, b = abs(f["净买额"]), abs(s1["净买额"])
        if b > 1e-6:
            mults.append(a / b)
    if mults:
        import statistics as st
        mults.sort()
        print(f"  n={len(mults)} 中位 {st.median(mults):.2f}x  "
              f"最大 {mults[-1]:.1f}x  >2x 占比 {sum(1 for m in mults if m > 2)/len(mults)*100:.0f}%")
    else:
        print("  无（first 恰好都取到单日行，或样本内无混合组）")


if __name__ == "__main__":
    main()
