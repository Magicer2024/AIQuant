"""
tools/diag_short_reco.py —— 短线推荐系统诊断（一次性分析脚本）

两块独立分析：
  A) 位置分布：用【最新】短线 scan_date，看推荐时点的"位置"
     （价相对MA20偏离 / RSI / 相对10日最低反弹）—— 越大=越已涨过/挂高位。
  B) 真实表现：用【有后续行情】的历史短线 scan_date，计算 T+1/T+3/T+5
     开盘->收盘(OC,现实可买口径) 与 收盘->收盘(CC) 胜率与均值，
     并按"高位/低位"分组验证"买在反弹高位"是否拖累收益。

仅读取，不写库。
"""
import sqlite3
import os

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")


def rsi14(closes):
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g = sum(gains[:14]) / 14.0
    avg_l = sum(losses[:14]) / 14.0
    for i in range(14, len(gains)):
        avg_g = (avg_g * 13 + gains[i]) / 14.0
        avg_l = (avg_l * 13 + losses[i]) / 14.0
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 所有短线 scan_date（降序）
    dates = [r["d"] for r in conn.execute(
        "SELECT DISTINCT scan_date AS d FROM stock_signal WHERE horizon='short' "
        "AND strategy IN ('短线融合','超跌反弹v3') ORDER BY scan_date DESC"
    ).fetchall()]
    if not dates:
        print("无短线信号数据")
        return
    latest = dates[0]
    print(f"[诊断] 最新短线 scan_date = {latest}，历史可选日期 {len(dates)} 个\n")

    # 预取全部日线
    px = conn.execute(
        "SELECT code, trade_date, open, high, low, close FROM daily_price ORDER BY code, trade_date"
    ).fetchall()
    data = {}
    for r in px:
        data.setdefault(r["code"], []).append(r)

    def find_idx(rows, date):
        for i, r in enumerate(rows):
            if r["trade_date"] >= date:
                return i
        return -1

    # ---------- A) 最新日位置分布 ----------
    print("=" * 64)
    print("A、最新推荐日位置分布（挂高位程度）")
    print("=" * 64)
    sigs = conn.execute(
        "SELECT code, name, price AS buy_price, fusion_score FROM stock_signal "
        "WHERE scan_date=? AND horizon='short' AND strategy IN ('短线融合','超跌反弹v3')",
        (latest,),
    ).fetchall()
    pa, rs, rb, fs = [], [], [], []
    for s in sigs:
        rows = data.get(s["code"])
        if not rows:
            continue
        idx = find_idx(rows, latest)
        if idx < 0:
            continue
        closes = [r["close"] for r in rows[: idx + 1]]
        close = rows[idx]["close"]
        ma20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else None
        r = rsi14(closes)
        low10 = min(closes[-10:]) if len(closes) >= 10 else close
        pa.append((close / ma20 - 1) * 100 if ma20 else 0)
        if r is not None:
            rs.append(r)
        rb.append((close / low10 - 1) * 100 if low10 > 0 else 0)
        fs.append(s["fusion_score"] or 0)
    n = len(pa)
    print(f"  候选数: {len(sigs)}，有效: {n}")
    print(f"  融合分均值:        {sum(fs)/n:.2f}" if n else "  (无)")
    if n:
        print(f"  价相对MA20偏离:   均值 {sum(pa)/n:+.2f}%  最大 {max(pa):.1f}%  "
              f">8%占比 {sum(1 for x in pa if x>8)/n*100:.1f}%")
        print(f"  RSI(14):          均值 {sum(rs)/len(rs):.1f}  "
              f">70占比 {sum(1 for x in rs if x>70)/len(rs)*100:.1f}%")
        print(f"  相对10日最低反弹: 均值 {sum(rb)/n:+.2f}%  "
              f">5%占比 {sum(1 for x in rb if x>5)/n*100:.1f}%")

    # ---------- B) 多历史 cohort 真实表现（聚合，避免单日偏差） ----------
    print("\n" + "=" * 64)
    print("B、历史推荐真实表现（聚合多个有≥6日后行情的历史日）")
    print("=" * 64)
    S = {k: [] for k in ["oc1", "oc3", "oc5", "cc1", "cc3", "cc5", "pa"]}
    pooled_dates = 0
    for d in dates[1:40]:  # 最近40个历史日里挑有效的
        sigs2 = conn.execute(
            "SELECT code, price AS buy_price, fusion_score FROM stock_signal "
            "WHERE scan_date=? AND horizon='short' AND strategy IN ('短线融合','超跌反弹v3')",
            (d,),
        ).fetchall()
        if not sigs2:
            continue
        # 先看是否有后续行情
        sample = data.get(sigs2[0]["code"])
        if not sample:
            continue
        if find_idx(sample, d) < 0 or find_idx(sample, d) + 6 >= len(sample):
            continue
        pooled_dates += 1
        for s in sigs2:
            rows = data.get(s["code"])
            if not rows:
                continue
            idx = find_idx(rows, d)
            if idx < 0 or idx + 6 >= len(rows):
                continue
            close = rows[idx]["close"]
            closes = [r["close"] for r in rows[: idx + 1]]
            ma20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else close
            S["pa"].append((close / ma20 - 1) * 100 if ma20 else 0)

            def fwd(off, kind):
                tgt = rows[idx + off]
                if kind == "OC":
                    base = rows[idx + 1]["open"]
                    return (tgt["close"] / base - 1) * 100 if base and base > 0 else None
                return (tgt["close"] / close - 1) * 100
            for off, key in [(1, "oc1"), (3, "oc3"), (5, "oc5"),
                             (1, "cc1"), (3, "cc3"), (5, "cc5")]:
                kind = "OC" if key.startswith("oc") else "CC"
                v = fwd(off, kind)
                if v is not None:
                    S[key].append(v)

    def avg(lst):
        return sum(lst) / len(lst) if lst else float("nan")

    def win(lst):
        return sum(1 for x in lst if x > 0) / len(lst) * 100 if lst else float("nan")

    print(f"  聚合 cohort 数: {pooled_dates}，T+1 样本 {len(S['oc1'])}\n")
    for key, lbl in [("oc1", "T+1"), ("oc3", "T+3"), ("oc5", "T+5")]:
        print(f"  {lbl} OC: 胜率 {win(S[key]):.1f}%  均值 {avg(S[key]):+.3f}%  (n={len(S[key])})")
    print("  （CC=收盘->收盘对照，含买不到的隔夜跳空）")
    for key, lbl in [("cc1", "T+1"), ("cc3", "T+3"), ("cc5", "T+5")]:
        print(f"  {lbl} CC: 胜率 {win(S[key]):.1f}%  均值 {avg(S[key]):+.3f}%  (n={len(S[key])})")

    print("\n  分位验证：高位(>MA20 8%) vs 低位(<MA20 2%) 的 T+1 OC")
    hi = [S["oc1"][i] for i in range(len(S["oc1"])) if S["pa"][i] > 8]
    lo = [S["oc1"][i] for i in range(len(S["oc1"])) if S["pa"][i] < 2]
    print(f"  高位组 T+1 OC: 胜率 {win(hi):.1f}% 均值 {avg(hi):+.3f}% (n={len(hi)})")
    print(f"  低位组 T+1 OC: 胜率 {win(lo):.1f}% 均值 {avg(lo):+.3f}% (n={len(lo)})")

    conn.close()


if __name__ == "__main__":
    main()
