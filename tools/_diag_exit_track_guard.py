"""诊断：出场跟踪 gap guard 用「最新价」做历史筛选的影响面。

假设：insert_new_outcomes / exit_advice 用 latest_price.close（今日最新价）
      判定「现价是否已越过止盈价」→ 剔除。回看 7/20 起的历史推荐时，
      任何在 7/20~今天之间涨到止盈价以上的票都会被踢出跟踪样本，
      而它们恰恰是赢家 ⇒ 跟踪胜率被系统性低估。

输出：被剔除 vs 保留 的组内比例 + 后续真实收益（信号日次日开盘 → T+5/T+10 收盘）。
"""
import sqlite3
import statistics as st

DB = "core/quant.db"
START = "2026-07-20"
BOARDS = ("688", "689", "300", "301", "8", "4", "92")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def fwd(code, scan_date, n):
    """信号日之后第 n 个交易日收盘 / 次日开盘 的收益（%）。"""
    rows = conn.execute(
        "SELECT trade_date, open, close FROM daily_price WHERE code=? AND trade_date>? "
        "ORDER BY trade_date ASC LIMIT ?", (code, scan_date, n)).fetchall()
    if len(rows) < n:
        return None
    o = rows[0]["open"]
    cl = rows[n - 1]["close"]
    if not o or not cl or o <= 0:
        return None
    return (cl - o) / o * 100


board = "".join(f" AND s.code NOT LIKE '{p}%'" for p in BOARDS)
sql = f"""
SELECT s.code, s.scan_date, COALESCE(s.horizon,'short') hz, s.strategy,
       s.buy_price, s.take_profit, s.stop_loss, s.fusion_score,
       lp.close AS last_close, lp.trade_date AS last_date,
       CASE WHEN lp.close IS NOT NULL AND lp.close>0 AND s.buy_price>0
                 AND s.take_profit IS NOT NULL AND s.take_profit>s.buy_price
                 AND (lp.close/s.buy_price-1) > (s.take_profit/s.buy_price-1)
            THEN 1 ELSE 0 END AS gap_cut
FROM stock_signal s LEFT JOIN latest_price lp ON lp.code = s.code
WHERE s.scan_date >= '{START}' AND s.buy_price IS NOT NULL AND s.buy_price > 0
  AND s.name NOT LIKE '%ST%' AND s.name NOT LIKE '%退%'
  AND COALESCE(s.strategy,'') != '强势突破'
  AND COALESCE(s.strategy,'') != '缩量回踩'
  {board}
"""
rows = [dict(r) for r in conn.execute(sql).fetchall()]
print(f"池子（7/20 起全部候选信号，未套名额/门控）：{len(rows)} 条\n")

print("=" * 78)
print("① gap guard 剔除面（按 horizon）")
print("=" * 78)
print(f"{'周期':<6} {'总数':>6} {'被剔':>6} {'剔除率':>8}   {'被剔T5均值':>11} {'保留T5均值':>11}")
for hz in ("short", "mid", "long"):
    sub = [r for r in rows if r["hz"] == hz]
    cut = [r for r in sub if r["gap_cut"]]
    keep = [r for r in sub if not r["gap_cut"]]

    def avg_t5(rs):
        vs = [v for v in (fwd(r["code"], r["scan_date"], 5) for r in rs) if v is not None]
        return round(st.mean(vs), 2) if vs else None, len(vs)

    ct5, cn = avg_t5(cut)
    kt5, kn = avg_t5(keep)
    rate = f"{len(cut)/len(sub)*100:.1f}%" if sub else "-"
    print(f"{hz:<6} {len(sub):>6} {len(cut):>6} {rate:>8}   "
          f"{str(ct5)+' (n='+str(cn)+')':>11} {str(kt5)+' (n='+str(kn)+')':>11}")

print()
print("=" * 78)
print("② 被 gap guard 剔除的票，真实后续收益（信号日次日开盘 → T+N 收盘）")
print("=" * 78)
cut_all = [r for r in rows if r["gap_cut"]]
for n in (1, 3, 5, 10):
    vs = [v for v in (fwd(r["code"], r["scan_date"], n) for r in cut_all) if v is not None]
    if vs:
        win = sum(1 for v in vs if v > 0) / len(vs) * 100
        print(f"  T+{n:<2}  n={len(vs):<4} 均值 {st.mean(vs):+6.2f}%  中位 {st.median(vs):+6.2f}%  胜率 {win:5.1f}%")
keep_all = [r for r in rows if not r["gap_cut"]]
print("  --- 对照：未被剔除的票 ---")
for n in (1, 3, 5, 10):
    vs = [v for v in (fwd(r["code"], r["scan_date"], n) for r in keep_all) if v is not None]
    if vs:
        win = sum(1 for v in vs if v > 0) / len(vs) * 100
        print(f"  T+{n:<2}  n={len(vs):<4} 均值 {st.mean(vs):+6.2f}%  中位 {st.median(vs):+6.2f}%  胜率 {win:5.1f}%")

print()
print("=" * 78)
print("③ 若把 gap guard 的判定基准从「最新价」换成「信号日收盘价」")
print("   （= 推荐当下是否已越过止盈，本意语义），剔除面变化：")
print("=" * 78)
n_old = sum(1 for r in rows if r["gap_cut"])
n_new = sum(1 for r in rows
            if r["take_profit"] and r["buy_price"] and r["take_profit"] > r["buy_price"]
            and r["buy_price"] > r["take_profit"])
print(f"  用最新价判定剔除: {n_old} 条")
print(f"  用信号日收盘价判定剔除: {n_new} 条  ← 语义正确，且不随未来价格变动")

print()
print("=" * 78)
print("④ 现有 recommend_outcome 实际收益分布（按周期 × 状态）")
print("=" * 78)
for hz in ("short", "mid", "long"):
    for label, cond in (("已出场", "exit_return IS NOT NULL"), ("持仓中", "exit_return IS NULL")):
        rs = [dict(r) for r in conn.execute(
            f"SELECT exit_return, t5_return, t10_return, exit_reason, entry_price FROM recommend_outcome "
            f"WHERE COALESCE(horizon,'short')=? AND scan_date>='{START}' AND {cond}", (hz,))]
        vs = [r["exit_return"] for r in rs if r["exit_return"] is not None]
        if not vs:
            # 持仓中：用 t10/t5 代理当前浮盈
            vs = [r["t10_return"] or r["t5_return"] for r in rs if (r["t10_return"] is not None or r["t5_return"] is not None)]
            vs = [v for v in vs if v is not None]
        if vs:
            win = sum(1 for v in vs if v > 0) / len(vs) * 100
            print(f"  {hz:<6}{label:<5} n={len(vs):<4} 均值 {st.mean(vs):+6.2f}%  胜率 {win:5.1f}%  "
                  f"最差 {min(vs):+.2f}%  最好 {max(vs):+.2f}%")
    # 出场原因分布
    rs = [dict(r) for r in conn.execute(
        f"SELECT exit_reason, COUNT(*) n, ROUND(AVG(exit_return),2) avg_r FROM recommend_outcome "
        f"WHERE COALESCE(horizon,'short')=? AND scan_date>='{START}' GROUP BY 1 ORDER BY n DESC", (hz,))]
    print(f"    {hz} 出场原因: " + " | ".join(
        f"{r['exit_reason'] or '持仓中'}×{r['n']}({r['avg_r']}%)" for r in rs))
    print()
