"""诊断 2：在实际「每日名额」内，gap guard 剔除了什么、替补上来的是谁。"""
import sqlite3
import statistics as st

DB = "core/quant.db"
START = "2026-07-20"
BOARDS = ("688", "689", "300", "301", "8", "4", "92")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def fwd(code, scan_date, n):
    rows = conn.execute(
        "SELECT trade_date, open, close FROM daily_price WHERE code=? AND trade_date>? "
        "ORDER BY trade_date ASC LIMIT ?", (code, scan_date, n)).fetchall()
    if len(rows) < n:
        return None
    o, cl = rows[0]["open"], rows[n - 1]["close"]
    if not o or not cl or o <= 0:
        return None
    return (cl - o) / o * 100


board = "".join(f" AND s.code NOT LIKE '{p}%'" for p in BOARDS)
rows = [dict(r) for r in conn.execute(f"""
    SELECT s.code, s.scan_date, COALESCE(s.horizon,'short') hz, s.strategy,
           s.buy_price, s.take_profit, s.fusion_score, s.pct_above_ma20,
           lp.close AS last_close,
           CASE WHEN lp.close IS NOT NULL AND lp.close>0 AND s.buy_price>0
                     AND s.take_profit IS NOT NULL AND s.take_profit>s.buy_price
                     AND (lp.close/s.buy_price-1) > (s.take_profit/s.buy_price-1)
                THEN 1 ELSE 0 END AS gap_cut
    FROM stock_signal s LEFT JOIN latest_price lp ON lp.code = s.code
    WHERE s.scan_date >= '{START}' AND s.buy_price > 0
      AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
      AND COALESCE(s.strategy,'') NOT IN ('强势突破','缩量回踩')
      {board}
""").fetchall()]

# 按线上排序取每日 top-N（short=3，mid/long=4）
CAPS = {"short": 3, "mid": 4, "long": 4}


def sort_key_short(r):
    st_rank = 0 if r["strategy"] == "隔日动量" else (1 if r["strategy"] == "缩量回踩" else 2)
    return (st_rank, r["pct_above_ma20"] if r["pct_above_ma20"] is not None else 0,
            -(r["fusion_score"] or 0))


def sort_key_fusion(r):
    return (-(r["fusion_score"] or 0),)


picked_cut, picked_keep = [], []
for hz in ("short", "mid", "long"):
    by_day = {}
    for r in rows:
        if r["hz"] == hz:
            by_day.setdefault(r["scan_date"], []).append(r)
    k = sort_key_short if hz == "short" else sort_key_fusion
    for d, lst in sorted(by_day.items()):
        lst.sort(key=k)
        chosen = lst[:CAPS[hz]]
        for r in chosen:
            (picked_cut if r["gap_cut"] else picked_keep).append((hz, r))

print("=" * 84)
print("在「每日名额内」被 gap guard 剔除的票 —— 这才是真正决定跟踪样本的地方")
print("=" * 84)
for hz in ("short", "mid", "long"):
    cut = [r for h, r in picked_cut if h == hz]
    keep = [r for h, r in picked_keep if h == hz]

    def stat(rs, n):
        vs = [v for v in (fwd(r["code"], r["scan_date"], n) for r in rs) if v is not None]
        if not vs:
            return "n/a"
        return f"{st.mean(vs):+6.2f}% / 胜率 {sum(1 for v in vs if v>0)/len(vs)*100:4.1f}% (n={len(vs)})"

    print(f"\n[{hz}]  名额占用 {len(cut)+len(keep)} 条 → 被剔 {len(cut)} 条 "
          f"({len(cut)/max(1,len(cut)+len(keep))*100:.1f}%)")
    print(f"   被剔（赢家）  T+5 {stat(cut,5)}   T+10 {stat(cut,10)}")
    print(f"   保留（替补）  T+5 {stat(keep,5)}   T+10 {stat(keep,10)}")

print()
print("=" * 84)
print("模拟：把 gap guard 的「最新价」基准改成「信号日收盘价」（语义正确口径）")
print("       等价于不再剔除任何历史信号，全部纳入跟踪 → 跟踪成绩单应当变成：")
print("=" * 84)
all_picked = picked_cut + picked_keep
for hz in ("short", "mid", "long"):
    rs = [r for h, r in all_picked if h == hz]
    vs = [v for v in (fwd(r["code"], r["scan_date"], 10) for r in rs) if v is not None]
    if vs:
        print(f"  {hz:<6} n={len(vs):<4} T+10 均值 {st.mean(vs):+6.2f}%  胜率 "
              f"{sum(1 for v in vs if v>0)/len(vs)*100:5.1f}%")
print()
print("对照：当前线上口径（gap guard 已剔除赢家后）：")
for hz in ("short", "mid", "long"):
    rs = [r for h, r in picked_keep if h == hz]
    vs = [v for v in (fwd(r["code"], r["scan_date"], 10) for r in rs) if v is not None]
    if vs:
        print(f"  {hz:<6} n={len(vs):<4} T+10 均值 {st.mean(vs):+6.2f}%  胜率 "
              f"{sum(1 for v in vs if v>0)/len(vs)*100:5.1f}%")
