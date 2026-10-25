"""
tools/repair_stock_bs_fields.py —— 用 baostock 不复权重取单只股票的量/额/换手/涨跌幅
================================================================================
适用场景：某只股票长期走腾讯兜底（腾讯接口无换手率、无涨跌幅），库里
  turnover=0 / pct_change=0，但 OHLC 是正常的。典型：302132 中航成飞。

根因（2026-10-22 排查）：
  core/sync.py 的 _format_code_for_baostock 未识别 302/303 开头的深市创业板
  代码，落进「默认 sh.」分支 → baostock 永远查不到 → 每天退回腾讯兜底，
  历史行情也整段由腾讯写入，于是换手率/涨跌幅全 0。

修法：
  以 baostock adjustflag="3"（不复权）为准，按交易日覆盖
  volume / amount / pct_change / turnover 四列；
  OHLC 一律不动（库内是历史时点前复权序列，换基准会破坏与相邻日的衔接）。
  逐行用「OHLC 只差一个复权系数 q」校验，q 折算后四项偏差 >1% 即拒绝写入，
  防止错股/错日期污染。

用法：
  python tools/repair_stock_bs_fields.py --code 302132          # dry-run
  python tools/repair_stock_bs_fields.py --code 302132 --apply
  python tools/repair_stock_bs_fields.py --code 302132 --since 2024-01-01 --apply

修完请重算该股策略分（volume/pct 参与打分）：
  python -c "from core.sync import sync_strategy_score; sync_strategy_score('302132')"
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import baostock as bs
from core.db import get_conn
from core.sync import _format_code_for_baostock
from tools.fix_tencent_amount_bug import _scaled_match, TOL


def main():
    ap = argparse.ArgumentParser(description="用 baostock 不复权修复单只股票的量/额/换手/涨跌幅")
    ap.add_argument("--code", required=True, help="6 位股票代码，如 302132")
    ap.add_argument("--since", default=None, help="只处理该日期之后，如 2024-01-01")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = ap.parse_args()

    code = args.code.strip()
    cond = "code=?"
    params = [code]
    if args.since:
        cond += " AND trade_date>=?"
        params.append(args.since)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT code, trade_date, open, high, low, close, volume, amount, "
            f"pct_change, turnover FROM daily_price WHERE {cond} ORDER BY trade_date",
            params).fetchall()]
    if not rows:
        print(f"[ERROR] 库内无 {code} 的行情")
        return

    zero_turn = sum(1 for r in rows if not r["turnover"])
    zero_pct = sum(1 for r in rows if not r["pct_change"])
    print(f"{code}: 库内 {len(rows)} 行（{rows[0]['trade_date']} ~ {rows[-1]['trade_date']}），"
          f"turnover=0 {zero_turn} 行，pct_change=0 {zero_pct} 行")

    bs_code = _format_code_for_baostock(code)
    lg = bs.login()
    if lg.error_code != '0':
        print(f"[ERROR] baostock 登录失败: {lg.error_msg}")
        sys.exit(1)
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=rows[0]["trade_date"], end_date=rows[-1]["trade_date"],
            frequency="d", adjustflag="3")
        got = {}
        while rs.error_code == '0' and rs.next():
            r = rs.get_row_data()
            got[r[0]] = r
    finally:
        bs.logout()
    print(f"baostock({bs_code}) 返回 {len(got)} 行")

    updates, mismatch, missing = [], 0, 0
    for r in rows:
        b = got.get(r["trade_date"])
        if not b:
            missing += 1
            continue
        try:
            b_o, b_h, b_l, b_c = (float(b[1]), float(b[2]), float(b[3]), float(b[4]))
            b_v, b_a, b_t, b_p = (float(b[5]), float(b[6]), float(b[7]), float(b[8]))
        except (ValueError, TypeError):
            missing += 1
            continue
        if not _scaled_match(r, b_o, b_h, b_l, b_c, TOL):
            mismatch += 1
            if mismatch <= 10:
                print(f"  [SKIP] {r['trade_date']} OHLC 无法对齐: "
                      f"库={r['open']}/{r['high']}/{r['low']}/{r['close']} "
                      f"bs={b_o}/{b_h}/{b_l}/{b_c}")
            continue
        if (abs(b_v - r["volume"]) < 1e-6 and abs(b_a - r["amount"]) < 1e-4
                and abs(b_p - r["pct_change"]) < 1e-6 and abs(b_t - r["turnover"]) < 1e-6):
            continue  # 已一致，不写
        updates.append((b_v, b_a, b_p, b_t, code, r["trade_date"], r))

    print(f"待更新 {len(updates)} 行（OHLC 对不齐 {mismatch}，baostock 无数据 {missing}）")
    for u in updates[:5]:
        r = u[-1]
        print(f"  {r['trade_date']}: vol {r['volume']:,.0f}→{u[0]:,.0f}  "
              f"turn {r['turnover']}→{u[3]}  pct {r['pct_change']:.4f}→{u[2]:.4f}")
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 才会覆盖。")
        return

    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    backup_path = os.path.join(reports_dir, f"repair_bs_{code}_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    with open(backup_path, "w", encoding="utf-8") as f:
        for u in updates:
            r = u[-1]
            f.write(json.dumps({k: r[k] for k in
                                ("code", "trade_date", "volume", "amount",
                                 "pct_change", "turnover")}, ensure_ascii=False) + "\n")

    with get_conn() as conn:
        conn.executemany(
            "UPDATE daily_price SET volume=?, amount=?, pct_change=?, turnover=? "
            "WHERE code=? AND trade_date=?",
            [(u[0], u[1], u[2], u[3], u[4], u[5]) for u in updates])
    print(f"\n[apply] 已更新 {len(updates)} 行；旧值备份 {backup_path}")

    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM daily_price WHERE code=? AND turnover=0 AND volume>0",
                         (code,)).fetchone()[0]
        z = conn.execute("SELECT COUNT(*) FROM daily_price WHERE code=? AND pct_change=0", (code,)).fetchone()[0]
    print(f"复查：turnover=0 且 volume>0 剩 {n} 行；pct_change=0 剩 {z} 行")
    print(f"提示：请重算策略分 → python -c \"from core.sync import sync_strategy_score; "
          f"sync_strategy_score('{code}')\"")


if __name__ == "__main__":
    main()
